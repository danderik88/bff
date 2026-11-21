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
  curl -fsSL https://tailscale.com/install.sh | LC_ALL=C.UTF-8 sh >/var/log/pygmy-tailscale.log 2>&1 \
    || { echo "Could not install Tailscale, see /var/log/pygmy-tailscale.log" >&2; exit 1; }
fi
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "Sign in to Tailscale: open the link below (use the same account as on your phone)."
  tailscale up
fi

# Pygmy keeps answering on your home network, so links and devices you already have keep working.
# (An earlier version of this script moved it onto the Tailscale address: put it back.)
BIND=$(sed -n 's/^PYGMY_MCP_BIND=//p' .env)
case "$BIND" in 100.*|"")
  BIND=$(ip route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
  [ -n "$BIND" ] || { echo "Could not find this machine's home address." >&2; exit 1; }
  sed -i "s/^PYGMY_MCP_BIND=.*/PYGMY_MCP_BIND=$BIND/" .env
  ./install.sh >/dev/null ;;
esac
echo "Publishing Pygmy on a secure address..."
echo "(If a link appears, open it, press the button to switch on HTTPS, and wait here.)"
tailscale serve --bg "http://$BIND:8765" >/dev/null
URL=$(tailscale serve status | grep -m1 -o 'https://[^ ]*') || true
[ -n "$URL" ] || { echo "Could not read the address. Run 'tailscale serve status'." >&2; exit 1; }
KEY=$(grep '^PYGMY_MCP_TOKENS=' .env | cut -d= -f3)
echo
echo "Done. This address never changes and works at home and away:"
echo "  $URL"
echo "On each phone: install the Tailscale app, sign in with the same account (keep it on),"
echo "then open this link once to pair the phone:"
echo "  $URL/app#k=$KEY"
echo "Your old home link keeps working as a backup."
