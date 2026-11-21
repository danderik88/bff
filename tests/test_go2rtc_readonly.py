import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pygmy"))
import go2rtc  # noqa: E402


def http_400(body):
    return urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(body))


class ReadOnlyConfig(unittest.TestCase):
    def test_read_only_config_is_not_logged(self):
        with mock.patch.object(go2rtc, "_request", side_effect=http_400(b"open /config/go2rtc.yaml: read-only file system")), \
                mock.patch("builtins.print") as out:
            go2rtc._soft("PUT", "/api/streams", {"name": "a", "src": "x"})
        out.assert_not_called()

    def test_other_errors_are_still_logged(self):
        with mock.patch.object(go2rtc, "_request", side_effect=http_400(b"bad source")), \
                mock.patch("builtins.print") as out:
            go2rtc._soft("PUT", "/api/streams", {"name": "a", "src": "x"})
        out.assert_called_once()


if __name__ == "__main__":
    unittest.main()
