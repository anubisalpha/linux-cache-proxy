# TODO

## Multi-worker / multi-threaded proxy

mitmproxy runs single-process (asyncio, not multi-worker like Squid/Nginx)
— load testing confirmed it saturates ~1 core and throughput plateaus
under sustained concurrent HTTPS load (see README "Known limits" and the
capacity-planning discussion: 100 concurrent continuous HTTPS requesters
hit ~20% timeouts, p99 7.3s). Need a real multi-worker story before this
carries a full office's general browsing traffic, not just downloads.

Options to evaluate:
- Run N `cache-proxy` instances (different ports) behind a TCP load
  balancer (HAProxy) or split clients across instances via WPAD/DNS
  round-robin.
- Each instance needs its own `CACHE_PROXY_CONFDIR` (mitmproxy CA) or a
  shared one — shared is simpler for clients (one CA to trust) but check
  mitmproxy doesn't assume exclusive ownership of the confdir.
- The SQLite-backed index (`store.py`) is currently single-writer-
  assumption-friendly (WAL mode helps concurrent reads) but multiple
  proxy processes writing to the same DB concurrently needs testing under
  real concurrent load -- may need to revisit if it becomes a bottleneck.
- Systemd: probably a templated unit (`cache-proxy@.service`) with an
  instance number, rather than N hand-copied unit files.

## Usage alerting -- flag abnormal per-user activity

`/usage` (`store.py`: `top_clients`, `access_summary`) already has raw
per-client data, but only ad-hoc (whatever window someone picks when they
load the page), not a real time series. Need to turn this into actual
per-IP-per-hour analytics with anomaly tracking, not just a page someone
has to remember to check.

Design sketched out (not built yet):
- `store.py`: `hourly_client_stats(since_ts, client_ip=None)` -- GROUP BY
  client_ip + hour bucket (`CAST(ts / 3600 AS INTEGER) * 3600`) over
  `access_log`, giving per-IP-per-hour requests/hits/bytes. Existing
  `idx_access_log_ts` / `idx_access_log_client` indexes should make this
  cheap enough to compute on demand rather than needing a separate
  rollup/materialized table -- revisit if `access_log` grows large enough
  that this stops being true.
- A small `analytics.py`: shape that into a per-client hourly series, and
  a `detect_anomalies()` -- per-client baseline (trailing average, not a
  single global threshold, since normal usage varies a lot client to
  client), flagging when the latest hour is e.g. 3x a client's own
  average. Needs a minimum amount of history per client before it'll flag
  anything, so a client that's simply new to the network doesn't trip a
  false alarm on hour one.
- Expose it two ways per the "Grafana or a simple onscreen display" ask:
  - `/api/hourly-stats` (JSON, same Basic Auth as everything else) --
    point Grafana's JSON API datasource at it, or anything else that
    wants the raw series.
  - `/stats` -- a plain page in the existing web UI: anomalies flagged at
    the top, per-client summary (avg/latest requests & bytes per hour)
    below.
- Still open regardless of which surface: what counts as "usage" for
  this purpose -- total bytes, request count, or specifically
  MISS-STORED events (new distinct downloads), since a user
  re-downloading the same cached installer repeatedly hits differently
  than one pulling many distinct large files. And whether flagging in the
  UI is enough for v1, or this needs to actually push somewhere (email --
  there's already a Claude Mail setup in this workspace -- or Slack).
