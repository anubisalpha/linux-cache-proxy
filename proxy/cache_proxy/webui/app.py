from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import logging
import os
import threading
import time
from urllib.parse import urlencode, urlsplit

from .. import analytics, categories, config, filterlists, mailer, store, workers
from .auth import require_auth

app = FastAPI(title="Cache Proxy", dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _format_datetime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _format_expiry(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    remaining = ts - time.time()
    if remaining <= 0:
        return "expired"
    if remaining < 3600:
        return f"{int(remaining // 60)}m"
    if remaining < 86400:
        return f"{remaining / 3600:.1f}h"
    return f"{remaining / 86400:.1f}d"


templates.env.filters["human_size"] = _human_size
templates.env.filters["datetime"] = _format_datetime
templates.env.filters["expiry"] = _format_expiry


@app.on_event("startup")
def startup() -> None:
    store.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = Query(default=""), kind: str = Query(default="")):
    kind = kind if kind in ("download", "asset") else ""
    counters = store.get_counters()
    disk = store.disk_usage()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "entries": store.list_entries(search=q or None, kind=kind or None),
            "stats": store.stats(),
            "q": q,
            "kind": kind,
            "workers": workers.read_worker_status(),
            "counters": counters,
            "disk": disk,
            "disk_low": disk["total"] and disk["free"] / disk["total"] < 0.1,
            "max_cache_bytes": config.MAX_CACHE_BYTES,
            "active": "files",
        },
    )


@app.get("/api/workers")
def api_workers():
    return JSONResponse(workers.read_worker_status())


@app.get("/usage", response_class=HTMLResponse)
def usage(request: Request, days: int = Query(default=7), client: Optional[str] = Query(default=None)):
    since_ts = (datetime.now() - timedelta(days=days)).timestamp() if days else None
    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "summary": store.access_summary(since_ts=since_ts),
            "top_clients": store.top_clients(since_ts=since_ts),
            "top_files": store.top_files(since_ts=since_ts),
            "recent": store.recent_access(limit=200, client_ip=client),
            "days": days,
            "client": client or "",
            "active": "usage",
        },
    )


def _hourly(hours: int, client: Optional[str]):
    since_ts = time.time() - hours * 3600
    return analytics.client_series(store.hourly_client_stats(since_ts=since_ts, client_ip=client))


@app.get("/api/hourly-stats")
def api_hourly_stats(hours: int = Query(default=0), client: Optional[str] = Query(default=None)):
    """Raw per-client hourly series, for Grafana's JSON API datasource or
    anything else that wants the data."""
    hours = hours or config.LOOKBACK_HOURS
    series = _hourly(hours, client)
    current_hour = int(time.time() // 3600) * 3600
    return {
        "hours": hours,
        "series": series,
        "anomalies": analytics.detect_anomalies(series, latest_hour=current_hour),
    }


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, hours: int = Query(default=0)):
    hours = hours or config.LOOKBACK_HOURS
    series = _hourly(hours, None)
    current_hour = int(time.time() // 3600) * 3600
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "anomalies": analytics.detect_anomalies(series, latest_hour=current_hour),
            "summaries": analytics.client_summaries(series),
            "hours": hours,
            "factor": config.ANOMALY_FACTOR,
            "min_history": config.MIN_HISTORY_HOURS,
            "current_hour": current_hour,
            "active": "stats",
        },
    )


@app.post("/purge-expired")
def purge_expired():
    store.purge_expired()
    return RedirectResponse("/", status_code=303)


@app.post("/delete/{url_hash}")
def delete(url_hash: str):
    store.delete_entry(url_hash)
    return RedirectResponse("/", status_code=303)


@app.get("/download/{url_hash}")
def download(url_hash: str):
    entry = store.get_entry(url_hash)
    if not entry:
        return RedirectResponse("/", status_code=303)
    path = config.CACHE_DIR / entry["path"]
    return FileResponse(
        path,
        filename=entry["filename"],
        media_type=entry["content_type"] or "application/octet-stream",
    )


@app.get("/blocks", response_class=HTMLResponse)
def blocks(request: Request, requested: int = Query(default=0), msg: str = Query(default=""), ok: int = Query(default=0)):
    rows = [
        {**dict(r), "allow_line": r["host"]}
        for r in store.list_blocked(limit=500, requested_only=bool(requested))
    ]
    now = time.time()
    return templates.TemplateResponse(
        request,
        "blocks.html",
        {
            "rows": rows,
            "requested": bool(requested),
            "msg": msg,
            "ok": bool(ok),
            "filtering_enabled": config.FILTERING_ENABLED,
            "categories": config.FILTER_BLOCK_CATEGORIES,
            "list_status": sorted(filterlists.read_status().items()),
            "allowed_file": config.ALLOWED_HOSTS_FILE,
            "email_missing": mailer.missing_settings(),
            "recipient": config.UNBLOCK_RECIPIENT,
            "now": now,
            "active": "blocks",
        },
    )


