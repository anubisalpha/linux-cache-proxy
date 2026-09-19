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

`/usage` (`store.py`: `top_clients`, `access_summary`) already has the
raw data (per-client request count, bytes served, hit rate, windowed by
day). Need to turn that into actual alerting rather than a page someone
has to remember to check:

- Define "higher than usual" -- rolling baseline per client (e.g. average
  over trailing N days) vs. a hard threshold? Baseline is more honest but
  needs enough history to be meaningful; a simple threshold is easier to
  reason about early on.
- Where should alerts go -- email (there's already a Claude Mail setup in
  this workspace), Slack, or just a flagged/highlighted row in the
  `/usage` page itself as a first cut?
- Decide what counts as "usage" for this purpose: total bytes, request
  count, or specifically MISS-STORED events (new distinct downloads) --
  a user re-downloading the same cached installer repeatedly hits
  differently than one pulling many distinct large files.
- Probably belongs in `store.py` as a new query (e.g. `clients_over_threshold`)
  called on a schedule (cron hitting a small script, or a background task
  in the webui process) rather than inline in the request path.
