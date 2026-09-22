# linux-cache-proxy

A native-Linux caching setup for reducing repeated software-update and
installer downloads across a workplace network, with a web UI to browse,
search, and delete cached files, and per-client usage logging. The general
download cache can also act as a **content filter**: it blocks sites by
category (adult, gambling, malware, phishing and dozens more) from lists it
downloads and refreshes itself, shows blocked users an explanatory page,
records every block, and lets users ask for an unblock by email.

Three parts, each solving a different traffic type:

| Traffic | Tool | Why |
|---|---|---|
| General installers/downloads (HTTP or HTTPS) | `proxy/` — custom mitmproxy addon | Squid's cache store is opaque; this stores real, browsable files |
| Debian/Ubuntu packages (`.deb`) | `apt-cacher-ng/` | Purpose-built, understands apt repo metadata |
| RPM packages (`.rpm`, Rocky/Alma/Fedora) | `rpm-cache/` — Nginx `proxy_cache` | apt-cacher-ng is apt-only; Nginx caching reverse-proxy is the standard equivalent for yum/dnf |

**Not covered:** Windows Update. WU is almost entirely HTTPS with its own
CDN behavior; the correct tool is WSUS, not a generic proxy. Skipped for
now per project decision — revisit if it becomes a priority.

## Documentation

This README is the overview. The detail is in `docs/`:

| Guide | What's in it |
|---|---|
| [Building and installing](docs/building.md) | Build prerequisites, building the `.deb` (on Ubuntu or in Docker), step-by-step install and first-run setup, connecting clients, checking it works, what is installed where, the Docker demo, running the tests |
| [Configuration reference](docs/configuration.md) | Every `config.toml` setting with defaults and guidance, the host exclusion lists, **content filtering** (list sources and refresh schedule, choosing categories, the block page, unblock-request email, the `blocked` table), settings that live outside the config file, environment variable overrides |
| [Web interface](docs/web-interface.md) | A tour of each admin page (files, usage and stats with screenshots; blocked and categories described), the page blocked users see, the shared menu, how to read the stats page and its flags, the JSON API, security notes |
| [Operations](docs/operations.md) | Services, timers and logs, checking caching and filtering work, sizing, network and security, upgrading, backup and restore, troubleshooting, removal |
| [Deploying on Proxmox](docs/proxmox.md) | Container vs VM, the two settings you must change in a container, storage and the dedicated cache volume, egress requirements, firewall options, the port 80 clash with nginx, proxy settings that systemd does not inherit, and the caveats that catch people out |

## 1. General download cache (`proxy/`)

