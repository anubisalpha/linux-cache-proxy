#!/usr/bin/env bash
# Route a Debian/Ubuntu client's package downloads through apt-cacher-ng.
#
# Handles both source formats:
#  - classic one-line sources.list / sources.list.d/*.list
#      deb http://archive.ubuntu.com/ubuntu noble main
#  - deb822 sources.list.d/*.sources (default on Ubuntu 24.04+)
#      URIs: http://archive.ubuntu.com/ubuntu/
#
# Usage: sudo ./configure-client.sh <apt-cacher-ng-host>[:port]
set -euo pipefail

PROXY_HOST="${1:?Usage: $0 <apt-cacher-ng-host>[:port]}"
PROXY_PORT="${PROXY_HOST##*:}"
if [ "$PROXY_PORT" = "$PROXY_HOST" ]; then
  PROXY_HOST="${PROXY_HOST}:3142"
fi

rewrite_classic() {
  local file="$1"
  [ -f "$file" ] || return 0
  # deb http://archive.ubuntu.com/ubuntu ...  ->  deb http://<proxy>/archive.ubuntu.com/ubuntu ...
  sed -i.bak -E \
    "s#(deb(-src)? )https?://([a-zA-Z0-9.-]+)/#\1http://${PROXY_HOST}/\3/#g" \
    "$file"
  echo "rewrote (classic) $file (backup at ${file}.bak)"
}

rewrite_deb822() {
  local file="$1"
  [ -f "$file" ] || return 0
  # URIs: http://archive.ubuntu.com/ubuntu/  ->  URIs: http://<proxy>/archive.ubuntu.com/ubuntu/
  sed -i.bak -E \
    "s#^(URIs:\s*)https?://([a-zA-Z0-9.-]+)/#\1http://${PROXY_HOST}/\2/#g" \
    "$file"
  echo "rewrote (deb822) $file (backup at ${file}.bak)"
}

rewrite_classic /etc/apt/sources.list
for f in /etc/apt/sources.list.d/*.list; do
  rewrite_classic "$f"
done
for f in /etc/apt/sources.list.d/*.sources; do
  rewrite_deb822 "$f"
done

echo "Done. Run 'apt-get update' to verify. Backups are alongside each rewritten file as *.bak."
