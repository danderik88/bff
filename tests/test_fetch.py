"""Public link fetcher: host allowlist, redirect safety, size cap, Printables/Thingiverse."""
import json
import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bff_mcp import fetch  # noqa: E402


def global_dns(ip="93.184.216.34"):
    return mock.patch.object(fetch.socket, "getaddrinfo",
                             side_effect=lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))])


class FakeResp:
    def __init__(self, body=b"", content_length=None, chunk=None):
        self._body = body
        self._chunk = chunk
        self._done = False
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, n=-1):
        if self._chunk is not None:
            return self._chunk
        if self._done:
            return b""
        self._done = True
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, req.data, dict(req.header_items())))
        return self.responses.pop(0)


class FetchBase(unittest.TestCase):
    def setUp(self):
        fetch.setup_thingiverse("")

    def tearDown(self):
        fetch.OPENER = urllib.request.build_opener(fetch._SafeRedirectHandler)

    def fake_open(self, *responses):
        op = FakeOpener(responses)
        fetch.OPENER = op
        return op


class TestHostAllowlist(FetchBase):
    def test_http_refused_before_request(self):
        op = self.fake_open()
        with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
            fetch.fetch_model("http://www.printables.com/model/1")
        self.assertEqual(op.calls, [])

    def test_other_host_refused_before_request(self):
        op = self.fake_open()
        with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
            fetch.fetch_model("https://evil.example/model/1")
        self.assertEqual(op.calls, [])

    def test_bad_scheme_and_host_checked_in_check_host(self):
        with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
            fetch.check_host("http://www.printables.com/model/1")
        with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
            fetch.check_host("https://evil.example/model/1")


class TestRedirectToPrivateIp(FetchBase):
    def test_redirect_target_resolving_private_is_refused(self):
        handler = fetch._SafeRedirectHandler()
        with global_dns("192.168.0.10"):
            with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
                handler.redirect_request(None, None, 302, None, {}, "https://www.printables.com/model/1")

    def test_check_host_refuses_private_resolution(self):
        with global_dns("192.168.0.10"):
            with self.assertRaisesRegex(fetch.FetchError, "This link is not allowed"):
                fetch.check_host("https://www.printables.com/model/1")


class TestAuthNotForwardedOnRedirect(FetchBase):
    def test_authorization_not_forwarded_on_redirect(self):
        captured = {}

        class O:
            def open(self, req, timeout=None):
                captured["req"] = req
                return FakeResp(b"bytes")

        fetch.OPENER = O()
        try:
            with global_dns():
                fetch.open_url("https://api.bambulab.com/v1/user-service/user/login",
                               headers={"Authorization": "Bearer sekrit"},
                               redirect_hosts={"api.bambulab.com"})
        finally:
            fetch.OPENER = urllib.request.build_opener(fetch._SafeRedirectHandler)
        req = captured["req"]
        self.assertIn("Authorization", dict(req.header_items()))
        handler = fetch._SafeRedirectHandler()
        with global_dns():
            new_req = handler.redirect_request(req, None, 302, None, {}, "https://api.bambulab.com/v1/other")
        self.assertNotIn("Authorization", dict(new_req.header_items()))


class TestDownload(FetchBase):
    def test_size_cap_stream(self):
        resp = FakeResp(chunk=b"A" * 5)
        fetch.MAX_BYTES = 10
        try:
            with self.assertRaisesRegex(fetch.FetchError, "File is over 100 MB"):
                fetch.read_capped(resp)
        finally:
            fetch.MAX_BYTES = 100 * 1024 * 1024

    def test_size_cap_content_length(self):
        resp = FakeResp(b"x", content_length=200)
        fetch.MAX_BYTES = 100
        try:
            with self.assertRaisesRegex(fetch.FetchError, "File is over 100 MB"):
                fetch.read_capped(resp)
        finally:
            fetch.MAX_BYTES = 100 * 1024 * 1024


