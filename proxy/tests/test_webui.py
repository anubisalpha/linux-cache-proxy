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


# ---- worker panel / stats / assets ---------------------------------------

def _write_status(env, **overrides):
    status = {
        "ts": time.time(), "min_workers": 2, "max_workers": 6, "avg_cpu": 41.0,
        "workers": [
            {"pid": 111, "state": "active", "cpu": 55.0, "connections": 3, "uptime": 600},
            {"pid": 222, "state": "active", "cpu": 27.0, "connections": 1, "uptime": 60},
        ],
    }
    status.update(overrides)
    Path(env["CACHE_PROXY_STATUS_FILE"]).write_text(__import__("json").dumps(status))


@pytest.fixture
def status_env(seeded_env, tmp_path):
    seeded_env["CACHE_PROXY_STATUS_FILE"] = str(tmp_path / "workers.json")
    return seeded_env


@pytest.fixture
def status_server(status_env):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cache_proxy.webui.app:app", "--port", str(port)],
        cwd=PROXY_ROOT, env=status_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            _get(base + "/")
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    yield base, status_env
    proc.terminate()
    proc.wait(timeout=5)


def test_index_shows_worker_pool(status_server):
    base, env = status_server
    _write_status(env)
    body = _get(base + "/").read().decode()
    assert "2 active of max 6" in body
    assert "min 2" in body and "41%" in body
    assert "111" in body and "222" in body


def test_index_without_status_says_unavailable(status_server):
    base, _env = status_server
    body = _get(base + "/").read().decode()
    assert "Proxy not running / status unavailable" in body


def test_stale_status_is_not_shown_as_live(status_server):
    base, env = status_server
    _write_status(env, ts=time.time() - 3600)
    assert "Proxy not running / status unavailable" in _get(base + "/").read().decode()
    data = __import__("json").loads(_get(base + "/api/workers").read())
    assert data["available"] is False


def test_api_workers_returns_status(status_server):
    base, env = status_server
    _write_status(env)
    data = __import__("json").loads(_get(base + "/api/workers").read())
    assert data["available"] is True
    assert [w["pid"] for w in data["workers"]] == [111, 222]


def test_api_workers_requires_auth(webui_server):
    for path in ("/api/workers", "/api/hourly-stats", "/stats"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(webui_server + path)
        assert exc.value.code == 401


def test_kind_filter_and_expiry_shown(webui_server):
    store.save_file("https://cdn.example.com/app.js", "app.js", "text/javascript", b"js", kind="asset", ttl=1800)
    body = _get(webui_server + "/").read().decode()
    assert "app.js" in body and "asset" in body
    only_assets = _get(webui_server + "/?kind=asset").read().decode()
    assert "app.js" in only_assets and "a.exe" not in only_assets
    only_dl = _get(webui_server + "/?kind=download").read().decode()
    assert "a.exe" in only_dl and "app.js" not in only_dl


def test_purge_expired_action(webui_server):
    store.save_file("https://cdn.example.com/old.js", "old.js", "t", b"x", kind="asset", ttl=-1)
    req = urllib.request.Request(webui_server + "/purge-expired", method="POST", headers=_auth_header())
    urllib.request.build_opener(urllib.request.HTTPRedirectHandler()).open(req)
    body = _get(webui_server + "/").read().decode()
    assert "old.js" not in body and "a.exe" in body


def test_hourly_stats_json_and_flagged_page(webui_server):
    now = time.time()
    hour = int(now // 3600) * 3600
    with store._connect() as conn:
        for i in range(1, 9):  # eight quiet prior hours
            conn.execute("INSERT INTO access_log (ts, client_ip, url_hash, filename, hit, bytes) VALUES (?,?,?,?,?,?)",
                         (hour - i * 3600 + 5, "10.9.9.9", "h", "f", 1, 1000))
        conn.execute("INSERT INTO access_log (ts, client_ip, url_hash, filename, hit, bytes) VALUES (?,?,?,?,?,?)",
                     (hour + 1, "10.9.9.9", "h", "f", 1, 900_000))
    data = __import__("json").loads(_get(webui_server + "/api/hourly-stats").read())
    assert "10.9.9.9" in data["series"]
    assert any(a["client_ip"] == "10.9.9.9" and a["metric"] == "bytes" for a in data["anomalies"])
    page = _get(webui_server + "/stats").read().decode()
    assert "Flagged this hour" in page and "10.9.9.9" in page
    quiet = __import__("json").loads(_get(webui_server + "/api/hourly-stats?client=nobody").read())
    assert quiet["series"] == {} and quiet["anomalies"] == []


def test_summary_counts_download_hits_only_but_lists_asset_hits(webui_server):
    store.save_file("https://cdn.example.com/app.js", "app.js", "text/javascript", b"js", kind="asset", ttl=1800)
    store.record_hits({store.url_hash("https://cdn.example.com/app.js"): 42})
    body = _get(webui_server + "/").read().decode()
    assert "0 download hits" in body  # a.exe has none; the 42 asset hits aren't folded in
    row = body.split("app.js</a>", 1)[1].split("</tr>", 1)[0]
    assert "<td>42</td>" in row  # ...but the file's own Hits column shows them
