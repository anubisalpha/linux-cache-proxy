"""The page blocked users land on (served on port 80, no login).

Two modes, decided per request:
  * a valid token (?t=...) whose recorded client IP matches the viewer:
    the full page -- what was blocked, why, who/when/what browser -- plus
    the unblock-request form.
  * anything else (opened directly, unknown/mistyped token, someone else's
    token): a template only, with no details and no form.

The form posts only the token and a free-text note. The email is built from
the database row, so nothing else can be forged from the browser.
"""
import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from cache_proxy import config, mailer, store

logger = logging.getLogger("cache_proxy.blockpage")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_LOOPBACK = {"127.0.0.1", "::1"}
NOTE_MAX = 1000


@app.on_event("startup")
def startup() -> None:
    store.init_db()


def client_ip(request: Request) -> str:
    """The proxy forwards block-page requests via loopback and tags them with
    the real client IP (X-Client-IP). That header is only believed when the
    connection itself comes from loopback; from anywhere else it is ignored,
    so a client can't claim to be someone else."""
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-client-ip", "").strip()
    return forwarded if peer in _LOOPBACK and forwarded else peer


def _row_for(token: str, ip: str):
    if not token or len(token) > 64:
        return None
    row = store.get_block(token)
    return row if row and row["client_ip"] == ip else None


def _describe(row) -> dict:
    return {
        "reason": row["reason"],
        "category": row["category"],
        "host": row["host"],
        "url": row["url"],
        "client_ip": row["client_ip"],
        "when": datetime.fromtimestamp(row["ts"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "user_agent": row["user_agent"],
        "already_requested": row["unblock_requested_at"] is not None,
    }


def _render(request: Request, row=None, status: int = 200, **extra):
    ctx = {
        "message": config.BLOCKPAGE_MESSAGE,
        "block": _describe(row) if row else None,
        "token": row["token"] if row else "",
        "can_request": bool(row) and mailer.configured(),
        "error": None,
        "sent": False,
        **extra,
    }
    resp = templates.TemplateResponse(request, "blocked.html", ctx, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return _render(request)


@app.get("/blocked", response_class=HTMLResponse)
def blocked(request: Request, t: str = Query(default="")):
    return _render(request, _row_for(t, client_ip(request)))


@app.post("/unblock", response_class=HTMLResponse)
def unblock(request: Request, t: str = Form(default=""), note: str = Form(default="")):
    ip = client_ip(request)
    row = _row_for(t, ip)
    if not row:
        return _render(request, status=400)
    if row["unblock_requested_at"] is not None:
        return _render(request, row)  # shows "already requested"
    if not mailer.configured():
        return _render(request, row, status=503, error="Unblock requests are not available right now.")
    if store.recent_unblock_requests(ip, time.time() - 3600) >= config.UNBLOCK_MAX_PER_IP_HOUR:
        return _render(request, row, status=429, error="Too many requests from your address. Please try again later.")
    note = note.strip()[:NOTE_MAX]
    # Claim it first so a double-click or a race can send only one email; give
    # the claim back if the email fails so the user can retry.
    if not store.mark_unblock_requested(t, note):
        return _render(request, store.get_block(t))
    try:
        mailer.send_unblock_request(row, note)
    except Exception as e:
        logger.warning("unblock email failed for block %s: %s: %s", row["id"], type(e).__name__, e)
        store.clear_unblock_requested(t)
        return _render(request, row, status=502, error="Your request could not be sent. Please try again later.")
    return _render(request, store.get_block(t), sent=True)
