"""HA entities are English by default: names, select options and state values."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pygmy import ha_mqtt  # noqa: E402

ITALIAN = ("Stato", "Piatto", "Ugello", "Ventola", "Luce", "Velocità", "Pausa", "Riprendi", "Ultima", "silenziosa")


class DiscoveryEnglish(unittest.TestCase):
    def setUp(self):
        # The full set: every fan, every option, AMS slots and units.
        self.pairs = ha_mqtt.discovery("a1", slots=("A1", "B2"), units=("A", "B"))

    def test_no_italian_in_names_or_options(self):
        for topic, cfg in self.pairs:
            text = " ".join([str(cfg.get("name") or ""), *(cfg.get("options") or [])])
            for word in ITALIAN:
                with self.subTest(topic=topic, word=word):
                    self.assertNotIn(word, text)

    def test_english_names_are_there(self):
        names = {cfg.get("name") for _, cfg in self.pairs}
        for name in ("Status", "Bed", "Last print", "Nozzle", "Pause", "Resume"):
            self.assertIn(name, names)

    def test_silent_option_maps_to_speed_1(self):
        select = dict(self.pairs)["homeassistant/select/pygmy_a1/speed/config"]
        self.assertEqual(select["options"], ["silent", "standard", "sport", "ludicrous"])
        self.assertEqual(ha_mqtt.parse_command("pygmy/a1/cmd/speed", b"silent", ("a1",)), ("a1", "speed", 1))
        self.assertEqual(ha_mqtt.parse_command("pygmy/a1/cmd/speed", b"ludicrous", ("a1",)), ("a1", "speed", 4))
        self.assertIsNone(ha_mqtt.parse_command("pygmy/a1/cmd/speed", b"silenziosa", ("a1",)))

    def test_state_defaults_are_english(self):
        self.assertIn("'none'", dict(self.pairs)["homeassistant/sensor/pygmy_a1/error/config"]["value_template"])
        spool = dict(self.pairs)["homeassistant/sensor/pygmy_a1/spool/config"]["value_template"]
        self.assertIn("'none'", spool)
        self.assertIn("'empty'", dict(self.pairs)["homeassistant/sensor/pygmy_a1/ams_a1/config"]["value_template"])
        spaghetti = dict(self.pairs)["homeassistant/sensor/pygmy_a1/spaghetti/config"]["value_template"]
        for value in ("'ok'", "'warning'", "'failure'", "'unsure'", "'off'"):
            self.assertIn(value, spaghetti)


if __name__ == "__main__":
    unittest.main()
