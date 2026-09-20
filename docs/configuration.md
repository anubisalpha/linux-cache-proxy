# Configuration reference

All settings live in `/etc/cache-proxy/config.toml`, plus two host-list files
in the same directory. Every key is optional: anything you leave out (or any
key added by a newer version that your older file doesn't contain) falls back
to the built-in default listed below, so you can trim the file down to only
what you've changed.

**Changes need a restart:** `sudo systemctl restart cache-proxy cache-webui`.
There is no live reload.

The shipped `config.toml` has a comment above every setting; this page adds
defaults, units and the reasoning for choosing values.

- [`[cache]`](#cache)
- [`[webcache]`](#webcache)
- [`[proxy]`](#proxy-worker-pool)
- [`[analytics]`](#analytics)
- [`[webui]`](#webui)
- [Host exclusion lists](#host-exclusion-lists)
- [Settings that are not in `config.toml`](#settings-that-are-not-in-configtoml)
- [Environment variable overrides](#environment-variable-overrides)

---

## `[cache]`

What gets cached as a **download** (installers, packages, archives, disk
images) and for how long.

| Key | Default | Meaning |
|---|---|---|
| `dir` | `/var/lib/cache-proxy/files` | Where cached files are stored, one file per URL, named by a hash of the URL plus the original extension. Put this on the biggest, fastest volume you have. |
| `db` | `/var/lib/cache-proxy/index.db` | SQLite index and usage log. Keep it on local disk (not a network share): several worker processes write to it. |
| `min_size_mb` | `1` | Downloads smaller than this are not cached (favicons, redirects, small API responses that happen to have a matching content type). Applies to downloads only; static assets have their own limit. |
| `max_buffer_size_mb` | `512` | The largest single download the proxy will hold in memory in order to cache it. Bigger files stream straight through **uncached**. See [memory](#memory-and-max_buffer_size_mb). |
| `download_ttl_days` | `30` | How long a cached download is served before it is fetched from the origin again. After this the entry counts as a miss, the file is refetched and the clock restarts. Expired files are also purged hourly. |
| `coalesce_wait_seconds` | `60` | When several clients request the same not-yet-cached file at the same moment, the first fetches it and the others wait up to this many seconds, then are served from the cache. `0` turns this off, so every one of those requests goes to the origin. |
| `max_size_gb` | `0` | Cap on the total cache size (downloads and web assets together). `0` means unlimited. Once exceeded, the least recently used entries are evicted first. Set it to roughly 80-90% of the volume so there is room for the database and for the file being written when eviction runs. |
| `extensions` | see file | URL-path endings (case-insensitive, query string ignored) that mark a response as a download: `.exe .msi .msix .msixbundle .cab .appx .deb .rpm .tar.gz .tgz .tar.xz .tar.bz2 .zip .iso .img .dmg .pkg .apk`. |
| `content_types` | see file | Fallback for URLs with no recognisable extension (for example `/download?id=123`), matched against the response `Content-Type`. Includes `application/octet-stream`, so be aware that any octet-stream response of at least `min_size_mb` will be cached. |

**Origin cache headers are not consulted for downloads.** A response that
matches a download extension or content type is stored for
`download_ttl_days` even if the server marked it `no-store`. Only web assets
(see `[webcache]`) honour those headers. To keep a site out of the cache
entirely, use `never-cache-hosts.conf`.

### Memory and `max_buffer_size_mb`

To cache a download the proxy has to hold it in memory before writing it to
disk. Each worker can buffer one file per in-flight download, so the worst
case is roughly `max_buffer_size_mb` x the number of downloads in progress
(x workers, since each worker has its own). With the default 512 MB and 6
workers all buffering a large file at once that is about 3 GB. Lower this if
memory is tight; files above the limit are not cached, they just pass
through.

Cache *hits* are different: large hits (over 8 MiB) and any request with a
`Range` header are streamed from disk, not loaded into memory, so serving a
big ISO does not cost RAM.

---

## `[webcache]`

A short-lived cache for **static web assets**: scripts, stylesheets, images
and fonts. HTML pages are deliberately never cached.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Set `false` to cache downloads only. |
| `ttl_minutes` | `60` | The **maximum** time an asset is served from cache. It is a cap, never a floor; see below. |
| `max_size_mb` | `5` | Assets larger than this are not cached. Assets have no minimum size. |
| `extensions` | `.js .mjs .css .png .jpg .jpeg .gif .webp .svg .ico .woff .woff2 .ttf .otf` | URL endings treated as static assets. |
| `content_types` | see file | Content types treated as static assets when the URL has no matching extension. |

**Origin headers are always respected.** A response is *not* stored if:

- its `Cache-Control` contains `no-store`, `private` or `no-cache` (or
  `max-age=0`), or it has `Pragma: no-cache`;
- it sets a cookie (`Set-Cookie`);
- it varies on anything other than `Accept-Encoding` (`Vary: Cookie`,
  `Vary: User-Agent`, ...);
- the request carried an `Authorization` header or a `Range` header.

If the origin sends a `max-age` (or `s-maxage`, which wins) shorter than
`ttl_minutes`, the shorter value is used. A longer one is capped at
`ttl_minutes`.

A request `Cookie` header does **not** prevent caching; browsers send cookies
on most static-asset requests, so ignoring them would defeat the cache. The
response-side rules above are the safeguard.

Asset hits are counted (hits, stored, bytes saved; shown on the main page,
and per file in the file list) but are **not** written to the usage log, so
ordinary browsing does not drown out the download statistics and anomaly
detection. To keep this cheap the counts are tallied in memory and written
to the database in batches about every ten seconds, so they can lag briefly,
and a hit in the last few seconds before a restart may not be recorded. Asset
hits also count when choosing what to evict once the cache is over its size
limit, so assets in active use are kept.

---

## `[proxy]` (worker pool)

One mitmproxy process uses about one CPU core, so the proxy runs a pool of
them on port 8080 and grows and shrinks it with load. See the [README](../README.md)
for how it works and [operations](operations.md#sizing) for sizing advice.

| Key | Default | Meaning |
|---|---|---|
| `min_workers` | `2` | Workers always running. At least 1. Two gives you a spare if one crashes and headroom for a sudden burst. |
| `max_workers` | `0` | Upper limit. `0` means "the number of CPUs the machine has". If set lower than `min_workers` it is raised to match. |
| `scale_up_cpu_pct` | `70` | Add a worker when the pool's **average** CPU (100 = one full core per worker) stays at or above this for two consecutive samples. |
| `scale_down_cpu_pct` | `20` | The pool counts as idle when average CPU is below this. |
| `scale_down_idle_seconds` | `300` | Retire a worker after the pool has been idle this long. It also has to be true that the remaining workers would stay below `scale_up_cpu_pct`, so the pool doesn't flap. |
| `sample_interval_seconds` | `5` | How often CPU is sampled and the status file is written. |
| `status_file` | `/var/lib/cache-proxy/workers.json` | Snapshot the web UI reads to show the worker panel. |

Things worth knowing:

- A worker is only retired when it has **no open client connections**,
  because stopping a worker drops its connections and a long download uses
  almost no CPU. If every worker always has a connection, the pool will not
  shrink back to `min_workers`.
- A worker that is still starting up (importing mitmproxy is CPU-heavy) is
  shown as `starting` and ignored when deciding whether to scale.
- Crashed workers are replaced immediately, up to `min_workers`.
- The proxy's listening **port** is not set here; see
  [Settings that are not in `config.toml`](#settings-that-are-not-in-configtoml).

---

## `[analytics]`

Controls per-client usage flagging (`/stats`, `/api/hourly-stats`) and how
long the usage log is kept.

| Key | Default | Meaning |
|---|---|---|
| `anomaly_factor` | `3.0` | A client is flagged when its current hour is more than this many times its own average, on any of bytes, requests or new downloads. |
| `min_history_hours` | `6` | A client needs at least this many earlier hours of history before it can be flagged, so a newcomer doesn't trip an alert on its first hour. |
| `lookback_hours` | `168` | How far back the stats page and JSON API look by default (168 = one week). This is also the window used to compute each client's average. |
| `retention_days` | `90` | Usage-log rows older than this are deleted daily. `0` keeps everything forever. Shorter retention also keeps the stats queries fast. |

"Usage" here means what the usage log records: cache **hits** and newly
**stored downloads**. It is not a record of all browsing. See
[web interface](web-interface.md#stats-stats) for how to read the flags.

---

## `[webui]`

| Key | Default | Meaning |
|---|---|---|
| `port` | `443` | HTTPS port for the web UI. The service runs as a non-root user and is granted just the capability to bind low ports, so 443 works without root. |
| `username` | `""` | Login name. |
| `password_hash` | `""` | PBKDF2 hash of the password (never the password itself). |
| `tls_cert` | `/etc/cache-proxy/webui-cert.pem` | Certificate the web UI serves. |
| `tls_key` | `/etc/cache-proxy/webui-key.pem` | Its private key. |

**The UI is locked until you set a login.** If `username` or `password_hash`
is empty, every request gets a 401. It fails closed rather than serving
unauthenticated.

Set the password:

```bash
sudo /opt/cache-proxy/venv-webui/bin/python3 -m cache_proxy.webui.hash_password
```

It prompts twice and prints a `password_hash = "pbkdf2_sha256$..."` line.
Paste that line into `[webui]` and set `username`. `config.toml` is made
readable only by root and the `cacheproxy` group because it holds this hash.

**TLS certificate.** On install a self-signed certificate is generated
(10-year validity, `CN` = the machine's hostname), so browsers warn on first
visit. To use a certificate from your own CA, put the cert and key somewhere
readable by the `cacheproxy` user and point `tls_cert` / `tls_key` at them.
Nothing else changes. The key file should not be world-readable.

---

## Host exclusion lists

Two plain-text files next to `config.toml`. Format for both: one hostname
pattern per line, `#` starts a comment, blank lines ignored. `example.com`
matches the bare domain **and** every subdomain; a leading `*.` is accepted
but not needed.

| File | Effect |
|---|---|
| `never-cache-hosts.conf` | Traffic is still proxied and TLS-intercepted, but nothing from these hosts is ever written to the cache. Use for internal sites, or anything where you don't want copies on disk. |
| `never-intercept-hosts.conf` | These hosts **bypass TLS interception entirely**: the proxy just tunnels the encrypted connection. Use for apps that pin their certificate (they break under interception, even with the CA trusted), and for anything sensitive such as banking. This implies never-cache for the same host. |

Example:

```
# never-intercept-hosts.conf
bank.example.com
*.mybank.co.uk
some-pinned-app.vendor.com
```

The interception list is applied when the proxy starts (it is turned into
mitmproxy `ignore_hosts` rules by `run_proxy.sh`), and the cache list is
checked on every response. Both are re-read on restart only.

Symptoms that a host needs to go on `never-intercept-hosts.conf`: an app or
site that works without the proxy but fails with a certificate error even
though the proxy CA is installed on the client.

---

## Settings that are not in `config.toml`

**The proxy's listening port (8080)** is set by the systemd unit, not the
config file, because the load balancing between workers depends on it. To
change it:

```bash
sudo systemctl edit cache-proxy
```

and add:

```ini
[Service]
Environment=CACHE_PROXY_PORT=3128
```

then `sudo systemctl restart cache-proxy`. Point clients at the new port.

**Fixed in the code** (not configurable): cache hits above 8 MiB, and every
`Range` request, are streamed from disk; the cache index each worker holds is
refreshed every 10 seconds; expired-entry purging and quota eviction run
hourly in the supervisor.

---

## Environment variable overrides

Each of these overrides the matching `config.toml` value. They exist mainly
for the test suite and the Docker demo, which use throwaway directories; on a
real install use `config.toml`, or a `systemctl edit` drop-in as above.

| Variable | Overrides |
|---|---|
| `CACHE_PROXY_CONFIG` | Path of the config file itself (default `/etc/cache-proxy/config.toml`). The two host-list files are looked up in the same directory. |
| `CACHE_PROXY_NEVER_CACHE_FILE`, `CACHE_PROXY_NEVER_INTERCEPT_FILE` | Paths of the two host-list files. |
| `CACHE_PROXY_DIR`, `CACHE_PROXY_DB` | `[cache] dir`, `db` |
| `CACHE_PROXY_MIN_SIZE` | `[cache] min_size_mb`, **in bytes** |
| `CACHE_PROXY_MAX_BUFFER_SIZE` | `[cache] max_buffer_size_mb`, **in bytes** |
| `CACHE_PROXY_DOWNLOAD_TTL_DAYS`, `CACHE_PROXY_MAX_SIZE_GB`, `CACHE_PROXY_COALESCE_WAIT` | the matching `[cache]` keys |
| `CACHE_PROXY_WEBCACHE_TTL_MINUTES` | `[webcache] ttl_minutes` |
| `CACHE_PROXY_MIN_WORKERS`, `CACHE_PROXY_MAX_WORKERS`, `CACHE_PROXY_SAMPLE_INTERVAL`, `CACHE_PROXY_SCALE_DOWN_IDLE_SECONDS`, `CACHE_PROXY_STATUS_FILE` | the matching `[proxy]` keys |
| `CACHE_PROXY_WEBUI_PORT`, `CACHE_PROXY_WEBUI_USERNAME`, `CACHE_PROXY_WEBUI_PASSWORD_HASH`, `CACHE_PROXY_WEBUI_TLS_CERT`, `CACHE_PROXY_WEBUI_TLS_KEY` | the matching `[webui]` keys |
| `CACHE_PROXY_PORT` | The proxy listening port (default 8080). Environment only. |
| `CACHE_PROXY_CONFDIR` | Where mitmproxy keeps its CA (default `/var/lib/cache-proxy/mitmproxy-ca`). |
| `CACHE_PROXY_REFRESH_INTERVAL` | Seconds between each worker refreshing its view of the cache (default 10). |

Not every `config.toml` key has an environment override (for example the
scaling thresholds, `[webcache]` lists and `[analytics]` are file-only).
