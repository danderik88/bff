#!/bin/sh
# One-time Orca Cloud login on the slicer server. Run as root on the slicer host (screen or ssh).
# Starts the always-on OrcaSlicer (orca-gui.service), shows its window over VNC until you
# disconnect, then closes VNC. The login is kept in an encrypted file in the Orca data dir
# (use_encrypted_token_file: a server has no keyring), bound to this machine and user.
set -eu
DATA=/home/slicer/orca-gui
command -v openbox >/dev/null || apt-get install -y x11vnc openbox firefox-esr xdg-utils locales
install -d -o slicer -g slicer -m 700 "$DATA"
systemctl stop orca-gui 2>/dev/null || true  # Orca rewrites its config on exit: edit it only while stopped
python3 - "$DATA/OrcaSlicer.conf" <<'EOF'
import json, sys
path = sys.argv[1]
try:
    text = open(path).read()
    conf = json.loads(text[:text.rfind("}") + 1])  # drop Orca's trailing "# MD5 checksum" line
except (OSError, ValueError):
    conf = {}
conf.setdefault("app", {})["use_encrypted_token_file"] = "true"
open(path, "w").write(json.dumps(conf, indent=4) + "\n")
EOF
chown slicer:slicer "$DATA/OrcaSlicer.conf"
systemctl enable --now orca-gui
PASS=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')
echo
echo "Da un computer in casa apri  vnc://$(hostname -I | cut -d' ' -f1):5900  password $PASS"
echo "Nella finestra di OrcaSlicer fai il login Orca Cloud (lo stesso account di Orca sul tuo computer)."
# The CT firewall lets in only Pygmy: open VNC to the home LAN just for this login.
NET=$(ip -4 route show scope link | awk '{print $1; exit}')
nft add rule inet filter input ip saddr "$NET" tcp dport 5900 accept comment orca-login 2>/dev/null || true
# openbox: without a window manager Xvfb gives no keyboard focus. -forever: macOS Screen Sharing connects twice.
su slicer -s /bin/sh -c "DISPLAY=:99 setsid openbox >/dev/null 2>&1 &"
x11vnc -display :99 -rfbport 5900 -passwd "$PASS" -forever -shared -quiet -bg -o /dev/null
printf "Premi Invio qui quando il login e' fatto... "; read _
pkill -x x11vnc || true; pkill -x openbox || true
for h in $(nft -a list chain inet filter input 2>/dev/null | awk '/orca-login/ {print $NF}'); do
  nft delete rule inet filter input handle "$h"
done
ls "$DATA"/user/*/sync_state >/dev/null 2>&1 \
  && echo "Login fatto: Pygmy ricevera' i profili Orca entro 10 minuti." \
  || echo "Login non ancora visibile: i profili arrivano dopo il primo sync di Orca (rilancia lo script se serve)."
