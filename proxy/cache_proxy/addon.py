"""mitmproxy addon: serves cached files from disk, and caches new ones.

Two kinds of entry, with different lifetimes (both set in config.toml):
  download  installers/packages, cached for `download_ttl_days` (30)
  asset     static web assets (js/css/images/fonts), cached for at most
            `webcache.ttl_minutes` (60) and only when the origin allows it

Run with: mitmdump -s cache_proxy/addon.py --set confdir=~/.mitmproxy
Requires SSL-bump (mitmproxy's default MITM behaviour) to see inside HTTPS
downloads. Clients must trust the mitmproxy CA cert (see README).

Several worker processes run this addon against one shared cache, so it
refreshes its in-memory view of the cache periodically rather than trusting
what it loaded at startup.
"""
import asyncio
import logging
import os
import re
import secrets
import sys
import time
from typing import Optional

from mitmproxy import http

# mitmdump loads this file as a standalone script (not as part of the
# cache_proxy package), so relative imports don't work here. Make the
# proxy/ directory importable and import cache_proxy absolutely instead.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cache_proxy import config, store  # noqa: E402

logger = logging.getLogger("cache_proxy")

# Hits bigger than this, and any Range request, are served by the loopback
# file server (streamed from disk) instead of being read into memory.
STREAM_THRESHOLD = 8 * 1024 * 1024
REFRESH_INTERVAL = float(os.environ.get("CACHE_PROXY_REFRESH_INTERVAL", "10"))  # seconds between cache-state refreshes / counter flushes
CHUNK = 1024 * 1024

_CACHE_FILE_RE = re.compile(r"^[0-9a-f]{64}(\.[A-Za-z0-9_.-]{1,32})?$")


