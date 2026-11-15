"""BFF talking to the printer itself."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from bff_mcp import server  # noqa: E402
from printer_link import PrinterLink, build_command  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"pause": True}}


class FakeLink(PrinterLink):
    """A real link fed by hand, whose publish only records."""

    def __init__(self, state="RUNNING", answers=True):
        super().__init__("a1", "host", "s", "code", "/nonexistent")
        self.published, self.can_send, self.answers = [], True, answers
        for line in (ROOT / "tests" / "data" / "a1_idle.jsonl").read_text().splitlines():
            self.feed(json.loads(line))
        self.feed({"print": {"gcode_state": state}})

    def publish(self, payload):
        if not self.can_send:
            return False
        self.published.append(payload)
        if self.answers and payload.get("print", {}).get("command") == "pause":
            self.feed({"print": {"gcode_state": "PAUSE"}})
        return True


class Direct(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp))
        server.open_db(str(tmp / "state.db"))
        self.link = server.LINKS["a1"] = FakeLink()

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def outcome(self, key):
        return server.DB.execute("SELECT outcome FROM commands WHERE key=?", (key,)).fetchone()[0]

    def locked(self):
        return server.DB.execute("SELECT 1 FROM locks WHERE printer='a1'").fetchone() is not None

    def test_status_comes_from_the_link(self):
        status = server.read_status("a1")
        self.assertEqual(status["state"], "RUNNING")
        self.assertTrue(status["connected"])

    def test_status_tool_reads_the_link(self):
        reply = json.loads(server.tool_status({"printer": "a1"})[0]["text"])
        self.assertEqual(reply["state"], "RUNNING")

    def test_missing_link_is_a_clear_error(self):
        server.LINKS.clear()
        self.assertRaises(server.ToolError, server.read_status, "a1")

    def test_pause_goes_over_mqtt_once(self):
        result = server.execute("a1", "pause", None, "key-00001")
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(self.link.published, [{"print": {"command": "pause", "sequence_id": "0"}}])
        self.assertFalse(self.locked())

    def test_no_replay_after_reconnect(self):
        self.link.can_send = False
        result = server.execute("a1", "pause", None, "key-00002")
        self.assertEqual(result["outcome"], "unknown")
        self.link.can_send = True
        self.assertEqual(self.link.published, [])
        self.assertEqual(self.outcome("key-00002"), "unknown")
        self.assertTrue(self.locked())
        self.assertRaises(server.ToolError, server.execute, "a1", "pause", None, "key-00003")
        self.assertEqual(self.link.published, [])

    def test_sent_but_never_confirmed_keeps_the_lock_and_sends_once(self):
        self.link.answers = False
        result = server.execute("a1", "pause", None, "key-00004")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(len(self.link.published), 1)
        self.assertTrue(self.locked())

    def test_silent_printer_refuses_the_command(self):
        self.link.last_report = -1000.0
        result = server.execute("a1", "pause", None, "key-00005")
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(self.link.published, [])

    def test_light_uses_the_link(self):
        server.set_light(1, True)
        system = self.link.published[0]["system"]
        self.assertEqual((system["command"], system["led_node"], system["led_mode"]), ("ledctrl", "chamber_light", "on"))

    def test_temperature_payloads(self):
        self.assertEqual(build_command("temperature", heater="bed", target=60)["print"]["param"], "M140 S60")
        self.assertEqual(build_command("temperature", heater="nozzle", target=200)["print"]["param"], "M104 T0 S200")

    def test_qos_zero_so_the_library_can_never_resend(self):
        link = PrinterLink("a1", "host", "s", "code", "/nonexistent")
        link.client, link.online = mock.Mock(), True
        link.client.publish.return_value.rc = 0
        self.assertTrue(link.publish({"print": {"command": "pause"}}))
        self.assertEqual(link.client.publish.call_args.kwargs.get("qos", 0), 0)
        self.assertEqual(len(link.client.publish.call_args.args), 2)

    def test_set_light_tool_is_listed_with_on_required(self):
        names = server.visible_tools()
        self.assertIn("set_light", names)
        self.assertEqual(names["set_light"][2]["required"], ["printer", "on"])
        self.assertEqual(names["set_light"][1], "Accende o spegne la luce della camera.")

    def test_set_light_tool_calls_set_light(self):
        server.tool_set_light({"printer": "a1", "on": True})
        self.assertEqual(self.link.published[-1], build_command("light", on=True))
        server.tool_set_light({"printer": "a1", "on": False})
        self.assertEqual(self.link.published[-1], build_command("light", on=False))


if __name__ == "__main__":
    unittest.main()
