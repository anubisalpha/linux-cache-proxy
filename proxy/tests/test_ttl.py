"""Cache lifetimes, asset classification, quota eviction, log retention, Range parsing."""
import sqlite3
import time
from unittest.mock import MagicMock

import pytest
from mitmproxy import http

from cache_proxy import addon, config, store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()


def _flow(url="https://cdn.example.com/app.js", req_headers=None):
    flow = MagicMock()
    flow.request.method = "GET"
    flow.request.pretty_url = url
    flow.request.host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    flow.request.headers = http.Headers(**(req_headers or {}))
    return flow


def _resp(ctype="application/javascript", **headers):
    return http.Response.make(200, b"x", {"Content-Type": ctype, **headers})


def _classify(url="https://cdn.example.com/app.js", req_headers=None, **resp):
    ctype = resp.pop("ctype", "application/javascript")
    return addon._classify(_flow(url, req_headers), _resp(ctype, **{k.replace("_", "-"): v for k, v in resp.items()}))


# ---- classification ------------------------------------------------------

def test_download_gets_download_ttl():
    kind, ttl = addon._classify(_flow("https://x.example.com/setup.exe"), _resp("application/x-msdownload"))
    assert kind == "download" and ttl == config.DOWNLOAD_TTL == 30 * 86400


def test_static_asset_gets_capped_ttl():
    assert _classify() == ("asset", config.WEBCACHE_TTL)
    assert config.WEBCACHE_TTL == 3600


def test_html_is_never_cached():
    assert _classify("https://example.com/index.html", ctype="text/html") is None
    assert _classify("https://example.com/", ctype="text/html; charset=utf-8") is None


def test_asset_matched_by_content_type_without_extension():
    assert _classify("https://example.com/bundle?v=3", ctype="text/css")[0] == "asset"


@pytest.mark.parametrize("headers", [
    {"cache_control": "no-store"},
    {"cache_control": "private, max-age=600"},
    {"cache_control": "no-cache"},
    {"cache_control": "max-age=0"},
    {"set_cookie": "sid=abc"},
    {"pragma": "no-cache"},
    {"vary": "Cookie"},
    {"vary": "Accept-Encoding, User-Agent"},
])
def test_origin_headers_that_forbid_caching_are_respected(headers):
    assert _classify(**headers) is None


def test_vary_accept_encoding_alone_is_fine():
    assert _classify(vary="Accept-Encoding")[0] == "asset"


def test_shorter_origin_max_age_wins_but_longer_is_capped():
    assert _classify(cache_control="public, max-age=120") == ("asset", 120.0)
    assert _classify(cache_control="public, max-age=31536000") == ("asset", 3600.0)
    assert _classify(cache_control="max-age=9999, s-maxage=60") == ("asset", 60.0)


def test_authorization_or_range_requests_not_cached():
    assert _classify(req_headers={"Authorization": "Bearer t"}) is None
    assert _classify(req_headers={"Range": "bytes=0-10"}) is None


def test_never_cache_hosts_apply_to_assets(monkeypatch):
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["example.com"])
    assert _classify() is None


def test_webcache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "WEBCACHE_ENABLED", False)
    assert _classify() is None


# ---- expiry / purge ------------------------------------------------------

def test_entry_lifetime_by_kind():
    now = time.time()
    store.save_file("https://a/setup.exe", "setup.exe", "x", b"1")
    store.save_file("https://a/app.js", "app.js", "text/javascript", b"2", kind="asset")
    dl = store.get_entry(store.url_hash("https://a/setup.exe"))
    asset = store.get_entry(store.url_hash("https://a/app.js"))
    assert dl["kind"] == "download" and dl["expires_at"] == pytest.approx(now + 30 * 86400, abs=5)
    assert asset["kind"] == "asset" and asset["expires_at"] == pytest.approx(now + 3600, abs=5)


def test_expired_entries_leave_the_known_set_and_are_purged():
    store.save_file("https://a/old.js", "old.js", "x", b"1", kind="asset", ttl=-1)
    store.save_file("https://a/new.js", "new.js", "x", b"1", kind="asset", ttl=1000)
    assert store.known_hashes() == {store.url_hash("https://a/new.js")}
    assert store.is_expired(store.get_entry(store.url_hash("https://a/old.js")))
    assert store.purge_expired() == 1
    assert store.get_entry(store.url_hash("https://a/old.js")) is None
    assert not (config.CACHE_DIR / f"{store.url_hash('https://a/old.js')}.js").exists()
    assert store.get_entry(store.url_hash("https://a/new.js")) is not None


