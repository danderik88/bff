"""No pilot printer: every printer comes from the app store."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bff_mcp import onboard, server  # noqa: E402

APP = (ROOT / "src" / "bff_mcp" / "app.html").read_text()
SERVER = (ROOT / "src" / "bff_mcp" / "server.py").read_text()


class EnvelopeFromStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        onboard.save_store(Path(self.tmp.name) / "printers.json", {
            "delta": {"model": "N2S"}, "mini": {"model": "N1"}, "alpha": {"model": "C11"}})

    def test_alias_not_in_store_has_no_envelope(self):
        self.assertIsNone(server.envelope_name_of("nope"))

    def test_models_map_to_their_envelopes(self):
        self.assertEqual(server.envelope_name_of("delta"), "A1")
        self.assertEqual(server.envelope_name_of("mini"), "A1M")
        self.assertEqual(server.envelope_name_of("alpha"), "P1")


class A1MiniEnvelope(unittest.TestCase):
    def test_measured_envelope(self):
        env = server.ENVELOPES["A1M"]
        self.assertEqual(env["x"], (0, 180))
        self.assertEqual(env["z"], (0, 180))
        self.assertEqual(env["first"], {"x": 90, "y": 90, "z": 10})
        self.assertNotIn("bed_z", env)


class NoPilotPrinter(unittest.TestCase):
    def test_app_has_no_pilot_printer(self):
        self.assertNotIn('"a1"', APP)
        self.assertNotIn("'a1'", APP)
        self.assertNotIn("Bravo", APP)
        self.assertIn("bff.printer", APP)

    def test_server_has_no_printer_lists(self):
        self.assertNotIn("BFF_MCP_PRINTERS_FILE", SERVER)
        self.assertNotIn("BFF_PRINTERS", SERVER)


class ListPrinters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        onboard.save_store(Path(self.tmp.name) / "printers.json", {
            "delta": {"model": "N2S", "name": "Delta", "host": "192.168.0.10", "serial": "S1", "access_code": "11111111"},
            "mini": {"model": "N1", "name": "Mini", "host": "192.168.0.11", "serial": "S2", "access_code": "22222222"}})
        for patch in (mock.patch.dict(server.CFG, {"printers": {"delta": "app:delta", "mini": "app:mini"}}),
                      mock.patch.object(server, "DB", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_lists_aliases_and_names_without_secrets(self):
        result = json.loads(server.tool_list_printers({})[0]["text"])
        self.assertEqual(result["printers"], ["delta", "mini"])
        self.assertEqual(result["names"], {"delta": "Delta", "mini": "Mini"})
        self.assertIn("models", result)
        keys = set()

        def walk(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(result)
        self.assertFalse(keys & {"access_code", "host", "serial"})

    def test_app_no_longer_parses_the_error_message(self):
        self.assertNotIn("Alias validi", APP)
        self.assertIn('rpcReal("list_printers")', APP)


if __name__ == "__main__":
    unittest.main()
