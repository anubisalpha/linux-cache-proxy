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
