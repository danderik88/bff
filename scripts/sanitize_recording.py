#!/usr/bin/env python3
"""Strip identifiers from an MQTT recording so it can live in the repo.

Usage: sanitize_recording.py IN OUT SERIAL
"""
import json
import sys

PLACEHOLDER_SERIAL = "03919A000000000"
BLANKED_KEYS = {"sn", "tag_uid", "tray_uuid", "ip", "ssid", "net"}


def blank(value):
    if isinstance(value, dict):
        return {k: None if k in BLANKED_KEYS else blank(v) for k, v in value.items()}
    if isinstance(value, list):
        return [blank(v) for v in value]
    return value


def sanitize_line(line, serial):
    row = json.loads(line.replace(serial, PLACEHOLDER_SERIAL))
    return json.dumps(blank(row["message"]))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    with open(sys.argv[1]) as src, open(sys.argv[2], "w") as dst:
        for raw in src:
            dst.write(sanitize_line(raw, sys.argv[3]) + "\n")
