#!/usr/bin/env bash
# Install BFF + slicerd as systemd --user services on one host (no root, no container).
# Run from a checkout on any machine:  deploy/user/install.sh user@host
# Orca runs from the flatpak (the AppImage needs glibc 2.38); it needs a real $DISPLAY on the host.
# Secrets: ~/bff/env/*.env (0600), generated once and never overwritten. printers.json is NOT created
# here; add printers from the app, or copy a store file to ~/bff/state/printers.json.
set -euo pipefail
PATH="$HOME/.local/bin:$PATH"  # uv; transient systemd units start with a minimal PATH
HOST="${1:?usage: install.sh user@host}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ORCA_VERSION=2.5.0
SSH=(ssh -o BatchMode=yes "$HOST")

# Dependencies built here for the target's Python (3.10, manylinux): the host needs no venv/pip.
LIB="$(mktemp -d)"
uv pip install -q --target "$LIB" --python-version 3.10 --python-platform x86_64-manylinux_2_28 -r "$ROOT/deploy/requirements.txt"
"${SSH[@]}" 'mkdir -p ~/bff/{src,lib,state,env,slicer,bin}'
rsync -a --delete "$LIB/" "$HOST:bff/lib/"
rsync -a --delete --exclude __pycache__ "$ROOT/src/pygmy/" "$HOST:bff/src/pygmy/"
rsync -a --exclude __pycache__ "$ROOT/tools/slicer/" "$HOST:bff/slicer/"
rsync -a "$ROOT/profiles/" "$HOST:bff/profiles/"
rm -rf "$LIB"

"${SSH[@]}" ORCA_VERSION=$ORCA_VERSION bash -s <<'REMOTE'
set -euo pipefail
PATH="$HOME/.local/bin:$PATH"  # uv; transient systemd units start with a minimal PATH
cd ~/bff
IP="$(tailscale ip -4 | head -1)"
# Orca flatpak (user install) + wrapper that slicerd uses as ORCA_BIN.
flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak --user list --app | grep -q com.orcaslicer.OrcaSlicer || {
  curl -fsSL -o /tmp/orca.flatpak "https://github.com/SoftFever/OrcaSlicer/releases/download/v$ORCA_VERSION/OrcaSlicer-Linux-flatpak_V${ORCA_VERSION}_x86_64.flatpak"
  flatpak --user install -y --noninteractive --bundle /tmp/orca.flatpak; rm -f /tmp/orca.flatpak; }
cat > bin/orca <<W
#!/bin/sh
export DISPLAY=\${DISPLAY:-:0}
exec flatpak --user run --filesystem=host --filesystem=/tmp com.orcaslicer.OrcaSlicer "\$@"
W
chmod +x bin/orca
# Official profiles of the same tag, flattened (same step as deploy/slicer/provision.sh).
grep -q "Sovol SV08" slicer/profiles/index.json 2>/dev/null || { rm -rf /tmp/orca-src && git clone -q --depth 1 --branch "v$ORCA_VERSION" --filter=blob:none --sparse https://github.com/SoftFever/OrcaSlicer.git /tmp/orca-src \
  && (cd /tmp/orca-src && git sparse-checkout set --no-cone /resources/profiles/BBL/ /resources/profiles/BBL.json /resources/profiles/Sovol/ /resources/profiles/Sovol.json /resources/profiles/OrcaFilamentLibrary/ /resources/profiles/OrcaFilamentLibrary.json \
  && python3 ~/bff/slicer/flatten_profiles.py resources/profiles ~/bff/slicer/profiles) && rm -rf /tmp/orca-src; }
gen() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
umask 077
[ -f env/slicer.env ] || { S=$(gen); printf 'SLICER_SECRET=%s\n' "$S" > env/slicer.env; echo "$S" > env/.slicer_secret; }
SECRET=$(sed -n 's/^SLICER_SECRET=//p' env/slicer.env)
[ -f env/bff.env ] || cat > env/bff.env <<W
BFF_MCP_TOKENS=admin=$(gen)
BFF_MCP_BIND=$IP
BFF_MCP_PORT=8765
BFF_MCP_CONTROL=1
BFF_MCP_PROFILES=$HOME/bff/profiles
BFF_FILES_DIR=$HOME/bff/state/files
BFF_SLICER_URL=http://127.0.0.1:8775
BFF_SLICER_SECRET=$SECRET
PYTHONPATH=$HOME/bff/lib
STATE_DIRECTORY=$HOME/bff/state
W
rm -f env/.slicer_secret
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/bff-slicer.service <<W
[Unit]
Description=BFF slicer (Orca flatpak behind slicerd)
[Service]
EnvironmentFile=%h/bff/env/slicer.env
Environment=SLICER_PROFILES=%h/bff/slicer/profiles ORCA_BIN=%h/bff/bin/orca SLICER_PORT=8775 SLICER_BIND=127.0.0.1
ExecStart=/usr/bin/python3 %h/bff/slicer/slicerd.py
Restart=on-failure
MemoryMax=3500M
[Install]
WantedBy=default.target
W
cat > ~/.config/systemd/user/bff.service <<W
[Unit]
Description=BFF server
After=network-online.target bff-slicer.service
[Service]
EnvironmentFile=%h/bff/env/bff.env
ExecStart=/usr/bin/python3 %h/bff/src/pygmy/server.py
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
W
systemctl --user daemon-reload
systemctl --user enable bff-slicer.service bff.service
systemctl --user restart bff-slicer.service bff.service  # restart: a redeploy must pick up new code and units
loginctl enable-linger "$USER" 2>/dev/null || echo "WARN: linger not enabled; services stop at logout (sudo loginctl enable-linger $USER)"
sleep 3; systemctl --user is-active bff-slicer bff; curl -fsS http://127.0.0.1:8775/health; echo
REMOTE
