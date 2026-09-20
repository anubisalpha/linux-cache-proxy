"""End-to-end tests through real mitmdump processes: static-asset caching,
Range/streamed hits, and the supervised multi-worker pool.
"""
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cache_proxy import addon, store

PROXY_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pool relies on /proc and SO_REUSEPORT")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, proc, seconds: float = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("proxy did not start:\n" + proc.stdout.read().decode(errors="replace"))


@pytest.fixture
def origin(tmp_path):
    """Origin serving a script, a no-store script, an HTML page, and files."""
    small = os.urandom(2 * 1024 * 1024)
    big = os.urandom(addon.STREAM_THRESHOLD + 3 * 1024 * 1024)
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "small.exe").write_bytes(small)
    (tmp_path / "www" / "big.iso").write_bytes(big)
    hits = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] += 1
            p = self.path.split("?")[0]
            headers = {}
            if p == "/app.js":
                body, ctype, headers = b"console.log(1);" * 10, "application/javascript", {"Cache-Control": "public, max-age=600"}
            elif p == "/private.js":
                body, ctype, headers = b"secret();", "application/javascript", {"Cache-Control": "no-store"}
            elif p == "/page.html":
                body, ctype = b"<html>hi</html>", "text/html"
            elif p in ("/small.exe", "/big.iso"):
                body, ctype = (tmp_path / "www" / p[1:]).read_bytes(), "application/octet-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{port}", "small": small, "big": big, "hits": hits}
    httpd.shutdown()


def _env(tmp_path, **extra):
    env = os.environ.copy()
    env.update(
        CACHE_PROXY_DIR=str(tmp_path / "files"),
        CACHE_PROXY_DB=str(tmp_path / "index.db"),
        CACHE_PROXY_CONFDIR=str(tmp_path / "ca"),
        CACHE_PROXY_STATUS_FILE=str(tmp_path / "workers.json"),
        CACHE_PROXY_REFRESH_INTERVAL="1",
    )
    env.update({k: str(v) for k, v in extra.items()})
    return env


@pytest.fixture
def single_proxy(tmp_path):
    mitmdump = Path(sys.executable).parent / "mitmdump"
    if not mitmdump.exists() and not shutil.which("mitmdump"):
        pytest.skip("mitmdump not installed")
    port = _free_port()
    proc = subprocess.Popen(
        [str(mitmdump), "--listen-port", str(port), "--set", f"confdir={tmp_path / 'ca'}", "-s", "cache_proxy/addon.py"],
        cwd=PROXY_ROOT, env=_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    _wait_port(port, proc)
    yield f"http://127.0.0.1:{port}", tmp_path
    proc.terminate()
    proc.wait(timeout=10)


def _get(proxy, url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy}))
    return opener.open(urllib.request.Request(url, headers=headers or {}), timeout=30)


def _wait_until(fn, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.3)
    return False


# ---- static assets -------------------------------------------------------

def test_static_asset_cached_then_served_from_cache(origin, single_proxy):
    proxy, _ = single_proxy
    url = origin["base"] + "/app.js"
    r1 = _get(proxy, url)
    assert r1.headers["X-Cache-Proxy"] == "MISS-STORED"
    body = r1.read()
    before = origin["hits"]["n"]
    r2 = _get(proxy, url)
    assert r2.headers["X-Cache-Proxy"] == "HIT"
    assert r2.read() == body
    assert origin["hits"]["n"] == before  # origin not contacted again


def test_no_store_and_html_are_not_cached(origin, single_proxy):
    proxy, _ = single_proxy
    for path in ("/private.js", "/page.html"):
        assert _get(proxy, origin["base"] + path).headers.get("X-Cache-Proxy") is None
        assert _get(proxy, origin["base"] + path).headers.get("X-Cache-Proxy") is None


def test_asset_shorter_than_min_download_size_is_still_cached(origin, single_proxy, tmp_path):
    proxy, tmp = single_proxy
    _get(proxy, origin["base"] + "/app.js").read()
    assert _wait_until(lambda: os.path.exists(tmp / "index.db"))
    import sqlite3
    row = sqlite3.connect(tmp / "index.db").execute("SELECT kind, size FROM files").fetchone()
    assert row[0] == "asset" and row[1] < 1024 * 1024


# ---- range + streamed hits ----------------------------------------------

