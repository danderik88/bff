"""Run with: python3 -m unittest discover -s tests"""
import base64
import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from bff_mcp import server  # noqa: E402

BEARER = "m" * 40
SERIAL = "03919A000000000"
PRINTER_IP = "192.0.2.77"
JPEG = b"\xff\xd8\xff\xe0fakejpeg"

STATUS = {
    "id": 1,
    "name": f"A1 {SERIAL}",
    "connected": True,
    "state": "RUNNING",
    "subtask_name": "cubo",
    "gcode_file": f"/{PRINTER_IP}/cubo.gcode",
    "progress": 42.0,
    "remaining_time": 10,
    "layer_num": 3,
    "total_layers": 9,
    "temperatures": {"bed": 60, "nozzle": 220},
    "vt_tray": [{"tag_uid": SERIAL}],
    "hms_errors": [{"code": "0300_0001", "attr": 7, "module": 3, "severity": 2, "actions": ["CHECK"], "job_id": SERIAL, "description": "Z axis homing failed"}],
}


class FakeLink:
    """Raw printer report, with the serial and IP a real one carries."""
    drops = 0

    def snapshot(self):
        return json.loads(json.dumps(STATUS))


class FakeFeed:
    def latest(self, **_):
        return JPEG


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.mcp.serve_forever, daemon=True).start()
        server.CFG.update(printers={"a1": 1}, tokens={"imac": BEARER})
        server.DB = None  # read-only server: no state database

    @classmethod
    def tearDownClass(cls):
        cls.mcp.shutdown()

    def setUp(self):
        server.LINKS["a1"] = FakeLink()
        server.FEEDS["a1"] = FakeFeed()
        self.addCleanup(server.LINKS.clear)
        self.addCleanup(server.FEEDS.clear)

    def post(self, body, token=BEARER):
        conn = http.client.HTTPConnection("127.0.0.1", self.mcp.server_address[1], timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        conn.request("POST", "/mcp", json.dumps(body), headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def call(self, name, printer="a1"):
        status, data = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": {"printer": printer}},
        })
        self.assertEqual(status, 200)
        return data, json.loads(data)["result"]

    def assert_redacted(self, data):
        for secret in (SERIAL, PRINTER_IP, BEARER):
            self.assertNotIn(secret.encode(), data)

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.post({"method": "ping", "id": 1}, token=None)[0], 401)
        self.assertEqual(self.post({"method": "ping", "id": 1}, token="x" * 40)[0], 401)

    def test_initialize_echoes_protocol(self):
        _, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        result = json.loads(data)["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])

    def test_notification_gets_202(self):
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)

    def test_only_read_tools_are_listed(self):
        _, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in json.loads(data)["result"]["tools"]}
        self.assertEqual(names, {"status", "list_printers", "printer_info", "camera_snapshot", "plate_photo", "hms_diagnose", "ready_files", "print_requests", "queue_list"})

    def test_status_is_allowlisted(self):
        data, result = self.call("status")
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["state"], "RUNNING")
        self.assertEqual(payload["printer"], "a1")
        self.assertEqual(payload["hms_count"], 1)
        self.assertEqual(payload["attention"][0]["handling"], "sicurezza")
        self.assert_redacted(data)

    def test_hms_is_allowlisted(self):
        data, result = self.call("hms_diagnose")
        error = json.loads(result["content"][0]["text"])["errors"][0]
        self.assertEqual(error, {"code": "0300_0001", "module": 3, "severity": "avviso (stampa in pausa)", "actions": ["CHECK"], "description": "Z axis homing failed", "handling": "sicurezza"})
        self.assert_redacted(data)

    def test_hms_handling_defaults_to_safety(self):
        self.assertEqual(server.hms_handling({"module": 7, "code": "0x8007"}), "persona")
        self.assertEqual(server.hms_handling({"module": 7, "code": "0x9999"}), "sicurezza")
        self.assertEqual(server.hms_handling({"module": 3, "code": "0x8007"}), "sicurezza")

    def test_camera_returns_jpeg_without_token(self):
        data, result = self.call("camera_snapshot")
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(base64.b64decode(result["content"][0]["data"]), JPEG)
        self.assert_redacted(data)

    def test_unknown_printer(self):
        _, result = self.call("status", printer="p2s")
        self.assertTrue(result["isError"])

    def test_mutating_tool_does_not_exist(self):
        status, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pause", "arguments": {"printer": "a1"}}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["error"]["code"], -32602)

    def test_malformed_requests(self):
        self.assertEqual(json.loads(self.post([1, 2])[1])["error"]["code"], -32600)
        status, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["error"]["code"], -32602)
        for args in ("x", {"printer": ["a1"]}, {}):
            status, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status", "arguments": args}})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
