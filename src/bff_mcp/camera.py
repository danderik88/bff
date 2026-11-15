"""Chamber camera of A1 / A1 mini / P1P / P1S: JPEG frames over TLS on port 6000."""
import socket
import ssl
import struct
import threading
import time

MAX_FRAME = 10_000_000
IDLE_S = 30  # The reader stops this long after the last caller: the printer is not streaming for nobody.


class CameraError(Exception):
    pass


def auth_payload(access_code):
    """80 bytes: header (0x40, 0x3000, 0, 0), then user and access code padded to 32 bytes each."""
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise CameraError("camera chiusa dalla stampante")
        buf += chunk
    return buf


def read_frame(sock):
    """One frame: a 16-byte header whose first 4 bytes are the JPEG size (little endian), then the JPEG."""
    size = struct.unpack("<I", read_exact(sock, 16)[:4])[0]
    if not 0 < size <= MAX_FRAME:
        raise CameraError(f"dimensione frame non valida: {size}")
    jpeg = read_exact(sock, size)
    if not jpeg.startswith(b"\xff\xd8"):
        raise CameraError("il frame non e' un JPEG")
    return jpeg


def tls_connect(host, check_cert=None, timeout=10):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Self-signed: pinned by check_cert.
    sock = ctx.wrap_socket(socket.create_connection((host, 6000), timeout), server_hostname=host)
    if check_cert:
        check_cert(sock.getpeercert(binary_form=True))
    return sock


class CameraFeed:
    """One reader per printer, started on demand; every caller gets the newest frame."""

    def __init__(self, host, access_code, check_cert=None, connect=tls_connect, clock=time.monotonic):
        self.host, self.access_code, self.check_cert = host, access_code, check_cert
        self.connect, self.clock = connect, clock
        self.cond = threading.Condition()
        self.frame, self.at, self.asked, self.error, self.running = None, 0.0, 0.0, None, False

    def latest(self, max_age_s=2.0, wait_s=12.0):
        with self.cond:
            self.asked = self.clock()
            if self.frame is not None and self.clock() - self.at <= max_age_s:
                return self.frame
            if not self.running:
                self.running, self.error = True, None
                threading.Thread(target=self._run, daemon=True).start()
            start = self.at
            self.cond.wait_for(lambda: self.at != start or self.error or not self.running, wait_s)
            if self.at != start:
                return self.frame
            raise CameraError(self.error or "nessun frame dalla camera")

    def _run(self):
        sock = None
        try:
            sock = self.connect(self.host, self.check_cert)
            sock.sendall(auth_payload(self.access_code))
            while self.clock() - self.asked < IDLE_S:
                jpeg = read_frame(sock)
                with self.cond:
                    self.frame, self.at = jpeg, self.clock()
                    self.cond.notify_all()
        except Exception as err:  # Any failure ends this reader; the next caller starts a fresh one.
            with self.cond:
                self.error = str(err) or type(err).__name__
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self.cond:
                self.running = False
                self.cond.notify_all()


class Go2rtcFeed:
    """Same interface as CameraFeed, with frames fetched from go2rtc (the RTSP-only printers)."""

    def __init__(self, alias, clock=time.monotonic, fetch=None):
        self.alias, self.clock = alias, clock
        self.frame, self.at = None, 0.0
        if fetch is None:
            def fetch():
                try:
                    from . import go2rtc  # imported as bff_mcp.camera (tests)
                except ImportError:
                    import go2rtc  # sibling module, on sys.path once server.py is loaded
                return go2rtc.frame(self.alias)
        self.fetch = fetch

    def latest(self, max_age_s=2.0, wait_s=12.0):
        """A frame at most a second old, like a live view; max_age_s and wait_s keep CameraFeed's signature."""
        if self.frame is not None and self.clock() - self.at <= 1.0:
            return self.frame
        try:
            self.frame, self.at = self.fetch(), self.clock()
        except Exception as err:  # go2rtc down or no frame: the same error the JPEG feed raises
            raise CameraError(str(err) or type(err).__name__) from None
        return self.frame
