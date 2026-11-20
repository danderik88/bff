"""Run with: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "bff_mcp"))
from bff_mcp import server  # noqa: E402
from printer_link import STALE_AFTER_S, PUSHALL, CertificateChanged, PrinterLink, check_fingerprint  # noqa: E402

DATA = ROOT / "tests" / "data"
HMS_ATTR_FILAMENT = 0x07000300
HMS_CODE_FILAMENT = 0x20002


class FakeClock:
    now = 1000.0

    def __call__(self):
        return self.now


def replay(name, clock=None):
    kwargs = {"clock": clock} if clock else {}
    link = PrinterLink("a1", "host", "03919A000000000", "code", "/nonexistent", **kwargs)
    for line in (DATA / name).read_text().splitlines():
        link.feed(json.loads(line))
    return link


class Snapshot(unittest.TestCase):
    def test_replay_idle_gives_the_fields_server_reads(self):
        s = replay("a1_idle.jsonl").snapshot()
        self.assertIn(s["state"], ("IDLE", "FINISH", "FAILED"))
        for key in ("temperatures", "hms_errors", "chamber_light", "tray_now", "vt_tray", "nozzles", "ams"):
            self.assertIn(key, s)
        for key in server.STATUS_FIELDS:
            self.assertIn(key, s)

    def test_values_have_the_shape_the_server_expects(self):
        s = replay("a1_idle.jsonl").snapshot()
        self.assertTrue(s["connected"])
        self.assertEqual(s["tray_now"], 254)
        self.assertEqual(s["vt_tray"][0]["tray_type"], "PLA")
        self.assertEqual(s["nozzles"][0], {"nozzle_type": "hardened_steel", "nozzle_diameter": "0.4"})
        self.assertIs(s["chamber_light"], True)
        self.assertEqual(s["ams"], [])
        self.assertEqual(s["total_layers"], 120)
        self.assertEqual(s["hms_errors"], [])
        self.assertFalse(s["temperatures"]["nozzle_heating"])

    def test_partial_message_keeps_known_fields(self):
        link = replay("a1_idle.jsonl")
        before = link.snapshot()["vt_tray"]
        link.feed({"print": {"mc_percent": 7}})
        self.assertEqual(link.snapshot()["vt_tray"], before)
        self.assertEqual(link.snapshot()["progress"], 7.0)

    def test_partial_nested_message_keeps_sibling_keys(self):
        link = replay("a1_idle.jsonl")
        link.feed({"print": {"ams": {"tray_now": "255"}}})
        s = link.snapshot()
        self.assertEqual(s["tray_now"], 255)
        self.assertEqual(s["ams"], [])

    def test_silence_marks_not_connected(self):
        clock = FakeClock()
        link = replay("a1_idle.jsonl", clock=clock)
        clock.now += STALE_AFTER_S + 1
        self.assertFalse(link.snapshot()["connected"])

    def test_no_report_yet_is_not_connected(self):
        link = PrinterLink("a1", "host", "s", "code", "/nonexistent")
        s = link.snapshot()
        self.assertFalse(s["connected"])
        self.assertIsNone(s["state"])
        self.assertEqual(s["hms_errors"], [])


class NozzleType(unittest.TestCase):
    def map(self, code):
        link = replay("a1_idle.jsonl")
        link.feed({"print": {"nozzle_type": code}})
        return link.snapshot()

    def test_hs01_maps_to_hardened_steel(self):
        s = self.map("HS01")
        self.assertEqual(s["nozzles"][0]["nozzle_type"], "hardened_steel")
        self.assertEqual(s["nozzles"][0]["nozzle_code"], "HS01")
        self.assertEqual(s["nozzle_type"], "hardened_steel")

    def test_ss01_maps_to_stainless_steel(self):
        s = self.map("SS01")
        self.assertEqual(s["nozzles"][0]["nozzle_type"], "stainless_steel")
        self.assertEqual(s["nozzles"][0]["nozzle_code"], "SS01")

    def test_known_word_passes_through(self):
        s = self.map("hardened_steel")
        self.assertEqual(s["nozzles"][0]["nozzle_type"], "hardened_steel")
        self.assertNotIn("nozzle_code", s["nozzles"][0])

    def test_unknown_code_stays_unchanged(self):
        s = self.map("AX01")
        self.assertEqual(s["nozzles"][0]["nozzle_type"], "AX01")
        self.assertEqual(s["nozzles"][0]["nozzle_code"], "AX01")


class Hms(unittest.TestCase):
    def errors(self, **fields):
        link = replay("a1_idle.jsonl")
        link.feed({"print": fields})
        return link.snapshot()["hms_errors"]

    def test_hms_code_matches_the_format_used_by_hands_on_list(self):
        e = self.errors(hms=[{"attr": HMS_ATTR_FILAMENT, "code": HMS_CODE_FILAMENT}])[0]
        self.assertIn((e["module"], e["code"]), server.HANDS_ON_HMS)

    def test_print_error_code_matches_the_hands_on_list(self):
        e = self.errors(print_error=0x07FF8007)[0]
        self.assertIn((e["module"], e["code"]), server.HANDS_ON_HMS)

    def test_status_values_and_cancel_echoes_are_not_faults(self):
        self.assertEqual(self.errors(hms=[{"attr": HMS_ATTR_FILAMENT, "code": 2}]), [])
        self.assertEqual(self.errors(print_error=0x0500400E), [])
        self.assertEqual(self.errors(print_error=0), [])

    def test_same_fault_on_both_paths_is_listed_once(self):
        found = self.errors(hms=[{"attr": 0x05000200, "code": 0x18061}], print_error=0x05008061)
        self.assertEqual(len(found), 1)

    def test_cleared_fault_disappears(self):
        link = replay("a1_idle.jsonl")
        link.feed({"print": {"hms": [{"attr": HMS_ATTR_FILAMENT, "code": HMS_CODE_FILAMENT}]}})
        link.feed({"print": {"hms": []}})
        self.assertEqual(link.snapshot()["hms_errors"], [])

    def test_every_fault_has_a_description(self):
        self.assertTrue(self.errors(print_error=0x07FF8007)[0]["description"])

    def test_description_is_the_code_the_printer_shows(self):
        self.assertEqual(self.errors(hms=[{"attr": 0x03000D00, "code": 0x00010001}])[0]["description"],
                         "HMS error 0300-0D00-0001-0001")
        self.assertEqual(self.errors(print_error=0x0500C010)[0]["description"], "print_error 0x0500C010")


class AmsModelFromInfo(unittest.TestCase):
    """The unit's own info hex field (type = low nibble) overrides the get_version model."""

    def feed(self, info_hex):
        link = PrinterLink("a1", "host", "03919A000000000", "code", "/nonexistent")
        link.feed({"info": {"command": "get_version", "module": [{"name": "ams/0", "sn": "S0"}]}})
        link.feed({"print": {"ams": {"ams": [{"id": 0, "ams_id": "0" * 20, "info": info_hex}]}}})
        return link.snapshot()["ams"][0]

    def test_info_1004_reports_ams_ht(self):
        unit = self.feed("1004")
        self.assertEqual(unit["model"], "AMS HT")

    def test_info_1001_reports_ams(self):
        unit = self.feed("1001")
        self.assertEqual(unit["model"], "AMS")

    def test_invalid_info_keeps_get_version_model(self):
        unit = self.feed("zz")
        self.assertEqual(unit["model"], "AMS")


