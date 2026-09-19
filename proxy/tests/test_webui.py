"""Integration tests for the web UI, run as a real HTTP server (not
ASGI TestClient) to avoid coupling to a specific starlette/httpx pairing.
"""
import base64
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cache_proxy import config, store
from cache_proxy.webui.auth import hash_password

PROXY_ROOT = Path(__file__).resolve().parents[1]
TEST_USERNAME = "testadmin"
TEST_PASSWORD = "test-password-123"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _auth_header() -> dict:
    token = base64.b64encode(f"{TEST_USERNAME}:{TEST_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or _auth_header())
    return urllib.request.urlopen(req)


@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()
    store.save_file("https://example.com/a.exe", "a.exe", "application/x-msdownload", b"x" * 2048)

    env = os.environ.copy()
    env["CACHE_PROXY_DIR"] = str(tmp_path / "files")
    env["CACHE_PROXY_DB"] = str(tmp_path / "index.db")
    env["CACHE_PROXY_WEBUI_USERNAME"] = TEST_USERNAME
    env["CACHE_PROXY_WEBUI_PASSWORD_HASH"] = hash_password(TEST_PASSWORD)
    return env


@pytest.fixture
def webui_server(seeded_env):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cache_proxy.webui.app:app", "--port", str(port)],
        cwd=PROXY_ROOT,
        env=seeded_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            _get(base + "/")
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("webui did not start:\n" + proc.stdout.read().decode(errors="replace"))
    yield base
    proc.terminate()
    proc.wait(timeout=5)


def test_index_lists_file(webui_server):
    body = _get(webui_server + "/").read().decode()
    assert "a.exe" in body
    assert "2.0 KB" in body


def test_search_filters(webui_server):
    body = _get(webui_server + "/?q=nomatch").read().decode()
    assert "No cached files yet" in body


def test_download(webui_server):
    h = store.url_hash("https://example.com/a.exe")
    resp = _get(webui_server + f"/download/{h}")
    assert resp.status == 200
    assert len(resp.read()) == 2048


def test_delete(webui_server):
    h = store.url_hash("https://example.com/a.exe")
    req = urllib.request.Request(webui_server + f"/delete/{h}", method="POST", headers=_auth_header())
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    opener.open(req)
    body = _get(webui_server + "/").read().decode()
    assert "No cached files yet" in body


def test_usage_page_shows_activity(webui_server):
    h = store.url_hash("https://example.com/a.exe")
    store.log_access("10.0.0.5", h, "a.exe", hit=False, size=2048)
    store.log_access("10.0.0.5", h, "a.exe", hit=True, size=2048)

    body = _get(webui_server + "/usage").read().decode()
    assert "10.0.0.5" in body
    assert "a.exe" in body


def test_no_credentials_rejected(webui_server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(webui_server + "/")
    assert exc_info.value.code == 401


def test_wrong_password_rejected(webui_server):
    bad_headers = {
        "Authorization": "Basic "
        + base64.b64encode(f"{TEST_USERNAME}:wrong-password".encode()).decode()
    }
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get(webui_server + "/", headers=bad_headers)
    assert exc_info.value.code == 401


def test_unconfigured_password_locks_ui_out(seeded_env):
    """No username/password_hash set at all -- must fail closed, not
    serve the UI unauthenticated."""
    env = dict(seeded_env)
    del env["CACHE_PROXY_WEBUI_USERNAME"]
    del env["CACHE_PROXY_WEBUI_PASSWORD_HASH"]
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cache_proxy.webui.app:app", "--port", str(port)],
        cwd=PROXY_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(urllib.request.Request(base + "/", headers=_auth_header()))
                break
            except urllib.error.HTTPError as e:
                assert e.code == 401
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        else:
            raise RuntimeError("webui did not start:\n" + proc.stdout.read().decode(errors="replace"))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
