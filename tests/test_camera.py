import struct
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bff_mcp import camera  # noqa: E402

JPEG = b"\xff\xd8" + b"x" * 30 + b"\xff\xd9"


def framed(payload, size=None):
    return struct.pack("<IIII", len(payload) if size is None else size, 0, 1, 0) + payload


class FakeSock:
    def __init__(self, data, gate=None):
        self.data, self.sent, self.gate = data, b"", gate

    def recv(self, n):
        if not self.data and self.gate:
            self.gate.wait(2)
        out, self.data = self.data[:n], self.data[n:]
        return out

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass


class Camera(unittest.TestCase):
    def test_auth_payload_is_80_bytes(self):
        p = camera.auth_payload("12345678")
        self.assertEqual(len(p), 80)
        self.assertEqual(p[:8], struct.pack("<II", 0x40, 0x3000))
        self.assertEqual(p[16:20], b"bblp")
        self.assertEqual(p[48:56], b"12345678")

    def test_frame_is_read_from_header_size(self):
        self.assertEqual(camera.read_frame(FakeSock(framed(JPEG))), JPEG)

    def test_oversize_frame_is_refused(self):
        with self.assertRaises(camera.CameraError):
            camera.read_frame(FakeSock(framed(b"", size=10_000_001)))

    def test_non_jpeg_is_refused(self):
        with self.assertRaises(camera.CameraError):
            camera.read_frame(FakeSock(framed(b"GIF89a....")))

    def test_two_callers_share_one_connection(self):
        gate, calls = threading.Event(), []

        def connect(host, check):
            calls.append(host)
            return FakeSock(framed(JPEG), gate)

        feed = camera.CameraFeed("h", "code", connect=connect)
        got = []
        threads = [threading.Thread(target=lambda: got.append(feed.latest())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        gate.set()
        self.assertEqual(got, [JPEG, JPEG])
        self.assertEqual(calls, ["h"])

    def test_failure_is_reported(self):
        def connect(host, check):
            raise OSError("refused")

        with self.assertRaisesRegex(camera.CameraError, "refused"):
            camera.CameraFeed("h", "code", connect=connect).latest(wait_s=2)


if __name__ == "__main__":
    unittest.main()
