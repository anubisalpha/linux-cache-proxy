from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, store
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


templates.env.filters["human_size"] = _human_size
templates.env.filters["datetime"] = _format_datetime


@app.on_event("startup")
def startup() -> None:
    store.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = Query(default="")):
    entries = store.list_entries(search=q or None)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"entries": entries, "stats": store.stats(), "q": q, "active": "files"},
    )


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
