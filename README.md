# linux-cache-proxy

A native-Linux caching setup for reducing repeated software-update and
installer downloads across a workplace network, with a web UI to browse,
search, and delete cached files, and per-client usage logging.

Three parts, each solving a different traffic type:

| Traffic | Tool | Why |
|---|---|---|
| General installers/downloads (HTTP or HTTPS) | `proxy/` — custom mitmproxy addon | Squid's cache store is opaque; this stores real, browsable files |
| Debian/Ubuntu packages (`.deb`) | `apt-cacher-ng/` | Purpose-built, understands apt repo metadata |
| RPM packages (`.rpm`, Rocky/Alma/Fedora) | `rpm-cache/` — Nginx `proxy_cache` | apt-cacher-ng is apt-only; Nginx caching reverse-proxy is the standard equivalent for yum/dnf |

**Not covered:** Windows Update. WU is almost entirely HTTPS with its own
CDN behavior; the correct tool is WSUS, not a generic proxy. Skipped for
now per project decision — revisit if it becomes a priority.

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
- **Web UI** (`cache_proxy/webui/`), FastAPI on port 443 (HTTPS,
  password-protected — see below):
  - `/` — cached files, search, download, delete
  - `/usage` — request/hit-rate summary, top clients by bytes served,
    most-requested files, recent activity log (filterable by client IP,
    windowed by 1/7/30 days or all-time)

### Installing the package

Everything under `proxy/` ships as a single `.deb`, built with both
Python venvs bundled in (no internet access or pip resolution needed on
the target machine):

```bash
bash packaging/build-deb.sh 1.0.0          # run on Ubuntu 24.04 (or via docker/Dockerfile)
sudo apt install ./cache-proxy_1.0.0_amd64.deb
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

| File | Contents |
|---|---|
| `config.toml` | Cache dir/db paths, size limits, cacheable extensions/content-types, web UI port/username/password_hash/TLS cert paths |
| `never-cache-hosts.conf` | Hosts (+ subdomains) proxied normally but never written to the cache |
| `never-intercept-hosts.conf` | Hosts (+ subdomains) that bypass TLS interception entirely — mitmproxy tunnels them raw. Use for cert-pinned apps and anything sensitive (banking, etc). Implies never-cache for the same host |

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

- No automatic quota/eviction — delete stale entries via the web UI.
- **Not tuned for full-office "everything goes through this" duty.**
  mitmproxy is single-process (asyncio, not multi-worker like Squid/Nginx)
  and SSL-bump adds a double TLS handshake to every HTTPS connection —
  fine for the download-caching job this was built for, but a real
  throughput ceiling if you route *all* general browsing through it too.
  Load-test with realistic concurrent user counts before relying on it as
  the office's default gateway proxy.
- **Certificate pinning breaks under SSL-bump**, regardless of the CA
  being trusted (banking apps, some SaaS clients). Exempt those hosts by
  adding them to `never-intercept-hosts.conf` (see Configuration above).
- No built-in horizontal scaling. If one box isn't enough, you're running
  multiple instances by hand, and the SQLite-backed index doesn't cleanly
  support that without extra work.
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
docker compose build
docker compose run --rm test      # runs both venvs' test suites
docker compose up -d cache-proxy cache-webui   # run the real services
```

`compose.yml` builds one image (`docker/Dockerfile`, Ubuntu 24.04) with
both venvs, and three services: `cache-proxy`, `cache-webui` (sharing a
named volume so the UI can see what the proxy caches), and `test`
(profile-gated, ephemeral cache dir).

`cache-webui` serves HTTPS on host port **8443** (container 443) with a
throwaway self-signed cert and demo login `admin` / `cache-proxy-dev` —
dev-only, baked into `compose.yml`, never used by the real `.deb` install
(that generates its own cert and starts with no password until you set
one). `https://localhost:8443/`, click through the cert warning.

## Files

```
proxy/
  requirements-proxy.txt   # mitmproxy + pytest
  requirements-webui.txt   # fastapi/uvicorn/jinja2 + pytest
  run_proxy.sh / run_webui.sh   # read config.toml/host-list files at startup
  cache_proxy/
    config.py              # loads /etc/cache-proxy/config.toml + host-list files
    store.py                # SQLite index + on-disk file storage + access log
    addon.py                 # mitmproxy addon (cache, exclusion lists, streaming)
    webui/
      app.py                  # FastAPI app: files list, usage, download, delete
      auth.py                  # HTTP Basic Auth, PBKDF2 password hashing
      hash_password.py          # CLI: generate a password_hash for config.toml
      templates/ (base/index/usage.html)
  systemd/cache-proxy.service, cache-webui.service
  tests/                   # pytest: store, config, auth, webui, addon (unit + integration)
docker/Dockerfile
compose.yml
packaging/
  build-deb.sh            # builds cache-proxy_<version>_amd64.deb, bundles both venvs
  debian/                 # control, postinst, prerm, postrm, conffiles
  etc/cache-proxy/        # default config.toml + host-list files shipped in the package
apt-cacher-ng/
  zzz_cache-proxy.conf
  configure-client.sh
rpm-cache/
  nginx-rpm-cache.conf
  configure-client.sh
```
