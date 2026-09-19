#!/usr/bin/env bash
# Rewrite baseurl= lines in /etc/yum.repos.d/*.repo on an RPM-based client
# to route through the nginx rpm-cache proxy, for mirrors it knows about
# (see nginx-rpm-cache.conf's location blocks).
#
# Usage: sudo ./configure-client.sh <rpm-cache-host>[:port]
set -euo pipefail

PROXY_HOST="${1:?Usage: $0 <rpm-cache-host>[:port]}"
PROXY_PORT="${PROXY_HOST##*:}"
if [ "$PROXY_PORT" = "$PROXY_HOST" ]; then
  PROXY_HOST="${PROXY_HOST}:8090"
fi

# host -> location prefix, must match nginx-rpm-cache.conf
declare -A MIRRORS=(
  [dl.rockylinux.org]=rocky
  [repo.almalinux.org]=almalinux
  [dl.fedoraproject.org]=fedora
)

for repo_file in /etc/yum.repos.d/*.repo; do
  [ -f "$repo_file" ] || continue
  changed=0
  for host in "${!MIRRORS[@]}"; do
    prefix="${MIRRORS[$host]}"
    if grep -q "://${host}/" "$repo_file"; then
      sed -i.bak -E "s#https?://${host}/#http://${PROXY_HOST}/${prefix}/#g" "$repo_file"
      changed=1
    fi
  done
  if [ "$changed" = "1" ]; then
    echo "rewrote $repo_file (backup at ${repo_file}.bak)"
  fi
done

echo "Done. Run 'dnf makecache' (or 'yum makecache') to verify."
echo "Note: only mirrors listed in nginx-rpm-cache.conf get rewritten -- add a"
echo "location block + an entry in this script's MIRRORS map for any others you use."
