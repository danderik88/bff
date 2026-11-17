#!/bin/sh
# Pygmy one-command installer: write .env, build the containers, print the link.
# Usage: ./install.sh [this-machine-ip]
set -eu

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with the compose plugin is required: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# An existing .env is the user's: never touch it.
if [ ! -f .env ]; then
  if [ "$#" -ge 1 ] && [ -n "$1" ]; then
    ip=$1
  else
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  fi
  if [ -z "$ip" ]; then
    echo "Could not find this machine's address. Run: ./install.sh 192.168.1.50" >&2
    exit 1
  fi
  cp .env.example .env
  sed -i.bak "s/CHANGE_ME_IP/$ip/" .env && rm -f .env.bak
  for k in TOKEN SLICER RTSP; do
    sed -i.bak "s/CHANGE_ME_$k/$(rand_hex)/" .env && rm -f .env.bak
  done
  echo "Wrote .env with this machine's address ($ip)."
else
  echo "Using the existing .env."
fi

# OrcaSlicer has no Linux ARM build: no Print now there, everything else works.
case "$(uname -m)" in
  x86_64)
    docker compose up -d --build
    ;;
  *)
    docker compose up -d --build bff go2rtc
    echo "Print now is off on ARM: OrcaSlicer has no Linux ARM build. Everything else works."
    ;;
esac

echo "Open on your phone: http://$(grep '^BFF_MCP_BIND=' .env | cut -d= -f2):8765/app#k=$(grep '^BFF_MCP_TOKENS=' .env | cut -d= -f3)"
