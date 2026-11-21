import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "pygmy"))
import moonraker_link as m  # noqa: E402


class StandardLights(unittest.TestCase):
    OBJECTS = ["output_pin caselight", "neopixel chamber_led", "neopixel btt_mini12864", "output_pin beeper",
               "led sb_leds", "gcode_macro LIGHT_ON", "fan_generic aux"]

    def test_found_by_kind_and_name(self):
        self.assertEqual(m.standard_lights(self.OBJECTS),
                         ["output_pin caselight", "neopixel chamber_led", "led sb_leds"])

    def test_states(self):
        raw = {"output_pin caselight": {"value": 0.0}, "neopixel chamber_led": {"color_data": [[0, 0, 0, 0.5]]}}
        self.assertEqual([l["on"] for l in m.light_states(raw, ["output_pin caselight", "neopixel chamber_led",
                                                                "led sb_leds"])], [False, True, None])

    def link(self, lights, light=None):
        link = m.MoonrakerLink("k", "h", light=light)
        link.lights, link.sent = lights, []
        link.gcode = lambda text: link.sent.append(text) or True
        return link

    def test_toggle_drives_all(self):
        link = self.link(["output_pin caselight", "neopixel chamber_led"])
        self.assertTrue(link.command("light", on=True))
        self.assertEqual(link.sent, ["SET_PIN PIN=caselight VALUE=1\n"
                                     "SET_LED LED=chamber_led RED=1 GREEN=1 BLUE=1 WHITE=1"])

    def test_one_light_and_unknown_refused(self):
        link = self.link(["output_pin caselight", "neopixel chamber_led"])
        self.assertTrue(link.command("light", on=False, light="output_pin caselight"))
        self.assertEqual(link.sent, ["SET_PIN PIN=caselight VALUE=0"])
        self.assertFalse(link.command("light", on=True, light="output_pin beeper"))

    def test_snapshot_main_and_wled_extra(self):
        link = self.link(["output_pin caselight"], light={"led": None, "on": None, "off": None, "wled": "light"})
        link.raw = {"output_pin caselight": {"value": 1}}
        link.wled = {"light": {"status": "off"}}
        link.last_report = link.clock()
        snap = link.snapshot()
        self.assertTrue(snap["chamber_light"])
        self.assertEqual(snap["lights"], [{"name": "output_pin caselight", "label": "Caselight", "on": True},
                                          {"name": "wled", "label": "WLED", "on": False}])

    def test_without_standard_lights_wled_stays_main(self):
        link = self.link([], light={"led": None, "on": None, "off": None, "wled": "light"})
        link.wled = {"light": {"status": "on"}}
        link.last_report = link.clock()
        snap = link.snapshot()
        self.assertTrue(snap["chamber_light"])
        self.assertEqual(snap["lights"], [])


if __name__ == "__main__":
    unittest.main()
