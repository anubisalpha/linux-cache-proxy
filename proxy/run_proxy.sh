#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export CACHE_PROXY_DIR="${CACHE_PROXY_DIR:-/var/lib/cache-proxy/files}"
export CACHE_PROXY_DB="${CACHE_PROXY_DB:-/var/lib/cache-proxy/index.db}"

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

exec mitmdump \
  --listen-port "${CACHE_PROXY_PORT:-8080}" \
  --set confdir="${CACHE_PROXY_CONFDIR:-/var/lib/cache-proxy/mitmproxy-ca}" \
  "${IGNORE_HOSTS_ARGS[@]}" \
  -s cache_proxy/addon.py
