"""mitmproxy addon: serves cached downloads from disk, and caches new ones.

Run with: mitmdump -s cache_proxy/addon.py --set confdir=~/.mitmproxy
Requires SSL-bump (mitmproxy's default MITM behaviour) to see inside HTTPS
downloads. Clients must trust the mitmproxy CA cert (see README).
"""
import logging
import os
import sys

from mitmproxy import http

# mitmdump loads this file as a standalone script (not as part of the
# cache_proxy package), so relative imports don't work here. Make the
# proxy/ directory importable and import cache_proxy absolutely instead.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cache_proxy import config, store  # noqa: E402

logger = logging.getLogger("cache_proxy")


class CacheAddon:
    def load(self, loader):
        store.init_db()
        # Every GET the whole office makes runs through request() below --
        # not just downloads. A SQLite query per request doesn't scale to
        # that traffic. Hold the known cache keys in memory instead; only
        # the tiny minority that are actual hits ever touch the DB.
        self._known_hashes = store.known_hashes()

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
        path = config.CACHE_DIR / entry["path"]
        if not path.exists():
            return
        data = path.read_bytes()
        flow.response = http.Response.make(
            200,
            data,
            {
                "Content-Type": entry["content_type"] or "application/octet-stream",
                "X-Cache-Proxy": "HIT",
            },
        )
        store.record_hit(entry["url_hash"])
        store.log_access(_client_ip(flow), entry["url_hash"], entry["filename"], hit=True, size=len(data))
        logger.info("HIT %s <- %s", url, _client_ip(flow))

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Decide, before the body arrives, whether to buffer or stream it.

        Without this, mitmproxy buffers every response body in full for
        every flow -- web pages, images, video, SaaS traffic, all of it --
        regardless of whether we'll ever cache it. Only buffer candidates
        we might actually store; stream everything else straight through.
        """
        if flow.response is None or flow.request.method != "GET":
            return
        if flow.response.headers.get("X-Cache-Proxy") == "HIT":
            return
        if flow.response.status_code != 200 or not _is_cacheable(flow, flow.response):
            flow.response.stream = True
            return
        content_length = flow.response.headers.get("Content-Length")
        if content_length and int(content_length) > config.MAX_BUFFER_SIZE:
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None or flow.request.method != "GET":
            return
        if flow.response.headers.get("X-Cache-Proxy") == "HIT":
            return
        if flow.response.status_code != 200:
            return
        if flow.response.stream:
            return

        url = flow.request.pretty_url
        if not _is_cacheable(flow, flow.response):
            return

        data = flow.response.content
        if not data or len(data) < config.MIN_CACHE_SIZE:
            return
        if len(data) > config.MAX_BUFFER_SIZE:
            # No Content-Length header warned us upfront (chunked transfer),
            # so it got fully buffered anyway -- still refuse to write an
            # unbounded blob to disk.
            return

        filename = _filename_from_path(flow.request.path)
        content_type = flow.response.headers.get("Content-Type", "")
        store.save_file(url, filename, content_type, data)
        h = store.url_hash(url)
        self._known_hashes.add(h)
        flow.response.headers["X-Cache-Proxy"] = "MISS-STORED"
        store.log_access(_client_ip(flow), h, filename, hit=False, size=len(data))
        logger.info("STORED %s (%d bytes) <- %s", url, len(data), _client_ip(flow))


def _is_cacheable(flow: http.HTTPFlow, response: http.Response) -> bool:
    host = flow.request.host
    if config.host_matches(host, config.NEVER_CACHE_HOSTS):
        return False
    # Belt-and-braces: if ignore_hosts wasn't wired up at launch for some
    # reason, never_intercept_hosts still refuses to cache even though
    # this addon shouldn't be seeing decrypted traffic for these at all.
    if config.host_matches(host, config.NEVER_INTERCEPT_HOSTS):
        return False

    url = flow.request.pretty_url
    path = url.split("?")[0].lower()
    if any(path.endswith(ext) for ext in config.CACHEABLE_EXTENSIONS):
        return True
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return content_type in config.CACHEABLE_CONTENT_TYPES


def _client_ip(flow: http.HTTPFlow) -> str:
    peername = flow.client_conn.peername
    return peername[0] if peername else "unknown"


def _filename_from_path(path: str) -> str:
    name = path.rstrip("/").split("/")[-1].split("?")[0]
    return name or "download.bin"


addons = [CacheAddon()]
