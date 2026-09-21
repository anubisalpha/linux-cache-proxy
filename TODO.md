# TODO

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
