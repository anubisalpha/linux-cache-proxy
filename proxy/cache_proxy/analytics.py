"""Per-client hourly usage series and anomaly detection.

Two things are measured per client per hour, and they are not the same:

- requests / hits / bytes / new_downloads come from access_log, which
  records only cache HITs and MISS-STORED downloads.
- total_requests / total_bytes come from hourly_traffic, which meters
  every response the proxy handled. A client that only browses appears
  here and nowhere else.

merge_hourly() joins the two so a client is visible whether it downloads,
browses, or both.
"""
from collections import defaultdict
from typing import Optional

from . import config

METRICS = ("requests", "bytes", "new_downloads", "total_requests", "total_bytes")

# Columns a merged row always carries, so callers can sum them blindly.
_EMPTY = {"requests": 0, "hits": 0, "bytes": 0, "new_downloads": 0,
          "total_requests": 0, "total_bytes": 0}


def merge_hourly(download_rows, traffic_rows) -> list:
    """Full outer join of the access_log series and the hourly_traffic
    series on (client_ip, hour).

    Outer, not inner: a client that only browses has hourly_traffic rows
    and no access_log rows at all, and dropping it is exactly the blind
    spot this exists to close."""
    merged: dict = {}

    def row_for(r):
        key = (r["client_ip"] or "unknown", r["hour"])
        if key not in merged:
            merged[key] = {**_EMPTY, "client_ip": key[0], "hour": key[1]}
        return merged[key]

    for r in download_rows:
        row_for(r).update({k: r[k] for k in ("requests", "hits", "bytes", "new_downloads") if k in r})
    for r in traffic_rows:
        row = row_for(r)
        row["total_requests"] += r["requests"]
        row["total_bytes"] += r["bytes"]

    return sorted(merged.values(), key=lambda r: (r["client_ip"], r["hour"]))


def client_series(rows) -> dict:
    """Group hourly_client_stats() rows into {client_ip: [hour rows sorted by hour]}."""
    series = defaultdict(list)
    for r in rows:
        series[r["client_ip"] or "unknown"].append(dict(r))
    for hours in series.values():
        hours.sort(key=lambda r: r["hour"])
    return dict(series)


def detect_anomalies(
    series: dict,
    factor: Optional[float] = None,
    min_history_hours: Optional[int] = None,
    latest_hour: Optional[int] = None,
) -> list:
    """Flag clients whose latest hour exceeds `factor` x their own trailing
    average on any metric.

    The baseline is per client (usage varies enormously between clients) and
    excludes the hour being judged. A client needs `min_history_hours` prior
    hours of history first, so one new to the network never trips on hour one.
    `latest_hour` is the hour bucket to judge; default is each client's own
    most recent hour, so pass the current hour to ignore clients that have
    gone quiet.
    """
    factor = config.ANOMALY_FACTOR if factor is None else factor
    min_history_hours = config.MIN_HISTORY_HOURS if min_history_hours is None else min_history_hours
    anomalies = []
    for client, hours in series.items():
        if not hours:
            continue
        latest = hours[-1]
        if latest_hour is not None and latest["hour"] != latest_hour:
            continue
        history = hours[:-1]
        if len(history) < min_history_hours:
            continue
        # Hours with no traffic have no row; the baseline spans the whole
        # elapsed window so a mostly-quiet client's average reflects that.
        span = max(1, (latest["hour"] - history[0]["hour"]) // 3600)
        for metric in METRICS:
            baseline = sum(h.get(metric, 0) for h in history) / span
            value = latest.get(metric, 0)
            if baseline > 0 and value > factor * baseline:
                anomalies.append(
                    {
                        "client_ip": client,
                        "metric": metric,
                        "hour": latest["hour"],
                        "value": value,
                        "baseline": baseline,
                        "ratio": value / baseline,
                    }
                )
    anomalies.sort(key=lambda a: a["ratio"], reverse=True)
    return anomalies


def client_summaries(series: dict) -> list:
    """Per-client avg/latest per hour, for the stats page."""
    out = []
    for client, hours in series.items():
        latest = hours[-1]
        span = max(1, (latest["hour"] - hours[0]["hour"]) // 3600 + 1)
        row = {"client_ip": client, "latest_hour": latest["hour"], "hours_seen": len(hours)}
        for metric in METRICS:
            row[f"avg_{metric}"] = sum(h.get(metric, 0) for h in hours) / span
            row[f"latest_{metric}"] = latest.get(metric, 0)
        out.append(row)
    out.sort(key=lambda r: (r.get("latest_total_bytes", 0), r.get("latest_bytes", 0)), reverse=True)
    return out