def parse_range(header: Optional[str], size: int):
    """Parse a Range header against a file size.

    Returns None (no/unsupported range: serve the whole file), "invalid"
    (unsatisfiable: 416) or an inclusive (start, end) byte pair. Multi-range
    requests are treated as unsupported and get the whole file.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes=") or "," in header:
        return None
    spec = header[6:].strip()
    if "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    try:
        if first == "":  # suffix range: last N bytes
            n = int(last)
            if n <= 0:
                return "invalid"
            return (max(0, size - n), size - 1) if size else "invalid"
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return "invalid"
    return (start, min(end, size - 1))


class LocalFileServer:
    """Tiny HTTP server on loopback that streams cache files with Range
    support. mitmproxy can't stream a response body from a request hook, so
    for big or ranged hits the addon re-points the flow here and lets
    mitmproxy stream the (X-Cache-Proxy: HIT) response through."""

    def __init__(self, token: str):
        self.token = token
        self.port = 0
        self._server = None

    async def start(self) -> None:
        # asyncio.streams.start_server, not asyncio.start_server: worker_main
        # patches the latter to set SO_REUSEPORT, which we don't want here.
        self._server = await asyncio.streams.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _reply(self, writer, status: str, headers: dict, body: bytes = b"") -> None:
        head = f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(head.encode("latin-1") + b"\r\n" + body)
        await writer.drain()

    async def _handle(self, reader, writer) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = raw.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            base = {"Connection": "close", "Content-Length": "0"}
            if headers.get("x-cache-internal") != self.token or method not in ("GET", "HEAD"):
                await self._reply(writer, "403 Forbidden", base)
                return
            name = target.split("?", 1)[0].lstrip("/")
            if not _CACHE_FILE_RE.match(name):
                await self._reply(writer, "404 Not Found", base)
                return
            try:
                size = (config.CACHE_DIR / name).stat().st_size
            except OSError:
                await self._reply(writer, "404 Not Found", base)
                return

            rng = parse_range(headers.get("range"), size)
            out = {
                "Content-Type": headers.get("x-cache-content-type") or "application/octet-stream",
                "Accept-Ranges": "bytes",
                "X-Cache-Proxy": "HIT",
                "Connection": "close",
            }
            if rng == "invalid":
                await self._reply(writer, "416 Range Not Satisfiable", {**base, "Content-Range": f"bytes */{size}"})
                return
            if rng is None:
                status, start, length = "200 OK", 0, size
            else:
                start, end = rng
                length = end - start + 1
                status = "206 Partial Content"
                out["Content-Range"] = f"bytes {start}-{end}/{size}"
            out["Content-Length"] = str(length)
            await self._reply(writer, status, out)
            if method == "GET":
                with open(config.CACHE_DIR / name, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                        remaining -= len(chunk)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


class CacheAddon:
    def __init__(self):
        self._known_hashes: set = set()
        self._counters: dict = {}
        self._asset_hits: dict = {}  # url_hash -> hits not yet written to the DB
        # flow.id -> (lock fd, taken at): fetches this worker is leading.
        self._leading: dict = {}
        self._token = secrets.token_hex(16)
        self._server = LocalFileServer(self._token)

    def load(self, loader):
        store.init_db()
        # Every GET the whole office makes runs through request() below --
        # not just downloads. A SQLite query per request doesn't scale to
        # that traffic. Hold the known cache keys in memory instead; only
        # the tiny minority that are actual hits ever touch the DB.
        self._known_hashes = store.known_hashes()

    async def running(self):
        await self._server.start()
        asyncio.get_running_loop().create_task(self._refresh_loop())

    async def _refresh_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            try:
                # Other workers add (and the supervisor purges) entries; pick
                # that up. Off the event loop so a slow query can't stall traffic.
                self._known_hashes = await loop.run_in_executor(None, store.known_hashes)
                counters, asset_hits = self._take_stats()
                try:
                    await loop.run_in_executor(None, self._write_stats, counters, asset_hits)
                except Exception:
                    self._restore_stats(counters, asset_hits)
                    raise
                # Safety net: never let a leader that somehow missed every
                # release hook keep a URL locked past the waiters' patience.
                stale = time.monotonic() - max(2 * config.COALESCE_WAIT, 300)
                for fid in [f for f, (_fd, t) in self._leading.items() if t < stale]:
                    self._release(fid)
            except Exception as e:
                logger.warning("cache refresh failed: %s", e)

    # Stats tallies (aggregate counters and per-asset hit counts) live in
    # memory and are written to the database in batches. The snapshot is taken
    # on the event-loop thread, the write can run elsewhere, and a failed
    # write puts the tallies back so nothing is lost.

    def _take_stats(self):
        counters, self._counters = self._counters, {}
        asset_hits, self._asset_hits = self._asset_hits, {}
        return counters, asset_hits

    @staticmethod
    def _write_stats(counters: dict, asset_hits: dict) -> None:
        store.add_counters(counters)
        store.record_hits(asset_hits)

    def _restore_stats(self, counters: dict, asset_hits: dict) -> None:
        for k, v in counters.items():
            self._counters[k] = self._counters.get(k, 0) + v
        for k, v in asset_hits.items():
            self._asset_hits[k] = self._asset_hits.get(k, 0) + v

    def _flush_stats(self) -> None:
        counters, asset_hits = self._take_stats()
        try:
            self._write_stats(counters, asset_hits)
        except Exception:
            self._restore_stats(counters, asset_hits)
            raise

    def _count(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    # ---- request coalescing ------------------------------------------------
    #
    # When many clients ask for the same not-yet-cached file at once, only the
    # first should go upstream. It takes a per-URL file lock (shared by every
    # worker process); the others wait for it to be released and are then
    # served from the cache. The leader releases as soon as the outcome is
    # known: stored, or clearly *not* going to be (streamed, uncacheable,
    # error). Waiters therefore never queue behind a multi-GB download, and an
    # uncacheable response doesn't serialise them.

    @staticmethod
    def _coalescible(flow: http.HTTPFlow) -> bool:
        if config.COALESCE_WAIT <= 0 or not store.locks_available():
            return False
        req = flow.request
        if req.method != "GET" or req.headers.get("Range") or req.headers.get("Authorization"):
            return False
        if config.host_matches(req.host, config.NEVER_CACHE_HOSTS) or config.host_matches(
            req.host, config.NEVER_INTERCEPT_HOSTS
        ):
            return False
        path = req.pretty_url.split("?")[0].lower()
        if any(path.endswith(e) for e in config.CACHEABLE_EXTENSIONS):
            return True
        return config.WEBCACHE_ENABLED and any(path.endswith(e) for e in config.WEBCACHE_EXTENSIONS)

    @staticmethod
    def _fresh_entry(h: str) -> bool:
        entry = store.get_entry(h)
        return entry is not None and not store.is_expired(entry)

    def _release(self, flow_id: str) -> None:
        held = self._leading.pop(flow_id, None)
        if held:
            store.release_lock(held[0])

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        if not self._coalescible(flow):
            return
        h = store.url_hash(flow.request.pretty_url)
        if h in self._known_hashes:
            return
        fd = store.try_lock(h)
        if fd is not None:
            # Leader. Another worker may have stored it since our last index
            # refresh; if so there's nothing to fetch.
            if self._fresh_entry(h):
                store.release_lock(fd)
                self._known_hashes.add(h)
            else:
                self._leading[flow.id] = (fd, time.monotonic())
            return
        # Someone else is fetching this URL: wait for them, then use their copy.
        deadline = time.monotonic() + config.COALESCE_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            fd = store.try_lock(h)
            if fd is not None:  # released; we only wanted to know that
                store.release_lock(fd)
                break
        if self._fresh_entry(h):
            self._known_hashes.add(h)
            self._count("coalesced_requests")

    def error(self, flow: http.HTTPFlow) -> None:
        self._release(flow.id)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method != "GET":
            return
        url = flow.request.pretty_url
        h = store.url_hash(url)
        if h not in self._known_hashes:
            return
        entry = store.get_entry(h)
        if not entry:
            return
        if store.is_expired(entry):
            # Treat as a miss: the fresh response overwrites it and resets
            # the TTL. Until then stop advertising it as known.
            self._known_hashes.discard(h)
            return
        path = config.CACHE_DIR / entry["path"]
        if not path.exists():
            return
        is_asset = entry["kind"] == "asset"
        size = entry["size"]
        range_header = flow.request.headers.get("Range")

        if range_header or size > STREAM_THRESHOLD:
            served = self._route_to_file_server(flow, entry, range_header)
        else:
            served = self._serve_in_memory(flow, entry, path)
        if not served:
            return
        self._release(flow.id)

        if is_asset:
            self._count("asset_hits")
            self._asset_hits[h] = self._asset_hits.get(h, 0) + 1
            self._count("asset_bytes_saved", size)
        else:
            store.record_hit(h)
            store.log_access(_client_ip(flow), h, entry["filename"], hit=True, size=self._served_bytes(range_header, size))
        logger.info("HIT %s <- %s", url, _client_ip(flow))

    @staticmethod
    def _served_bytes(range_header: Optional[str], size: int) -> int:
        rng = parse_range(range_header, size)
        if isinstance(rng, tuple):
            return rng[1] - rng[0] + 1
        return 0 if rng == "invalid" else size

    def _serve_in_memory(self, flow, entry, path) -> bool:
        data = path.read_bytes()
        flow.response = http.Response.make(
            200,
            data,
            {
                "Content-Type": entry["content_type"] or "application/octet-stream",
                "Accept-Ranges": "bytes",
                "X-Cache-Proxy": "HIT",
            },
        )
        return True

    def _route_to_file_server(self, flow, entry, range_header) -> bool:
        if not self._server.port:
            return False  # server not up yet; fall through to upstream
        flow.request.scheme = "http"
        flow.request.host = "127.0.0.1"
        flow.request.port = self._server.port
        flow.request.path = "/" + entry["path"]
        flow.request.headers["Host"] = f"127.0.0.1:{self._server.port}"
        flow.request.headers["X-Cache-Internal"] = self._token
        flow.request.headers["X-Cache-Content-Type"] = entry["content_type"] or "application/octet-stream"
        return True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Decide, before the body arrives, whether to buffer or stream it.

        Without this, mitmproxy buffers every response body in full for
        every flow -- web pages, images, video, SaaS traffic, all of it --
        regardless of whether we'll ever cache it. Only buffer candidates
        we might actually store; stream everything else straight through.
        """
        if flow.response is None or flow.request.method != "GET":
            self._release(flow.id)
            return
        if flow.response.headers.get("X-Cache-Proxy") == "HIT":
            flow.response.stream = True  # from the loopback file server
            self._release(flow.id)
            return
        cls = _classify(flow, flow.response)
        if flow.response.status_code != 200 or cls is None:
            flow.response.stream = True
            self._release(flow.id)  # won't be stored: let waiters go upstream
            return
        content_length = flow.response.headers.get("Content-Length")
        limit = config.WEBCACHE_MAX_SIZE if cls[0] == "asset" else config.MAX_BUFFER_SIZE
        if content_length and content_length.isdigit() and int(content_length) > limit:
            flow.response.stream = True
            self._release(flow.id)  # too big to store, don't hold anyone up

    def response(self, flow: http.HTTPFlow) -> None:
        try:
            self._store_response(flow)
        finally:
            self._release(flow.id)

    def _store_response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None or flow.request.method != "GET":
            return
        if flow.response.headers.get("X-Cache-Proxy") == "HIT":
            return
        if flow.response.status_code != 200:
            return
        if flow.response.stream:
            return

        cls = _classify(flow, flow.response)
        if cls is None:
            return
        kind, ttl = cls
        url = flow.request.pretty_url

        data = flow.response.content
        if not data:
            return
        if kind == "download" and len(data) < config.MIN_CACHE_SIZE:
            return
        limit = config.WEBCACHE_MAX_SIZE if kind == "asset" else config.MAX_BUFFER_SIZE
        if len(data) > limit:
            # No Content-Length header warned us upfront (chunked transfer),
            # so it got fully buffered anyway -- still refuse to write an
            # unbounded blob to disk.
            return

        filename = _filename_from_path(flow.request.path)
        content_type = flow.response.headers.get("Content-Type", "")
        store.save_file(url, filename, content_type, data, kind=kind, ttl=ttl)
        h = store.url_hash(url)
        self._known_hashes.add(h)
        flow.response.headers["X-Cache-Proxy"] = "MISS-STORED"
        if kind == "asset":
            self._count("asset_misses")
        else:
            store.log_access(_client_ip(flow), h, filename, hit=False, size=len(data))
            store.evict_to_quota()
        logger.info("STORED %s [%s] (%d bytes) <- %s", url, kind, len(data), _client_ip(flow))


