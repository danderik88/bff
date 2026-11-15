"""Filament catalog: generated from Orca profiles, filtered per printer, picked by vendor in the app."""
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from bff_mcp import server  # noqa: E402
import filament_catalog  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402
from test_bff_tools_more import PROFILE  # noqa: E402

CATALOG = ROOT / "profiles" / "filaments.json"
BASE = {"name": "Bambu ASA @base", "filament_id": "GFB00", "filament_vendor": ["Bambu Lab"], "filament_type": ["ASA"],
        "nozzle_temperature_range_low": ["240"], "nozzle_temperature_range_high": ["270"]}
CHILD = {"name": "Bambu ASA @BBL P2S", "inherits": "Bambu ASA @base", "instantiation": "true",
         "compatible_printers": ["Bambu Lab P2S 0.4 nozzle"]}
GENERIC = {"name": "Generic PLA", "instantiation": "true", "filament_id": "GFL99", "filament_vendor": ["Generic"],
           "filament_type": ["PLA"], "nozzle_temperature_range_low": ["190"], "nozzle_temperature_range_high": ["230"],
           "compatible_printers": []}


def fake_system(tmp):
    """A tiny Orca system folder: a base, a P2S child that inherits it, and a generic valid everywhere."""
    (tmp / "BBL" / "filament").mkdir(parents=True)
    (tmp / "OrcaFilamentLibrary" / "filament" / "Generic").mkdir(parents=True)
    (tmp / "BBL" / "BBL.json").write_text(json.dumps({"version": "02.02.01.51"}))
    (tmp / "BBL" / "filament" / "Bambu ASA @base.json").write_text(json.dumps(BASE))
    (tmp / "BBL" / "filament" / "Bambu ASA @BBL P2S.json").write_text(json.dumps(CHILD))
    (tmp / "OrcaFilamentLibrary" / "filament" / "Generic" / "Generic PLA.json").write_text(json.dumps(GENERIC))


class Generator(unittest.TestCase):
    def test_entries_models_and_vendor_order(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            fake_system(tmp)
            doc = filament_catalog.build(tmp)
        self.assertEqual(doc["generated"], "02.02.01.51")
        self.assertEqual(doc["filaments"], [
            {"id": "GFB00", "name": "Bambu ASA", "vendor": "Bambu Lab", "type": "ASA", "min": 240, "max": 270,
             "models": ["Bambu Lab P2S"]},
            {"id": "GFL99", "name": "Generic PLA", "vendor": "Generic", "type": "PLA", "min": 190, "max": 230,
             "models": "all"}])

    def test_base_without_instantiation_is_skipped(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            fake_system(tmp)
            (tmp / "BBL" / "filament" / "Bambu ASA @BBL P2S.json").unlink()
            doc = filament_catalog.build(tmp)
        self.assertEqual([e["name"] for e in doc["filaments"]], ["Generic PLA"])

    def test_generic_name_gets_generic_vendor(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            fake_system(tmp)
            (tmp / "OrcaFilamentLibrary" / "filament" / "Generic" / "Generic PETG.json").write_text(json.dumps(
                {**GENERIC, "name": "Generic PETG", "filament_id": "GFG00", "filament_vendor": ["eSUN"],
                 "filament_type": ["PETG"]}))
            doc = filament_catalog.build(tmp)
        petg = next(e for e in doc["filaments"] if e["name"] == "Generic PETG")
        self.assertEqual(petg["vendor"], "Generic")

    def test_one_entry_per_vendor_and_name_bambu_id_wins(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            fake_system(tmp)
            (tmp / "OrcaFilamentLibrary" / "filament" / "Bambu ASA.json").write_text(json.dumps(
                {**CHILD, "name": "Bambu ASA", "filament_id": "OFASA01",
                 "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"]}))
            doc = filament_catalog.build(tmp)
        asa = [e for e in doc["filaments"] if e["name"] == "Bambu ASA"]
        self.assertEqual(len(asa), 1)
        self.assertEqual(asa[0]["id"], "GFB00")
        self.assertEqual(asa[0]["models"], ["Bambu Lab P2S", "Bambu Lab X1 Carbon"])

    def test_models_all_wins_on_duplicate(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            fake_system(tmp)
            (tmp / "OrcaFilamentLibrary" / "filament" / "Bambu ASA.json").write_text(json.dumps(
                {**CHILD, "name": "Bambu ASA", "filament_id": "OFASA01", "compatible_printers": []}))
            doc = filament_catalog.build(tmp)
        asa = next(e for e in doc["filaments"] if e["name"] == "Bambu ASA")
        self.assertEqual(asa["models"], "all")


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        (tmp / "p2s.json").write_text(json.dumps({**PROFILE, "slicer_model": "Bambu Lab P2S"}))
        (tmp / "h2d.json").write_text(json.dumps({**PROFILE, "slicer_model": "Bambu Lab X1 Carbon"}))
        self.system = tmp / "system"
        fake_system(self.system)
        (tmp / "filaments.json").write_text(json.dumps(filament_catalog.build(self.system)))
        server.CFG.update(printers={"p2s": 1, "h2d": 1}, tokens={}, control=True, profiles=str(tmp),
                          public_url="http://bff")
        server.open_db(str(tmp / "state.db"))
        self.link = server.LINKS["p2s"] = direct_tests.FakeLink(state="IDLE")

    def test_known_model_gets_its_profiles_and_all(self):
        profiles = server.spool_profiles("p2s")
        self.assertEqual([(p["id"], p["name"], p["material"], p["vendor"], p["min"], p["max"]) for p in profiles], [
            ("GFB00", "Bambu ASA", "ASA", "Bambu Lab", 240, 270),
            ("GFL99", "Generic PLA", "PLA", "Generic", 190, 230)])

    def test_unknown_model_gets_only_all(self):
        self.assertEqual([p["id"] for p in server.spool_profiles("h2d")], ["GFL99"])

    def test_set_spool_accepts_a_catalog_id(self):
        server.tool_set_spool({"printer": "p2s", "profile_id": "GFB00", "color": "FFFFFFFF",
                               "idempotency_key": "key-" + secrets.token_hex(6)})
        sent = [p for p in self.link.published if p.get("print", {}).get("command") == "ams_filament_setting"][-1]["print"]
        self.assertEqual((sent["tray_info_idx"], sent["tray_type"], sent["nozzle_temp_min"], sent["nozzle_temp_max"]),
                         ("GFB00", "ASA", 240, 270))


@unittest.skipUnless(CATALOG.exists(), "run tools/filament_catalog.py on a Mac with OrcaSlicer")
class CommittedCatalog(unittest.TestCase):
    def test_bambu_asa_for_p2s(self):
        entries = json.loads(CATALOG.read_text())["filaments"]
        asa = next(e for e in entries if e["name"] == "Bambu ASA")
        self.assertTrue(asa["models"] == "all" or "Bambu Lab P2S" in asa["models"])
        self.assertEqual(asa["id"], "GFB01")  # Bambu's own id: the AMS recognises it


class App(unittest.TestCase):
    def test_vendor_and_filament_selects(self):
        html = (ROOT / "src" / "bff_mcp" / "app.html").read_text()
        self.assertIn('<label class="fld">Vendor<select id="fvendor">', html)
        self.assertIn('<label class="fld">Filament<select id="fprof">', html)


if __name__ == "__main__":
    unittest.main()
