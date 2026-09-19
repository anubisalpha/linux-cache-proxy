#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export CACHE_PROXY_DIR="${CACHE_PROXY_DIR:-/var/lib/cache-proxy/files}"
export CACHE_PROXY_DB="${CACHE_PROXY_DB:-/var/lib/cache-proxy/index.db}"

read -r PORT TLS_CERT TLS_KEY < <(python3 -c "
from cache_proxy import config
print(config.WEBUI_PORT, config.WEBUI_TLS_CERT, config.WEBUI_TLS_KEY)
")

exec uvicorn cache_proxy.webui.app:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --ssl-certfile "$TLS_CERT" \
  --ssl-keyfile "$TLS_KEY"
