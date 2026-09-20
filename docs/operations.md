# Operations

Running, sizing, upgrading, backing up and troubleshooting an installed
`cache-proxy`. For installing it see [building.md](building.md); for every
setting see [configuration.md](configuration.md).

- [Services and logs](#services-and-logs)
- [Checking that caching works](#checking-that-caching-works)
- [Sizing](#sizing)
- [Network and security](#network-and-security)
- [Upgrading](#upgrading)
- [Backup and restore](#backup-and-restore)
- [Troubleshooting](#troubleshooting)
- [Removing it](#removing-it)

---

## Services and logs

Two systemd services, both run as the non-root `cacheproxy` user:

| Service | What it runs | Listens on |
|---|---|---|
| `cache-proxy` | The supervisor and its pool of mitmproxy workers | TCP 8080 |
| `cache-webui` | The web interface | TCP 443 |

```bash
sudo systemctl status cache-proxy cache-webui
sudo systemctl restart cache-proxy cache-webui      # after editing config.toml
journalctl -u cache-proxy -f                          # follow the proxy log
journalctl -u cache-webui -f
```

Both start on boot. If the supervisor process is killed, systemd restarts the
service; stopping the service stops every worker (none are left behind).

**Restarting drops in-flight downloads.** Clients that were mid-download get
a broken connection and have to retry. Restart at a quiet time.

### Reading the proxy log

`journalctl -u cache-proxy` carries two kinds of lines.

Supervisor lines start with `supervisor:`:

| Line | Meaning |
|---|---|
| `started worker pid N (k running)` | A worker was started (at boot, on scale-up, or to replace a crashed one). |
| `worker pid N retired` | A worker was shut down after the pool went idle. Normal. |
| `worker pid N exited unexpectedly (code)` | A worker died and will be replaced. Look at the lines just above it for why. |
| `purged N expired, evicted M over quota` | Hourly housekeeping removed entries. |
| `housekeeping failed: ...` / `could not write status file: ...` | Something went wrong in a background job. The proxy keeps running, but the cause needs fixing (often disk space or permissions). |

Worker lines come from the workers themselves: `HIT <url> <- <client>` when
a file was served from the cache, and `STORED <url> [download|asset]
(<bytes> bytes) <- <client>` when a new one was saved. The workers also print
mitmproxy's normal per-request output, so **the journal is verbose: roughly
one entry per request through the proxy.** On a busy site, check that journald
retention and disk space are set up for that.

---

## Checking that caching works

Every response the proxy handles for a cacheable file carries an
`X-Cache-Proxy` header:

| Value | Meaning |
|---|---|
| `HIT` | Served from the cache. |
| `MISS-STORED` | Fetched from the internet and saved; the next request will be a `HIT`. |
| *(absent)* | The proxy did not treat it as cacheable (see [troubleshooting](#nothing-is-being-cached)). |

From a client machine, fetch the same file twice and look at the header
(this discards the body and prints the headers):

```bash
curl -s -o /dev/null -D - -x http://PROXY:8080 http://example.com/some/installer.msi | grep -i x-cache-proxy
```

The first run should print `MISS-STORED`, the second `HIT`. (Use `-k` too if
you're testing an HTTPS URL from a machine that doesn't have the proxy CA
installed.)

Two things to know about what is cached:

- **Downloads are cached regardless of the origin's cache headers.** A file
  that matches a download extension or content type is stored for
  `download_ttl_days` even if the server said `no-store`. Only the short-lived
  **web assets** honour those headers. Use `never-cache-hosts.conf` for
  anything that must not be stored.
- Only `GET` requests are cached, and only `200` responses.

---

## Sizing

I have not run this against your traffic, so this is guidance based on what I
measured and reasoned, not a guarantee.

**Workers and CPU.** One worker uses about one CPU core, and the pool grows
and shrinks by itself between `min_workers` and `max_workers`. Practical
guidance:

- Give the machine enough cores that `max_workers` can sit above what you
  normally need. More cores than workers do nothing.
- Keep `min_workers = 2` unless you have a reason not to: a spare in case one
  crashes, and headroom for a sudden burst.
- Don't heavily overcommit the virtual machine's CPUs on the hypervisor.
  Scaling decisions rely on each worker's CPU use, and stolen time makes those
  readings unreliable.
- Watch the worker panel on the main page for a week or two. If the pool
  rarely leaves the minimum, you have more than enough. If it regularly hits
  the maximum, add cores rather than raising the minimum.

**Example: a virtual machine with 8 vCPUs and 16 GB of RAM.** Keep
`min_workers = 2`. Set `max_workers = 6` if apt-cacher-ng, the Nginx RPM cache
or anything else shares the machine (it leaves two cores free), or `0` (all
eight) if it only runs the proxy. Keep the default `max_buffer_size_mb`.

**Throughput.** A synthetic worst-case test (100 clients each opening a fresh
TLS connection over and over with no pause, 10-second client timeout) on a VM
with the proxy on 4 cores gave:

| Workers | Throughput | Timed out |
|---|---|---|
| 1 | 11.1 req/s | 55% |
| 2 | 17.4 req/s | 38% |
| 4 | 30.5 req/s | 19% |

Real users pause and reuse connections, so real load from 100 people is much
lighter than that test, but I don't know by how much for your site. I also
only tested up to 8 workers, and that only for correctness (no errors, no
database problems), not for throughput.

**Memory.** Each worker is a mitmproxy process with its own memory, and it
can buffer one download of up to `max_buffer_size_mb` for each download in
progress. See [the note in the configuration reference](configuration.md#memory-and-max_buffer_size_mb).

**Disk.** The cache can grow without limit unless you set `max_size_gb`. Set
it to roughly 80-90% of the volume the cache lives on. Use fast local storage
(SSD or NVMe if you can): with several workers and a good cache hit rate,
disk and network speed become the limits before CPU does. Keep the database
(`db`) on local disk, not a network share.

**Database.** All workers share one SQLite database. I forced 8 workers to
hit it hard with concurrent stores and hits and saw no errors or lock
contention, and integrity checks passed. I have not tested more than 8. If
you run many more workers, watch the log for slowdowns.

---

## Network and security

- **Who can use the proxy.** Port 8080 has **no authentication** and listens
  on all interfaces. Restrict it with a firewall so that only your client
  networks can reach it. (I believe mitmproxy refuses connections from public
  internet addresses by default, but I haven't checked that on this build, so
  don't rely on it; firewall it regardless.)
- **Who can use the web UI.** Port 443 has a single login and no lock-out.
  Restrict it to a management network. See
  [web-interface.md](web-interface.md#security-notes).
- **The proxy certificate authority is the most sensitive thing on the
  machine.** Clients trust it, so whoever holds its private key can
  impersonate any HTTPS site to those clients. It lives in
  `/var/lib/cache-proxy/mitmproxy-ca/` (the private key is
  `mitmproxy-ca.pem`). Keep that directory readable only by `cacheproxy` and
  root, and treat backups of it accordingly.
- **Everything is decrypted.** The proxy sees the content of all HTTPS
  traffic sent through it (except hosts on `never-intercept-hosts.conf`).
  Decide what belongs on that exclusion list (banking, health, anything
  regulated) and tell your users that HTTPS traffic is inspected. Only
  downloads and static assets are ever stored, and only downloads are logged
  per client; other traffic is passed through, but the proxy can technically
  see it.
- **Ports to open:** clients to the proxy on 8080; administrators to the web
  UI on 443. Nothing else needs to be reachable from outside the machine.

Clients also need the proxy CA certificate installed and to be pointed at the
proxy. That's covered in the [README](../README.md#installing-the-package).

---

## Upgrading

Install the new package over the old one:

```bash
sudo apt install ./cache-proxy_NEW_amd64.deb
```

- Both services are restarted automatically (in-flight downloads drop; see
  above).
- Your cache, database, CA and certificates are kept.
- `config.toml` and the two host lists are treated as configuration files. If
  you have edited them, the installer asks whether to keep your version. **Keep
  yours.** Any new setting missing from your file simply takes its built-in
  default, so you don't need to add anything. To see what's new, compare with
  the copy the installer saves next to yours (typically
  `config.toml.dpkg-dist`) or with the [reference](configuration.md).
- An existing database is upgraded in place the first time the new version
  starts. If you are upgrading from a version older than 1.1 (before cache
  lifetimes existed), the entries already cached get the current
  `download_ttl_days` counted from when they were cached.

---

## Backup and restore

What is worth keeping, in order of importance:

| Path | Why | Back up? |
|---|---|---|
| `/var/lib/cache-proxy/mitmproxy-ca/` | The certificate authority your clients trust. **If you lose it, every client needs a new CA installed.** | **Yes.** Protect the copy. |
| `/etc/cache-proxy/` | `config.toml` (which holds the web UI password hash), the two host lists and the web UI TLS certificate and key. | **Yes.** |
| `/var/lib/cache-proxy/index.db` | The usage history and the cache index. | Yes if you want the history. |
| `/var/lib/cache-proxy/files/` | The cached files themselves. | Usually **no**: they are refetched on demand. |
| `/var/lib/cache-proxy/workers.json` | A live status snapshot. | No. |

The database is in use while the proxy runs, so don't just copy the file.
Take a consistent copy with SQLite's backup mechanism, which is safe to run
against a live database:

```bash
sudo -u cacheproxy python3 -c "
import sqlite3
src = sqlite3.connect('/var/lib/cache-proxy/index.db')
dst = sqlite3.connect('/var/lib/cache-proxy/index-backup.db')
src.backup(dst)"
```

then copy `index-backup.db` wherever you keep backups.

**Restoring:**

```bash
sudo systemctl stop cache-proxy cache-webui
# put the files back, then:
sudo chown -R cacheproxy:cacheproxy /var/lib/cache-proxy
sudo systemctl start cache-proxy cache-webui
```

If you restore the database without the cached files, the file list will show
entries whose files are gone. Those are treated as misses and refetched, and
you can delete the stale entries from the web UI. If you restore the files
without the database they are not used (the database is the index), and
they'll be replaced as things are re-cached; clearing the `files/` directory
first avoids wasting disk space.

---

## Troubleshooting

### Nothing is being cached

Check the `X-Cache-Proxy` header as described [above](#checking-that-caching-works).
If it's absent on everything, work through these:

1. **Is the traffic going through the proxy at all?** Look for the request in
   `journalctl -u cache-proxy -f`. If it's not there, the client isn't using
   the proxy (check its proxy setting or WPAD/GPO).
2. **Is the client rejecting the proxy's certificate?** For HTTPS sites the
   proxy CA must be installed on the client. Otherwise the browser shows
   certificate errors, and many tools fail outright.
3. **Does the URL look like a download?** It must end in one of the
   `[cache] extensions`, or the response must have one of the `content_types`.
   A download served as `text/html` (for example a download page rather than
   the file) won't match.
4. **Size.** Downloads under `min_size_mb` (default 1) are ignored. Downloads
   over `max_buffer_size_mb` (default 512) stream through uncached.
5. **Excluded host?** Check `never-cache-hosts.conf` and
   `never-intercept-hosts.conf`.
6. **A resumed or partial request?** A `Range` request for something not yet
   cached goes straight through; only a full `200` response is stored.
7. **For web assets:** the origin may be forbidding it (`no-store`, `private`,
   `no-cache`, a cookie being set, `Vary: Cookie`...). See
   [configuration](configuration.md#webcache). Web assets are also never
   cached if `[webcache] enabled = false`.

### Browsers or tools show certificate errors on every HTTPS site

The proxy CA isn't trusted on that client. Install
`/var/lib/cache-proxy/mitmproxy-ca/mitmproxy-ca-cert.cer` (or the `.pem`)
into the client's trusted root store. The CA is created the first time
`cache-proxy` starts. See the [README](../README.md#installing-the-package)
for the Group Policy steps.

### One application fails but everything else works

It probably pins its certificate, which breaks under interception even when
the CA is trusted. Add its hosts to `never-intercept-hosts.conf` and restart
`cache-proxy`. The traffic then bypasses the proxy's inspection and is not
cached.

### The web page says "Proxy not running / status unavailable"

The web UI can't find a fresh status snapshot from the proxy:

```bash
sudo systemctl status cache-proxy
journalctl -u cache-proxy -n 100 --no-pager
ls -l /var/lib/cache-proxy/workers.json
```

If the service is active but the file is missing or old, check that
`/var/lib/cache-proxy` is writable by `cacheproxy` and that the disk isn't
full; the supervisor logs `could not write status file` in that case. A
freshly started proxy needs 10-15 seconds before the first snapshot appears.

### The web UI rejects my login (401)

- If the message mentions the login not being configured, `[webui] username`
  or `password_hash` is empty in `config.toml`. Set them (see
  [configuration](configuration.md#webui)) and restart `cache-webui`.
- If you did set them, check the hash was pasted whole and inside quotes, and
  that you restarted the service after editing.

### The web UI won't start, or port 443 is refused

```bash
journalctl -u cache-webui -n 50 --no-pager
sudo ss -ltnp | grep -E ':443|:8080'
```

Usual causes: another program already uses 443 (change `[webui] port`), or
the certificate and key named in `tls_cert` / `tls_key` aren't readable by the
`cacheproxy` user.

### The proxy port refuses connections

Check the service is active and something is listening on 8080 (the `ss`
command above). If a firewall sits in between, check it allows clients to
reach 8080. A freshly started service takes a few seconds before workers are
listening.

### Everything shows the same client address

The proxy logs the address that connects to it. If your clients reach the
proxy through NAT or another proxy, that's the address you'll see, and the
per-client statistics can't tell users apart. Fix it at the network level
(let clients reach the proxy directly).

### The pool never shrinks back to the minimum

A worker is only retired when it has no open client connections. If every
worker always has at least one connection, none will be retired. This is safe
but wasteful; it's listed as a known limitation in [TODO.md](../TODO.md).

### The pool sits at the maximum

Sustained high CPU with every worker busy means you need more CPU. Add
cores and raise `max_workers` (or set it to `0`). Also check that something
isn't hammering the proxy (see the [stats page](web-interface.md#stats-stats)).

### The disk is full

Set `max_size_gb` (see [configuration](configuration.md#cache)) so the cache
evicts old entries by itself, and restart. To free space now, use **Purge
expired now** or delete large entries from the web UI. A full disk also stops
the database and status file from being written.

### "database is locked" in the log

Writers wait up to 30 seconds for the lock before giving up, so seeing this
means something is holding it far longer than normal. The usual cause is
storing `index.db` on a network file system. Keep it on local disk.

---

## Removing it

```bash
sudo apt remove cache-proxy     # stops and disables the services; keeps config and cached data
sudo apt purge cache-proxy      # also deletes the cache, database, CA, web UI certificate and the cacheproxy user
```

**`purge` deletes the certificate authority.** If you might reinstall and want
clients to keep trusting the same CA, back up
`/var/lib/cache-proxy/mitmproxy-ca/` first (see [above](#backup-and-restore)).
Remember to remove the proxy setting and the CA certificate from clients when
you retire the proxy.
