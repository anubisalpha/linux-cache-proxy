"""SQLite-backed index over cached files.

Files are stored on disk under CACHE_DIR, named by the sha256 of their
source URL plus the original extension (so a downloaded .msi is still a
real, openable .msi on disk). The DB just indexes metadata for the web UI.

Two kinds of entry: "download" (installers/packages, long TTL) and "asset"
(static web assets, short TTL). Several proxy worker processes share this
DB and cache directory, so writes are atomic and connections wait on locks.
"""
import hashlib
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import config

try:
    import fcntl
except ImportError:  # not Linux; request coalescing is simply unavailable
    fcntl = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_hit_at REAL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    kind TEXT NOT NULL DEFAULT 'download'
);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    client_ip TEXT,
    url_hash TEXT,
    filename TEXT,
    hit INTEGER NOT NULL,
    bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
-- How much each client moved through the proxy, per hour. Deliberately
-- volumes only -- no URLs, no hostnames -- so this stays a traffic meter
-- rather than a browsing history. access_log answers "which downloads";
-- this answers "how much did each client shift", which access_log cannot,
-- because it only ever sees cacheable downloads and hits.
--
-- One row per client per hour: a few thousand rows a day for a whole site,
-- versus millions if every request were logged.
CREATE TABLE IF NOT EXISTS hourly_traffic (
    client_ip TEXT NOT NULL,
    hour INTEGER NOT NULL,          -- unix ts truncated to the hour
    requests INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_ip, hour)
);
-- Every request the content filter blocked. Deliberately never pruned (unlike
-- access_log): it is the audit trail for what was blocked and who asked why.
CREATE TABLE IF NOT EXISTS blocked (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    client_ip TEXT,
    host TEXT,
    url TEXT,
    kind TEXT,
    category TEXT,
    source TEXT,
    reason TEXT,
    user_agent TEXT,
    unblock_requested_at REAL,
    unblock_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocked_ts ON blocked(ts);
CREATE INDEX IF NOT EXISTS idx_blocked_requested ON blocked(unblock_requested_at);
CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log(ts);
CREATE INDEX IF NOT EXISTS idx_access_log_client ON access_log(client_ip);
CREATE INDEX IF NOT EXISTS idx_access_log_url_hash ON access_log(url_hash);
"""


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Several proxy worker processes write here concurrently; wait for the
    # lock rather than failing immediately with "database is locked".
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        # WAL: readers (webui) don't block on writers (proxy logging every
        # request) and vice versa -- matters once this is under real
        # office-wide traffic, not just occasional installer downloads.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-TTL database (no expires_at/kind columns) up to date.
    Existing rows are downloads and get the current download TTL counted
    from when they were cached."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(files)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN kind TEXT NOT NULL DEFAULT 'download'")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN expires_at REAL")
    conn.execute(
        "UPDATE files SET expires_at = created_at + ? WHERE expires_at IS NULL",
        (config.DOWNLOAD_TTL,),
    )


def known_hashes() -> set:
    """url_hashes of entries that haven't expired, for the proxy to hold in
    memory so a cache-miss lookup (the overwhelming majority of requests)
    never touches SQLite."""
    with _connect() as conn:
        return {
            row["url_hash"]
            for row in conn.execute(
                "SELECT url_hash FROM files WHERE expires_at IS NULL OR expires_at > ?",
                (time.time(),),
            )
        }


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def get_entry(url_hash_: str) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM files WHERE url_hash = ?", (url_hash_,)
        ).fetchone()


def save_file(
    url: str,
    filename: str,
    content_type: str,
    data: bytes,
    kind: str = "download",
    ttl: Optional[float] = None,
) -> Path:
    h = url_hash(url)
    suffix = Path(filename).suffix or ""
    rel_path = f"{h}{suffix}"
    dest = config.CACHE_DIR / rel_path
    # Write to a temp name then rename: another worker process may be
    # serving this URL right now and must never see a half-written file.
    tmp = config.CACHE_DIR / f".{rel_path}.{os.getpid()}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, dest)

    if ttl is None:
        ttl = config.WEBCACHE_TTL if kind == "asset" else config.DOWNLOAD_TTL
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO files (url_hash, url, filename, content_type, size, path, created_at, hit_count, expires_at, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                filename=excluded.filename,
                content_type=excluded.content_type,
                size=excluded.size,
                path=excluded.path,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                kind=excluded.kind
            """,
            (h, url, filename, content_type, len(data), rel_path, now, now + ttl, kind),
        )
    return dest


