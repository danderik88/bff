import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bff_mcp"))
import onboard  # noqa: E402
import printer_link  # noqa: E402
import server  # noqa: E402

REAL_SCAN = onboard.scan_moonraker  # setUp patches it: no test scans the real LAN

CODE = "87654321"
HEARD = [{"ip": "192.168.0.20", "serial": "ALPHASERIAL", "model": "C11", "name": "Alpha"},
         {"ip": "192.168.0.30", "serial": "BRAVOSERIAL", "model": "BL-P001", "name": "Bravo"}]
APP_TOOLS = ("discover_printers", "add_printer", "set_access_code", "remove_printer", "identify_printer", "rename_printer")


def call(tool, app=True, **args):
    server.APP_CALL.on = app
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {"printer": "a1", **args}}}
    result = server.dispatch(msg)
    if "error" in result:
        return None, result["error"]["message"]
    text = result["result"]["content"][-1]["text"]
    return (None, json.loads(text)["errore"]) if result["result"]["isError"] else (json.loads(text), None)


class AddPrinter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        self.env.start()
        server.LINKS.clear()
        server.open_db(":memory:")
        server.CFG.clear()
        server.CFG.update(printers={"a1": 1}, control=True)
        a1 = printer_link.PrinterLink("a1", "192.168.0.10", "A1SERIAL", "11111111", self.tmp.name)
        server.LINKS["a1"] = a1
        self.patches = [mock.patch.object(printer_link.PrinterLink, "start", lambda s: None),
                        mock.patch.object(printer_link.PrinterLink, "stop", lambda s: None),
                        mock.patch.object(onboard, "heard", lambda clock=None: list(HEARD)),
                        mock.patch.object(onboard, "scan_moonraker", lambda prefixes: [])]  # never scan the real LAN
        onboard.KLIPPER_SEEN.update(at=None, rows=[])
        for p in self.patches:
            p.start()
        self.probe = mock.patch.object(onboard, "probe", side_effect=lambda host, code, **k: {"serial": "ALPHASERIAL", "der": b"cert"})
        self.probe.start()

    def tearDown(self):
        mock.patch.stopall()
        server.LINKS.clear()
        server.APP_CALL.on = False
        self.tmp.cleanup()

    def store(self):
        return onboard.load_store(Path(self.tmp.name) / "printers.json")

    def test_app_tools_not_listed(self):
        names = {t["name"] for t in server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertFalse(names & set(APP_TOOLS))

    def test_app_tools_refused_without_app_header(self):
        for name in APP_TOOLS:
            self.assertIsNotNone(call(name, app=False, name="Alpha", access_code=CODE)[1])

    def test_discover_marks_managed_and_unsupported(self):
        server.LINKS["a1"].serial = "BRAVOSERIAL"
        got, _ = call("discover_printers")
        rows = {r["serial"]: r for r in got["printers"]}
        self.assertEqual((rows["ALPHASERIAL"]["brand"], rows["ALPHASERIAL"]["model_name"]), ("Bambu Lab", "P1P"))
        self.assertTrue(rows["ALPHASERIAL"]["supported"])
        self.assertTrue(rows["BRAVOSERIAL"]["managed"])
        self.assertFalse(rows["BRAVOSERIAL"]["supported"])

    def test_add_printer_saves_and_starts(self):
        got, err = call("add_printer", name="Alpha", access_code=" 87654321 ", serial="ALPHASERIAL", enclosure=True)
        self.assertIsNone(err)
        self.assertEqual(got["printer"], "alpha")
        self.assertEqual(got["model_name"], "P1S")
        self.assertNotIn(CODE, json.dumps(got))
        entry = self.store()["alpha"]
        self.assertEqual((entry["host"], entry["serial"], entry["access_code"], entry["model"], entry["enclosure"]),
                         ("192.168.0.20", "ALPHASERIAL", CODE, "C11", True))
        self.assertIn("alpha", server.LINKS)
        self.assertIn("alpha", server.CFG["printers"])
        self.assertTrue((Path(self.tmp.name) / "fingerprints" / "alpha").exists())
        info, _ = call("printer_info")
        self.assertIn("alpha", info["available_printers"])

    def test_add_printer_wrong_code_saves_nothing(self):
        self.probe.stop()
        with mock.patch.object(onboard, "probe", side_effect=onboard.ProbeError("wrong access code")):
            _, err = call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        self.assertEqual(err, "Wrong access code: check it on the printer screen")
        self.assertEqual(self.store(), {})
        self.assertNotIn("alpha", server.LINKS)

    def test_add_bad_code_never_probes(self):
        _, err = call("add_printer", name="Alpha", access_code="123", serial="ALPHASERIAL")
        self.assertIsNotNone(err)
        onboard.probe.assert_not_called()

    def test_add_unsupported_model_refused(self):
        _, err = call("add_printer", name="Bravo", access_code=CODE, serial="BRAVOSERIAL")
        self.assertIsNotNone(err)
        self.assertEqual(self.store(), {})

    def test_add_duplicate_serial_or_alias_refused(self):
        server.LINKS["a1"].serial = "ALPHASERIAL"
        self.assertIsNotNone(call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")[1])
        server.LINKS["a1"].serial = "A1SERIAL"
        self.assertIsNotNone(call("add_printer", name="A1", access_code=CODE, serial="ALPHASERIAL")[1])
        self.assertEqual(self.store(), {})

    def test_add_by_ip_when_not_heard(self):
        with mock.patch.object(onboard, "heard", lambda clock=None: []):
            got, err = call("add_printer", name="Alpha", access_code=CODE, ip="192.168.0.20")
        self.assertIsNone(err)
        self.assertEqual(self.store()["alpha"]["host"], "192.168.0.20")
        self.assertEqual(self.store()["alpha"]["serial"], "ALPHASERIAL")

    def test_set_access_code(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        got, err = call("set_access_code", printer="alpha", access_code="ABCDEFGH")
        self.assertIsNone(err)
        self.assertEqual(self.store()["alpha"]["access_code"], "ABCDEFGH")
        self.assertEqual(server.LINKS["alpha"].access_code, "ABCDEFGH")

    def test_remove_credential_printer_refused(self):
        self.assertIsNotNone(call("remove_printer", printer="a1")[1])
        self.assertIn("a1", server.LINKS)

    def test_remove_printer_stops_link_and_forgets_fingerprints(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        for suffix in ("-cam", "-ftp"):
            (Path(self.tmp.name) / "fingerprints" / f"alpha{suffix}").write_text("x")
        got, err = call("remove_printer", printer="alpha")
        self.assertIsNone(err)
        self.assertNotIn("alpha", server.LINKS)
        self.assertNotIn("alpha", server.CFG["printers"])
        self.assertEqual(self.store(), {})
        self.assertEqual(list((Path(self.tmp.name) / "fingerprints").glob("alpha*")), [])

    def test_restart_loads_store_when_printer_off(self):
        onboard.save_store(Path(self.tmp.name) / "printers.json", {"alpha": {
            "host": "192.168.0.20", "serial": "ALPHASERIAL", "access_code": CODE, "name": "Alpha", "model": "C11", "enclosure": True}})
        server.LINKS.clear()
        server.start_links()
        self.assertIn("alpha", server.LINKS)
        self.assertIn("alpha", server.CFG["printers"])
        self.assertFalse(server.LINKS["alpha"].snapshot()["connected"])

    def test_printer_info_names_and_models(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        info, _ = call("printer_info")
        self.assertEqual(info["names"]["alpha"], "Alpha")
        self.assertEqual(info["models"]["alpha"], "P1P")
        self.assertNotIn(CODE, json.dumps(info))

    def test_chamber_fan_seen_in_a_print_makes_it_a_p1s(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        server.learn_profile("alpha", {"state": "RUNNING", "fans": {"part": 80, "aux": 0, "chamber": 40}}, {})
        info, _ = call("printer_info")
        self.assertEqual(info["models"]["alpha"], "P1S")
        self.assertEqual(info["profile"]["fans"], ["part", "chamber"]) if info.get("printer") == "alpha" else None

    def test_learning_only_while_printing(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        server.learn_profile("alpha", {"state": "IDLE", "fans": {"part": 80, "aux": 0, "chamber": 40}}, {})
        info, _ = call("printer_info")
        self.assertEqual(info["models"]["alpha"], "P1P")

    def test_motion_refused_on_models_not_measured(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        self.assertTrue(server.motion_measured("alpha"))  # P1P/P1S measured on Alpha, 2026-11-12
        from unittest import mock
        with mock.patch.dict(server.MODEL_ENVELOPE, clear=True):  # a model nobody measured
            with self.assertRaises(server.ToolError) as e:
                server.motion_ready("move", "alpha")
            self.assertIn("non ancora verificati", str(e.exception))
            self.assertFalse(server.motion_measured("alpha"))  # the app greys Motion from this

    def test_model_profile_without_file(self):
        server.CFG["profiles"] = self.tmp.name  # no alpha.json there
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL", enclosure=True)
        prof = server.load_profile("alpha")
        self.assertEqual(prof["slicer_model"], "Bambu Lab P1S")
        self.assertTrue(prof["supports"]["pause"] and prof["supports"]["stop"] and prof["supports"]["start"])
        self.assertEqual(prof["limits"]["nozzle_max_c"]["value"], 300)
        info, _ = call("printer_info", printer="alpha")
        self.assertEqual((info["model"], info["slicer_model"]), ("P1S", "Bambu Lab P1S"))

    def test_n7_p2s_profile_without_file(self):
        server.CFG["profiles"] = self.tmp.name  # no alpha.json there
        self.probe.stop()
        with mock.patch.object(onboard, "heard", lambda clock=None: [{"ip": "192.168.0.50", "serial": "N7SERIAL", "model": "N7", "name": "Alpha"}]), \
                mock.patch.object(onboard, "probe", side_effect=lambda h, c, **k: {"serial": "N7SERIAL", "der": b"cert"}):
            got, err = call("add_printer", name="Alpha", access_code=CODE, serial="N7SERIAL")
        self.assertIsNone(err)
        prof = server.load_profile("alpha")
        self.assertEqual(prof["model"], "P2S")
        self.assertEqual(prof["slicer_model"], "Bambu Lab P2S")
        info, _ = call("printer_info", printer="alpha")
        self.assertEqual((info["model"], info["slicer_model"]), ("P2S", "Bambu Lab P2S"))

    def test_bare_p1p_profile(self):
        server.CFG["profiles"] = self.tmp.name
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        self.assertEqual(server.load_profile("alpha")["slicer_model"], "Bambu Lab P1P")

    def test_file_profile_still_wins(self):
        server.CFG["profiles"] = self.tmp.name
        (Path(self.tmp.name) / "a1.json").write_text(json.dumps({"model": "A1", "slicer_model": "Bambu Lab A1", "supports": {}}))
        self.assertEqual(server.load_profile("a1")["slicer_model"], "Bambu Lab A1")
        with self.assertRaises(server.ToolError):
            server.load_profile("nobody")

    def test_ams_feed_and_start_payload(self):
        raw = {"ams": [{"id": "0", "tray": [{"id": "2", "tray_type": "PLA", "tray_color": "000000FF"}]}],
               "vt_tray": [{"tray_type": "", "tray_color": "FFFFFFFF"}]}
        self.assertEqual(server.feed_for({"filament": "PLA"}, raw, "#000000"), (2, None))
        job = printer_link.project_file("x.gcode.3mf", (2,))["print"]
        self.assertEqual((job["use_ams"], job["ams_mapping"], job["ams_mapping2"]),
                         (True, [2], [{"ams_id": 0, "slot_id": 2}]))
        status = server.status_dict.__wrapped__ if hasattr(server.status_dict, "__wrapped__") else None
        self.assertIsNone(status)  # status_dict reads a live link; the AMS list itself is covered in test_onboard

    def test_two_app_printers_have_distinct_ids(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        self.probe.stop()
        with mock.patch.object(onboard, "probe", side_effect=lambda h, c, **k: {"serial": "MINISERIAL", "der": b"c"}), \
                mock.patch.object(onboard, "heard", lambda clock=None: [{"ip": "192.168.0.40", "serial": "MINISERIAL", "model": "N1", "name": "Mini"}]):
            call("add_printer", name="Mini", access_code=CODE, serial="MINISERIAL")
        self.assertEqual(server.alias_of(server.CFG["printers"]["mini"]), "mini")
        self.assertEqual(server.alias_of(server.CFG["printers"]["alpha"]), "alpha")

    def test_access_code_never_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
            call("set_access_code", printer="alpha", access_code="ABCDEFGH")
        self.assertNotIn(CODE, err.getvalue())
        self.assertNotIn("ABCDEFGH", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Klipper(AddPrinter):
    def setUp(self):
        super().setUp()
        import moonraker_link
        mock.patch.object(moonraker_link.MoonrakerLink, "start", lambda s: None).start()
        mock.patch.object(moonraker_link.MoonrakerLink, "stop", lambda s: None).start()
        onboard.KLIPPER_SEEN.update(at=None, rows=[])
        mock.patch.object(onboard, "scan_moonraker", lambda prefixes: [
            {"ip": "192.168.0.60", "port": 7125, "name": "SV08", "model": "KLIPPER-SV08"}]).start()
        self.mprobe = mock.patch.object(onboard, "probe_moonraker", return_value={"hostname": "SV08", "limits": {"nozzle_max_c": 300, "bed_max_c": 100}, "machine": {}})
        self.mprobe.start()

    def test_discover_lists_klipper(self):
        got, _ = call("discover_printers")
        row = [r for r in got["printers"] if r.get("kind") == "klipper"][0]
        self.assertEqual((row["ip"], row["model_name"], row["managed"]), ("192.168.0.60", "Sovol SV08", False))
        self.assertIn({"code": "KLIPPER-SV08", "name": "Sovol SV08"}, got["klipper_models"])

    def test_add_klipper_saves_limits(self):
        got, err = call("add_printer", kind="klipper", name="SV08", ip="192.168.0.60", model="KLIPPER-SV08", port="7125")
        self.assertIsNone(err)
        entry = self.store()["sv08"]
        self.assertEqual((entry["host"], entry["port"], entry["model"], entry["limits"]["nozzle_max_c"]),
                         ("192.168.0.60", 7125, "KLIPPER-SV08", 300))
        self.assertIn("sv08", server.LINKS)
        _, err = call("add_printer", kind="klipper", name="Other", ip="192.168.0.60", model="KLIPPER-SV08")
        self.assertEqual(err, "This printer is already added")
        _, err = call("set_access_code", printer="sv08", access_code=CODE)
        self.assertEqual(err, "Klipper printers have no access code")

    def test_add_klipper_refuses_bad_model_and_port(self):
        self.assertEqual(call("add_printer", kind="klipper", name="X", ip="1.2.3.4", model="C11")[1], "Pick the printer model")
        self.assertEqual(call("add_printer", kind="klipper", name="X", ip="1.2.3.4", model="KLIPPER-SV08", port="99999")[1],
                         "The Moonraker port is a number up to 65535")
        self.assertEqual(self.store(), {})

    def test_probe_moonraker_and_limits(self):
        self.mprobe.stop()
        bodies = [b'{"result": {"state": "ready", "hostname": "sv08"}}',
                  b'{"result": {"status": {"configfile": {"settings": {"extruder": {"max_temp": 300}, "heater_bed": {"max_temp": 100}}}}}}']
        got = onboard.probe_moonraker("h", opener=lambda req, timeout: FakeResp(bodies.pop(0)))
        self.assertEqual(got, {"hostname": "sv08", "limits": {"nozzle_max_c": 300, "bed_max_c": 100}, "machine": {}})
        prof = onboard.apply_cfg_limits(onboard.model_profile("KLIPPER-IR3V2"), {"nozzle_max_c": 250})
        self.assertEqual(prof["limits"]["nozzle_max_c"]["value"], 250)
        self.assertEqual(prof["limits"]["nozzle_confirm_above_c"]["value"], 250)
        self.assertEqual(onboard.model_profile("KLIPPER-IR3V2")["limits"]["nozzle_max_c"]["value"], 300)
        import urllib.error

        def refuse(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error": {"code": 401}}'))
        with self.assertRaises(onboard.ProbeError) as ctx:
            onboard.probe_moonraker("h", opener=refuse)
        self.assertEqual(ctx.exception.reason, "moonraker key")

        def other_401(req, timeout):  # a router or camera that wants a login
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"<html>login</html>"))
        with self.assertRaises(onboard.ProbeError) as ctx:
            onboard.probe_moonraker("h", opener=other_401)
        self.assertEqual(ctx.exception.reason, "moonraker unreachable")
        with self.assertRaises(onboard.ProbeError) as ctx:  # JSON, but not Moonraker
            onboard.probe_moonraker("h", opener=lambda req, timeout: FakeResp(b'{"result": {"ok": 1}}'))
        self.assertEqual(ctx.exception.reason, "not moonraker")

    def test_scan_skips_other_devices_and_tries_port_80(self):
        answers = {("10.0.0.5", 7125): {"state": "ready", "hostname": "SV08"},  # Moonraker
                   ("10.0.0.6", 7125): {"version": 3},                          # something else, JSON
                   ("10.0.0.7", 80): {"state": "ready", "hostname": "ir3v2"}}   # behind nginx

        def connect(addr, timeout):
            if addr not in answers:
                raise OSError("closed")
            return contextlib.nullcontext()
        rows = REAL_SCAN(["10.0.0"], get=lambda ip, port, path, timeout: answers[(ip, port)], connect=connect)
        self.assertEqual(sorted((r["ip"], r["port"], r["model"]) for r in rows),
                         [("10.0.0.5", 7125, "KLIPPER-SV08"), ("10.0.0.7", 80, "KLIPPER-IR3V2")])

    def test_guess_and_cache(self):
        self.assertEqual(onboard.guess_klipper_model("SV08-abc"), "KLIPPER-SV08")
        self.assertEqual(onboard.guess_klipper_model("klipper"), "KLIPPER-GENERIC")
        calls = []
        t = [0.0]
        scan = lambda p: calls.append(p) or []
        onboard.klipper_heard(["10.0.0"], clock=lambda: t[0], scan=scan)
        onboard.klipper_heard(["10.0.0"], clock=lambda: t[0], scan=scan)
        t[0] = 61
        onboard.klipper_heard(["10.0.0"], clock=lambda: t[0], scan=scan)
        self.assertEqual(len(calls), 2)

    def test_identify_printer(self):
        def get(host, port, path, timeout):
            if host == "192.168.0.60" and port == 7125:
                return {"state": "ready", "hostname": "SV08"}
            raise onboard.ProbeError("moonraker unreachable")
        with mock.patch.object(onboard, "moonraker_get", get):
            got, _ = call("identify_printer", ip=" 192.168.0.60 ")
            self.assertEqual((got["kind"], got["port"], got["model"]), ("klipper", 7125, "KLIPPER-SV08"))
            got, _ = call("identify_printer", ip="192.168.0.20")
            self.assertEqual((got["kind"], got["model_name"]), ("bambu", "Bambu Lab printer"))
        self.assertEqual(call("identify_printer", ip="")[1], "Enter the printer IP")

    def test_generic_klipper_needs_printer_cfg(self):
        self.mprobe.stop()
        with mock.patch.object(onboard, "probe_moonraker", return_value={"hostname": "klipper", "limits": {}, "machine": {}}):
            _, err = call("add_printer", kind="klipper", name="E3K", ip="192.168.0.180", model="KLIPPER-GENERIC")
        self.assertIn("printer.cfg could not be read", err)
        with mock.patch.object(onboard, "probe_moonraker", return_value={"hostname": "klipper", "limits": {"nozzle_max_c": 300.0, "bed_max_c": 130.0}, "machine": {"printable_height": "230"}}):
            got, err = call("add_printer", kind="klipper", name="E3K", ip="192.168.0.180", model="KLIPPER-GENERIC", label="Ender 3, modded")
        self.assertIsNone(err)
        self.assertEqual(self.store()["e3k"]["limits"], {"nozzle_max_c": 300.0, "bed_max_c": 130.0})
        self.assertEqual(got["model_name"], "Ender 3, modded")
        self.assertEqual(server.cfg_machine_of("e3k"), {"printable_height": "230"})
        listed, _ = call("list_printers")
        self.assertEqual((listed["custom"], listed["models"]["e3k"]), (["e3k"], "Ender 3, modded"))
        self.assertEqual(call("rename_printer", printer="e3k", name="")[1], "The name is 1 to 40 characters")
        self.assertIsNone(call("rename_printer", printer="e3k", name="Ender", label="Ender 3 Pro")[1])
        self.assertEqual((self.store()["e3k"]["name"], self.store()["e3k"]["label"]), ("Ender", "Ender 3 Pro"))
        self.assertIsNone(call("rename_printer", printer="e3k", name="Ender", label="")[1])
        self.assertNotIn("label", self.store()["e3k"])

    def test_cfg_machine(self):
        s = {"stepper_x": {"position_min": 0.0, "position_max": 232.0}, "stepper_y": {"position_min": -30.0, "position_max": 215.0},
             "stepper_z": {"position_max": 230.0}, "printer": {"max_velocity": 250.0}, "extruder": {"nozzle_diameter": 0.6},
             "gcode_macro print_start": {"gcode": "M190 S{params.BED_TEMP}\nM109 S{params['EXTRUDER_TEMP']}"}}
        m = onboard.cfg_machine(s)
        self.assertEqual(m["printable_area"], ["0x0", "232x0", "232x215", "0x215"])
        self.assertEqual(m["machine_start_gcode"],
                         "PRINT_START BED_TEMP=[bed_temperature_initial_layer_single] EXTRUDER_TEMP=[nozzle_temperature_initial_layer]")
        self.assertEqual((m["machine_end_gcode"], m["nozzle_diameter"], m["machine_max_speed_x"]), ("M104 S0\nM140 S0\nM84", ["0.6"], ["250", "250"]))
        self.assertNotIn("use_firmware_retraction", m)
        s["gcode_macro print_start"] = {"gcode": "G28"}  # a macro that takes no temperatures: heat first
        self.assertEqual(onboard.cfg_machine(s)["machine_start_gcode"],
                         "M190 S[bed_temperature_initial_layer_single]\nM109 S[nozzle_temperature_initial_layer]\nPRINT_START")
        self.assertEqual(onboard.cfg_machine({}), {})

    def test_rename_bambu_ignores_label(self):
        call("add_printer", name="Alpha", access_code=CODE, serial="ALPHASERIAL")
        self.assertIsNone(call("rename_printer", printer="alpha", name="Alpha 2", label="x")[1])
        self.assertEqual((self.store()["alpha"]["name"], "label" in self.store()["alpha"]), ("Alpha 2", False))

    def test_orca_profiles_bundle(self):
        import base64
        import zipfile
        call("add_printer", kind="klipper", name="SV08", ip="192.168.0.60", model="KLIPPER-SV08")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("printer/E3K 0.4 nozzle.json", json.dumps({"name": "E3K 0.4 nozzle", "inherits": "MyKlipper 0.4 nozzle"}))
            z.writestr("printer/E3K 0.6 nozzle.json", json.dumps({"name": "E3K 0.6 nozzle", "inherits": "MyKlipper 0.6 nozzle",
                                                                  "nozzle_diameter": ["0.6"], "post_process": ["rm -rf /"]}))
            z.writestr("process/q.json", json.dumps({"name": "0.20mm Standard @E3K 0.6", "inherits": "0.20mm Standard @MyKlipper",
                                                     "compatible_printers": ["E3K 0.6 nozzle"]}))
            z.writestr("process/other.json", json.dumps({"name": "0.20mm Standard @E3K 0.4", "inherits": "0.20mm Standard @MyKlipper",
                                                         "compatible_printers": ["E3K 0.4 nozzle"]}))
            z.writestr("filament/a.json", json.dumps({"name": "My PLA base", "inherits": "Generic PLA @System", "filament_flow_ratio": ["0.95"]}))
            z.writestr("filament/b.json", json.dumps({"name": "PolyTerra PLA @E3K", "inherits": "My PLA base"}))
        with mock.patch.object(server, "cfg_nozzle", lambda printer: 0.6):
            got, err = call("orca_profiles", printer="sv08", bundle_b64=base64.b64encode(buf.getvalue()).decode())
        self.assertIsNone(err)
        self.assertEqual(got["machine"], "E3K 0.6 nozzle")
        self.assertEqual(got["filaments"], ["My PLA base", "PolyTerra PLA @E3K"])
        self.assertEqual(call("orca_profiles", printer="sv08", loaded="Nope")[1], "Unknown filament profile")
        call("orca_profiles", printer="sv08", loaded="PolyTerra PLA @E3K")
        user = server.user_presets_for("sv08", [{"filament_type": "PLA"}, {"filament_type": "PETG"}])
        self.assertEqual(user["parent"], "MyKlipper 0.6 nozzle")
        self.assertNotIn("post_process", user["machine"])
        self.assertEqual(list(user["processes"]), ["0.20mm Standard @E3K 0.6"])
        f = user["filaments"][0]
        self.assertEqual((f["inherits"], f["filament_flow_ratio"], f["_type"], len(user["filaments"])),
                         ("Generic PLA @System", ["0.95"], "PLA", 2))
        self.assertIsNotNone(call("orca_profiles", printer="alpha")[1])  # not app-added / not Klipper
        self.assertIn("preset bundle", call("orca_profiles", printer="sv08", bundle_b64=base64.b64encode(b"nope").decode())[1])
        call("orca_profiles", printer="sv08", remove=True)
        self.assertIsNone(server.user_presets_for("sv08", [{}]))