def test_range_request_on_cached_download(origin, single_proxy):
    proxy, _ = single_proxy
    url = origin["base"] + "/small.exe"
    assert _get(proxy, url).headers["X-Cache-Proxy"] == "MISS-STORED"
    r = _get(proxy, url, {"Range": "bytes=100-199"})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes 100-199/{len(origin['small'])}"
    assert r.headers["X-Cache-Proxy"] == "HIT"
    assert r.read() == origin["small"][100:200]

    tail = _get(proxy, url, {"Range": "bytes=-50"})
    assert tail.read() == origin["small"][-50:]

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(proxy, url, {"Range": f"bytes={len(origin['small']) + 5}-"})
    assert exc.value.code == 416


def test_resume_download_from_cache(origin, single_proxy):
    proxy, _ = single_proxy
    url = origin["base"] + "/small.exe"
    _get(proxy, url).read()
    half = len(origin["small"]) // 2
    rest = _get(proxy, url, {"Range": f"bytes={half}-"})
    assert origin["small"][:half] + rest.read() == origin["small"]


def test_large_hit_is_streamed_from_disk_intact(origin, single_proxy):
    proxy, _ = single_proxy
    url = origin["base"] + "/big.iso"
    assert _get(proxy, url).headers["X-Cache-Proxy"] == "MISS-STORED"
    r = _get(proxy, url)
    assert r.headers["X-Cache-Proxy"] == "HIT"
    assert r.read() == origin["big"]


def test_internal_file_server_rejects_requests_without_the_token(origin, single_proxy):
    # The loopback file server must not serve cache files to whoever finds its port.
    a = addon.CacheAddon()

    async def run():
        await a._server.start()
        reader, writer = await __import__("asyncio").open_connection("127.0.0.1", a._server.port)
        writer.write(b"GET /" + b"0" * 64 + b" HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return line

    assert b"403" in __import__("asyncio").run(run())


# ---- worker pool ---------------------------------------------------------

def _two_active(status_file):
    try:
        st = json.loads(status_file.read_text())
    except (OSError, ValueError):
        return False
    return len([w for w in st["workers"] if w["state"] == "active"]) == 2


@pytest.fixture
def pool(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cache_proxy.supervisor", "--listen-port", str(port),
         "--set", f"confdir={tmp_path / 'ca'}", "-s", "cache_proxy/addon.py"],
        cwd=PROXY_ROOT,
        env=_env(tmp_path, CACHE_PROXY_PORT=port, CACHE_PROXY_MIN_WORKERS=2, CACHE_PROXY_MAX_WORKERS=2,
                 CACHE_PROXY_SAMPLE_INTERVAL=1),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    _wait_port(port, proc, seconds=60)
    # Both workers must be up and listening before a test disturbs the pool.
    assert _wait_until(lambda: _two_active(tmp_path / "workers.json"), seconds=60)
    yield f"http://127.0.0.1:{port}", tmp_path, proc
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_pool_shares_cache_across_workers_and_reports_status(origin, pool):
    proxy, tmp, _proc = pool
    url = origin["base"] + "/small.exe"
    assert _get(proxy, url).headers["X-Cache-Proxy"] == "MISS-STORED"

    # A file stored by whichever worker took that connection must become a
    # HIT no matter which worker later connections land on.
    def all_hits():
        return all(_get(proxy, url).headers.get("X-Cache-Proxy") == "HIT" for _ in range(20))

    assert _wait_until(all_hits, seconds=30)
    # Until the other worker refreshes its index it may fetch the file once
    # itself; after that it never goes back to the origin. So: at most one
    # fetch per worker, not one per request.
    assert origin["hits"]["n"] <= 2

    status_file = tmp / "workers.json"
    status = json.loads(status_file.read_text())
    assert len([w for w in status["workers"] if w["state"] == "active"]) == 2
    assert status["min_workers"] == 2 and status["max_workers"] == 2
    assert len({w["pid"] for w in status["workers"]}) == 2


def test_pool_replaces_a_crashed_worker(origin, pool):
    proxy, tmp, _proc = pool
    status_file = tmp / "workers.json"
    victim = json.loads(status_file.read_text())["workers"][0]["pid"]
    os.kill(victim, signal.SIGKILL)

    def replaced():
        st = json.loads(status_file.read_text())
        pids = {w["pid"] for w in st["workers"] if w["state"] == "active"}
        return victim not in pids and len(pids) == 2  # replacement is up and listening

    assert _wait_until(replaced, seconds=30)
    assert _get(proxy, origin["base"] + "/app.js").status == 200  # still serving