def is_expired(entry, now: Optional[float] = None) -> bool:
    exp = entry["expires_at"]
    return exp is not None and exp <= (now if now is not None else time.time())


def record_hit(url_hash_: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE files SET hit_count = hit_count + 1, last_hit_at = ? WHERE url_hash = ?",
            (time.time(), url_hash_),
        )


def record_hits(deltas: dict) -> None:
    """Add per-file hit counts in one go ({url_hash: hits}). Used for static
    assets, whose hits are tallied in memory and flushed in batches instead of
    costing a database write per request."""
    if not deltas:
        return
    now = time.time()
    with _connect() as conn:
        conn.executemany(
            "UPDATE files SET hit_count = hit_count + ?, last_hit_at = ? WHERE url_hash = ?",
            [(n, now, h) for h, n in deltas.items()],
        )


def list_entries(
    search: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
    kind: Optional[str] = None,
):
    query = "SELECT * FROM files"
    params: list = []
    where = []
    if search:
        where.append("(filename LIKE ? OR url LIKE ?)")
        like = f"%{search}%"
        params += [like, like]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def delete_entry(url_hash_: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT path FROM files WHERE url_hash = ?", (url_hash_,)
        ).fetchone()
        if not row:
            return False
        path = config.CACHE_DIR / row["path"]
        path.unlink(missing_ok=True)
        conn.execute("DELETE FROM files WHERE url_hash = ?", (url_hash_,))
        return True


_BLOCK_COLS = ("token", "ts", "client_ip", "host", "url", "kind", "category", "source", "reason", "user_agent")


def new_block_token() -> str:
    # 128 random bits: the token is what lets a blocked user open their own
    # block page, so it must not be guessable or sequential.
    return secrets.token_urlsafe(16)


def record_blocks(rows: list) -> None:
    """rows: dicts with the _BLOCK_COLS keys. One transaction for the batch."""
    if not rows:
        return
    with _connect() as conn:
        conn.executemany(
            f"INSERT INTO blocked ({', '.join(_BLOCK_COLS)}) VALUES ({', '.join('?' * len(_BLOCK_COLS))})",
            [tuple(r[c] for c in _BLOCK_COLS) for r in rows],
        )


def get_block(token: str):
    with _connect() as conn:
        return conn.execute("SELECT * FROM blocked WHERE token = ?", (token,)).fetchone()


def list_blocked(limit: int = 200, requested_only: bool = False):
    query = "SELECT * FROM blocked"
    if requested_only:
        query += " WHERE unblock_requested_at IS NOT NULL"
    query += " ORDER BY ts DESC LIMIT ?"
    with _connect() as conn:
        return conn.execute(query, (limit,)).fetchall()


