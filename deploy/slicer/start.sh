#!/bin/sh
# Slicer container start: a per-install machine id (Orca keys its saved Orca Cloud login to /etc/machine-id,
# so it must survive image rebuilds), the virtual display Orca draws with, then slicerd.
set -eu
DATA=/home/slicer/orca-gui
mkdir -p "$DATA"
[ -s "$DATA/.machine-id" ] || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$DATA/.machine-id"
cat "$DATA/.machine-id" > /etc/machine-id
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
exec python3 /opt/slicer/slicerd.py
