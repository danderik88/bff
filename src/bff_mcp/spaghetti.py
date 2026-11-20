"""Spaghetti check through the Obico ML API already running on the LAN (CT 108).

The API is GET-only: /p/?img=<URL> fetches the photo itself, with a short read timeout.
So BFF keeps the frame under a one-shot nonce and hands Obico a URL on its LAN
listener (/obico/<nonce>.jpg) that returns the bytes at once.

The ML API only says where it sees spaghetti and how sure it is. The verdict is Pygmy's own
(written from scratch, no Obico code): each frame counts its most confident detection, minus a
per-printer floor that hides a steady false positive; the last minute of frames then votes.
It only reports; it never pauses.
"""
import json
import secrets
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

WINDOW = 6          # frames that vote: one minute at the default 10 s interval
WARMUP_FRAMES = 18  # first 3 minutes of a print: purge lines and the first layer look messy
# (votes needed, height above the floor). ponytail: tuned on the A1/P2S/E3K cameras; adjust per camera if needed.
WARN, FAIL = (3, 0.30), (4, 0.50)
SENSITIVITY = {"low": 1.3, "medium": 1.0, "high": 0.75}
# The floor learns a steady false positive (a cable the model likes) by creeping up slowly, and drops at once
# to any lower reading. Real spaghetti grows far faster than FLOOR_CREEP, and the cap keeps a print that is
# already failing when Pygmy starts from ever being learned as "normal".
FLOOR_CREEP, FLOOR_CAP = 0.002, 0.25
FRAME_TTL_S = 30.0


class Floor:
    """Per printer, outlives the print."""

    def __init__(self):
        self.level = None

    def update(self, p):
        self.level = p if self.level is None or p < self.level else min(self.level + FLOOR_CREEP, p, FLOOR_CAP)
        return min(self.level, FLOOR_CAP)


class PrintState:
    """Per print: the recent frames above the printer's floor."""

    def __init__(self, floor):
        self.floor, self.recent, self.frames = floor, deque(maxlen=WINDOW), 0
        self.warmup = WARMUP_FRAMES

    def update(self, p):
        self.frames += 1
        self.recent.append(max(0.0, p - self.floor.update(p)))
        return tuple(self.recent), self.frames <= self.warmup


def classify(reading, sensitivity="medium"):
    """'safe', 'warning' (tell the user) or 'failure' (pause, if the user allows it)."""
    recent, warmup = reading
    if warmup:
        return "safe"
    mult = SENSITIVITY.get(sensitivity, 1.0)
    for verdict, (votes, height) in (("failure", FAIL), ("warning", WARN)):
        if sum(x >= height * mult for x in recent) >= votes:
            return verdict
    return "safe"


def score_of(detections):
    """The most confident detection in the frame (0 when there is none)."""
    best = 0.0
    for det in detections or []:
        try:
            best = max(best, float(det[1]))
        except (TypeError, ValueError, IndexError):
            pass
    return best


FRAMES = {}
FRAMES_LOCK = threading.Lock()


def stash(jpeg, clock=time.monotonic):
    nonce = secrets.token_urlsafe(32)
    with FRAMES_LOCK:
        now = clock()
        for k in [k for k, (_, t) in FRAMES.items() if now - t > FRAME_TTL_S]:
            del FRAMES[k]
        FRAMES[nonce] = (jpeg, now)
    return nonce


def pop(nonce, clock=time.monotonic):
    """The frame once, or None if unknown or older than FRAME_TTL_S."""
    with FRAMES_LOCK:
        entry = FRAMES.pop(nonce, None)
    if entry and clock() - entry[1] <= FRAME_TTL_S:
        return entry[0]
    return None


def ask_obico(ml_url, token, img_url, timeout=30):
    req = urllib.request.Request(f"{ml_url.rstrip('/')}/p/?" + urllib.parse.urlencode({"img": img_url}))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("detections", [])


class Watch:
    """One printer. tick() every interval while the printer is RUNNING.

    state_of() -> printer state string; frame() -> recent JPEG or None;
    detect(jpeg) -> list of detections (raises when the engine is down).
    self.verdict: None (not checking), 'safe', 'warning', 'failure', 'unknown' (engine or camera down).
    """

    def __init__(self, state_of, frame, detect, sensitivity="medium", progress_of=lambda: 0):
        self.state_of, self.frame, self.detect, self.sensitivity = state_of, frame, detect, sensitivity
        self.progress_of = progress_of  # print progress in %, to tell a fresh print from one joined midway
        self.baseline, self.print, self.verdict, self.score, self.error = Floor(), None, None, None, None
        self.last_jpeg = None
        self.muted = False  # the user said "not spaghetti" for this print: keep scoring, stop alarming
        # None: the failure has not paused this print; True/False: it tried (and whether it worked).
        self.paused = None

    def tick(self):
        if self.state_of() != "RUNNING":
            if self.state_of() not in ("PAUSE",):  # a pause keeps the print's state; anything else ends it
                self.print, self.verdict, self.score, self.error, self.muted = None, None, None, None, False
                self.paused = None
            return
        if self.print is None:
            self.print, self.paused = PrintState(self.baseline), None
            # Joined a print already under way (Pygmy restarted mid-print): the warmup is for the first
            # layers, so skipping it here keeps the watch from going blind for 5 minutes (2026-11-17, E3K).
            if (self.progress_of() or 0) >= 5:
                self.print.warmup = 0
        jpeg = self.last_jpeg = self.frame()
        if not jpeg:
            self.verdict, self.error = "unknown", "camera"
            return
        try:
            detections = self.detect(jpeg)
        except Exception as err:  # engine down: never claim "safe"
            self.verdict, self.error = "unknown", f"obico: {type(err).__name__}"
            return
        reading = self.print.update(score_of(detections))
        self.verdict, self.score, self.error = classify(reading, self.sensitivity), round(statistics.median(reading[0]), 3), None
        if self.muted and self.verdict in ("warning", "failure"):
            self.verdict = "dismissed"


if __name__ == "__main__":
    w = Watch(lambda: "RUNNING", lambda: b"j", lambda j: [])
    for _ in range(40):
        w.tick()
    assert w.verdict == "safe"
    w.detect = lambda j: [["failure", 0.9, [0, 0, 1, 1]], ["failure", 0.8, [0, 0, 1, 1]]]
    for _ in range(20):
        w.tick()
    assert w.verdict in ("warning", "failure"), w.verdict
    w.detect = lambda j: (_ for _ in ()).throw(OSError())
    w.tick()
    assert w.verdict == "unknown"
    w.state_of = lambda: "FINISH"
    w.tick()
    assert w.verdict is None
    n = stash(b"x")
    assert pop(n) == b"x" and pop(n) is None
    print("ok")
