#!/usr/bin/env bash
# Remote access and notifications for Pygmy through Tailscale. Run it where Pygmy is installed, as root:
#   ./tailscale.sh
# It installs Tailscale if needed, asks you to sign in, switches Pygmy to the Tailscale network and
# publishes it on a secure https address. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo ./tailscale.sh)." >&2; exit 1; }

if ! command -v tailscale >/dev/null; then
  echo "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "Sign in to Tailscale: open the link below (use the same account as on your phone)."
  tailscale up
fi
IP=$(tailscale ip -4 | head -1)
[ -n "$IP" ] || { echo "Tailscale gave no address. Check 'tailscale status'." >&2; exit 1; }

sed -i "s/^PYGMY_MCP_BIND=.*/PYGMY_MCP_BIND=$IP/" .env
./install.sh >/dev/null
echo "Publishing Pygmy on a secure address..."
echo "(If a link appears, open it, press the button to switch on HTTPS, and wait here.)"
tailscale serve --bg "http://$IP:8765" >/dev/null
URL=$(tailscale serve status | grep -m1 -o 'https://[^ ]*') || true
[ -n "$URL" ] || { echo "Could not read the address. Run 'tailscale serve status'." >&2; exit 1; }
echo
echo "Done. On your phone install Tailscale, sign in, then open:"
echo "$URL/app#k=$(grep '^PYGMY_MCP_TOKENS=' .env | cut -d= -f3)"
echo "From now on use this address everywhere, also at home."
