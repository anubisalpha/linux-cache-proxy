"""SQLite-backed index over cached download files.

Files are stored on disk under CACHE_DIR, named by the sha256 of their
source URL plus the original extension (so a downloaded .msi is still a
real, openable .msi on disk). The DB just indexes metadata for the web UI.
"""
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import config

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
    hit_count INTEGER NOT NULL DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log(ts);
CREATE INDEX IF NOT EXISTS idx_access_log_client ON access_log(client_ip);
CREATE INDEX IF NOT EXISTS idx_access_log_url_hash ON access_log(url_hash);
"""


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
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


def known_hashes() -> set:
    """All cached url_hashes, for the proxy to hold in memory so a cache-miss
    lookup (the overwhelming majority of requests) never touches SQLite."""
    with _connect() as conn:
        return {row["url_hash"] for row in conn.execute("SELECT url_hash FROM files")}


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def get_entry(url_hash_: str) -> Optional[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM files WHERE url_hash = ?", (url_hash_,)
        ).fetchone()


def save_file(url: str, filename: str, content_type: str, data: bytes) -> Path:
    h = url_hash(url)
    suffix = Path(filename).suffix or ""
    rel_path = f"{h}{suffix}"
    dest = config.CACHE_DIR / rel_path
    dest.write_bytes(data)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO files (url_hash, url, filename, content_type, size, path, created_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(url_hash) DO UPDATE SET
                filename=excluded.filename,
                content_type=excluded.content_type,
                size=excluded.size,
                path=excluded.path,
                created_at=excluded.created_at
            """,
            (h, url, filename, content_type, len(data), rel_path, time.time()),
        )
    return dest


def record_hit(url_hash_: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE files SET hit_count = hit_count + 1, last_hit_at = ? WHERE url_hash = ?",
            (time.time(), url_hash_),
        )


def list_entries(search: Optional[str] = None, limit: int = 500, offset: int = 0):
    query = "SELECT * FROM files"
    params: list = []
    if search:
        query += " WHERE filename LIKE ? OR url LIKE ?"
        like = f"%{search}%"
        params += [like, like]
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
            "COALESCE(SUM(hit_count),0) AS total_hits, "
            "COALESCE(SUM(size * hit_count),0) AS bytes_saved FROM files"
        ).fetchone()
        return dict(row)
