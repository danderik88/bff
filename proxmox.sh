#!/usr/bin/env bash
# Pygmy on Proxmox VE: creates a Debian LXC container with Docker and installs Pygmy in it.
# Run on the Proxmox host as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/danderik88/pygmy/main/proxmox.sh)"
# Optional: CTID=120 STORAGE=local-lvm BRIDGE=vmbr0 before the command.
set -euo pipefail

command -v pct >/dev/null || { echo "Run this on a Proxmox VE host." >&2; exit 1; }
CTID=${CTID:-$(pvesh get /cluster/nextid)}
STORAGE=${STORAGE:-$(pvesm status -content rootdir | awk 'NR==2 {print $1}')}
BRIDGE=${BRIDGE:-vmbr0}

pveam update >/dev/null
TEMPLATE=$(pveam available --section system | awk -v a="_$(dpkg --print-architecture).tar" '/debian-13-standard/ && index($2, a) {print $2}' | sort -V | tail -1)
[ -n "$TEMPLATE" ] || { echo "No Debian 13 template found (pveam available)." >&2; exit 1; }
pveam list local | grep -q "$TEMPLATE" || pveam download local "$TEMPLATE"

echo "Creating container $CTID (Debian 13, 2 cores, 4 GB, 16 GB on $STORAGE)..."
pct create "$CTID" "local:vztmpl/$TEMPLATE" --hostname pygmy --cores 2 --memory 4096 --swap 1024 \
  --rootfs "$STORAGE:16" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" --unprivileged 1 \
  --features nesting=1,keyctl=1 --dev0 /dev/net/tun --onboot 1 --description "Pygmy: https://github.com/danderik88/pygmy"
pct start "$CTID"

for _ in $(seq 30); do  # wait for DHCP
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') && [ -n "$IP" ] && break
  sleep 2
done
[ -n "${IP:-}" ] || { echo "Container $CTID got no IP address (DHCP on $BRIDGE?)." >&2; exit 1; }

echo "Installing Docker and Pygmy (a few minutes)..."
pct exec "$CTID" -- bash -c '
  set -e
  apt-get update -qq && apt-get install -y -qq git curl ca-certificates >/dev/null
  curl -fsSL https://get.docker.com | sh >/dev/null
  git clone -q https://github.com/danderik88/pygmy /opt/pygmy
  cd /opt/pygmy && ./install.sh
'
echo "Update later with: pct exec $CTID -- bash -c 'cd /opt/pygmy && git pull && ./install.sh'"