- **Core**: [mitmproxy](https://mitmproxy.org/) in explicit-proxy mode,
  TLS-intercepting ("SSL-bump") so it can see inside HTTPS downloads —
  requires deploying its CA cert to every client (see below).
- **Cache addon** (`cache_proxy/addon.py`): on each response, checks
  whether the URL/content-type looks like a real download (installer
  extensions, package formats, min size 1 MiB) and if so saves it to disk
  under `/var/lib/cache-proxy/files/`, indexed in SQLite. Later requests
  for the same URL, from any client, are served straight from disk.
- **Access logging** (`cache_proxy/store.py`, table `access_log`): every
  cache hit and newly-cached download is logged with client IP, filename,
  hit/miss, and bytes. This is *not* full browsing history — only requests
  that match the cacheable-download rules get logged, not every page a
  client visits (SSL-bump means the proxy technically sees everything; we
  deliberately don't record all of it).
- **Traffic metering** (table `hourly_traffic`): because `access_log` only
  sees cacheable downloads, a client that merely browses appears nowhere in
  it — one machine idling put 600 requests through the proxy and showed up
  as nothing at all. So every response is additionally tallied into one row
  per client per hour: request count and bytes, and **nothing else**. No
  URL, no hostname, nothing about where the traffic went. That keeps it a
  traffic meter rather than a browsing history, and keeps it to a few
  thousand rows a day for a whole site instead of millions. Byte totals are
  a floor: a response that is both streamed and chunked declares no length
  and is never buffered, so it counts as a request with no bytes. Request
  counts are exact. Pruned on the same schedule and `retention_days` as
  `access_log`.
- **Web UI** (`cache_proxy/webui/`), FastAPI on port 443 (HTTPS,
  password-protected — see below):
  - `/` — cached files, search, download, delete
  - `/usage` — request/hit-rate summary, top clients by bytes served,
    most-requested files, recent activity log (filterable by client IP,
    windowed by 1/7/30 days or all-time)
  - `/stats` — per-client hourly figures with anomaly flags (below), in two
    groups: **all traffic** (every response the proxy handled, from
    `hourly_traffic`) and **cacheable downloads** (the subset `access_log`
    records). A client that only browses shows activity in the first and
    nothing in the second. Raw series at `/api/hourly-stats` (JSON, for
    Grafana's JSON API datasource)
  - `/` also shows the worker pool (active/max workers, pool and
    per-worker CPU, connections), refreshed live from `/api/workers`
  - `/blocks` — recent blocks, unblock requests with the exact line to add to
    approve one, block-list status, and a test-email button
  - `/categories` — tick which content-filter categories to block
  - every page shares one header and menu (`templates/header.html`)
- **Worker pool** (`cache_proxy/supervisor.py`): one mitmproxy process
  uses about one CPU core, so the proxy runs a pool of them sharing port
  8080 (`SO_REUSEPORT` — the kernel spreads connections across workers and
  each connection keeps its real client IP, so usage logging is
  unaffected). It starts `min_workers`, adds one when the pool's average
  CPU stays above `scale_up_cpu_pct`, and retires an idle one back down
  toward the minimum once the pool has been quiet for
  `scale_down_idle_seconds`. A worker is only retired when it has no open
  client connections (a long download uses almost no CPU, and stopping a
  worker drops its connections). Crashed workers are replaced. Workers share
  one cache directory and SQLite index (WAL); files are written atomically
  and each worker re-reads the index every few seconds, so a file stored by
  one worker is a hit on all of them (see request coalescing, next).
- **Cache lifetimes** (`config.toml`): downloads are served from cache for
  `download_ttl_days` (30); static web assets (JS, CSS, images, fonts —
  never HTML) for `[webcache] ttl_minutes` (60). Origin headers are always
  respected for assets: nothing is stored that is `no-store`/`private`/
  `no-cache`, sets a cookie, needs `Authorization`, or varies on anything
  but `Accept-Encoding`, and a shorter origin `max-age` wins. An expired
  entry is refetched and its lifetime restarts; expired files are purged
  hourly. Asset traffic is counted (hits/stored/bytes saved, and per file) but not
  written to `access_log`, so it doesn't drown out the usage statistics.
- **Request coalescing**: when several clients ask for the same
  not-yet-cached download or static asset at once (a "stampede" on a fresh
  installer), only the first goes upstream. It takes a per-URL file lock
  shared by every worker process; the rest wait, then are served from the
  cache. The lock is released as soon as the outcome is known — stored, or
  clearly not going to be (too big, uncacheable, an error) — so waiters
  never queue behind a multi-GB download and an uncacheable response doesn't
  serialise them. Waiting is capped by `[cache] coalesce_wait_seconds` (60;
  `0` turns it off), after which a request just goes upstream. Only URLs
  that look like a download or static asset take part, so ordinary page
  traffic is never held up. The kernel drops the lock if a worker dies.
- **Cache hits are streamed and resumable.** Hits over 8 MiB, and any
  request carrying `Range`, are served from disk by a small loopback file
  server inside each worker (mitmproxy can't stream a body from a request
  hook), so a multi-GB ISO isn't loaded into RAM and resumed downloads get
  `206 Partial Content`.
- **Quota**: `max_size_gb` (0 = unlimited) evicts least-recently-hit
  entries once the cache exceeds it. The main page warns when the cache
  volume is under 10% free. `access_log` and `hourly_traffic` rows older
  than `retention_days` (90) are pruned daily.
- **Usage alerting** (`cache_proxy/analytics.py`): `/stats` flags a client
  when its current hour exceeds `anomaly_factor` (3x) its own trailing
  average on any of its metrics, once it has `min_history_hours` of history
  so a new client never trips on hour one. The metrics are the download
  figures from `access_log` (requests, bytes, new downloads) **and** the
  all-traffic figures from `hourly_traffic` (total requests, total bytes) —
  so a client whose general browsing spikes is flagged even if it downloads
  nothing. Flagging is on-screen/JSON only; nothing is pushed anywhere yet.

### Content filtering

Off until you enable it (`[filtering] enabled = true`, then restart). Full
detail is in the [configuration reference](docs/configuration.md#content-filtering-filtering-blockpage-email);
in short:

- **Category lists** come from UT1 (Université Toulouse Capitole; 61
  categories), Phishing.Database and URLhaus. They are downloaded by a
  daily timer (plus an hourly refresh, 08:00 to 18:00 Monday to Friday, for
  the fast-moving malware and phishing feeds) into
  `/var/lib/cache-proxy/filter-lists/`, kept apart from `/etc`. A bad or
  truncated download never replaces a good list.
- **You choose what is blocked** on the web UI **Categories** page: tick
  categories, save, and the proxy applies it within about 10 seconds. The
  selection is stored in `/etc/cache-proxy/filter-categories.conf`.
- **Your own rules** live in `blocked-hosts.conf`, `allowed-hosts.conf` (never
  blocked, and where approved unblock requests go) and
  `blocked-url-patterns.conf`. Precedence: allow-list, blocked hosts,
  category lists, URL patterns.
- **HTTPS is filtered** for every host not in `never-intercept-hosts.conf`.
- **Blocked users** are redirected to a page on port 80 (its own service,
  `cache-blockpage`) showing the reason or category, the URL, their IP, the
  time and their browser. Only page loads are redirected; everything else
  gets a plain `403`. The page only shows details, and only offers the
  unblock form, for a real block recorded for the viewer's own IP.
- **Every block is recorded** in a never-pruned `blocked` table.
- **Unblock requests** are emailed to an address in `config.toml`
  (`[email]`, password in `secrets.env`). The email says exactly which line
  to add to `allowed-hosts.conf` to approve it. The web UI **Blocked** page
  has a **Send test email** button.
- **Size:** the default lists are about 5.5 million domains, kept as
  memory-mapped hash indexes: well under a second to load, and almost no extra
  memory per worker.

Set it up in this order: `[blockpage] url` and `[email]` in `config.toml`,
the SMTP password in `/etc/cache-proxy/secrets.env`, download the lists
(`sudo -u cacheproxy /opt/cache-proxy/venv-proxy/bin/python3 -m
cache_proxy.filterlists update`), then set `enabled = true`.

### Installing the package

*(Short version. [docs/building.md](docs/building.md) has the full
walk-through, including building the package and checking it works.)*

Everything under `proxy/` ships as a single `.deb`, built with both
Python venvs bundled in (no internet access or pip resolution needed on
the target machine):

```bash
bash packaging/build-deb.sh 1.1.0          # run on Ubuntu 24.04 (or via docker/Dockerfile)
sudo apt install ./cache-proxy_1.1.0_amd64.deb
```

`postinst` creates the `cacheproxy` system user, `/var/lib/cache-proxy/`,
a self-signed web UI TLS cert, and installs+enables (but does **not**
start) the two systemd services — this intercepts all HTTPS traffic and
the web UI has no password yet, so it deliberately waits for you:

```bash
sudo /opt/cache-proxy/venv-webui/bin/python3 -m cache_proxy.webui.hash_password
```

Paste the printed `password_hash` line into `[webui]` in
`/etc/cache-proxy/config.toml`, set `username` too, review the two host
exclusion files (below), then:

```bash
sudo systemctl start cache-proxy cache-webui
```

Both are real `systemctl`-managed services (`cache-proxy.service`,
`cache-webui.service`) — `systemctl status`, `journalctl -u cache-proxy`,
`systemctl restart`, survive reboot, etc. all work normally. Verified by
actually installing the built `.deb` under real systemd (not just
checking the unit files parse) and confirming both reach
`active (running)`, run as the non-root `cacheproxy` user, and that the
web UI genuinely binds port 443 via `AmbientCapabilities=CAP_NET_BIND_SERVICE`
rather than running as root.

`apt remove cache-proxy` stops/disables the services and leaves
`/etc/cache-proxy/` (a conffile-managed config) and cached data alone.
`apt purge cache-proxy` additionally deletes `/var/lib/cache-proxy/` (the
cache and SQLite index), the generated TLS cert, and the `cacheproxy`
user — full purge semantics, not just "package gone."

Deploy the mitmproxy CA cert to clients once `cache-proxy.service` has
started (it generates `/var/lib/cache-proxy/mitmproxy-ca/mitmproxy-ca-cert.cer`
on first run) via Group Policy (Computer Configuration → Policies →
Windows Settings → Security Settings → Public Key Policies → Trusted Root
Certification Authorities). Without this, clients get TLS certificate
errors on every HTTPS site.

Point clients at the proxy via GPO (Computer/User Configuration →
Preferences → Control Panel Settings → Internet Settings) or WPAD:
`Proxy server: <server-ip>:8080`.

### Configuration — `/etc/cache-proxy/`

*Every setting, with defaults and guidance, is in
[docs/configuration.md](docs/configuration.md).*

| File | Contents |
|---|---|
| `config.toml` | Cache dir/db paths, size limits, TTLs and quota, cacheable extensions/content-types, `[webcache]` asset rules, `[proxy]` worker-pool sizing, `[analytics]` thresholds/retention, web UI port/username/password_hash/TLS cert paths |
| `never-cache-hosts.conf` | Hosts (+ subdomains) proxied normally but never written to the cache |
| `never-intercept-hosts.conf` | Hosts (+ subdomains) that bypass TLS interception entirely — mitmproxy tunnels them raw. Use for cert-pinned apps and anything sensitive (banking, etc). Implies never-cache for the same host |
| `blocked-hosts.conf` / `allowed-hosts.conf` / `blocked-url-patterns.conf` | Content filtering overrides (off until `[filtering] enabled = true`): extra hosts to block, hosts never blocked (where approved unblock requests go), URL regexes. Downloaded category lists live separately in `/var/lib/cache-proxy/filter-lists`. Re-read automatically |
| `filter-categories.conf` | The enforced content-filter categories, one per line. Written by the web UI **Categories** page (overrides `block_categories` in `config.toml`); hand-editing is fine |
| `secrets.env` | SMTP password and URLhaus key, kept out of `config.toml` (root and service group only) |

Both host-list files are one pattern per line, `#` comments, `*.` prefix
optional (`example.com` already matches subdomains). Config changes need
a service restart (`sudo systemctl restart cache-proxy cache-webui`) —
there's no live-reload.

`never_intercept_hosts` is turned into mitmproxy `ignore_hosts` regex
options by `run_proxy.sh` at startup (reusing `config.py`'s own
parsing/conversion, not duplicated in bash). `never_cache_hosts` is
enforced directly in the addon, with `never_intercept_hosts` checked
there too as a belt-and-braces fallback in case `ignore_hosts` wasn't
wired up for some reason.

### Known limits

- **Content filtering only sees traffic the proxy decrypts.** Hosts in
  `never-intercept-hosts.conf` are tunnelled raw and are not filtered, and
  clients that bypass the proxy (VPN, DNS-over-HTTPS with a direct route,
  no proxy configured) are not filtered either. It is a host/URL filter
  driven by third-party lists, not content inspection, and those lists can
  be wrong in both directions (use the allow-list for false positives). A
  newly reported malware or phishing domain can stay reachable until the next
  refresh. For HTTPS sites a client must trust the proxy CA, otherwise it
  gets a certificate error before any block page can be shown.
- Alerts are visible on `/stats` and via the JSON API only; there is no
  email/Slack push.
- Scaling down never interrupts a client, so under steady light traffic
  where every worker always holds a connection it can take a while to
  shrink back to the minimum.
- **Throughput is bounded by workers x one core.** SSL-bump adds a double
  TLS handshake to every HTTPS connection, so routing *all* general
  browsing through this is heavier than the download-caching job it was
  built for. The worker pool removes the single-process ceiling. Measured
  on a Docker VM with 8 CPUs (proxy workers pinned to 4 cores, load
  generator and origin on the other 4), with 100 concurrent clients each
  looping CONNECT + TLS handshake + a 2 KB GET, no think time, 10 s client
  timeout, 20 s runs:

  | Workers | Throughput | Timeouts |
  |---|---|---|
  | 1 | 11.1 req/s | 55% |
  | 2 | 17.4 req/s | 38% |
  | 4 | 30.5 req/s | 19% |

  Caveats: this is a synthetic worst case (real users pause and reuse
  connections), the workers were at ~60-80% CPU rather than saturated at 4
  workers so the generator was part of the limit, and the absolute figures
  are not comparable to the earlier single-worker measurement (a different,
  lighter generator). Serving a cacheable asset from cache with one worker
  reached 17.3 req/s at 99% CPU versus 11.1 req/s for the same load passing
  through, so asset caching didn't cost throughput here. Load-test with your
  own realistic concurrency before relying on it as the office's default
  gateway proxy.
- **Certificate pinning breaks under SSL-bump**, regardless of the CA
  being trusted (banking apps, some SaaS clients). Exempt those hosts by
  adding them to `never-intercept-hosts.conf` (see Configuration above).
- The worker pool scales across the cores of one machine. Spreading
  across several machines is still manual, and the SQLite index and cache
  directory are per-machine.
- Buffering is now scoped correctly (`responseheaders` hook, see
  `addon.py`): only responses that look like a real cacheable download get
  buffered in memory; everything else (web pages, images, video, SaaS
  traffic) streams straight through unbuffered, capped at 512 MiB
  (`CACHE_PROXY_MAX_BUFFER_SIZE`) even for cacheable candidates, so a
  multi-GB ISO doesn't get held entirely in RAM. Verified in
  `tests/test_addon_integration.py`.

## 2. Debian/Ubuntu packages (`apt-cacher-ng/`)

Standard apt-cacher-ng, verified against the real package on Ubuntu 24.04.

```bash
sudo apt-get install apt-cacher-ng
sudo cp apt-cacher-ng/zzz_cache-proxy.conf /etc/apt-cacher-ng/
sudo systemctl restart apt-cacher-ng
```

Config files live flat in `/etc/apt-cacher-ng/*.conf` (no `conf.d/`
subdirectory) — apt-cacher-ng loads every `.conf` file it finds there in
filename order; `zzz_` is its own convention for an override file.

On each client, run `apt-cacher-ng/configure-client.sh <proxy-host>[:port]`
(default port 3142) as root. It rewrites both source formats:
- classic `sources.list` / `sources.list.d/*.list` (`deb http://...`)
- deb822 `sources.list.d/*.sources` (`URIs: http://...`) — the default on
  Ubuntu 24.04+; the classic-only version of this script would have
  silently done nothing on a stock 24.04 box, so both are handled.

Status/browse page: `http://<proxy-host>:3142/acng-report.html`.

Verified end-to-end: real `apt-get update` + package install through the
cache against Ubuntu's actual mirror, with `.deb` files landing in
`/var/cache/apt-cacher-ng/`.

## 3. RPM packages (`rpm-cache/`)

Nginx reverse-proxy with `proxy_cache`, for Rocky/AlmaLinux/Fedora mirrors
(add more `location` blocks for others you use).

```bash
sudo apt-get install nginx   # or: dnf install nginx

# Nginx only creates the last level of proxy_cache_path, so the parent
# has to exist first -- otherwise `nginx -t` fails with
# mkdir() "/var/cache/nginx/rpm-cache" failed (2: No such file or directory)
sudo mkdir -p /var/cache/nginx/rpm-cache
sudo chown -R www-data:www-data /var/cache/nginx   # nginx:nginx on RPM distros

# On Debian/Ubuntu, nginx ships a default site on port 80. If cache-proxy
# is on the same host its block page already owns that port, and nginx
# will fail to start with "bind() to 0.0.0.0:80 failed".
sudo rm -f /etc/nginx/sites-enabled/default

sudo cp rpm-cache/nginx-rpm-cache.conf /etc/nginx/conf.d/rpm-cache.conf
sudo nginx -t && sudo systemctl reload nginx
```

Listens on port **8090**. On each client, run
`rpm-cache/configure-client.sh <proxy-host>[:port]` as root — rewrites
`baseurl=` lines in `/etc/yum.repos.d/*.repo` for mirrors listed in both
the nginx config and the script's `MIRRORS` map.

Verified end-to-end against Rocky Linux's live mirror: first request
`X-Cache-Status: MISS`, second `HIT`, real `.rpm`/metadata files landing
in `/var/cache/nginx/rpm-cache/`.

## Testing (Docker, Ubuntu 24.04)

`proxy/` has a pytest suite (`proxy/tests/`) covering the SQLite store,
the web UI (real HTTP requests, not ASGI TestClient — avoids coupling to
a specific starlette/httpx pairing), and a full mitmdump-in-the-middle
integration test. Run it in the real target OS rather than relying on
whatever's installed locally:

```bash
docker compose --profile test build   # the test image is profile-gated: plain
                                      # `build` would leave it stale
docker compose run --rm test          # runs both venvs' test suites
docker compose up -d cache-proxy cache-webui cache-blockpage   # the real services
```

`compose.yml` builds one image (`docker/Dockerfile`, Ubuntu 24.04) with
both venvs, and four services: `cache-proxy`, `cache-webui` and
`cache-blockpage` (sharing a named volume so the UI sees what the proxy
caches and blocks), and `test` (profile-gated, ephemeral cache dir). The suite
is currently 156 proxy-venv tests and 64 web-UI-venv tests, including real
mitmdump runs for the block redirect, the plain 403 and the block-page routing,
and a fake SMTP server for the unblock email.

The demo block page is at `http://localhost:8081/` (the package uses port
80). It shows a template only, with no details or form, unless you arrive with
a valid block token, which only a real blocked request creates.

`cache-webui` serves HTTPS on host port **8443** (container 443) with a
throwaway self-signed cert and demo login `admin` / `cache-proxy-dev` —
dev-only, baked into `compose.yml`, never used by the real `.deb` install
(that generates its own cert and starts with no password until you set
one). `https://localhost:8443/`, click through the cert warning.

## Files

```
docs/
  building.md              # build, install, first-run setup, Docker demo, tests
  configuration.md         # every config.toml setting, host lists, env overrides
  web-interface.md         # tour of the web UI, stats flagging, JSON API
  operations.md            # logs, sizing, security, upgrade, backup, troubleshooting
  proxmox.md               # Proxmox VE: container vs VM, storage, egress, caveats
  images/                  # screenshots used by the docs (demo data)
proxy/
  requirements-proxy.txt   # mitmproxy + pytest
  requirements-webui.txt   # fastapi/uvicorn/jinja2 + pytest
  run_proxy.sh / run_webui.sh / run_blockpage.sh   # read config.toml/host-list files at startup
  cache_proxy/
    config.py              # loads /etc/cache-proxy/config.toml + host-list files
    store.py               # SQLite index, file storage, usage log, quota/expiry,
                           #   hourly stats, per-URL fetch locks
    addon.py               # mitmproxy addon: cache hits/misses, download vs asset
                           #   rules, TTLs, request coalescing, Range/streamed hits
    supervisor.py          # worker pool: scaling, status file, housekeeping jobs
    worker_main.py         # launches one worker with SO_REUSEPORT enabled
    workers.py             # reads the supervisor's status snapshot for the UI
    analytics.py           # hourly per-client series and anomaly detection
    contentfilter.py       # blocking decision: allow-list, hosts, category hash indexes, URL patterns
    filterlists.py         # downloads/refreshes the category lists (CLI: update, status)
    categories.py          # the 61-category catalogue and the saved selection
    mailer.py              # unblock-request and test emails (CLI: --test)
    blockpage/             # the page blocked users see (port 80): app.py, templates/
    webui/
      app.py               # FastAPI app: files, usage, stats, JSON API, actions
      auth.py              # HTTP Basic Auth, PBKDF2 password hashing
      hash_password.py     # CLI: generate a password_hash for config.toml
      templates/           # base + header (shared menu), index (files + worker panel),
                           #   usage, stats, blocks, categories
  systemd/                 # cache-proxy, cache-webui, cache-blockpage services;
                           #   cache-proxy-lists (daily) and -hourly service+timer pairs
  tests/                   # pytest: store, config, auth, webui, addon (unit +
                           #   integration), TTLs, analytics, supervisor, worker
                           #   pool and coalescing end-to-end, content filter, list
                           #   downloader, block page + unblock email, categories page
docker/Dockerfile
compose.yml
packaging/
  build-deb.sh            # builds cache-proxy_<version>_amd64.deb, bundles both venvs
  debian/                 # control, postinst, prerm, postrm, conffiles
  etc/cache-proxy/        # default config.toml, host lists, filter lists/categories, secrets.env
apt-cacher-ng/
  zzz_cache-proxy.conf
  configure-client.sh
rpm-cache/
  nginx-rpm-cache.conf
  configure-client.sh
TODO.md                   # known gaps and next steps
```
