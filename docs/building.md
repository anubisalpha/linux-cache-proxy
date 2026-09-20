# Building and installing

How to build the `cache-proxy` package, install it, and check that it works.
Once it is running, see [operations.md](operations.md); for settings see
[configuration.md](configuration.md).

- [What you need](#what-you-need)
- [Building the package](#building-the-package)
- [Installing](#installing)
- [Checking it works](#checking-it-works)
- [What gets installed where](#what-gets-installed-where)
- [Trying it in Docker (no install)](#trying-it-in-docker-no-install)
- [Running the tests](#running-the-tests)

---

## What you need

| | |
|---|---|
| **Target machine** | Ubuntu 24.04 (or another system with Python 3.12 or newer and systemd), 64-bit x86 (`amd64`). The package depends on `python3 (>= 3.12)`, `openssl` and `adduser`. |
| **Build machine** | The same OS and architecture as the target. The package bundles its Python environments, including compiled libraries, so build it on the kind of system you'll install it on. |
| **Build tools** | `git`, `python3`, `python3-venv`, `python3-pip`, plus internet access during the build (it downloads the Python dependencies once, and bundles them so the *target* needs no internet or pip). |
| **Time and space** | A few minutes, and the finished package is about 42 MB. |

A build machine can be a spare Linux box, a virtual machine, or a throwaway
Docker container (see below).

---

## Building the package

### On Ubuntu 24.04

```bash
sudo apt install git python3 python3-venv python3-pip
git clone https://github.com/anubisalpha/linux-cache-proxy
cd linux-cache-proxy
bash packaging/build-deb.sh 1.1.1
```

The argument is the version number to stamp on the package (default `1.0.0`
if you leave it off). The result is written to the repository root as
`cache-proxy_<version>_amd64.deb`.

### Using Docker (from Windows, macOS or any machine with Docker)

This builds inside a clean Ubuntu 24.04 container and leaves the package in a
`dist` folder next to where you run it:

```bash
mkdir -p dist
docker run --rm -v "$PWD/dist:/out" ubuntu:24.04 bash -c '
  apt-get update -qq &&
  apt-get install -y -qq git python3 python3-venv python3-pip ca-certificates &&
  git clone https://github.com/anubisalpha/linux-cache-proxy /tmp/build &&
  cd /tmp/build &&
  bash packaging/build-deb.sh 1.1.1 &&
  cp cache-proxy_*.deb /out/'
```

I tested this from Git Bash on Windows and it produced a working package.
(In Git Bash on Windows, if a path you pass into a container gets mangled,
set `MSYS_NO_PATHCONV=1` first.)

### Notes on building

- **Build from a fresh clone.** The repository forces Unix (LF) line endings
  for scripts and config so they work on Linux. If you build from an old
  Windows working copy that was checked out with Windows (CRLF) line endings,
  the shell scripts inside the package can break. A fresh `git clone` inside
  Linux, or inside the Docker container as above, avoids the problem.
- **What the script does.** It copies the application code, creates two
  separate Python virtual environments (one for the proxy, one for the web UI;
  their dependencies conflict, so they can't share one), installs the
  requirements into them, fixes the environments' paths so they work from
  their installed location, adds the default configuration and the systemd unit
  files, and runs `dpkg-deb`.
- **Test-only dependencies (`pytest`) are removed** from the bundled
  environments before packaging.

---

## Installing

Copy the package to the target machine, then:

```bash
sudo apt install ./cache-proxy_1.1.1_amd64.deb
```

This creates a `cacheproxy` system user and `/var/lib/cache-proxy`, generates
a self-signed certificate for the web UI, and installs and **enables** both
services but does **not start** them. That is deliberate: once running, the
proxy intercepts HTTPS traffic and the web UI has no password yet, so it
waits for you to finish setup.

### 1. Set a web UI login

```bash
sudo /opt/cache-proxy/venv-webui/bin/python3 -m cache_proxy.webui.hash_password
```

Type a password twice. It prints a `password_hash = "..."` line. Edit
`/etc/cache-proxy/config.toml`, paste that line under `[webui]`, and set
`username` on the line above it:

```toml
[webui]
username = "admin"
password_hash = "pbkdf2_sha256$600000$..."
```

### 2. Review the host lists

Look at `/etc/cache-proxy/never-cache-hosts.conf` and
`/etc/cache-proxy/never-intercept-hosts.conf`. Anything sensitive (banking,
health, applications that pin their certificates) belongs on the second list.
See [configuration](configuration.md#host-exclusion-lists).

### 3. Adjust the settings you care about

The defaults are reasonable. The ones most sites want to look at are the
cache size cap (`[cache] max_size_gb`, unlimited by default) and the worker
counts (`[proxy] min_workers`, `max_workers`). See the
[configuration reference](configuration.md) and
[sizing advice](operations.md#sizing).

### 4. Start it

```bash
sudo systemctl start cache-proxy cache-webui
```

### 5. Point clients at it

Nothing is cached until clients use the proxy, and HTTPS only works once they
trust its certificate authority.

1. **Install the CA certificate on every client.** The proxy creates it the
   first time it starts: `/var/lib/cache-proxy/mitmproxy-ca/mitmproxy-ca-cert.cer`
   (a `.pem` copy is next to it). On Windows, deploy it through Group Policy
   (Computer Configuration → Policies → Windows Settings → Security Settings →
   Public Key Policies → Trusted Root Certification Authorities). Without it,
   clients get certificate errors on every HTTPS site.
2. **Set the proxy on clients** to `<server-address>:8080`, by Group Policy
   (Computer/User Configuration → Preferences → Control Panel Settings →
   Internet Settings), by WPAD, or on each application.
3. **Open the network ports:** clients to the proxy on TCP 8080, and
   administrators to the web UI on TCP 443. Port 8080 has no
   authentication, so limit it to your client networks (see
   [network and security](operations.md#network-and-security)).

For the Debian/Ubuntu package cache and the RPM cache, which are separate
components with their own client setup, see the [README](../README.md).

---

## Checking it works

```bash
# Both services running?
sudo systemctl status cache-proxy cache-webui

# Web UI answering, and how many workers are running (replace admin:PASSWORD;
# -k accepts the self-signed certificate)
curl -k -u admin:PASSWORD https://localhost/api/workers

# Proxy caching? Fetch a large file twice; the second run should say HIT.
curl -s -o /dev/null -D - -x http://localhost:8080 http://SOME-SERVER/some/installer.msi | grep -i x-cache-proxy
```

Then open `https://<server-address>/` in a browser, sign in, and look at the
worker panel and the file list. Files appear there once something has been
downloaded through the proxy.

---

## What gets installed where

| Path | Contents |
|---|---|
| `/opt/cache-proxy/` | The application code, the launch scripts and the two bundled Python environments (`venv-proxy`, `venv-webui`). Replaced on every upgrade; don't edit. |
| `/etc/cache-proxy/` | `config.toml`, the two host lists, and the web UI certificate and key. These are your settings and survive upgrades. |
| `/etc/systemd/system/` | `cache-proxy.service` and `cache-webui.service`. |
| `/var/lib/cache-proxy/` | All data: `files/` (the cache), `index.db` (index and usage log), `mitmproxy-ca/` (the certificate authority) and `workers.json` (live status). |
| `/usr/share/doc/cache-proxy/` | A copy of the README. |

`apt remove` stops and disables the services and leaves settings and data.
`apt purge` deletes settings, data, the certificate authority and the
`cacheproxy` user too. See [operations](operations.md#removing-it).

---

## Trying it in Docker (no install)

To look at it without installing anything, or to develop against it, the
repository has a Docker setup on Ubuntu 24.04:

```bash
docker compose build
docker compose up -d cache-proxy cache-webui
```

- The proxy listens on `localhost:8080`.
- The web UI is at `https://localhost:8443/` (accept the self-signed
  certificate warning). The login is `admin` / `cache-proxy-dev`. That demo
  login is baked into `compose.yml` for this setup only and is never used by the
  real package.
- The services share a named volume, so the UI sees what the proxy caches.

This is a demo for trying things out, not a production deployment.

---

## Running the tests

```bash
docker compose build
docker compose run --rm test
```

This runs the full suite in the Ubuntu 24.04 image against a throwaway
cache: the proxy's own tests (which include real end-to-end runs with actual
mitmproxy processes and a worker pool) and then the web UI's tests. It takes
a few minutes. Everything should pass.
