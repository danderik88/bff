#!/usr/bin/env python3
"""Record a printer's MQTT reports as JSONL, one message per line.

Usage: BFF_A1_ACCESS_CODE=... record_mqtt.py HOST SERIAL OUTFILE SECONDS
Keep OUTFILE outside the repo: it holds identifiers until sanitized.
"""
import json
import os
import ssl
import sys
import time

import paho.mqtt.client as mqtt


def record(host, serial, outfile, seconds):
    state = {"messages": 0, "disconnects": 0, "asked": False, "refused": None}
    report, request = f"device/{serial}/report", f"device/{serial}/request"

    def on_connect(client, _userdata, _flags, reason, _props):
        if reason.is_failure:
            state["refused"] = str(reason)
            return
        client.subscribe(report)
        if not state["asked"]:  # One full report only, even if the link drops.
            state["asked"] = True
            client.publish(request, json.dumps({"pushing": {"command": "pushall", "sequence_id": "0"}}))

    def on_disconnect(_client, _userdata, _flags, _reason, _props):
        state["disconnects"] += 1

    with open(outfile, "x") as out:  # "x": never overwrite an earlier recording.
        def on_message(_client, _userdata, msg):
            try:
                message = json.loads(msg.payload)
            except ValueError:
                return
            out.write(json.dumps({"t": round(time.time(), 3), "message": message}) + "\n")
            out.flush()
            state["messages"] += 1

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set("bblp", os.environ["BFF_A1_ACCESS_CODE"])
        # The printer's certificate is self-signed; only this one-off tool skips the check.
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.connect(host, 8883)
        client.loop_start()
        time.sleep(seconds)
        client.loop_stop()
        client.disconnect()
    return state


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    result = record(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]))
    print(f"messages={result['messages']} disconnects={result['disconnects']} refused={result['refused']}")
    sys.exit(0 if result["messages"] else 1)
