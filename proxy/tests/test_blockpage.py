"""Block page service (real uvicorn server, like the web UI tests) with a
real fake SMTP server: template-only vs full page, token/IP checks, the
unblock request (one per block, rate limit, email failure), and the admin
Blocked page + test email."""
import base64
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email import message_from_bytes, policy
from pathlib import Path

import pytest

from cache_proxy import config, store
from cache_proxy.webui.auth import hash_password

PROXY_ROOT = Path(__file__).resolve().parents[1]
IP = "10.0.0.5"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeSMTP(socketserver.ThreadingTCPServer):
    """Just enough SMTP to receive messages; set .fail to refuse them."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.messages = []
        self.fail = False
        threading.Thread(target=self.serve_forever, daemon=True).start()


class _SMTPHandler(socketserver.StreamRequestHandler):
    def _say(self, line):
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self):
        self._say("220 fake ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode(errors="replace").strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                self._say("250 fake")
            elif cmd.startswith(("MAIL", "RCPT", "RSET", "NOOP")):
                self._say("250 OK")
            elif cmd.startswith("DATA"):
                if self.server.fail:
                    self._say("554 refused")
                    continue
                self._say("354 go")
                data = b""
                while True:
                    row = self.rfile.readline()
                    if row in (b".\r\n", b""):
                        break
                    data += row
                self.server.messages.append(message_from_bytes(data, policy=policy.default))
                self._say("250 queued")
            elif cmd.startswith("QUIT"):
                self._say("221 bye")
                return
            else:
                self._say("500 ?")


@pytest.fixture
def smtp():
    server = FakeSMTP()
    yield server
    server.shutdown()


def _serve(app_path, port, env, ready_path="/"):
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_path, "--port", str(port)],
        cwd=PROXY_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("server did not start:\n" + proc.stdout.read().decode(errors="replace"))


@pytest.fixture
def env(tmp_path, smtp):
    e = os.environ.copy()
    e.update({
        "CACHE_PROXY_DIR": str(tmp_path / "files"),
        "CACHE_PROXY_DB": str(tmp_path / "index.db"),
        "CACHE_PROXY_SMTP_HOST": "127.0.0.1",
        "CACHE_PROXY_SMTP_PORT": str(smtp.server_address[1]),
        "CACHE_PROXY_SMTP_SECURITY": "none",
        "CACHE_PROXY_EMAIL_FROM": "proxy@test.example",
        "CACHE_PROXY_UNBLOCK_RECIPIENT": "admin@test.example",
        "CACHE_PROXY_UNBLOCK_MAX_PER_HOUR": "2",
        "CACHE_PROXY_ALLOWED_HOSTS_FILE": str(tmp_path / "allowed-hosts.conf"),
        "CACHE_PROXY_WEBUI_USERNAME": "admin",
        "CACHE_PROXY_WEBUI_PASSWORD_HASH": hash_password("pw-12345"),
    })
    os.environ["CACHE_PROXY_DB"] = e["CACHE_PROXY_DB"]  # this process seeds the same DB
    return e


@pytest.fixture
def db(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()


@pytest.fixture
def page(env, db):
    port = _free_port()
    proc = _serve("cache_proxy.blockpage.app:app", port, env)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def admin(env, db):
    port = _free_port()
    proc = _serve("cache_proxy.webui.app:app", port, env)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _req(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        r = urllib.request.build_opener(_NoRedirect).open(req, timeout=15)
    except urllib.error.HTTPError as e:
        r = e
    return r.code, r.read().decode(), r.headers


def _block(ip=IP, host="porn.example"):
    token = store.new_block_token()
    store.record_blocks([{
        "token": token, "ts": 1_700_000_000.0, "client_ip": ip, "host": host,
        "url": f"https://{host}/x?y=1", "kind": "category", "category": "adult", "source": "ut1",
        "reason": "Category not allowed: adult", "user_agent": "TestBrowser/1.0 <b>",
    }])
    return token


def _view(page, token, ip=IP):
    # The proxy is the loopback peer and names the real client in X-Client-IP.
    return _req(f"{page}/blocked?t={urllib.parse.quote(token)}", headers={"X-Client-IP": ip})


def _submit(page, token, note="", ip=IP):
    return _req(f"{page}/unblock", data={"t": token, "note": note}, headers={"X-Client-IP": ip})


def _wait_for(pred, timeout=5):
    end = time.time() + timeout
    while not pred() and time.time() < end:
        time.sleep(0.05)
    return pred()


def test_direct_visit_is_template_only_with_no_form(page):
    for path in ("/", "/blocked", "/blocked?t=nope"):
        code, body, _ = _req(page + path)
        assert code == 200 and "<form" not in body and "Send unblock request" not in body
        assert "porn.example" not in body


def test_valid_token_shows_all_details_and_the_form(page):
    code, body, headers = _view(page, _block())
    assert code == 200
    for want in ("Category not allowed: adult", "adult", "porn.example", "https://porn.example/x?y=1",
                 IP, "2023-11-1", "TestBrowser/1.0 &lt;b&gt;", "<form", "Send unblock request"):
        assert want in body, want
    assert headers["Cache-Control"] == "no-store"


def test_someone_elses_token_shows_template_only(page):
    _, body, _ = _view(page, _block(ip=IP), ip="10.0.0.99")
    assert "<form" not in body and "porn.example" not in body


def test_forged_client_ip_from_a_non_loopback_peer_is_ignored():
    """Only a loopback peer (the proxy) may say who the client is."""
    from types import SimpleNamespace
    from cache_proxy.blockpage import app as bp
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.5"), headers={"x-client-ip": "9.9.9.9"})
    assert bp.client_ip(req) == "10.0.0.5"
    req.client.host = "127.0.0.1"
    assert bp.client_ip(req) == "9.9.9.9"


def test_unblock_request_emails_the_db_details_once(page, smtp):
    token = _block()
    code, body, _ = _submit(page, token, note="need it for work")
    assert code == 200 and "has been sent" in body
    assert _wait_for(lambda: smtp.messages)
    (msg,) = smtp.messages
    text = msg.get_content()
    assert msg["To"] == "admin@test.example" and msg["From"] == "proxy@test.example"
    assert "porn.example" in msg["Subject"]
    for want in ("Category not allowed: adult", "Category       : adult", "List source    : ut1", IP,
                 "TestBrowser/1.0", "https://porn.example/x?y=1", "2023-11-1", "need it for work",
                 "allowed-hosts.conf", "    porn.example"):
        assert want in text, want
    code, body, _ = _submit(page, token)  # resubmit / double-click
    assert "already been sent" in body and len(smtp.messages) == 1
    assert "<form" not in _view(page, token)[1]


def test_unblock_rejects_wrong_ip_and_unknown_token(page, smtp):
    token = _block()
    assert _submit(page, token, ip="10.0.0.99")[0] == 400
    assert _submit(page, "nope")[0] == 400
    assert smtp.messages == []


def test_email_failure_lets_the_user_retry_without_leaking_details(page, smtp):
    token = _block()
    smtp.fail = True
    code, body, _ = _submit(page, token)
    assert code == 502 and "could not be sent" in body and "refused" not in body
    assert store.get_block(token)["unblock_requested_at"] is None
    smtp.fail = False
    assert "has been sent" in _submit(page, token)[1]


def test_per_ip_rate_limit(page, smtp):
    for _ in range(2):
        assert _submit(page, _block())[0] == 200
    assert _submit(page, _block())[0] == 429
    assert _wait_for(lambda: len(smtp.messages) == 2) and len(smtp.messages) == 2


def test_no_form_when_email_not_configured(env, db):
    env["CACHE_PROXY_UNBLOCK_RECIPIENT"] = ""
    env["CACHE_PROXY_CONFIG"] = "/nonexistent"
    port = _free_port()
    proc = _serve("cache_proxy.blockpage.app:app", port, env)
    try:
        page = f"http://127.0.0.1:{port}"
        token = _block()
        code, body, _ = _view(page, token)
        assert "<form" not in body and "not available" in body
        assert _submit(page, token)[0] == 503
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_blocked_rows_survive_maintenance(db):
    token = _block()
    store.purge_expired()
    assert store.get_block(token) is not None


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:pw-12345").decode()}


def test_admin_blocked_page_shows_rows_and_exact_allow_line(admin, page):
    token = _block(host="shop.example")
    _submit(page, token, note="please")
    body = _req(admin + "/blocks", headers=_auth())[1]
    assert "shop.example" in body and "Category not allowed: adult" in body and IP in body
    assert "requested" in body and "please" in body
    assert "allowed-hosts.conf" in body and "<code>shop.example</code>" in body


def test_admin_test_email_button_sends_and_reports(admin, smtp):
    code, _, headers = _req(admin + "/blocks/test-email", data={}, headers=_auth())
    assert code == 303 and "Test+email+sent" in headers["Location"]
    assert _wait_for(lambda: smtp.messages)
    assert smtp.messages[0]["Subject"] == "cache-proxy test email"
    smtp.fail = True
    _, _, headers = _req(admin + "/blocks/test-email", data={}, headers=_auth())
    assert "Test+email+failed" in headers["Location"]


def test_admin_blocked_page_requires_login(admin):
    assert _req(admin + "/blocks")[0] == 401
