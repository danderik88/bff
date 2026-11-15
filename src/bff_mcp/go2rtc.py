"""go2rtc on the same host: one stream per printer, RTSP for Home Assistant, WebRTC for the app.

The config is static; BFF adds and removes streams through go2rtc's API at runtime, reads single
frames back for photos and the plate sensor. Every call soft-fails: BFF works as before when
go2rtc is down, only the RTSP and WebRTC cameras go missing.
"""
import hashlib
import hmac
import os
import sys
import urllib.parse
import urllib.request

API = os.environ.get("BFF_GO2RTC_API", "http://127.0.0.1:1984")
CALL_TIMEOUT_S = 3
FRAME_TIMEOUT_S = 10
WEBRTC_TIMEOUT_S = 10


def camera_kind(model):
    try:
        from . import onboard  # imported as bff_mcp.go2rtc (tests)
    except ImportError:
        import onboard  # sibling module, on sys.path once server.py is loaded
    return onboard.camera_kind(model)


def source_for(alias, entry, bff_base, stream_token):
    """What go2rtc reads for this printer: its own RTSP, or BFF's /stream/ MJPEG when it has no RTSP."""
    if camera_kind(entry.get("model")) == "rtsp":
        return f"rtspx://bblp:{entry['access_code']}@{entry['host']}:322/streaming/live/1"
    if not stream_token:
        return None
    # H.264, because Home Assistant's stream player takes no MJPEG. The encoder runs only while someone watches.
    return f"ffmpeg:{bff_base}/stream/{alias}/{stream_token}#video=h264"


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
        reason = str(err)
        for secret in (query.get("src", ""), urllib.parse.quote(query.get("src", ""), safe=""),
                       urllib.parse.quote_plus(query.get("src", ""))):
            if secret:
                reason = reason.replace(secret, "<src>")
        print(f"go2rtc: {method} {endpoint}: {reason}", file=sys.stderr, flush=True)


def add_stream(name, src):
    """PUT is idempotent: the same call updates an existing stream after a go2rtc restart."""
    _soft("PUT", "/api/streams", {"name": name, "src": src})


def remove_stream(name):
    _soft("DELETE", "/api/streams", {"src": name})


def frame(name):
    """One JPEG of the stream; raises on failure (the feed turns it into CameraError)."""
    return _request("GET", "/api/frame.jpeg", {"src": name}, timeout=FRAME_TIMEOUT_S)


def webrtc_answer(name, offer_sdp):
    """The answer to a browser offer, for live video in the app; raises on failure."""
    return _request("POST", "/api/webrtc", {"src": name}, data=offer_sdp.encode(),
                    headers={"Content-Type": "application/sdp"}, timeout=WEBRTC_TIMEOUT_S).decode()
