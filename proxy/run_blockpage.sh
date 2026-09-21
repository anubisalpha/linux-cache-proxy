#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export CACHE_PROXY_DIR="${CACHE_PROXY_DIR:-/var/lib/cache-proxy/files}"
export CACHE_PROXY_DB="${CACHE_PROXY_DB:-/var/lib/cache-proxy/index.db}"

PORT="$(python3 -c "from cache_proxy import config; print(config.BLOCKPAGE_PORT)")"

# Plain HTTP on purpose: a blocked HTTPS site's redirect target must load
# without a certificate warning. The page holds nothing secret -- only the
# viewer's own block details, behind an unguessable token.
exec uvicorn cache_proxy.blockpage.app:app --host 0.0.0.0 --port "$PORT"
