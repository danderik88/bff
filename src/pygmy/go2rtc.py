"""go2rtc on the same host: one stream per printer, RTSP for Home Assistant, WebRTC for the app.

The config is static; BFF adds and removes streams through go2rtc's API at runtime, reads single
frames back for photos and the plate sensor. Every call soft-fails: BFF works as before when
go2rtc is down, only the RTSP and WebRTC cameras go missing.
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("BFF_GO2RTC_API", "http://127.0.0.1:1984")
CALL_TIMEOUT_S = 3
FRAME_TIMEOUT_S = 10
WEBRTC_TIMEOUT_S = 10


def camera_kind(model):
    try:
        from . import onboard  # imported as pygmy.go2rtc (tests)
    except ImportError:
        import onboard  # sibling module, on sys.path once server.py is loaded
    return onboard.camera_kind(model)


# EXIF Orientation 2..8 -> the h264_o<N> templates in deploy/go2rtc.yaml (ffmpeg -vf, then the usual H.264).


def source_for(alias, entry, bff_base, stream_token, orientation=1):
    """What go2rtc reads for this printer: its own RTSP, or BFF's /stream/ MJPEG when it has no RTSP."""
    if camera_kind(entry.get("model")) == "rtsp":
        return f"rtspx://bblp:{entry['access_code']}@{entry['host']}:322/streaming/live/1"
    if not stream_token:
        return None
    # H.264, because Home Assistant's stream player takes no MJPEG. The encoder runs only while someone watches.
    video = f"h264_o{orientation}" if orientation in range(2, 9) else "h264"
    return f"ffmpeg:{bff_base}/stream/{alias}/{stream_token}#video={video}"


def _request(method, endpoint, query, data=None, headers=None, timeout=CALL_TIMEOUT_S):
    url = f"{API}{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _soft(method, endpoint, query):
    """go2rtc is a bonus: log one line and go on. The log never holds the source (it has the access code)."""
    try:
        _request(method, endpoint, query)
    except Exception as err:
        # The config file is mounted read-only on purpose: go2rtc keeps the stream in memory, then fails to save it.
        if isinstance(err, urllib.error.HTTPError) and b"read-only file system" in (err.read() or b""):
            return
        reason = str(err)
        for secret in (query.get("src", ""), urllib.parse.quote(query.get("src", ""), safe=""),
                       urllib.parse.quote_plus(query.get("src", ""))):
            if secret:
                reason = reason.replace(secret, "<src>")
        print(f"go2rtc: {method} {endpoint}: {reason}", file=sys.stderr, flush=True)


# go2rtc saves every PUT into its config file; two PUTs at once left a broken file and every later PUT
# answered 400 (2026-11-15). So: one call at a time, and no PUT while go2rtc already has the same source.
_LOCK = threading.Lock()
_SENT = {}


def add_stream(name, src):
    """PUT is idempotent: the same call updates an existing stream after a go2rtc restart."""
    with _LOCK:
        try:
            have = name in json.loads(_request("GET", "/api/streams", {}) or b"{}")
        except Exception:
            have = False
        if have and _SENT.get(name) == src:
            return
        _soft("PUT", "/api/streams", {"name": name, "src": src})
        _SENT[name] = src


def remove_stream(name):
    _SENT.pop(name, None)
    _soft("DELETE", "/api/streams", {"src": name})


def frame(name):
    """One JPEG of the stream; raises on failure (the feed turns it into CameraError)."""
    return _request("GET", "/api/frame.jpeg", {"src": name}, timeout=FRAME_TIMEOUT_S)


def webrtc_answer(name, offer_sdp):
    """The answer to a browser offer, for live video in the app; raises on failure."""
    return _request("POST", "/api/webrtc", {"src": name}, data=offer_sdp.encode(),
                    headers={"Content-Type": "application/sdp"}, timeout=WEBRTC_TIMEOUT_S).decode()
