"""Spaghetti check through the Obico ML API already running on the LAN (CT 108).

The API is GET-only: /p/?img=<URL> fetches the photo itself, with a short read timeout.
So BFF keeps the frame under a one-shot nonce and hands Obico a URL on its LAN
listener (/obico/<nonce>.jpg) that returns the bytes at once.

The score is Obico's own decision, as ported from (obico-server lib/prediction.py,
FD_1ST_GEN_PARAMS): per-frame sum of confidences, EWM (span 12) minus a long baseline kept
across prints, compared with LOW/HIGH scaled by sensitivity. It only reports; it never pauses.
"""
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

EWM_ALPHA = 2.0 / (12 + 1)
ROLLING_SHORT, ROLLING_LONG, WARMUP_FRAMES = 310, 7200, 30
SHORT_MULTIPLE, ESCALATING_FACTOR = 3.8, 1.75
BASE_LOW, BASE_HIGH = 0.38, 0.78
SENSITIVITY = {"low": 1.25, "medium": 1.0, "high": 0.75}
FRAME_TTL_S = 30.0
# The baseline only hides small steady false positives (a cable Obico likes). Capped so a print that is
# already failing when BFF starts (or one that fails slowly) is never learned as "normal" (2026-11-12, A1).
# ponytail: fixed cap; raise it if a printer's camera has a bigger steady false positive.
BASELINE_CAP = 0.25


class Rolling:
    def __init__(self, size):
        self.buf, self.sum = deque(maxlen=size), 0.0

    def add(self, v):
        if len(self.buf) == self.buf.maxlen:
            self.sum -= self.buf[0]
        self.buf.append(v)
        self.sum += v

    @property
    def mean(self):
        return self.sum / len(self.buf) if self.buf else 0.0


class PrintState:
    """Per print; the baseline belongs to the printer and outlives it."""

    def __init__(self, baseline):
        self.baseline, self.short, self.frames, self.ewm = baseline, Rolling(ROLLING_SHORT), 0, 0.0
        self.warmup = WARMUP_FRAMES

    def update(self, p):
        self.frames += 1
        self.ewm = p if self.frames == 1 else EWM_ALPHA * p + (1 - EWM_ALPHA) * self.ewm
        self.short.add(p)
        self.baseline.add(p)
        base = min(self.baseline.mean, BASELINE_CAP)
        return self.ewm - base, self.short.mean - base, self.frames <= self.warmup


def failing(score, short_rise, warmup, sensitivity, factor):
    if warmup:
        return False
    mult = SENSITIVITY.get(sensitivity, 1.0)
    score /= factor
    if score < BASE_LOW * mult:
        return False
    if score > BASE_HIGH * mult:
        return True
    return score > short_rise * SHORT_MULTIPLE * mult


def classify(reading, sensitivity="medium"):
    """'safe', 'warning' (Obico would alert) or 'failure' (Obico would pause)."""
    if failing(*reading, sensitivity, ESCALATING_FACTOR):
        return "failure"
    return "warning" if failing(*reading, sensitivity, 1.0) else "safe"


def score_of(detections):
    total = 0.0
    for det in detections or []:
        try:
            total += float(det[1])
        except (TypeError, ValueError, IndexError):
            pass
    return total


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
        self.baseline, self.print, self.verdict, self.score, self.error = Rolling(ROLLING_LONG), None, None, None, None
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
        self.verdict, self.score, self.error = classify(reading, self.sensitivity), round(max(0.0, reading[0]), 3), None
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