class TestBundle(unittest.TestCase):
    def test_one_file_stays_as_it_is(self):
        self.assertEqual(fetch.bundle([("a.stl", b"x")]), ("a.stl", b"x"))

    def test_two_files_become_a_bundle(self):
        name, data = fetch.bundle([("a.stl", b"aa"), ("b.step", b"bb")])
        self.assertTrue(name.endswith(fetch.BUNDLE_EXT))
        self.assertEqual(fetch.bundle_names(data), ["a.stl", "b.step"])

    def test_pick_keeps_the_chosen_ones(self):
        name, data = fetch.bundle([("a.stl", b"aa"), ("b.stl", b"bb")])
        self.assertEqual(fetch.bundle_pick(data, [1]), ("b.stl", b"bb"))
        picked_name, picked = fetch.bundle_pick(data, [0, 1])
        self.assertTrue(picked_name.endswith(fetch.BUNDLE_EXT))
        self.assertEqual(fetch.bundle_names(picked), ["a.stl", "b.stl"])

    def test_pick_nothing_is_refused(self):
        _, data = fetch.bundle([("a.stl", b"aa"), ("b.stl", b"bb")])
        with self.assertRaisesRegex(fetch.FetchError, "Pick at least one file"):
            fetch.bundle_pick(data, [])

    def test_download_all_keeps_a_3mf_next_to_stls_aside(self):
        rows = [{"name": "a.stl"}, {"name": "b.stl"}, {"name": "p.3mf"}]
        name, data, others = fetch._download_all(rows, lambda row: b"<" + row["name"].encode() + b">")
        self.assertTrue(name.endswith(fetch.BUNDLE_EXT))
        self.assertEqual(fetch.bundle_names(data), ["a.stl", "b.stl"])
        self.assertEqual(others, ["p.3mf"])

    def test_download_all_page_of_3mf_is_a_bundle(self):
        rows = [{"name": "3mf_Base-pattern-1.3mf"}, {"name": "3mf_Rail.3mf"},
                {"name": "3mf_Roller-single-ring.3mf"}]
        name, data, others = fetch._download_all(rows, lambda row: b"<" + row["name"].encode() + b">")
        self.assertTrue(name.endswith(fetch.BUNDLE_EXT))
        self.assertEqual(fetch.bundle_names(data),
                         ["3mf_Base-pattern-1.3mf", "3mf_Rail.3mf", "3mf_Roller-single-ring.3mf"])
        self.assertEqual(others, [])

    def test_download_all_one_3mf_stays_bare(self):
        name, data, others = fetch._download_all([{"name": "p.3mf"}], lambda row: b"3mf!")
        self.assertEqual((name, data, others), ("p.3mf", b"3mf!", []))


class TestPrintables(FetchBase):
    def test_printables_link(self):
        q1 = {"data": {"model": {"id": "123456", "name": "Cute Frog",
                             "stls": [{"id": "aaa", "name": "frog.stl", "fileSize": 10},
                                      {"id": "bbb", "name": "frog.3mf", "fileSize": 20}]}}}
        q2 = {"data": {"getDownloadLink": {"ok": True, "output": {"link": "https://files.printables.com/dl/frog.stl"}}}}
        with global_dns():
            op = self.fake_open(FakeResp(json.dumps(q1).encode()),
                                FakeResp(json.dumps(q2).encode()),
                                FakeResp(b"stl-bytes"))
            name, data, others = fetch.fetch_model("https://www.printables.com/model/123456-cute-frog")
        self.assertEqual(name, "frog.stl")
        self.assertEqual(data, b"stl-bytes")
        self.assertEqual(others, ["frog.3mf"])
        self.assertEqual(len(op.calls), 3)
        url0, body0, hdrs0 = op.calls[0]
        self.assertEqual(url0, fetch.GRAPHQL)
        self.assertIn("print(id:$id)", body0.decode())
        self.assertEqual(json.loads(body0)["variables"], {"id": "123456"})
        url1, body1, _ = op.calls[1]
        self.assertEqual(url1, fetch.GRAPHQL)
        self.assertEqual(json.loads(body1)["variables"], {"id": "aaa", "m": "123456"})
        self.assertEqual(op.calls[2][0], "https://files.printables.com/dl/frog.stl")
        ua = next((v for k, v in hdrs0.items() if k.lower() == "user-agent"), None)
        self.assertEqual(ua, fetch.UA)

    def test_no_printable_file(self):
        q1 = {"data": {"model": {"id": "1", "name": "X", "stls": [{"id": "a", "name": "frog.pdf", "fileSize": 1}]}}}
        with global_dns():
            self.fake_open(FakeResp(json.dumps(q1).encode()))
            with self.assertRaisesRegex(fetch.FetchError, "No printable file on this page"):
                fetch.fetch_model("https://printables.com/model/1")


class TestThingiverse(FetchBase):
    def test_thingiverse_link(self):
        files = [{"name": "part.stl", "download_url": "https://cdn.thingiverse.com/part.stl"}]
        fetch.setup_thingiverse("test-token")
        with global_dns():
            op = self.fake_open(FakeResp(json.dumps(files).encode()), FakeResp(b"thing-bytes"))
            name, data, others = fetch.fetch_model("https://www.thingiverse.com/thing:12345")
        self.assertEqual(name, "part.stl")
        self.assertEqual(data, b"thing-bytes")
        self.assertEqual(others, [])
        self.assertEqual(op.calls[0][0], "https://api.thingiverse.com/things/12345/files")
        self.assertEqual(op.calls[0][2]["Authorization"], "Bearer test-token")
        self.assertEqual(op.calls[1][2]["Authorization"], "Bearer test-token")
        self.assertEqual(op.calls[1][0], "https://cdn.thingiverse.com/part.stl")

    def test_thingiverse_needs_token(self):
        with self.assertRaisesRegex(fetch.FetchError, "need a Thingiverse app token"):
            fetch.thingiverse("https://www.thingiverse.com/thing:12345")


if __name__ == "__main__":
    unittest.main()
