import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pygmy"))
import server  # noqa: E402


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RootRedirect(unittest.TestCase):
    def test_bare_address_goes_to_the_app(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(NoFollow)
            with self.assertRaises(urllib.error.HTTPError) as got:
                opener.open(f"http://127.0.0.1:{httpd.server_port}/")
            self.assertEqual(got.exception.code, 302)
            self.assertEqual(got.exception.headers["Location"], "/app")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
