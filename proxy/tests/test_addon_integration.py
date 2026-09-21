"""End-to-end test: real mitmdump process + cache_proxy addon + a local
file server, proxied through mitmdump, verifying MISS-STORED then HIT.
"""
import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cache_proxy import store

PROXY_ROOT = Path(__file__).resolve().parents[1]


def _find_mitmdump() -> str | None:
    # Prefer the mitmdump next to the interpreter running this test (covers
    # running via `venv-proxy/bin/pytest` without activating the venv).
    candidate = Path(sys.executable).parent / "mitmdump"
    if candidate.exists():
        return str(candidate)
    return shutil.which("mitmdump")


MITMDUMP = _find_mitmdump()
pytestmark = pytest.mark.skipif(MITMDUMP is None, reason="mitmdump not installed")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_file_server(tmp_path):
    data = os.urandom(1024 * 1024 * 2)
    root = tmp_path / "www"
    root.mkdir()
    (root / "tool-setup.exe").write_bytes(data)

    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/tool-setup.exe", data
    httpd.shutdown()


@pytest.fixture
def fake_media_server(tmp_path):
    """A large response with a non-cacheable extension/content-type --
    e.g. what a video stream or big webpage asset looks like."""
    data = os.urandom(1024 * 1024 * 3)
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(data)

    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/clip.mp4", data
    httpd.shutdown()


@pytest.fixture
def mitm_proxy(tmp_path):
    port = _free_port()
    env = os.environ.copy()
    env["CACHE_PROXY_DIR"] = str(tmp_path / "files")
    env["CACHE_PROXY_DB"] = str(tmp_path / "index.db")
    proc = subprocess.Popen(
        [
            MITMDUMP,
            "--listen-port", str(port),
            "--set", f"confdir={tmp_path / 'ca'}",
            "-s", "cache_proxy/addon.py",
        ],
        cwd=PROXY_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # mitmdump has no simple stdout readiness marker; poll the port instead.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("mitmdump did not start:\n" + proc.stdout.read().decode(errors="replace"))
    yield f"http://127.0.0.1:{port}", tmp_path
    proc.terminate()
    proc.wait(timeout=5)


def test_cache_hit_after_first_fetch(fake_file_server, mitm_proxy):
    file_url, expected_data = fake_file_server
    proxy_url, _tmp_path = mitm_proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url}))

    r1 = opener.open(file_url, timeout=15)
    assert r1.headers.get("X-Cache-Proxy") == "MISS-STORED"
    assert r1.read() == expected_data

    r2 = opener.open(file_url, timeout=15)
    assert r2.headers.get("X-Cache-Proxy") == "HIT"
    assert r2.read() == expected_data


