import time

import pytest

from cache_proxy import config, store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()


def test_save_and_get_entry():
    store.save_file("https://example.com/a.exe", "a.exe", "application/x-msdownload", b"x" * 10)
    entry = store.get_entry(store.url_hash("https://example.com/a.exe"))
    assert entry["filename"] == "a.exe"
    assert entry["size"] == 10


def test_record_hit_increments():
    url = "https://example.com/a.exe"
    store.save_file(url, "a.exe", "application/x-msdownload", b"x" * 10)
    h = store.url_hash(url)
    store.record_hit(h)
    store.record_hit(h)
    entry = store.get_entry(h)
    assert entry["hit_count"] == 2


def test_list_entries_search():
    store.save_file("https://example.com/foo.deb", "foo.deb", "application/x-debian-package", b"x" * 10)
    store.save_file("https://example.com/bar.rpm", "bar.rpm", "application/x-rpm", b"y" * 10)
    results = store.list_entries(search="foo")
    assert len(results) == 1
    assert results[0]["filename"] == "foo.deb"


def test_delete_entry_removes_file_and_row():
    url = "https://example.com/a.exe"
    dest = store.save_file(url, "a.exe", "application/x-msdownload", b"x" * 10)
    h = store.url_hash(url)
    assert dest.exists()
    assert store.delete_entry(h) is True
    assert store.get_entry(h) is None
    assert not dest.exists()
    assert store.delete_entry(h) is False


def test_stats_totals():
    url = "https://example.com/a.exe"
    store.save_file(url, "a.exe", "application/x-msdownload", b"x" * 100)
    store.record_hit(store.url_hash(url))
    s = store.stats()
    assert s["count"] == 1
    assert s["total_size"] == 100
    assert s["total_hits"] == 1
    assert s["bytes_saved"] == 100


def test_log_access_and_aggregates():
    url = "https://example.com/a.exe"
    h = store.url_hash(url)
    store.log_access("10.0.0.5", h, "a.exe", hit=False, size=1000)
    store.log_access("10.0.0.5", h, "a.exe", hit=True, size=1000)
    store.log_access("10.0.0.9", h, "a.exe", hit=True, size=1000)

    summary = store.access_summary()
    assert summary["requests"] == 3
    assert summary["clients"] == 2
    assert summary["hits"] == 2
    assert summary["bytes_served"] == 3000
    assert summary["bytes_from_cache"] == 2000

    clients = {c["client_ip"]: c for c in store.top_clients()}
    assert clients["10.0.0.5"]["requests"] == 2
    assert clients["10.0.0.9"]["requests"] == 1

    files = store.top_files()
    assert files[0]["requests"] == 3

    recent = store.recent_access(client_ip="10.0.0.9")
    assert len(recent) == 1
    assert recent[0]["client_ip"] == "10.0.0.9"


def test_access_summary_since_ts_filters_old_rows():
    url = "https://example.com/a.exe"
    h = store.url_hash(url)
    store.log_access("10.0.0.5", h, "a.exe", hit=False, size=1000)
    future_cutoff = time.time() + 3600
    summary = store.access_summary(since_ts=future_cutoff)
    assert summary["requests"] == 0


# --- hourly_traffic: per-client, per-hour volumes for all traffic ----------
# access_log only ever sees cacheable downloads and hits, so a client that
# merely browses never appears in it. hourly_traffic is the meter that does
# see that traffic -- volumes only, no URLs.

def _hour(ts=None):
    return int(ts or time.time()) // 3600 * 3600


def test_hourly_traffic_accumulates_rather_than_replacing():
    """Several workers flush the same client-hour independently; the upsert
    has to add, not overwrite, or all but the last worker's traffic is lost."""
    h = _hour()
    store.add_hourly_traffic({("10.0.0.5", h): (3, 1500)})
    store.add_hourly_traffic({("10.0.0.5", h): (2, 500)})

    rows = store.hourly_traffic(client_ip="10.0.0.5")
    assert len(rows) == 1
    assert rows[0]["requests"] == 5
    assert rows[0]["bytes"] == 2000


def test_hourly_traffic_separates_clients_and_hours():
    h = _hour()
    prev = h - 3600
    store.add_hourly_traffic({
        ("10.0.0.5", h): (1, 100),
        ("10.0.0.9", h): (2, 200),
        ("10.0.0.5", prev): (4, 400),
    })

    rows = {(r["client_ip"], r["hour"]): r for r in store.hourly_traffic()}
    assert rows[("10.0.0.5", h)]["bytes"] == 100
    assert rows[("10.0.0.9", h)]["requests"] == 2
    assert rows[("10.0.0.5", prev)]["requests"] == 4


def test_hourly_traffic_since_ts_filters_older_hours():
    h = _hour()
    store.add_hourly_traffic({("10.0.0.5", h - 7200): (9, 900), ("10.0.0.5", h): (1, 100)})
    rows = store.hourly_traffic(since_ts=h)
    assert len(rows) == 1
    assert rows[0]["requests"] == 1


def test_add_hourly_traffic_ignores_empty():
    store.add_hourly_traffic({})
    assert store.hourly_traffic() == []


def test_prune_hourly_traffic_drops_old_rows_only():
    h = _hour()
    old = h - 100 * 86400
    store.add_hourly_traffic({("10.0.0.5", old): (1, 10), ("10.0.0.5", h): (1, 10)})

    removed = store.prune_hourly_traffic(retention_days=90)
    assert removed == 1
    rows = store.hourly_traffic()
    assert len(rows) == 1 and rows[0]["hour"] == h


def test_prune_hourly_traffic_zero_retention_keeps_everything():
    store.add_hourly_traffic({("10.0.0.5", _hour() - 100 * 86400): (1, 10)})
    assert store.prune_hourly_traffic(retention_days=0) == 0
    assert len(store.hourly_traffic()) == 1