def test_refetch_overwrites_and_resets_expiry():
    store.save_file("https://a/x.js", "x.js", "t", b"old", kind="asset", ttl=-1)
    store.save_file("https://a/x.js", "x.js", "t", b"newer", kind="asset", ttl=1000)
    e = store.get_entry(store.url_hash("https://a/x.js"))
    assert not store.is_expired(e) and e["size"] == 5


def test_expired_hit_is_treated_as_a_miss():
    store.save_file("https://a/x.js", "x.js", "t", b"old", kind="asset", ttl=-1)
    a = addon.CacheAddon()
    a._known_hashes = {store.url_hash("https://a/x.js")}  # stale view, as between refreshes
    flow = _flow("https://a/x.js")
    flow.response = None
    a.request(flow)
    assert flow.response is None
    assert store.url_hash("https://a/x.js") not in a._known_hashes


def test_asset_hits_are_counted_not_logged():
    store.save_file("https://a/x.js", "x.js", "text/javascript", b"body", kind="asset", ttl=1000)
    a = addon.CacheAddon()
    a.load(None)
    flow = _flow("https://a/x.js")
    flow.client_conn.peername = ("10.0.0.5", 1)
    flow.response = None
    a.request(flow)
    assert flow.response.headers["X-Cache-Proxy"] == "HIT"
    assert store.recent_access() == []  # assets must not flood access_log
    assert a._counters == {"asset_hits": 1, "asset_bytes_saved": 4}


def test_counters_accumulate():
    store.add_counters({"asset_hits": 2, "asset_bytes_saved": 10})
    store.add_counters({"asset_hits": 3})
    assert store.get_counters() == {"asset_hits": 5, "asset_bytes_saved": 10}


# ---- migration from a pre-TTL database ----------------------------------