def test_non_cacheable_content_streams_through_uncached(fake_media_server, mitm_proxy, monkeypatch):
    media_url, expected_data = fake_media_server
    proxy_url, tmp_path = mitm_proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url}))

    resp = opener.open(media_url, timeout=15)
    # Not one of our cacheable extensions/content-types, so no cache header
    # and no attempt to buffer-and-store it.
    assert resp.headers.get("X-Cache-Proxy") is None
    assert resp.read() == expected_data

    monkeypatch.setattr(store.config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(store.config, "DB_PATH", tmp_path / "index.db")
    assert store.list_entries() == []


def _filtering_proxy(tmp_path, blockpage_url="", blockpage_port=80):
    """mitmdump with content filtering on: 127.0.0.1 is listed as blocked,
    so anything served by the fake origin servers is 'a blocked site'."""
    (tmp_path / "blocked.conf").write_text("127.0.0.1\n")
    (tmp_path / "allowed.conf").write_text("")
    (tmp_path / "patterns.conf").write_text("")
    port = _free_port()
    env = os.environ.copy()
    env.update({
        "CACHE_PROXY_DIR": str(tmp_path / "files"),
        "CACHE_PROXY_DB": str(tmp_path / "index.db"),
        "CACHE_PROXY_FILTERING": "1",
        "CACHE_PROXY_LISTS_DIR": str(tmp_path / "lists"),
        "CACHE_PROXY_BLOCKED_HOSTS_FILE": str(tmp_path / "blocked.conf"),
        "CACHE_PROXY_ALLOWED_HOSTS_FILE": str(tmp_path / "allowed.conf"),
        "CACHE_PROXY_BLOCKED_URL_PATTERNS_FILE": str(tmp_path / "patterns.conf"),
        "CACHE_PROXY_BLOCKPAGE_URL": blockpage_url,
        "CACHE_PROXY_BLOCKPAGE_PORT": str(blockpage_port),
        "CACHE_PROXY_REFRESH_INTERVAL": "1",
    })
    proc = subprocess.Popen(
        [MITMDUMP, "--listen-port", str(port), "--set", f"confdir={tmp_path / 'ca'}", "-s", "cache_proxy/addon.py"],
        cwd=PROXY_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    return proc, f"http://127.0.0.1:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _open(proxy_url, url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url}), _NoRedirect)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        return opener.open(req, timeout=15)
    except urllib.error.HTTPError as e:
        return e


def test_blocked_site_through_real_proxy_inline_403_then_allow_list(fake_file_server, tmp_path):
    file_url, expected_data = fake_file_server
    proc, proxy_url = _filtering_proxy(tmp_path)
    try:
        r = _open(proxy_url, file_url)
        assert r.code == 403 and r.headers["X-Cache-Proxy"] == "BLOCKED" and b"Access blocked" in r.read()
        assert not list((tmp_path / "files").glob("*"))  # nothing cached from a blocked site
        (tmp_path / "allowed.conf").write_text("127.0.0.1\n")
        deadline = time.time() + 30  # list edits are picked up by the refresh loop
        while _open(proxy_url, file_url).code == 403:
            assert time.time() < deadline, "allow-list edit was never picked up"
            time.sleep(1)
        assert _open(proxy_url, file_url).read() == expected_data
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_page_load_redirects_and_other_requests_get_403_and_all_are_recorded(fake_file_server, tmp_path):
    file_url, _ = fake_file_server
    proc, proxy_url = _filtering_proxy(tmp_path, blockpage_url="http://blocked-page.test")
    try:
        nav = _open(proxy_url, file_url, {"Sec-Fetch-Dest": "document", "User-Agent": "E2E/1.0"})
        assert nav.code == 302 and nav.headers["Location"].startswith("http://blocked-page.test/blocked?t=")
        sub = _open(proxy_url, file_url, {"Sec-Fetch-Dest": "script"})
        assert sub.code == 403 and "Location" not in sub.headers
        plain = _open(proxy_url, file_url)  # apt/curl style: no Sec-Fetch headers
        assert plain.code == 403 and "Location" not in plain.headers
        time.sleep(3)  # subresource blocks are batched to the DB on the refresh tick
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "index.db")
    rows = conn.execute("SELECT client_ip, kind, user_agent, token FROM blocked").fetchall()
    assert len(rows) == 3 and {r[1] for r in rows} == {"host"} and all(r[0] == "127.0.0.1" for r in rows)
    assert nav.headers["Location"].split("t=")[1] in {r[3] for r in rows}
    assert any(r[2] == "E2E/1.0" for r in rows)


def test_requests_for_the_block_page_go_to_loopback_tagged_with_client_ip(tmp_path):
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["client_ip"] = self.path, self.headers.get("X-Client-IP")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    proc, proxy_url = _filtering_proxy(tmp_path, blockpage_url="http://blocked-page.test", blockpage_port=port)
    try:
        # blocked-page.test does not resolve: only the rewrite to loopback makes this work.
        r = _open(proxy_url, "http://blocked-page.test/blocked?t=abc", {"X-Client-IP": "6.6.6.6"})
        assert r.read() == b"ok"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        httpd.shutdown()
    assert seen == {"path": "/blocked?t=abc", "client_ip": "127.0.0.1"}
