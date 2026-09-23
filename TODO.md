# TODO

## Web UI facility for managing certificates (2026-09-23)

Today's session (see `Projects\linux-cache-proxy-wfc\memory\project_linux_cache_proxy.md`
for the full detail) involved a lot of manual, SSH-only certificate
wrangling:

- Fetching the mitmproxy CA cert off the box by hand (`ssh ... cat
  /var/lib/cache-proxy/mitmproxy-ca/mitmproxy-ca-cert.cer`) to embed in
  `set-test-proxy.sh`, then re-fetching and re-embedding whenever the CA
  regenerates.
- Discovering fc's CA never actually persisted on one client, invisible
  until someone went looking at the failure symptom days later.
- Manually editing `config.toml`'s `[tls_trust] seed_hosts` over SSH and
  running `python -m cache_proxy.vendorcas update --host <x>` by hand to
  force an immediate vendor-CA fetch, rather than waiting for the daily
  timer.
- Windows clients needing the CA cert downloaded and manually imported
  into the right store (`LocalMachine\Root`, not just `CurrentUser\Root`
  — a mistake made more than once this session), with no guided path.

**Idea, not yet designed:** let the web UI itself serve and manage
certificates instead of everything being an SSH/SCP round-trip:
- A **download link for the current mitmproxy CA cert** (`.crt`/`.cer`),
  so a new client (especially a Windows one, where the whole
  double-click-and-import flow already exists) doesn't need anyone to SSH
  in and fetch it manually. Solves the exact "which store did I import it
  into" confusion hit twice today.
- A page showing **vendor-CA seed host status** (host, last successful
  fetch, cert count, last error) — currently only visible via `python -m
  cache_proxy.vendorcas status` over SSH — plus a button to add a seed
  host and trigger an immediate fetch, instead of editing `config.toml`
  by hand and waiting for (or manually invoking) the daily timer.
- Worth considering whether this belongs behind the same auth the rest of
  the web UI already has, or needs something more restrictive given it's
  effectively trust-store administration.

## Push alerts for real errors, not just usage anomalies (2026-09-23)

Right now the only way to find a real breakage is someone manually reading
`journalctl -u cache-proxy`. That's exactly how today's two real bugs were
found: fc's ClamAV CA silently never persisting (552 failed TLS handshakes
over 48h before anyone noticed), and Docker Desktop's MCP toolkit sending a
malformed `Authorization: Bearer ` header that mitmproxy's HTTP/2 layer
rejects outright. Neither would have surfaced without someone asking.

**Decided (2026-09-23), not yet built:** a scheduled scan on CT 914,
same pattern as the existing `cache-proxy-lists*` timers, that tails the
journal for the error classes actually seen so far and emails a digest via
the internal relay (172.16.2.25:25, see `reference_email_smtp.md`) —
**only when something NEW appears**, not on every run, so it doesn't
become noise. Needs a small state file (e.g. hash of seen error
signatures) to dedupe recurring errors from genuinely new ones.

Error classes to watch for, from what's actually happened:
- `Client TLS handshake failed... tlsv1 alert unknown ca` (fc's CA bug)
- `HTTP/2 protocol error` (mcp.docker.com's malformed header)
- `Addon error` / unhandled `Traceback` in the mitmproxy addon itself

This is a different problem from the "Push alerts for abnormal usage" item
below (volume/anomaly, not correctness) but should probably share the same
delivery mechanism once both exist — no need to build two separate email
pipelines.

## Push alerts for abnormal usage

`/stats` and `/api/hourly-stats` flag clients whose current hour is well
above their own baseline, but only on screen / via the JSON API. Nothing is
pushed. Next step is a notification (email, using the Claude Mail SMTP setup
in this workspace, or Slack) once the thresholds in `[analytics]` have been
tuned against real traffic, so it doesn't just generate noise.

Still open: which metric should page someone. The page currently flags on
bytes, requests or new downloads; a re-download of the same cached
installer looks very different from a client pulling many distinct large
files.

## Multi-machine scaling

The worker pool scales across the cores of one box. Several machines would
each need their own cache and SQLite index today; sharing them would mean
moving the index to a real database and the cache to shared storage.

## Scale-down can be slow under steady traffic

A worker is only retired when it holds no client connections (stopping it
would drop them). If every worker always has at least one open connection,
the pool won't shrink back to `min_workers`. A proper fix needs a way to
stop new connections reaching one worker while it drains, which
`SO_REUSEPORT` doesn't offer on its own.

## Content filtering: not yet verified in production conditions

The proxy side is tested (unit tests plus real mitmdump runs for the redirect,
the plain 403, the block-page routing and the allow-list), and the package
installs cleanly, but a few things have only been checked in pieces:

- The `cache-blockpage` service and the two list-refresh timers have not been
  run as live systemd units (the test container has no systemd). The Python
  behind them is tested, and systemd accepts the timer schedule.
- The block page has not been seen in a real browser going through a real
  SSL-bumped proxy. The redirect and the routing to the block page are tested
  against a fake origin.
- Unblock email has only been sent to a fake SMTP server, never a real one.
- The default sources (UT1, Phishing.Database, URLhaus) were downloaded and
  loaded for real, but their licences and fair-use terms should be checked
  before relying on them commercially. UT1's own `malware` tarball currently
  contains the phishing list, so malware comes from UT1's GitHub mirror.

## Content filtering: known limits and ideas

- **Block page is plain HTTP.** Chosen so it loads without a certificate
  warning, but the token and the user's details cross the LAN in clear text.
  An HTTPS option with a real certificate would fix that.
- **No outcome sent to the user.** Approving an unblock means adding a line to
  `allowed-hosts.conf`; the proxy doesn't tell the user. A button on the
  **Blocked** page (and a "your request was approved/declined" message) would
  close the loop. It was left manual on purpose for now.
- **No per-client or per-group rules.** Every client gets the same categories.
- **Only fast lists refresh hourly, only in working hours.** A domain reported
  outside 08:00-18:00 Mon-Fri, or on a daily-only list, waits for the next run.
  Conditional requests (`If-Modified-Since`) would cut the download cost of
  refreshing more often.
- **The `blocked` table is never pruned.** Deliberate (it is an audit trail),
  but it grows without bound and holds personal data. There is no export, retention
  setting or anonymisation.
- **List failures are visible only on the web UI and in the journal.** Nothing
  pushes an alert when a list stops refreshing (see also "Push alerts" above).
- **Hash-index false positives.** Lists are stored as 64-bit hashes; a
  collision (about one in a trillion per lookup) would block an innocent domain.
  The allow-list fixes it, but it would be invisible in the listed source.
- **Untested at scale on the categories page.** Enabling many big categories at
  once downloads them one at a time in the web UI process; there is no progress
  bar (refresh the page).