def _cache_control(response: http.Response) -> dict:
    """Parse Cache-Control into {directive: value-or-True}."""
    out: dict = {}
    for part in response.headers.get("Cache-Control", "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        name, _, value = part.partition("=")
        out[name.strip()] = value.strip().strip('"') if value else True
    return out


def _asset_ttl(flow: http.HTTPFlow, response: http.Response) -> Optional[float]:
    """TTL for a static-asset response, or None if it must not be cached.

    Origin headers are always respected: the configured lifetime is a cap,
    never a floor."""
    if not config.WEBCACHE_ENABLED:
        return None
    if flow.request.headers.get("Range") or flow.request.headers.get("Authorization"):
        return None
    if response.headers.get("Set-Cookie"):
        return None
    cc = _cache_control(response)
    if any(d in cc for d in ("no-store", "private", "no-cache")):
        return None
    if "no-cache" in response.headers.get("Pragma", "").lower():
        return None
    vary = response.headers.get("Vary", "")
    if any(v.strip().lower() not in ("", "accept-encoding") for v in vary.split(",")):
        return None
    ttl = float(config.WEBCACHE_TTL)
    for directive in ("s-maxage", "max-age"):  # shared-cache directive wins
        if directive in cc:
            try:
                age = float(cc[directive])
            except (TypeError, ValueError):
                return None
            if age <= 0:
                return None
            ttl = min(ttl, age)
            break
    return ttl


def _classify(flow: http.HTTPFlow, response: http.Response) -> Optional[tuple]:
    """Returns ("download", ttl), ("asset", ttl) or None (don't cache)."""
    host = flow.request.host
    if config.host_matches(host, config.NEVER_CACHE_HOSTS):
        return None
    # Belt-and-braces: if ignore_hosts wasn't wired up at launch for some
    # reason, never_intercept_hosts still refuses to cache even though
    # this addon shouldn't be seeing decrypted traffic for these at all.
    if config.host_matches(host, config.NEVER_INTERCEPT_HOSTS):
        return None

    path = flow.request.pretty_url.split("?")[0].lower()
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()

    if any(path.endswith(ext) for ext in config.CACHEABLE_EXTENSIONS):
        return ("download", float(config.DOWNLOAD_TTL))
    if content_type in config.WEBCACHE_CONTENT_TYPES or any(
        path.endswith(ext) for ext in config.WEBCACHE_EXTENSIONS
    ):
        ttl = _asset_ttl(flow, response)
        return ("asset", ttl) if ttl is not None else None
    if content_type in config.CACHEABLE_CONTENT_TYPES:
        return ("download", float(config.DOWNLOAD_TTL))
    return None


def _client_ip(flow: http.HTTPFlow) -> str:
    peername = flow.client_conn.peername
    return peername[0] if peername else "unknown"


def _filename_from_path(path: str) -> str:
    name = path.rstrip("/").split("/")[-1].split("?")[0]
    return name or "download.bin"


addons = [CacheAddon()]
