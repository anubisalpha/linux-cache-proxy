#!/usr/bin/env bash
# Build cache-proxy_<version>_amd64.deb -- run on Ubuntu 24.04 (or the
# Docker image via docker/Dockerfile, which has the same toolchain).
# Bundles both venvs into the package so installing it needs no internet
# access and no pip resolution on the target machine.
set -euo pipefail

VERSION="${1:-1.0.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
BUILD_DIR="$(mktemp -d)"
PKG_ROOT="$BUILD_DIR/pkgroot"

trap 'rm -rf "$BUILD_DIR"' EXIT

echo "Building cache-proxy ${VERSION} in ${BUILD_DIR}"

mkdir -p "$PKG_ROOT/opt/cache-proxy" \
         "$PKG_ROOT/etc/cache-proxy" \
         "$PKG_ROOT/etc/systemd/system" \
         "$PKG_ROOT/usr/share/doc/cache-proxy" \
         "$PKG_ROOT/DEBIAN"

# --- application code ---
cp -r "$REPO_ROOT/proxy/cache_proxy" "$PKG_ROOT/opt/cache-proxy/"
find "$PKG_ROOT/opt/cache-proxy/cache_proxy" -name "__pycache__" -type d -exec rm -rf {} +
find "$PKG_ROOT/opt/cache-proxy/cache_proxy" -type d -exec chmod 755 {} +
find "$PKG_ROOT/opt/cache-proxy/cache_proxy" -type f -exec chmod 644 {} +

cp "$REPO_ROOT/proxy/run_proxy.sh" "$REPO_ROOT/proxy/run_webui.sh" "$PKG_ROOT/opt/cache-proxy/"
chmod 755 "$PKG_ROOT/opt/cache-proxy/run_proxy.sh" "$PKG_ROOT/opt/cache-proxy/run_webui.sh"

# --- bundled venvs (built now, shipped in the package -- no pip on target) ---
python3 -m venv "$PKG_ROOT/opt/cache-proxy/venv-proxy"
"$PKG_ROOT/opt/cache-proxy/venv-proxy/bin/pip" install --no-cache-dir -q \
  -r "$REPO_ROOT/proxy/requirements-proxy.txt"

python3 -m venv "$PKG_ROOT/opt/cache-proxy/venv-webui"
"$PKG_ROOT/opt/cache-proxy/venv-webui/bin/pip" install --no-cache-dir -q \
  -r "$REPO_ROOT/proxy/requirements-webui.txt"

# pytest is a test-time dependency (requirements-*.txt include it so the
# same files work for `docker compose run test`) -- no reason to ship it.
"$PKG_ROOT/opt/cache-proxy/venv-proxy/bin/pip" uninstall -y -q pytest || true
"$PKG_ROOT/opt/cache-proxy/venv-webui/bin/pip" uninstall -y -q pytest || true

# Make cache_proxy importable from either venv regardless of cwd (needed
# for e.g. `python3 -m cache_proxy.webui.hash_password` run ad-hoc by an
# admin, not just from run_proxy.sh/run_webui.sh which cd there first).
for venv in venv-proxy venv-webui; do
  site_packages="$("$PKG_ROOT/opt/cache-proxy/$venv/bin/python3" -c \
    "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
  echo "/opt/cache-proxy" > "$site_packages/cache-proxy.pth"
done

# pip bakes the venv's build-time absolute path into each console
# script's shebang (#!<staging-dir>/venv-x/bin/python3). Since this venv
# is built in a temp staging dir but always installed at
# /opt/cache-proxy, rewrite shebangs to the real install path -- without
# this, every entry point (mitmdump, uvicorn, ...) fails with "required
# file not found" once installed.
for venv in venv-proxy venv-webui; do
  bindir="$PKG_ROOT/opt/cache-proxy/$venv/bin"
  target="/opt/cache-proxy/$venv/bin/python3"
  for f in "$bindir"/*; do
    [ -f "$f" ] || continue
    if head -c2 "$f" | grep -q "^#!"; then
      first_line="$(head -1 "$f")"
      case "$first_line" in
        "#!"*/bin/python3*)
          sed -i "1s|^#!.*|#!${target}|" "$f"
          ;;
      esac
    fi
  done
done

# --- config, systemd units, docs ---
cp "$REPO_ROOT/packaging/etc/cache-proxy/"* "$PKG_ROOT/etc/cache-proxy/"
chmod 644 "$PKG_ROOT/etc/cache-proxy/"*

cp "$REPO_ROOT/proxy/systemd/cache-proxy.service" "$REPO_ROOT/proxy/systemd/cache-webui.service" \
  "$PKG_ROOT/etc/systemd/system/"
chmod 644 "$PKG_ROOT/etc/systemd/system/"*.service

cp "$REPO_ROOT/README.md" "$PKG_ROOT/usr/share/doc/cache-proxy/"
chmod 644 "$PKG_ROOT/usr/share/doc/cache-proxy/README.md"

# --- DEBIAN control files ---
sed "s/VERSION_PLACEHOLDER/${VERSION}/" "$REPO_ROOT/packaging/debian/control" > "$PKG_ROOT/DEBIAN/control"
cp "$REPO_ROOT/packaging/debian/conffiles" "$PKG_ROOT/DEBIAN/"
for script in postinst prerm postrm; do
  cp "$REPO_ROOT/packaging/debian/$script" "$PKG_ROOT/DEBIAN/$script"
  chmod 755 "$PKG_ROOT/DEBIAN/$script"
done

OUT="$REPO_ROOT/cache-proxy_${VERSION}_amd64.deb"
dpkg-deb --root-owner-group --build "$PKG_ROOT" "$OUT"

echo "Built: $OUT"
