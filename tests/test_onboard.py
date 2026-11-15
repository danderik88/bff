import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bff_mcp"))
import onboard  # noqa: E402

A1_NOTIFY = (
    "NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: 192.168.0.10\r\n"
    "NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 0309XXXX\r\nCache-Control: max-age=1800\r\n"
    "DevModel.bambu.com: N2S\r\nDevName.bambu.com: AUNA\r\nDevConnect.bambu.com: lan\r\n\r\n"
)
TV_NOTIFY = "NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nLocation: http://192.168.0.128:1234/\r\nUSN: uuid:tv\r\n\r\n"


class FakeSock:
    def __init__(self, packets):
        self.packets = list(packets)

    def recvfrom(self, n):
        if self.packets:
            return self.packets.pop(0).encode(), ("192.168.0.10", 2021)
        raise socket.timeout()

    def close(self):
        pass


class Discovery(unittest.TestCase):
    def test_parse_notify_a1(self):
        self.assertEqual(onboard.parse_notify(A1_NOTIFY),
                         {"ip": "192.168.0.10", "serial": "0309XXXX", "model": "N2S", "name": "AUNA"})

    def test_parse_notify_ignores_non_bambu(self):
        self.assertIsNone(onboard.parse_notify(TV_NOTIFY))

    def test_listen_dedups(self):
        found = onboard.listen(seconds=0.2, sock=FakeSock([A1_NOTIFY, A1_NOTIFY, TV_NOTIFY]))
        self.assertEqual([p["serial"] for p in found], ["0309XXXX"])

    def test_heard_keeps_recent_only(self):
        onboard.HEARD.clear()
        onboard.HEARD["OLD"] = ({"serial": "OLD"}, 0.0)
        onboard.HEARD["NEW"] = ({"serial": "NEW"}, 100.0)
        self.assertEqual([p["serial"] for p in onboard.heard(clock=lambda: 120.0)], ["NEW"])
        onboard.keep_listening(sock=FakeSock([A1_NOTIFY]), clock=lambda: 5.0, seconds=0.1)
        self.assertIn("0309XXXX", onboard.HEARD)
        onboard.HEARD.clear()

    def test_model_name_enclosure(self):
        self.assertEqual(onboard.model_name("C11"), "P1P")
        self.assertEqual(onboard.model_name("C11", True), "P1S")
        self.assertEqual(onboard.model_name("N2S", True), "A1")
        self.assertIs(onboard.MODELS["BL-P001"]["supported"], False)
        self.assertIs(onboard.MODELS["O1D"]["supported"], True)
        self.assertIs(onboard.MODELS["N7"]["supported"], True)
        self.assertEqual(onboard.model_name("XYZ"), "Unknown (XYZ)")

    def test_untested_model_flag(self):
        self.assertEqual(onboard.TESTED_MODELS, {"N2S", "N1", "C11", "C12", "N7"})


