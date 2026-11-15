#!/usr/bin/env bash
# Create and provision the BFF slicer container on a Proxmox host.
# Run ON the Proxmox host, from a folder holding this script, bff-slicer.service,
# slicerd.py and flatten_profiles.py:
#   bash provision.sh <ctid> <ip/cidr> <gateway> <bff_ct_ip> [bff_ctid]
# Idempotent per step: re-running skips what is already there.
# The secret is generated in the slicer CT and, when bff_ctid is given, copied into
# that CT's /etc/bff-mcp.env (BFF_SLICER_URL, BFF_SLICER_SECRET). It is never printed.
set -euo pipefail

CTID=${1:?ctid}; IPCIDR=${2:?ip/cidr}; GW=${3:?gateway}; BFF_IP=${4:?bff ct ip}; BFF_CTID=${5:-}
ORCA_VERSION=2.5.0
ORCA_URL="https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage"
TEMPLATE=local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
HERE=$(cd "$(dirname "$0")" && pwd)
IP=${IPCIDR%/*}
in_ct() { pct exec "$CTID" -- bash -c "$1"; }

if ! pct status "$CTID" >/dev/null 2>&1; then
  pct create "$CTID" "$TEMPLATE" --hostname bff-slicer --cores 4 --memory 4096 --swap 1024 \
    --rootfs local-lvm:12 --net0 "name=eth0,bridge=vmbr0,ip=$IPCIDR,gw=$GW" \
    --unprivileged 1 --features nesting=1 --onboot 1
fi
pct start "$CTID" 2>/dev/null || true
sleep 5

in_ct "export DEBIAN_FRONTEND=noninteractive; apt-get -qq update && apt-get -qq -y install \
  curl ca-certificates git python3 nftables libmspack0t64 libgtk-3-0t64 libwebkit2gtk-4.1-0 libgl1 libegl1 \
  libglu1-mesa libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libsoup-3.0-0 libosmesa6 >/dev/null"

# OrcaSlicer, pinned, AppImage extracted (no FUSE in an LXC).
in_ct "test -x /opt/orca/AppRun || (cd /opt && curl -fsSL -o orca.AppImage '$ORCA_URL' && chmod +x orca.AppImage \
  && ./orca.AppImage --appimage-extract >/dev/null && rm -rf orca && mv squashfs-root orca && rm -f orca.AppImage)"
in_ct "test \$(ldd /opt/orca/bin/orca-slicer | grep -c 'not found') -eq 0"
in_ct "chmod -R a+rX /opt/orca"  # the AppImage extracts as root-only; the service runs as user slicer

# Service code + official profiles of the same tag, flattened.
in_ct "mkdir -p /opt/slicer && id slicer >/dev/null 2>&1 || useradd -r -m -d /home/slicer -s /usr/sbin/nologin slicer"
pct push "$CTID" "$HERE/slicerd.py" /opt/slicer/slicerd.py
pct push "$CTID" "$HERE/flatten_profiles.py" /opt/slicer/flatten_profiles.py
in_ct "chmod -R a+rX /opt/slicer"
in_ct "rm -rf /tmp/orca-src && git clone -q --depth 1 --branch v$ORCA_VERSION --filter=blob:none --sparse \
  https://github.com/SoftFever/OrcaSlicer.git /tmp/orca-src && cd /tmp/orca-src \
  && git sparse-checkout set --no-cone /resources/profiles/BBL/ /resources/profiles/BBL.json /resources/profiles/Sovol/ /resources/profiles/Sovol.json /resources/profiles/OrcaFilamentLibrary/ /resources/profiles/OrcaFilamentLibrary.json \
  && rm -rf /opt/slicer/profiles && python3 /opt/slicer/flatten_profiles.py resources/profiles /opt/slicer/profiles \
  && rm -rf /tmp/orca-src"

# Secret (generated once, 0600) and unit.
in_ct "test -s /etc/bff-slicer.env || (umask 077; printf 'SLICER_SECRET=%s\n' \$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') > /etc/bff-slicer.env)"
pct push "$CTID" "$HERE/bff-slicer.service" /etc/systemd/system/bff-slicer.service
in_ct "systemctl daemon-reload && systemctl enable -q --now bff-slicer && systemctl restart bff-slicer"

# Firewall: only BFF reaches the slicer port; no other inbound service.
in_ct "cat > /etc/nftables.conf <<EOF
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ip protocol icmp accept
    ip saddr $BFF_IP tcp dport 8765 accept
  }
}
EOF
systemctl enable -q --now nftables && nft -f /etc/nftables.conf"

# Hand the URL and secret to BFF without printing the secret.
if [ -n "$BFF_CTID" ]; then
  SECRET=$(pct exec "$CTID" -- sed -n 's/^SLICER_SECRET=//p' /etc/bff-slicer.env)
  pct exec "$BFF_CTID" -- bash -c "cp -f /etc/bff-mcp.env /etc/bff-mcp.env.pre-slicer; \
    sed -i '/^BFF_SLICER_URL=/d;/^BFF_SLICER_SECRET=/d' /etc/bff-mcp.env; \
    printf 'BFF_SLICER_URL=http://%s:8765\nBFF_SLICER_SECRET=%s\n' '$IP' '$SECRET' >> /etc/bff-mcp.env"
  unset SECRET
fi

sleep 2
in_ct "curl -fsS http://127.0.0.1:8765/health"; echo
