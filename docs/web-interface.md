# Web interface

The web UI is a small password-protected site for looking at what the proxy
has cached, who is using it and how the worker pool is doing. It also has a
small JSON API.

It listens on **HTTPS port 443** by default (`[webui] port` in
`config.toml`). On a fresh install it uses a self-signed certificate, so your
browser will warn on first visit. See [configuration](configuration.md#webui)
to set a login and to use your own certificate.

Every admin page shares one header and menu: **Cached Files, Usage, Stats,
Blocked, Categories**, with the current page underlined. The header is a
separate template (`cache_proxy/webui/templates/header.html`) included by
`base.html`, so a new page appears in the menu by adding one row there.

> The Files, Usage and Stats screenshots below are from a demo instance filled
> with made-up traffic (seven imaginary clients on `10.20.4.x`, and a stand-in
> download server). They show what each page looks like, not real usage. The
> Blocked and Categories pages are described in words only.

- [Signing in](#signing-in)
- [Cached files (`/`)](#cached-files-)
- [Usage (`/usage`)](#usage-usage)
- [Stats (`/stats`)](#stats-stats)
- [Blocked (`/blocks`)](#blocked-blocks)
- [Categories (`/categories`)](#categories-categories)
- [The block page (what a blocked user sees)](#the-block-page-what-a-blocked-user-sees)
- [JSON API](#json-api)
- [Security notes](#security-notes)

---

## Signing in

Every page and every API endpoint uses HTTP Basic authentication. Your
browser shows its own login prompt. The username and a PBKDF2 password hash
come from `[webui]` in `config.toml`.

If no username and password hash are configured, **every request is
refused with a 401**. The UI never falls back to running open. Until you set
a login you will just see the browser's login prompt, and every attempt
fails. See [configuration](configuration.md#webui) for how to create the
hash.

---

## Cached files (`/`)

![Cached files page](images/web-files.png)

The main page. From top to bottom:

**Proxy workers panel.** Shows how many workers are running out of the
maximum, the minimum, and the pool's average load. Below that is one row per
worker:

| Column | Meaning |
|---|---|
| PID | The worker's process id. |
| State | `starting` (booting, not yet taking traffic), `active`, or `retiring` (being shut down after the pool went idle). |
| CPU | That worker's CPU use, where 100% is one full core. |
| Connections | Client connections that worker currently holds. |
| Uptime | How long it has been running. |

The panel refreshes itself every 5 seconds without reloading the page, so
whatever you typed in the search box is not lost. If the proxy isn't running,
or its status file is more than a few sampling intervals old, it says
**"Proxy not running / status unavailable"** instead of showing stale
numbers.

**Summary line.**

- files cached and total size on disk (and the quota, if you set one);
- download hits and how much download traffic was served from the cache
  (downloads only; web assets are counted separately, next);
- web-asset hits, how many assets are stored and how much they saved;
- how many duplicate fetches were avoided when several clients asked for the
  same new file at once (only shown once this has happened).

A highlighted **low-disk warning** appears when the cache volume has less than 10%
free space.

**Search and filter.** The search box matches part of the filename or the
URL. The dropdown limits the list to **Downloads** or **Web assets**. The
list shows the newest 500 entries.

**Purge expired now.** Immediately deletes every entry that has passed its
lifetime. This also happens automatically every hour, so you only need it to
reclaim space sooner.

**The table.**

| Column | Meaning |
|---|---|
| Filename | Click to download the cached copy. The original URL is shown underneath. |
| Kind | `download` (installers and packages, kept 30 days by default) or `asset` (scripts, styles, images, fonts, kept up to an hour). |
| Size, Type | Size on disk and the content type the origin reported. |
| Cached | When it was fetched (server local time). |
| Expires in | Time left before it must be fetched again: `59m`, `3.5h`, `30.0d`, or `expired`. |
| Hits | How many times it has been served from cache. For web assets this can lag reality by up to about ten seconds, because those counts are written in batches. |
| Delete | Removes that entry and its file (asks to confirm). |

---

## Usage (`/usage`)

![Usage page](images/web-usage.png)

Who is using the proxy and what for. It only covers cache **hits** and newly
**stored downloads**, not all browsing (see the
[README](../README.md#1-general-download-cache-proxy) for why). Web-asset
traffic is deliberately not included here.

- **Window links** (top): 1 day, 7 days, 30 days, or all time. The default is
  7 days. In the URL this is `?days=7` (`?days=0` means all time).
- **Summary line:** requests, distinct clients, cache hits and hit rate,
  bytes served in total, and bytes served from the cache.
- **Top clients:** the 25 clients that received the most data, with requests,
  hit rate, bytes served and bytes that came from the cache. Click a client
  to filter the page to just that address (`?client=10.20.4.14`); the
  filter link shows how to clear it.
- **Most-requested files:** the 25 most-requested downloads with hits and
  bytes. Click a name to download it.
- **Recent activity:** the latest 200 requests. **HIT** means it was served
  from the cache; **MISS (stored)** means it was fetched from the internet
  and saved for next time.

The client address is whatever address connected to the proxy. If your
clients sit behind NAT or another proxy, you will see that device's address,
not the individual user's.

---

## Stats (`/stats`)

![Stats page](images/web-stats.png)

Per-client usage by the hour, with unusual activity flagged. The default
window is the last 168 hours (one week); `?hours=48` narrows it.

**Two groups of figures, and they measure different things.**

| Group | Source | What it covers |
|---|---|---|
| **All traffic** | `hourly_traffic` | Every response the proxy handled for that client |
| **Cacheable downloads** | `access_log` | Only the subset that matched the download rules |

A client that merely browses shows activity in the first group and nothing
at all in the second — it never appears in `access_log`, because that table
only records cache hits and newly stored downloads. Before the all-traffic
figures existed, such a client was invisible on this page entirely.

Byte totals under **All traffic** are a floor, not an exact total: a
response that is both streamed and chunked declares no `Content-Length` and
is never buffered, so there is nothing to measure and it counts as a request
with no bytes. **Request counts are exact.**

**How flagging works.** For each client, the page takes the client's own
average per hour over its earlier history in the window (not counting the
current hour) and compares it with the current hour. A
client is flagged if the current hour is more than `anomaly_factor` times
(default 3x) that average, on any of five measures:

| Measure | What it counts |
|---|---|
| total requests | Every response handled for the client. |
| total bytes | Bytes across all of it (a floor, see above). |
| requests | Cacheable-download requests served (hits plus new downloads). |
| bytes | Bytes served for those. |
| new downloads | Files the proxy had to fetch from the internet (misses). |

The first two mean a client whose general browsing spikes is flagged even
if it downloads nothing.

Two safeguards keep it from crying wolf: each client is compared with
**itself**, not with other clients, so a heavy user is not flagged for being
heavy; and a client needs at least `min_history_hours` (default 6) of earlier
history before it can be flagged at all, so a machine that just joined the
network is not flagged on its first hour. The average includes hours when the
client did nothing, so a client that is normally quiet and suddenly busy
stands out.

**Flagged this hour** lists each flag with the client, the measure, this
hour's value, the client's usual value per hour and the ratio. In the
screenshot, `10.20.4.14` is flagged on all three: it made 40 requests this
hour against a usual 1.5, and pulled 14 new downloads against a usual 0.3.

**Per-client summary** lists every client seen in the window, with its
average and latest-hour requests, bytes and new downloads.

It is only a flag on the page. Nothing sends an email or a message, so
someone has to look. The thresholds are in the `[analytics]` section of
[`config.toml`](configuration.md#analytics).

A high number is not proof of misuse. A person legitimately installing
several large tools at once looks the same as something unwanted, and a
re-download of the *same* cached file counts as requests and bytes but not
as a new download.

---

## Blocked (`/blocks`)

The admin view of content filtering. It has four parts, top to bottom:

- **Status line.** Whether filtering is on (`[filtering] enabled`) and which
  categories are enforced. It also says how to approve an unblock request
  (below).
- **Unblock-request email.** Whether `[email]` is configured, and if not, which
  settings are still empty. Where it is configured, the address requests go to
  and a **Send test email** button. Pressing it sends a short message to
  `unblock_recipient` and reports success, or the error (wrong password,
  refused connection, and so on), at the top of the page. Use it before
  relying on unblock requests.
- **Block lists.** One row per source and category: how many domains, when it
  last updated, and the last error if a refresh failed. A failed refresh keeps
  the previous list in use, so an error here means the list is getting stale,
  not that blocking stopped.
- **Recent blocks.** The latest 500, newest first: time, client IP, the site
  and full URL, the reason (and which list supplied it), the browser string and
  whether the user asked for an unblock. The **only with an unblock request** link
  narrows it to the ones needing a decision.

**Approving a request.** For each row with an unblock request the page shows
the exact line to add, and the file to add it to:

```
To allow, add to /etc/cache-proxy/allowed-hosts.conf:
shop.example.com
```

That file is read by the proxy within about 10 seconds. The host and all its
subdomains stop being blocked, whatever rule blocked them (a category list, the
administrator's list or a URL pattern). There is no button and no restart. To
decline, do nothing. The user is not told the outcome by the proxy, so reply to
them yourself.

The table behind this page is never pruned; see
[configuration](configuration.md#the-blocked-table).

---

## Categories (`/categories`)

Choose which content-filter categories are blocked.

- **61 categories in 7 groups** (Security, Adult, Gambling and games, Social
  and communication, Media and content, Money and shopping, Circumvention and
  network). Each row has a checkbox, the category name, a description, the
  source that supplies it, the number of domains downloaded and when they were
  last updated.
- **Ticked** means enforced. **Search categories** filters the list as you
  type; a group with no match hides itself. A counter shows how many boxes are
  ticked.
- **Save selection** (top and bottom of the page) writes the selection to
  `/etc/cache-proxy/filter-categories.conf`. The proxy applies it within about
  10 seconds. The lists for newly ticked categories are downloaded in the
  background, and the page confirms which. Refresh to watch the *Domains* and
  *Updated* columns fill in. A ticked category that still says "not downloaded"
  is not blocking anything yet.
- **Unticking** stops the blocking; the downloaded list stays on disk.
- If the page says the file is not writable, the service can't save. Use the
  command it shows to fix the permissions (the package sets them correctly).

Some categories are broad. `shopping`, `jobsearch`, `games`, `press`,
`social_networks` and `update` block a lot of ordinary business traffic, and
`update` can stop software updates. Turn on only what your policy needs, and
use the allow-list for exceptions.

---

## The block page (what a blocked user sees)

This is a separate small service (`cache-blockpage`, port 80 by default), not
part of the admin UI: no login, plain HTTP, so it loads without a certificate
warning. Its address is `[blockpage] url`.

When a user opens a blocked site in a browser they are redirected to
`http://<proxy>/blocked?t=<token>`, which shows:

| Item | Example |
|---|---|
| Reason | Category not allowed: gambling |
| Category | gambling |
| Site and address | `casino.example` and the full URL requested |
| Your IP address | `10.20.4.14` |
| Date and time | `2026-09-22 09:41:07 BST` |
| Browser | the browser string the request carried |

Below that, if `[email]` is configured, is a form with an optional note and a
**Send unblock request** button. The email to the administrator carries all of
the above plus the note, and says which line to add to `allowed-hosts.conf`.

What the page will and won't do:

- **Opened directly** (`/`, `/blocked`, or a wrong or mistyped token) it shows
  an empty template with no details and **no form**. A full page needs a token
  created by a real block.
- **Someone else's token** shows the same template: the page only reveals a
  block, and offers the form, to the IP address that was blocked.
- **One request per block.** After sending, the page says it has been sent.
  If the email fails to send, the user is told to try again later (with no
  technical detail) and can retry. Each IP address may send a limited number
  per hour (`max_requests_per_ip_per_hour`, default 5).
- **The email is built from the stored record**, not from the form, so a
  browser can't put anything in it except the note (capped at 1000 characters).
- Only **page loads** are redirected. Images, scripts, API calls, `apt`,
  `curl` and older browsers get a plain `403` with the header
  `X-Cache-Proxy: BLOCKED`, which is also on the redirect. Tools can use that
  header to tell a proxy block from a real `403` at the origin.

---

## JSON API

Both endpoints need the same login as the pages and return JSON.

### `GET /api/workers`

The worker pool's current state, the same data as the panel on the main page.

```json
{
  "available": true,
  "age": 1.4,
  "ts": 1789903008.06,
  "min_workers": 3,
  "max_workers": 6,
  "avg_cpu": 46.2,
  "workers": [
    { "pid": 11, "state": "active", "cpu": 44.0, "connections": 7, "uptime": 360 }
  ]
}
```

- `available` is `false` if the proxy isn't running or the snapshot is stale.
  If the status file is missing you get just `{"available": false,
  "workers": []}`; if it exists but is old you get its last contents with
  `available` set to `false` and `age` showing how old it is. Don't act on
  the numbers when `available` is `false`.
- `age` is how many seconds old the snapshot is (only present when a status
  file exists).
- `cpu` is a percentage of one core, so a busy worker sits near 100. `avg_cpu`
  is the mean over active workers.
- `uptime` is in seconds. `state` is `starting`, `active` or `retiring`.

### `GET /api/hourly-stats`

The raw hourly series behind the stats page, plus the current flags.

Query parameters (both optional): `hours` (window, default 168) and `client`
(an IP address, to restrict the series to one client).

```json
{
  "hours": 168,
  "series": {
    "10.20.4.14": [
      { "client_ip": "10.20.4.14", "hour": 1789898400,
        "requests": 3, "hits": 2, "bytes": 31457280, "new_downloads": 1,
        "total_requests": 418, "total_bytes": 41496951 }
    ]
  },
  "anomalies": [
    { "client_ip": "10.20.4.14", "metric": "bytes", "hour": 1789902000,
      "value": 412316860, "baseline": 17091010.5, "ratio": 24.1 }
  ]
}
```

- `hour` is the start of the hour as a Unix timestamp (seconds). Hours in
  which a client did nothing have no entry.
- In each entry, `requests` = `hits` + `new_downloads`. Those three, and
  `bytes`, count **cacheable downloads only**.
- `total_requests` and `total_bytes` count **everything** the proxy handled
  for that client, browsing included. A client that only browses has those
  two set and the rest zero. `total_bytes` is a floor (see above).
- `anomalies` holds only flags for the **current** hour, and is empty when
  nothing is unusual. `metric` is one of `requests`, `bytes`,
  `new_downloads`, `total_requests`, `total_bytes`.
  `baseline` is the client's usual value per hour and `ratio` is `value` divided
  by `baseline`.

**Grafana.** The data is meant to be usable from Grafana's JSON API style of
data source (with basic authentication and your certificate trusted). I have
not tested this against a real Grafana. Note the series comes back keyed by
client rather than as a flat table, so expect to need a transformation
step to turn it into panels.

### Actions

These are form posts used by the buttons on the pages (they need the same
login): `POST /delete/{hash}` deletes one entry, `POST /purge-expired` purges
expired entries, and `GET /download/{hash}` downloads a cached file. `{hash}`
is the entry's identifier, visible in the download links.

The new pages add `POST /categories` (save the category selection) and
`POST /blocks/test-email` (send the test email); the block page service has
its own `POST /unblock`, which needs no login.

---

## Security notes

- Login is HTTP Basic over HTTPS. Use a certificate your browsers trust so
  the connection can't be silently intercepted.
- There is **no lock-out or rate-limiting** on failed logins, and no audit log
  of who deleted what. Keep the UI reachable only from a management network.
- The delete and purge buttons are plain authenticated form posts with **no
  CSRF token**. A logged-in administrator who visits a malicious page could
  in principle have a delete triggered. The damage is limited (cached files
  are re-fetched on demand), but it is another reason to keep the UI on a
  restricted network.
- **Saving categories is protected against cross-site posts** (a request from
  another site is refused), but the older delete and purge buttons still are
  not. The block page is deliberately unauthenticated: it exposes only the
  viewer's own block, behind a 128-bit token and a matching IP address.
- The **Blocked** page and the `blocked` table show which clients tried to
  reach which sites. That is personal data in most jurisdictions; decide who
  may see it, how long you keep it (it is never pruned automatically) and
  whether users must be told.
- There is a single account. There are no roles, so anyone who can sign in
  can delete cache entries.