def test_migrates_old_schema(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE files (url_hash TEXT PRIMARY KEY, url TEXT NOT NULL, filename TEXT NOT NULL, "
        "content_type TEXT, size INTEGER NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL, "
        "last_hit_at REAL, hit_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO files VALUES ('h','u','f.exe','t',5,'h.exe',1000.0,NULL,3)")
    conn.commit()
    conn.close()

    store.init_db()
    row = store.get_entry("h")
    assert row["kind"] == "download"
    assert row["expires_at"] == pytest.approx(1000.0 + 30 * 86400)
    assert row["hit_count"] == 3
    store.init_db()  # idempotent


# ---- quota eviction / retention / atomic write --------------------------

def test_evicts_least_recently_hit_first_until_under_quota():
    for name in ("a", "b", "c"):
        store.save_file(f"https://x/{name}.zip", f"{name}.zip", "t", b"x" * 100)
    now = time.time()
    with store._connect() as conn:
        conn.execute("UPDATE files SET last_hit_at=? WHERE filename='a.zip'", (now,))
        conn.execute("UPDATE files SET last_hit_at=? WHERE filename='b.zip'", (now - 500,))
        conn.execute("UPDATE files SET last_hit_at=? WHERE filename='c.zip'", (now - 1000,))
    assert store.evict_to_quota(max_bytes=250) == 1
    left = {r["filename"] for r in store.list_entries()}
    assert left == {"a.zip", "b.zip"}  # c was the least recently used
    assert store.total_size() == 200
    assert store.evict_to_quota(max_bytes=250) == 0
    assert store.evict_to_quota(max_bytes=0) == 0  # 0 = unlimited


def test_prune_access_log_keeps_recent_rows():
    now = time.time()
    with store._connect() as conn:
        for ts in (now - 200 * 86400, now - 91 * 86400, now - 1 * 86400):
            conn.execute("INSERT INTO access_log (ts, client_ip, url_hash, filename, hit, bytes) VALUES (?,?,?,?,?,?)",
                         (ts, "c", "h", "f", 1, 1))
    assert store.prune_access_log(retention_days=90) == 2
    assert len(store.recent_access()) == 1
    assert store.prune_access_log(retention_days=0) == 0  # 0 = keep forever


def test_save_file_leaves_no_temp_files():
    store.save_file("https://x/a.zip", "a.zip", "t", b"data")
    assert [p.name for p in config.CACHE_DIR.iterdir() if p.name.endswith(".tmp")] == []


# ---- Range parsing -------------------------------------------------------

@pytest.mark.parametrize("header,size,expected", [
    (None, 100, None),
    ("bytes=0-9", 100, (0, 9)),
    ("bytes=10-", 100, (10, 99)),
    ("bytes=-20", 100, (80, 99)),
    ("bytes=90-500", 100, (90, 99)),      # end clamped to file size
    ("bytes=100-", 100, "invalid"),       # starts past the end
    ("bytes=50-10", 100, "invalid"),
    ("bytes=-0", 100, "invalid"),
    ("bytes=0-1,5-6", 100, None),         # multi-range: whole file
    ("items=0-1", 100, None),
    ("bytes=abc-def", 100, None),
])
def test_parse_range(header, size, expected):
    assert addon.parse_range(header, size) == expected


# ---- per-file hit counts for assets ----------------------------------------

def _asset_addon(url="https://a/x.js"):
    store.save_file(url, "x.js", "text/javascript", b"body", kind="asset", ttl=1000)
    a = addon.CacheAddon()
    a.load(None)
    return a, url


def _hit(a, url):
    flow = _flow(url)
    flow.client_conn.peername = ("10.0.0.5", 1)
    flow.response = None
    a.request(flow)
    assert flow.response is not None


def test_record_hits_updates_count_and_last_hit_time():
    store.save_file("https://a/x.js", "x.js", "t", b"b", kind="asset", ttl=1000)
    h = store.url_hash("https://a/x.js")
    before = time.time()
    store.record_hits({h: 3, "0" * 64: 9})  # unknown hash is ignored
    e = store.get_entry(h)
    assert e["hit_count"] == 3 and e["last_hit_at"] >= before
    store.record_hits({h: 2})
    assert store.get_entry(h)["hit_count"] == 5
    store.record_hits({})  # no-op


def test_asset_hits_are_tallied_in_memory_then_flushed_in_one_batch():
    a, url = _asset_addon()
    h = store.url_hash(url)
    for _ in range(4):
        _hit(a, url)
    assert a._asset_hits == {h: 4}
    assert store.get_entry(h)["hit_count"] == 0  # not written per request
    a._flush_stats()
    assert store.get_entry(h)["hit_count"] == 4
    assert a._asset_hits == {} and store.get_counters()["asset_hits"] == 4
    a._flush_stats()  # nothing new: no double counting
    assert store.get_entry(h)["hit_count"] == 4


def test_failed_flush_keeps_the_tallies_for_the_next_one(monkeypatch):
    a, url = _asset_addon()
    h = store.url_hash(url)
    _hit(a, url)
    with monkeypatch.context() as m:  # scoped, so the fixture's temp-dir patches stay
        m.setattr(store, "record_hits", MagicMock(side_effect=RuntimeError("db busy")))
        with pytest.raises(RuntimeError):
            a._flush_stats()
    assert a._asset_hits == {h: 1}  # put back
    _hit(a, url)
    a._flush_stats()
    assert store.get_entry(h)["hit_count"] == 2


def test_summary_hits_and_bytes_saved_are_downloads_only():
    store.save_file("https://a/setup.exe", "setup.exe", "t", b"x" * 100)
    store.record_hits({store.url_hash("https://a/setup.exe"): 2})
    store.save_file("https://a/x.js", "x.js", "t", b"y" * 10, kind="asset", ttl=1000)
    store.record_hits({store.url_hash("https://a/x.js"): 50})
    s = store.stats()
    assert s["count"] == 2  # both are cached files
    assert s["total_hits"] == 2 and s["bytes_saved"] == 200  # assets aren't double-counted


def test_recently_hit_asset_survives_quota_eviction():
    store.save_file("https://a/old.js", "old.js", "t", b"x" * 100, kind="asset", ttl=1000)
    store.save_file("https://a/busy.js", "busy.js", "t", b"x" * 100, kind="asset", ttl=1000)
    with store._connect() as conn:  # both cached long ago
        conn.execute("UPDATE files SET created_at = created_at - 5000")
    store.record_hits({store.url_hash("https://a/busy.js"): 7})  # ...but one is in use
    assert store.evict_to_quota(max_bytes=150) == 1
    assert {r["filename"] for r in store.list_entries()} == {"busy.js"}