def mark_unblock_requested(token: str, note: str) -> bool:
    """Atomically claim the one unblock request a block allows. False if it
    was already requested (or the token is unknown)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE blocked SET unblock_requested_at = ?, unblock_note = ? "
            "WHERE token = ? AND unblock_requested_at IS NULL",
            (time.time(), note, token),
        )
        return cur.rowcount == 1


def clear_unblock_requested(token: str) -> None:
    """Undo mark_unblock_requested when the email could not be sent, so the
    user can try again."""
    with _connect() as conn:
        conn.execute("UPDATE blocked SET unblock_requested_at = NULL, unblock_note = NULL WHERE token = ?", (token,))


def recent_unblock_requests(client_ip: str, since_ts: float) -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM blocked WHERE client_ip = ? AND unblock_requested_at >= ?",
            (client_ip, since_ts),
        ).fetchone()[0]


def log_access(client_ip: str, url_hash_: str, filename: str, hit: bool, size: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO access_log (ts, client_ip, url_hash, filename, hit, bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), client_ip, url_hash_, filename, 1 if hit else 0, size),
        )


def recent_access(limit: int = 200, client_ip: Optional[str] = None):
    query = "SELECT * FROM access_log"
    params: list = []
    if client_ip:
        query += " WHERE client_ip = ?"
        params.append(client_ip)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def top_clients(since_ts: Optional[float] = None, limit: int = 25):
    query = (
        "SELECT client_ip, COUNT(*) AS requests, SUM(hit) AS hits, "
        "SUM(bytes) AS bytes_served, "
        "SUM(CASE WHEN hit=1 THEN bytes ELSE 0 END) AS bytes_from_cache "
        "FROM access_log"
    )
    params: list = []
    if since_ts:
        query += " WHERE ts >= ?"
        params.append(since_ts)
    query += " GROUP BY client_ip ORDER BY bytes_served DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def top_files(since_ts: Optional[float] = None, limit: int = 25):
    query = (
        "SELECT url_hash, filename, COUNT(*) AS requests, SUM(hit) AS hits, "
        "SUM(bytes) AS bytes_served FROM access_log"
    )
    params: list = []
    if since_ts:
        query += " WHERE ts >= ?"
        params.append(since_ts)
    query += " GROUP BY url_hash ORDER BY requests DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        return conn.execute(query, params).fetchall()


def access_summary(since_ts: Optional[float] = None) -> dict:
    query = (
        "SELECT COUNT(*) AS requests, COUNT(DISTINCT client_ip) AS clients, "
        "COALESCE(SUM(hit),0) AS hits, COALESCE(SUM(bytes),0) AS bytes_served, "
        "COALESCE(SUM(CASE WHEN hit=1 THEN bytes ELSE 0 END),0) AS bytes_from_cache "
        "FROM access_log"
    )
    params: list = []
    if since_ts:
        query += " WHERE ts >= ?"
        params.append(since_ts)
    with _connect() as conn:
        return dict(conn.execute(query, params).fetchone())


def stats() -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS total_size, "
            # Hits and bytes saved are for downloads only; web-asset traffic is
            # reported separately (counters), so it isn't counted twice.
            "COALESCE(SUM(CASE WHEN kind='download' THEN hit_count END),0) AS total_hits, "
            "COALESCE(SUM(CASE WHEN kind='download' THEN size * hit_count END),0) AS bytes_saved FROM files"
        ).fetchone()
        return dict(row)


def purge_expired(now: Optional[float] = None) -> int:
    """Delete expired files and their rows. Idempotent, so it's safe even if
    two processes run it at once. Returns how many entries were removed."""
    now = now if now is not None else time.time()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT url_hash, path FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            (config.CACHE_DIR / row["path"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM files WHERE url_hash = ?", (row["url_hash"],))
    return len(rows)


def total_size() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]


def evict_to_quota(max_bytes: Optional[int] = None) -> int:
    """Evict least-recently-hit entries until the cache fits in max_bytes
    (0/None = unlimited). Returns how many entries were evicted."""
    max_bytes = config.MAX_CACHE_BYTES if max_bytes is None else max_bytes
    if not max_bytes:
        return 0
    evicted = 0
    with _connect() as conn:
        total = conn.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
        if total <= max_bytes:
            return 0
        rows = conn.execute(
            "SELECT url_hash, path, size FROM files "
            "ORDER BY COALESCE(last_hit_at, created_at) ASC"
        ).fetchall()
        for row in rows:
            if total <= max_bytes:
                break
            (config.CACHE_DIR / row["path"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM files WHERE url_hash = ?", (row["url_hash"],))
            total -= row["size"]
            evicted += 1
    return evicted


def disk_usage() -> dict:
    """Free/total space on the volume holding the cache."""
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(config.CACHE_DIR)
    return {"total": u.total, "used": u.used, "free": u.free}


def prune_access_log(retention_days: Optional[int] = None) -> int:
    """Delete access_log rows older than the retention window (0 = keep all)."""
    days = config.RETENTION_DAYS if retention_days is None else retention_days
    if not days:
        return 0
    with _connect() as conn:
        cur = conn.execute("DELETE FROM access_log WHERE ts < ?", (time.time() - days * 86400,))
        return cur.rowcount


def add_counters(deltas: dict) -> None:
    """Add to named counters (asset hits/misses/bytes saved), flushed in
    batches by the proxy rather than written per request."""
    if not deltas:
        return
    with _connect() as conn:
        for name, value in deltas.items():
            conn.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                (name, int(value)),
            )


def get_counters() -> dict:
    with _connect() as conn:
        return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM counters")}


def add_hourly_traffic(deltas: dict) -> None:
    """Add to the per-client, per-hour traffic tallies.

    `deltas` is {(client_ip, hour): (requests, bytes)}, accumulated in
    memory by each worker and flushed in batches on the same cycle as
    add_counters(). The upsert adds rather than replaces, so several
    workers flushing the same client-hour sum correctly instead of
    clobbering each other."""
    if not deltas:
        return
    with _connect() as conn:
        for (client_ip, hour), (requests, nbytes) in deltas.items():
            conn.execute(
                "INSERT INTO hourly_traffic (client_ip, hour, requests, bytes) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(client_ip, hour) DO UPDATE SET "
                "requests = requests + excluded.requests, bytes = bytes + excluded.bytes",
                (client_ip, int(hour), int(requests), int(nbytes)),
            )


def hourly_traffic(since_ts: Optional[float] = None, client_ip: Optional[str] = None):
    """Per-client, per-hour request counts and bytes for *all* traffic --
    not just the cacheable downloads access_log records."""
    query = "SELECT client_ip, hour, requests, bytes FROM hourly_traffic"
    where = []
    params: list = []
    if since_ts:
        where.append("hour >= ?")
        params.append(int(since_ts) // 3600 * 3600)
    if client_ip:
        where.append("client_ip = ?")
        params.append(client_ip)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY client_ip, hour"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def prune_hourly_traffic(retention_days: Optional[int] = None) -> int:
    """Drop hourly_traffic rows older than the retention window, on the same
    schedule and setting as access_log (0 = keep all)."""
    days = config.RETENTION_DAYS if retention_days is None else retention_days
    if not days:
        return 0
    with _connect() as conn:
        cur = conn.execute("DELETE FROM hourly_traffic WHERE hour < ?", (time.time() - days * 86400,))
        return cur.rowcount


def hourly_client_stats(since_ts: Optional[float] = None, client_ip: Optional[str] = None):
    """Per-client, per-hour requests/hits/bytes/new downloads (MISS-STORED)
    from access_log. Computed on demand from the ts/client indexes."""
    query = (
        "SELECT client_ip, CAST(ts / 3600 AS INTEGER) * 3600 AS hour, "
        "COUNT(*) AS requests, COALESCE(SUM(hit),0) AS hits, "
        "COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(1 - hit),0) AS new_downloads "
        "FROM access_log"
    )
    where = []
    params: list = []
    if since_ts:
        where.append("ts >= ?")
        params.append(since_ts)
    if client_ip:
        where.append("client_ip = ?")
        params.append(client_ip)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " GROUP BY client_ip, hour ORDER BY client_ip, hour"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# --- per-URL fetch locks (request coalescing across worker processes) ------

def _lock_dir() -> Path:
    return config.CACHE_DIR / ".locks"


def locks_available() -> bool:
    return fcntl is not None


def try_lock(url_hash_: str) -> Optional[int]:
    """Take the fetch lock for a URL without blocking. Returns a file
    descriptor to hand to release_lock(), or None if another request (in any
    worker process) already holds it. flock is released by the kernel if the
    holder dies, so a crashed worker can't wedge a URL."""
    if fcntl is None:
        return None
    d = _lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / url_hash_, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(fd: int) -> None:
    try:
        os.close(fd)  # closing drops the flock
    except OSError:
        pass


def cleanup_locks(max_age: float = 3600) -> int:
    """Delete lock files nobody holds and that haven't been touched lately, so
    the directory doesn't grow forever. Worst case of racing a new request is
    one duplicate fetch."""
    d = _lock_dir()
    if fcntl is None or not d.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_age
    for f in d.iterdir():
        try:
            if f.stat().st_mtime > cutoff:
                continue
            fd = os.open(f, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
        finally:
            os.close(fd)
    return removed