class Link(unittest.TestCase):
    def test_changed_certificate_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            check_fingerprint(tmp_dir, "a1", b"cert-a")
            check_fingerprint(tmp_dir, "a1", b"cert-a")
            self.assertRaises(CertificateChanged, check_fingerprint, tmp_dir, "a1", b"cert-b")

    def test_access_code_goes_only_to_the_known_certificate(self):
        class Client:
            password = None

            def username_pw_set(self, _user, password):
                self.password = password

            def disconnect(self):
                pass

        class Sock:
            der = b"cert-a"

            def getpeercert(self, binary_form):
                return self.der

        with tempfile.TemporaryDirectory() as tmp_dir:
            link, client, sock = PrinterLink("a1", "host", "s", "code", tmp_dir), Client(), Sock()
            link._on_socket_open(client, None, sock)
            self.assertEqual(client.password, "code")
            sock.der = b"cert-b"
            with contextlib.redirect_stderr(io.StringIO()):
                link._on_socket_open(client, None, sock)
            self.assertEqual(client.password, "")
            self.assertTrue(link.refused)

    def test_publish_refuses_without_a_connection(self):
        link = PrinterLink("a1", "host", "s", "code", "/nonexistent")
        self.assertFalse(link.publish({"print": {"command": "pause"}}))


class Stale(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.link = PrinterLink("a1", "host", "s", "code", "/nonexistent", clock=self.clock)
        self.link.client = mock.Mock()
        self.link.client.publish.return_value.rc = 0
        self.link.online = True

    def test_silence_pushes_pushall_at_most_every_5s(self):
        self.link.feed({"print": {"gcode_state": "IDLE"}})
        self.clock.now += 6
        self.link.maybe_push_stale()
        self.assertEqual(len(self.link.client.publish.call_args_list), 1)
        self.assertEqual(json.loads(self.link.client.publish.call_args.args[1]), PUSHALL)
        self.link.maybe_push_stale()
        self.assertEqual(len(self.link.client.publish.call_args_list), 1)
        self.clock.now += 5
        self.link.maybe_push_stale()
        self.assertEqual(len(self.link.client.publish.call_args_list), 2)

    def test_silent_past_stale_s_is_disconnected_until_a_message_arrives(self):
        self.link.feed({"print": {"gcode_state": "IDLE"}})
        self.assertTrue(self.link.snapshot()["connected"])
        self.clock.now += STALE_AFTER_S + 1
        self.assertFalse(self.link.snapshot()["connected"])
        self.link.feed({"print": {"mc_percent": 7}})
        self.assertTrue(self.link.snapshot()["connected"])

    def test_a_10s_report_gap_is_not_a_disconnect(self):
        self.link.feed({"print": {"gcode_state": "IDLE"}})
        self.clock.now += 10.5  # P1/A1 firmware stalls its stream this long with the link healthy
        self.assertTrue(self.link.snapshot()["connected"])

    def test_connected_transitions_log_exactly_one_line_each(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.link.feed({"print": {"gcode_state": "IDLE"}})
            self.link.snapshot()
            lines = buf.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.clock.now += STALE_AFTER_S + 1
            self.link.snapshot()
            lines = buf.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.link.feed({"print": {"mc_percent": 7}})
            self.link.snapshot()
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("a1", lines[1])
        self.assertIn(f"nessun dato da {STALE_AFTER_S} s", lines[1])


if __name__ == "__main__":
    unittest.main()
