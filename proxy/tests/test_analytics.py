import time

import pytest

from cache_proxy import analytics, config, store

HOUR = 3600


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()


def _row(client, hour, requests=1, bytes_=100, new=0):
    return {"client_ip": client, "hour": hour, "requests": requests, "hits": requests - new,
            "bytes": bytes_, "new_downloads": new}


def _steady(client, start, hours, requests=2, bytes_=1000, new=0):
    return [_row(client, start + i * HOUR, requests, bytes_, new) for i in range(hours)]


def test_spike_is_flagged():
    rows = _steady("10.0.0.1", 0, 10) + [_row("10.0.0.1", 10 * HOUR, requests=2, bytes_=50_000)]
    found = analytics.detect_anomalies(analytics.client_series(rows), factor=3, min_history_hours=6)
    assert [(a["client_ip"], a["metric"]) for a in found] == [("10.0.0.1", "bytes")]
    assert found[0]["ratio"] > 3


def test_steady_client_is_not_flagged():
    rows = _steady("10.0.0.1", 0, 11)
    assert analytics.detect_anomalies(analytics.client_series(rows), factor=3, min_history_hours=6) == []


def test_new_client_without_history_is_not_flagged():
    rows = _steady("10.0.0.9", 0, 2, bytes_=10) + [_row("10.0.0.9", 2 * HOUR, bytes_=10_000_000)]
    assert analytics.detect_anomalies(analytics.client_series(rows), factor=3, min_history_hours=6) == []


def test_baselines_are_per_client():
    heavy = _steady("10.0.0.1", 0, 10, bytes_=1_000_000) + [_row("10.0.0.1", 10 * HOUR, bytes_=1_200_000)]
    light = _steady("10.0.0.2", 0, 10, bytes_=1_000) + [_row("10.0.0.2", 10 * HOUR, bytes_=50_000)]
    found = analytics.detect_anomalies(analytics.client_series(heavy + light), factor=3, min_history_hours=6)
    assert {a["client_ip"] for a in found} == {"10.0.0.2"}


def test_new_downloads_metric_is_tracked():
    rows = _steady("10.0.0.1", 0, 10, requests=2, new=1) + [_row("10.0.0.1", 10 * HOUR, requests=2, new=2)]
    rows[-1]["hits"] = 0
    found = analytics.detect_anomalies(analytics.client_series(rows), factor=1.5, min_history_hours=6)
    assert "new_downloads" in {a["metric"] for a in found}


def test_only_the_requested_hour_is_judged():
    rows = _steady("10.0.0.1", 0, 10) + [_row("10.0.0.1", 10 * HOUR, bytes_=999_999)]
    series = analytics.client_series(rows)
    assert analytics.detect_anomalies(series, factor=3, min_history_hours=6, latest_hour=99 * HOUR) == []
    assert analytics.detect_anomalies(series, factor=3, min_history_hours=6, latest_hour=10 * HOUR)


def test_quiet_gaps_lower_the_baseline():
    # Active in 2 of 12 elapsed hours: baseline is spread across the whole
    # span, so a modest burst after a long quiet stretch is unusual.
    rows = [_row("c", 0, bytes_=1000), _row("c", HOUR, bytes_=1000)]
    rows += [_row("c", 12 * HOUR, bytes_=3000)]
    found = analytics.detect_anomalies(analytics.client_series(rows), factor=3, min_history_hours=1)
    assert found and found[0]["metric"] == "bytes"


def test_summaries_report_average_and_latest():
    rows = _steady("a", 0, 4, requests=2, bytes_=100) + [_row("a", 4 * HOUR, requests=10, bytes_=900)]
    (s,) = analytics.client_summaries(analytics.client_series(rows))
    assert s["latest_requests"] == 10 and s["latest_bytes"] == 900
    assert s["avg_requests"] == pytest.approx(18 / 5)


def test_hourly_client_stats_buckets_by_client_and_hour():
    now = time.time()
    base = int(now // HOUR) * HOUR
    for ts, ip, hit, size in [
        (base + 10, "a", True, 100), (base + 20, "a", False, 300),
        (base + HOUR + 5, "a", True, 50), (base + 30, "b", True, 7),
    ]:
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO access_log (ts, client_ip, url_hash, filename, hit, bytes) VALUES (?,?,?,?,?,?)",
                (ts, ip, "h", "f", 1 if hit else 0, size),
            )
    rows = store.hourly_client_stats()
    by_key = {(r["client_ip"], r["hour"]): r for r in rows}
    a0 = by_key[("a", base)]
    assert (a0["requests"], a0["hits"], a0["bytes"], a0["new_downloads"]) == (2, 1, 400, 1)
    assert by_key[("a", base + HOUR)]["requests"] == 1
    assert by_key[("b", base)]["bytes"] == 7
    assert [r["client_ip"] for r in store.hourly_client_stats(client_ip="b")] == ["b"]
    assert store.hourly_client_stats(since_ts=base + HOUR) == [by_key[("a", base + HOUR)]]
