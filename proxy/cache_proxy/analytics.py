"""Per-client hourly usage series and anomaly detection.

Usage here means what access_log records: cache HITs and MISS-STORED
downloads. It is not a measure of all proxied browsing.
"""
from collections import defaultdict
from typing import Optional

from . import config

METRICS = ("requests", "bytes", "new_downloads")


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
            baseline = sum(h[metric] for h in history) / span
            value = latest[metric]
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
            row[f"avg_{metric}"] = sum(h[metric] for h in hours) / span
            row[f"latest_{metric}"] = latest[metric]
        out.append(row)
    out.sort(key=lambda r: r["latest_bytes"], reverse=True)
    return out
