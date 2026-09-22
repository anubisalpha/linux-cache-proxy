# Deploying on Proxmox VE

Running `cache-proxy` in a Proxmox container or VM. Everything here was
found by actually deploying it on a Proxmox VE cluster, not reasoned from
the documentation — where a claim is measured, the numbers are given.

For the general install see [building.md](building.md); for every setting
see [configuration.md](configuration.md); for day-to-day running see
[operations.md](operations.md).

- [Container or VM?](#container-or-vm)
- [Creating the container](#creating-the-container)
- [The two settings you must change](#the-two-settings-you-must-change)
- [Storage](#storage)
- [Networking and egress](#networking-and-egress)
- [Firewall](#firewall)
- [Port 80 and the block page](#port-80-and-the-block-page)
- [Proxy settings inside the container](#proxy-settings-inside-the-container)
- [Backup and snapshots](#backup-and-snapshots)
- [Caveats worth knowing before you start](#caveats-worth-knowing-before-you-start)

---

## Container or VM?

An **unprivileged LXC container** works completely and is the lighter
option. Everything the package needs is available in one:

| Requirement | Works unprivileged? |
|---|---|
| `SO_REUSEPORT` worker pool on 8080 | Yes |
| `AmbientCapabilities=CAP_NET_BIND_SERVICE` binding **443** as non-root | Yes |
| The same again binding **80** for the block page | Yes |
| `nftables` / `iptables` inside the container | Yes |
| systemd timers for the block-list refresh | Yes |

**You do not need `nesting=1`.** Proxmox prints

```
WARN: Systemd 255 detected. You may need to enable nesting.
```

when you create an Ubuntu 24.04 container. It is precautionary. With
`features: nesting=0` all three services start, both privileged ports bind
as the non-root `cacheproxy` user, and systemd reaches a clean `running`
state.

Prefer a **VM** if either of these matters to you:

- **The CA private key.** `root` on the Proxmox node can read an
  unprivileged container's filesystem, including
  `/var/lib/cache-proxy/mitmproxy-ca/mitmproxy-ca.pem`. Whoever holds that
  key can impersonate any HTTPS site to every client that trusts it. A VM
  is a real boundary; a container is not.
- **CPU accounting.** See the `max_workers` note below — a VM makes the
  worker pool's view of the machine honest, a container does not.

## Creating the container

```bash
pct create <vmid> local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname cache-proxy \
  --cores 4 --memory 4096 --swap 512 \
  --rootfs <storage>:16 \
  --mp0 <storage>:200,mp=/var/lib/cache-proxy,backup=0 \
  --net0 name=eth0,bridge=<bridge>,ip=<addr>/<cidr>,gw=<gw> \
  --unprivileged 1 --features nesting=0
```

The root disk only holds the OS and `/opt/cache-proxy` (both bundled
venvs, roughly 400 MB installed) — 16 GB is comfortable. Everything that
grows lives on `mp0`.

The container template is minimal: `curl` is **not** installed. Install
`python3-venv`, `dpkg-dev`, `git` and `curl` before building the package,
and do not write a first-run check that depends on `curl` being present.

## The two settings you must change

### `[proxy] max_workers` — never leave it at `0`

`0` means "number of CPUs", resolved with `os.cpu_count()`. In a Proxmox
container that returns the **host's** core count, not the container's
`cores` limit, because Proxmox limits container CPU with a CFS quota
rather than a cpuset.

On a dual-socket host reporting 72 threads, a 4-core container left at
`max_workers = 0` will happily scale to 72 mitmproxy workers. Set it
explicitly:

```toml
[proxy]
min_workers = 2
max_workers = 3      # cores - 1 is a reasonable starting point
```

Per-worker CPU readings come from `/proc/<pid>/stat` and stay accurate;
it is only the *count* that is wrong.

### `[cache] max_size_gb` — set it to fit the volume

The default `0` means unlimited. Set it to roughly 80% of the cache
volume so eviction happens before the filesystem fills.

## Storage

### Give the cache its own volume

Mount a dedicated volume at `/var/lib/cache-proxy` with `backup=0`. Two
reasons, both practical:

- The cache grows to whatever `max_size_gb` allows. On the root disk, a
  misconfigured quota takes the container down with it.
- It is entirely reconstructible. Backing up a few hundred GB of
  re-downloadable installers every night is waste.

The filter lists (~180 MB for four categories) live under the same path,
and are equally reconstructible.

### Local disk or Ceph?

`operations.md` advises fast local storage and specifically says to keep
the SQLite index off a network share. Ceph RBD is network block storage.

In practice a Ceph-backed container works, and no lock contention was
observed at small scale — but if the supervisor starts logging slow
housekeeping or database errors, storage latency is the first thing to
look at, not a software fault. Local NVMe remains the better choice where
you have the space.

Whichever you pick, check what else shares the pool. A ZFS `rpool` on a
Proxmox node typically also holds the node's own root filesystem, so a
runaway cache there can fill the *host's* root, not just the container's.

## Networking and egress

**The machine needs direct outbound access to the internet on TCP 80 and
443.** There is no upstream-proxy setting: `run_proxy.sh` passes no
`--mode upstream:` to mitmdump, so the proxy cannot chain through another
proxy.

This bites on any network that forces web traffic through a corporate
proxy — a common and sensible control, since it stops clients bypassing
inspection. Symptoms: the proxy accepts connections and logs nothing,
requests through it simply time out.

For comparison, of the three components:

| Component | Behind a corporate proxy? |
|---|---|
| `cache-proxy` | **No.** No upstream-proxy support. |
| `apt-cacher-ng` | Yes — add `Proxy: http://host:port` to its config. |
| nginx RPM cache | **No.** `proxy_pass` targets HTTPS mirrors and nginx has no CONNECT client. |

So either give the machine a firewall exception for direct egress, or
restrict yourself to `apt-cacher-ng`.

## Firewall

**Port 8080 has no authentication and binds all interfaces.** It must be
restricted, whatever else you do.

Two options on Proxmox:

**Proxmox cluster firewall** — the native mechanism. Write
`/etc/pve/firewall/<vmid>.fw` and set `firewall=1` on the network device
(note: it is a property of `net0`, not a top-level `pct set --firewall`
flag). This only takes effect if the **datacenter** firewall is enabled,
which requires `/etc/pve/firewall/cluster.fw` with `enable: 1`. On a
cluster that has never used it, switching that on activates host-level
filtering across every node — not a change to make casually, and not one
a single container's deployment should force.

**`ufw` inside the container** — works fine unprivileged, has no effect
outside the container, and disappears when the container does. For an
evaluation or a single guest this is the proportionate choice.

A minimal `ufw` policy:

```bash
ufw default deny incoming
ufw default allow outgoing          # the block-list timers need egress
ufw allow from <admin-net>   to any port 22   proto tcp
ufw allow from <client-net>  to any port 8080 proto tcp
ufw allow from <client-net>  to any port 80   proto tcp   # block page
ufw allow from <admin-net>   to any port 443  proto tcp   # web UI
ufw --force enable
```

Keep SSH open to a whole admin range rather than one address, so a
changed DHCP lease cannot lock you out of a container you are mid-way
through configuring.

## Port 80 and the block page

`cache-blockpage.service` binds port 80. On Debian and Ubuntu, nginx
ships an enabled default site that also binds port 80, so installing
nginx for the RPM cache on the same host makes **nginx fail to start**:

```
nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)
```

Whichever service starts first wins, which after a reboot is a race.
Remove the default site:

```bash
rm -f /etc/nginx/sites-enabled/default
```

## Proxy settings inside the container

If you point other containers at this proxy, note that **`/etc/environment`
is read by PAM at login only**. It is not seen by:

- `pct exec <vmid> -- …` from the Proxmox host
- **systemd services**

So a container service will silently keep going direct. For shells, add
`/etc/profile.d/`; for services, use a systemd drop-in:

```ini
# /etc/systemd/system/<unit>.service.d/proxy.conf
[Service]
Environment="http_proxy=http://<cache>:8080"
Environment="https_proxy=http://<cache>:8080"
```

The same applies to this package's own block-list refresh units if the
machine is behind a corporate proxy — they run as `cacheproxy` under
systemd and inherit nothing from a login shell. A timer that runs, fails
to fetch and leaves stale lists is the worst failure mode here, because
nothing looks broken.

Clients also need the mitmproxy CA in their trust store, and package
traffic is better pointed at `apt-cacher-ng` on 3142 than through the
intercepting proxy on 8080.

## Backup and snapshots

- `backup=0` on the cache mount point, as above.
- Snapshot after building the `.deb` and before installing it. Rolling
  back to a clean pre-install state costs nothing and makes testing the
  purge path free.
- What is worth backing up is small: `/etc/cache-proxy/` and
  `/var/lib/cache-proxy/mitmproxy-ca/`. The CA especially — regenerating
  it means redistributing trust to every client.

## Caveats worth knowing before you start

**Verified working on Proxmox VE, unprivileged Ubuntu 24.04 container:**

- Full test suite in the target OS: 203 passed, 1 skipped (a permissions
  test that cannot run as root).
- All three services active as non-root, with 443 and 80 bound via
  ambient capabilities.
- Both block-list timers running as live systemd units, with the hourly
  one correctly fetching only the sources marked `refresh = "hourly"`.
- Block page rendering through a real SSL-bumped proxy.
- ~5.5M domains across four categories using **75 MB RSS per worker**
  (230 MB total) — the lists use an on-disk index, not an in-memory set.
- Cache hits, `Range` requests and cross-client request coalescing: four
  separate client machines asking for the same uncached file produced one
  upstream fetch and three cache hits, with each client's real IP
  preserved through `SO_REUSEPORT`.

**Things that will catch you out:**

| Caveat | Effect |
|---|---|
| `max_workers = 0` in a container | Pool sizes itself from the *host's* core count |
| No direct egress | Proxy silently times out; there is no upstream-proxy setting |
| nginx default site | Fights the block page for port 80 |
| `/etc/environment` | Invisible to `pct exec` and to systemd services |
| `curl` absent from the template | First-run checks that use it fail confusingly |
| Ubuntu 24.04 Proxmox template | Uses classic `sources.list`, not deb822 — `configure-client.sh` handles both, but do not assume which you have |
| `nesting` warning on create | Precautionary; not required |
| Container `root` is not host `root` | Files owned by uids outside the container's map (e.g. `lost+found` on a mounted volume) cannot be read or removed from inside |

**Not specific to Proxmox but worth repeating here:** certificate pinning
breaks under SSL-bump regardless of the CA being trusted, and anything you
add to `never-intercept-hosts.conf` to fix that is simultaneously a host
the content filter can no longer see.