def self_signed(cn):
    """DER of a throwaway self-signed certificate (issuer == subject)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:1024", "-nodes", "-subj", f"/CN={cn}",
                        "-keyout", f"{d}/k", "-out", f"{d}/c", "-outform", "DER", "-days", "1"],
                       check=True, capture_output=True)
        return Path(f"{d}/c").read_bytes()


class Probe(unittest.TestCase):
    def test_clean_code(self):
        self.assertEqual(onboard.clean_code(" 12345678 "), "12345678")
        for bad in ("123", "1234 5678", "", None):
            with self.assertRaises(ValueError):
                onboard.clean_code(bad)

    def test_slug(self):
        self.assertEqual(onboard.slug("Alpha P1S!"), "alphap1s")
        with self.assertRaises(ValueError):
            onboard.slug("  !! ")

    def test_cert_names(self):
        self.assertEqual(onboard.cert_names(self_signed("SERIAL123")), ("SERIAL123", "SERIAL123"))

    def test_probe_not_bambu(self):
        with self.assertRaises(onboard.ProbeError) as e:
            onboard.probe("h", "12345678", cert=lambda h, t: self_signed("foo"), connect=lambda *a: (0, True))
        self.assertEqual(e.exception.reason, "not a Bambu printer")

    def test_probe_wrong_code(self):
        with self.assertRaises(onboard.ProbeError) as e:
            onboard.probe("h", "12345678", cert=lambda h, t: self_signed("BBL CA"), connect=lambda *a: (5, False))
        self.assertEqual(e.exception.reason, "wrong access code")

    def test_probe_unreachable(self):
        def down(h, t):
            raise OSError("no route")
        with self.assertRaises(onboard.ProbeError) as e:
            onboard.probe("h", "12345678", cert=down)
        self.assertEqual(e.exception.reason, "unreachable")

    def test_probe_ok_and_no_report(self):
        der = self_signed("BBL CA")  # self-signed, so subject CN == issuer CN == "BBL CA"
        got = onboard.probe("h", "12345678", cert=lambda h, t: der, connect=lambda *a: (0, True))
        self.assertEqual(got, {"serial": "BBL CA", "der": der})
        with self.assertRaises(onboard.ProbeError) as e:
            onboard.probe("h", "12345678", cert=lambda h, t: der, connect=lambda *a: (0, False))
        self.assertEqual(e.exception.reason, "no report")


class Store(unittest.TestCase):
    def test_store_roundtrip_mode_600(self):
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "printers.json"
            data = {"alpha": {"host": "192.168.0.20", "serial": "S", "access_code": "12345678",
                              "name": "Alpha", "model": "C11", "enclosure": True}}
            onboard.save_store(path, data)
            self.assertEqual(onboard.load_store(path), data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["printers.json"])

    def test_store_missing_is_empty(self):
        self.assertEqual(onboard.load_store(Path("/nonexistent/printers.json")), {})


class Profile(unittest.TestCase):
    def test_learn_union(self):
        p = onboard.learn(None, {"fans": {"part": 60, "aux": 0, "chamber": 0}, "xcam": {"first_layer_inspector": True}},
                          {"lights_report": [{"node": "chamber_light", "mode": "on"}]})
        p = onboard.learn(p, {"fans": {"part": 0, "aux": 40, "chamber": 0}, "options": {"auto_recovery": True}},
                          {"ams": {"ams": [{"id": "0"}]}})
        self.assertEqual(p, {"fans": ["part", "aux"], "xcam": ["first_layer_inspector"],
                             "options": ["auto_recovery"], "lights": ["chamber_light"]})

    def test_learn_ignores_echoed_fans(self):
        p = None
        for v in (0, 60, 100):
            p = onboard.learn(p, {"fans": {"part": v, "aux": v, "chamber": v}}, {})
        self.assertEqual(p["fans"], ["part"])

    def test_model_from_profile(self):
        self.assertEqual(onboard.model_name("C11", profile={"fans": ["part", "chamber"]}), "P1S")
        self.assertEqual(onboard.model_name("C11", profile={"fans": ["part", "aux"]}), "P1P")


ALPHA_AMS = [{"id": "0", "humidity": "5", "tray": [
    {"id": "0"}, {"id": "1"},
    {"id": "2", "remain": 96, "tray_type": "PLA", "tray_sub_brands": "PLA Matte", "tray_color": "000000FF",
     "tray_info_idx": "GFA01", "tag_uid": "SECRET", "tray_uuid": "SECRET"},
    {"id": "3", "remain": -1, "tray_type": "PETG", "tray_sub_brands": "", "tray_color": "FFFFFFFF", "tray_info_idx": "GFG00"}]}]


class Ams(unittest.TestCase):
    def test_trays_live_loaded_only_no_tag_ids(self):
        trays = onboard.ams_trays(ALPHA_AMS)
        self.assertEqual([(t["slot"], t["index"], t["material"], t["color"], t["remain"]) for t in trays],
                         [("A3", 2, "PLA", "000000FF", 96), ("A4", 3, "PETG", "FFFFFFFF", None)])
        self.assertEqual(trays[0]["name"], "PLA Matte")
        self.assertNotIn("SECRET", repr(trays))
        self.assertEqual(onboard.ams_trays([]), [])

    def test_units_for_the_app(self):
        units = onboard.ams_units(ALPHA_AMS, tray_now=2)
        self.assertEqual(len(units), 1)
        u = units[0]
        self.assertEqual((u["unit"], u["humidity"]), ("A", 5))
        self.assertEqual([s["slot"] for s in u["slots"]], ["A1", "A2", "A3", "A4"])
        self.assertTrue(u["slots"][0]["empty"] and u["slots"][1]["empty"])
        self.assertEqual((u["slots"][2]["material"], u["slots"][2]["color"], u["slots"][2]["active"]), ("PLA", "000000FF", True))
        self.assertFalse(u["slots"][3]["active"])
        self.assertNotIn("SECRET", repr(units))
        self.assertEqual(onboard.ams_units([], None), [])

    def test_slot_with_filament_but_no_data_is_unknown_not_empty(self):
        # A1 felt by the inlet sensor (bit 0) but no tag read: "Unknown". A2 (bit 1 off) stays empty.
        u = onboard.ams_units(ALPHA_AMS, tray_now=2, exist_bits="d")[0]
        self.assertEqual((u["slots"][0]["empty"], u["slots"][0]["material"], u["slots"][0].get("unknown")), (False, "Unknown", True))
        self.assertTrue(u["slots"][1]["empty"])
        self.assertEqual(u["slots"][2]["material"], "PLA")

    def test_pick_exact_colour(self):
        self.assertEqual(onboard.pick_feed("PLA", "#000000", onboard.ams_trays(ALPHA_AMS), None), (2, None))

    def test_pick_external_when_it_matches(self):
        self.assertEqual(onboard.pick_feed("PLA", "#AC95D5", [], {"material": "PLA", "color": "AC95D5FF"}), (254, None))

    def test_pick_only_tray_of_that_material_with_note(self):
        idx, note = onboard.pick_feed("PETG", "#FF0000", onboard.ams_trays(ALPHA_AMS), None)
        self.assertEqual(idx, 3)
        self.assertIn("A4", note)

    def test_pick_refuses_naming_what_is_loaded(self):
        with self.assertRaises(ValueError) as e:
            onboard.pick_feed("ABS", "#000000", onboard.ams_trays(ALPHA_AMS), {"material": None, "color": None})
        self.assertIn("A3 PLA", str(e.exception))
        self.assertIn("A4 PETG", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class NewerDeviceCa(unittest.TestCase):
    def test_p2s_device_ca_is_a_bambu_printer(self):
        der = self_signed("BBL Device CA N7-V2")
        got = onboard.probe("h", "12345678", cert=lambda h, t: der, connect=lambda *a: (0, True))
        self.assertEqual(got["der"], der)

    def test_other_issuer_still_refused(self):
        with self.assertRaises(onboard.ProbeError):
            onboard.probe("h", "12345678", cert=lambda h, t: self_signed("Some Router CA"), connect=lambda *a: (0, True))
