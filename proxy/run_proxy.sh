#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export CACHE_PROXY_DIR="${CACHE_PROXY_DIR:-/var/lib/cache-proxy/files}"
export CACHE_PROXY_DB="${CACHE_PROXY_DB:-/var/lib/cache-proxy/index.db}"
export CACHE_PROXY_VENDOR_CA_DIR="${CACHE_PROXY_VENDOR_CA_DIR:-/var/lib/cache-proxy/vendor-cas}"

# Extra CAs vendorcas.py has discovered and trusted for upstream TLS
# verification (Windows Update's own PKI, etc.) -- kept up to date by the
# cache-proxy-vendor-cas timer. Must exist before mitmproxy starts, even
# empty, or ssl_verify_upstream_trusted_confdir has nothing to point at.
mkdir -p "$CACHE_PROXY_VENDOR_CA_DIR"

# mitmproxy's net_tls.create_proxy_server_context only falls back to
# certifi's bundle when BOTH ssl_verify_upstream_trusted_ca and
# ssl_verify_upstream_trusted_confdir are unset -- setting either one on
# its own REPLACES the default trust store rather than adding to it.
# Passing certifi's own cacert.pem as the CA *file* alongside our confdir
# as the CA *path* loads both (OpenSSL's load_verify_locations accepts a
# file and a dir together), so upstream verification keeps trusting the
# public web PKI and gains the extra vendor CAs, instead of trusting only
# the two hosts vendorcas.py has seeded so far.
CERTIFI_BUNDLE="$(python3 -c 'import certifi; print(certifi.where())')"

# never-intercept-hosts.conf entries need to become mitmproxy ignore_hosts
# regexes (repeated --set flags -- it's a sequence option). Reuse
# config.py's own parsing/conversion rather than duplicating it in bash.
IGNORE_HOSTS_ARGS=()
while IFS= read -r regex; do
  [ -n "$regex" ] && IGNORE_HOSTS_ARGS+=(--set "ignore_hosts=$regex")
done < <(python3 -c "
from cache_proxy import config
for h in config.NEVER_INTERCEPT_HOSTS:
    print(config.host_to_regex(h))
")

# The supervisor runs a pool of mitmdump workers sharing the listen port
# (min/max in config.toml [proxy]); everything below is passed to each one.
exec python3 -m cache_proxy.supervisor \
  --listen-port "${CACHE_PROXY_PORT:-8080}" \
  --set confdir="${CACHE_PROXY_CONFDIR:-/var/lib/cache-proxy/mitmproxy-ca}" \
  --set ssl_verify_upstream_trusted_ca="$CERTIFI_BUNDLE" \
  --set ssl_verify_upstream_trusted_confdir="$CACHE_PROXY_VENDOR_CA_DIR" \
  "${IGNORE_HOSTS_ARGS[@]}" \
  -s cache_proxy/addon.py