@app.post("/blocks/test-email")
def blocks_test_email():
    missing = mailer.missing_settings()
    if missing:
        return RedirectResponse("/blocks?" + urlencode({"msg": "Email not configured: set " + ", ".join(missing) + " in [email]"}), status_code=303)
    try:
        mailer.send_test()
    except Exception as e:
        return RedirectResponse("/blocks?" + urlencode({"msg": f"Test email failed: {type(e).__name__}: {e}"}), status_code=303)
    return RedirectResponse("/blocks?" + urlencode({"msg": f"Test email sent to {config.UNBLOCK_RECIPIENT}", "ok": 1}), status_code=303)


# ---- Categories --------------------------------------------------------------

_download_lock = threading.Lock()


def _categories_writable() -> bool:
    path = config.CATEGORIES_FILE
    return os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK)


_in_flight: set = set()  # categories queued or downloading, guarded by _download_lock_state


_download_lock_state = threading.Lock()


def _download_in_background(names: list) -> list:
    """Fetch the lists for newly enabled categories without holding up the
    request. One download at a time; the proxy loads each list as it lands.
    Categories already queued or downloading are skipped, so saving twice in
    a row doesn't fetch the same 18 MB again. Returns the ones actually queued."""
    with _download_lock_state:
        names = [n for n in names if n not in _in_flight]
        _in_flight.update(names)
    if not names:
        return []

    def run():
        with _download_lock:
            try:
                filterlists.update_all(only_categories=names)
            except Exception:
                logging.getLogger("cache_proxy.webui").exception("category list download failed")
            finally:
                with _download_lock_state:
                    _in_flight.difference_update(names)
    threading.Thread(target=run, daemon=True).start()
    return names


def _list_status_by_category() -> dict:
    """category -> (domains across its sources, latest successful update)"""
    out: dict = {}
    for key, v in filterlists.read_status().items():
        cat = key.rsplit("/", 1)[-1]
        if v.get("ts"):
            domains, ts = out.get(cat, (0, 0))
            out[cat] = (domains + (v.get("count") or 0), max(ts, v["ts"]))
    return out


@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, msg: str = Query(default=""), ok: int = Query(default=0)):
    selected = config.block_categories()
    downloaded = _list_status_by_category()
    available = categories.available()
    for c in available:
        c["domains"], c["updated"] = downloaded.get(c["name"], (0, None))
    return templates.TemplateResponse(
        request,
        "categories.html",
        {
            "groups": categories.group(available),
            "selected": set(selected),
            "total": len(available),
            "categories_file": config.CATEGORIES_FILE,
            "writable": _categories_writable(),
            "filtering_enabled": config.FILTERING_ENABLED,
            "msg": msg,
            "ok": bool(ok),
            "active": "categories",
        },
    )


def _redirect_categories(msg: str, ok: bool = False) -> RedirectResponse:
    return RedirectResponse("/categories?" + urlencode({"msg": msg, "ok": int(ok)}), status_code=303)


@app.post("/categories")
async def categories_save(request: Request):
    # The admin login is HTTP Basic, which browsers resend automatically, so
    # refuse a form posted from another site.
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    form = await request.form()
    valid = [c["name"] for c in categories.available()]
    chosen = set(form.getlist("cat"))
    selected = [name for name in valid if name in chosen]  # catalogue order; unknown names ignored
    if not _categories_writable():
        return _redirect_categories(f"{config.CATEGORIES_FILE} is not writable by this service; nothing was saved.")
    try:
        categories.save_selected(selected)
    except OSError as e:
        return _redirect_categories(f"Could not save: {e}")
    downloaded = _list_status_by_category()
    missing = [c for c in selected if c not in downloaded]
    queued = _download_in_background(missing) if missing else []
    msg = f"Saved: {len(selected)} categories enforced."
    if queued:
        msg += f" Downloading lists for: {', '.join(queued)} (a few seconds to a minute; refresh to see progress)."
    if len(queued) < len(missing):
        msg += " Other lists are already downloading."
    return _redirect_categories(msg, ok=True)
