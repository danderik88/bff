"""Home Assistant bridge: discovery, state and the few commands HA may send."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from pygmy import ha_mqtt, server  # noqa: E402

STATUS = {"connected": True, "state": "RUNNING", "progress": 42, "remaining_time": 17, "layer_num": 3,
          "total_layers": 90, "subtask_name": "x", "speed_level": 2, "hms_count": 0, "chamber_light": True,
          "temperatures": {"nozzle": 220, "nozzle_target": 220, "bed": 60, "bed_target": 60}}


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))
        return mock.Mock(rc=0)

    def subscribe(self, topic):
        self.subscribed = topic


class HaBridgeTest(unittest.TestCase):
    def bridge(self, **kw):
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], kw.get("read", lambda a: STATUS), kw.get("run", mock.Mock()))
        b.client = FakeClient()
        return b

    def test_ha_accepts_only_known_actions(self):
        ok = {("pygmy/a1/cmd/pause", b"PRESS"): ("a1", "pause", None),
              ("pygmy/a1/cmd/stop", b"PRESS"): ("a1", "stop", None),
              ("pygmy/a1/cmd/light", b"ON"): ("a1", "light", True),
              ("pygmy/a1/cmd/light", b"OFF"): ("a1", "light", False)}
        for (topic, payload), want in ok.items():
            self.assertEqual(ha_mqtt.parse_command(topic, payload, ("a1",)), want)
        for topic, payload in (("pygmy/a1/cmd/light", b"PRESS"), ("pygmy/a1/cmd/pause", b"ON"),
                               ("pygmy/p1s/cmd/pause", b"PRESS"), ("pygmy/a1/cmd/gcode", b"PRESS"),
                               ("pygmy/a1/cmd/pause/x", b"PRESS"), ("other/a1/cmd/pause", b"PRESS")):
            with self.subTest(topic=topic, payload=payload):
                self.assertIsNone(ha_mqtt.parse_command(topic, payload, ("a1",)))

    def test_ha_cannot_start_a_print(self):
        for action in ("start", "print_start", "confirm_print_start", "gcode", "move", "home"):
            self.assertIsNone(ha_mqtt.parse_command(f"pygmy/a1/cmd/{action}", b"PRESS", ("a1",)))
        topics = [cfg.get("command_topic") for _, cfg in ha_mqtt.discovery("a1")]
        self.assertEqual(sorted(t for t in topics if t), sorted(f"pygmy/a1/cmd/{a}" for a in ha_mqtt.HA_ACTIONS))

    def test_discovery_is_unique_and_marked_with_availability(self):
        pairs = ha_mqtt.discovery("a1")
        self.assertEqual(len({cfg["unique_id"] for _, cfg in pairs}), len(pairs))
        for topic, cfg in pairs:
            self.assertTrue(topic.startswith("homeassistant/") and topic.endswith("/config"))
            self.assertEqual(cfg["availability_topic"], ha_mqtt.AVAILABILITY)
            component, obj = topic.split("/")[1], topic.split("/")[3]
            self.assertEqual(cfg["default_entity_id"], f"{component}.pygmy_a1_{obj}")
        temps = [cfg for _, cfg in pairs if cfg.get("device_class") == "temperature"]
        self.assertEqual(len(temps), 4)
        self.assertTrue(all(cfg["suggested_display_precision"] == 0 for cfg in temps))

    def test_last_will_marks_bff_offline(self):
        client = mock.Mock()
        with mock.patch("paho.mqtt.client.Client", return_value=client), mock.patch("threading.Thread"):
            self.bridge().start()
        client.will_set.assert_called_once_with(ha_mqtt.AVAILABILITY, "offline", retain=True)
        b = self.bridge()
        b._on_connect(b.client, None, None, mock.Mock(is_failure=False), None)
        self.assertIn((ha_mqtt.AVAILABILITY, "online", True), b.client.sent)
        self.assertEqual(b.client.subscribed, "pygmy/+/cmd/+")
        retained = [t for t, _, r in b.client.sent if r and t.endswith("/config")]
        self.assertEqual(len(retained), len(ha_mqtt.discovery("a1")))

    def test_state_is_flat_and_sent_only_on_change(self):
        b = self.bridge()
        b.publish_states()
        b.publish_states()
        self.assertEqual(len(b.client.sent), 1)
        topic, payload, retain = b.client.sent[0]
        self.assertEqual((topic, retain), ("pygmy/a1/state", False))
        self.assertEqual(json.loads(payload)["nozzle"], 220)
        self.assertEqual(json.loads(payload)["light"], "ON")

    def test_state_read_error_is_skipped(self):
        b = self.bridge(read=mock.Mock(side_effect=server.ToolError("giù")))
        b.publish_states()
        self.assertEqual(b.client.sent, [])

    def test_command_runs_once_with_fresh_key_and_errors_are_not_retried(self):
        run = mock.Mock(side_effect=server.ToolError("no"))
        b = self.bridge(run=run)
        b._run("a1", "stop", None)
        run.assert_called_once()
        self.assertTrue(run.call_args[0][3].startswith("ha-"))

    def test_new_commands_are_accepted_and_bad_ones_are_not(self):
        ok = {("pygmy/a1/cmd/speed", b"sport"): ("a1", "speed", 3),
              ("pygmy/a1/cmd/fan_part", b"50"): ("a1", "fan_part", 50),
              ("pygmy/a1/cmd/fan_aux", b"50.0"): ("a1", "fan_aux", 50),
              ("pygmy/a1/cmd/fan_chamber", b"100"): ("a1", "fan_chamber", 100),
              ("pygmy/a1/cmd/opt_auto_recovery", b"OFF"): ("a1", "opt_auto_recovery", False),
              ("pygmy/a1/cmd/read_a1", b"PRESS"): ("a1", "read_a1", "A1"),
              ("pygmy/a1/cmd/read_d4", b"PRESS"): ("a1", "read_d4", "D4"),
              ("pygmy/a1/cmd/clear_errors", b"PRESS"): ("a1", "clear_errors", None),
              ("pygmy/a1/cmd/dismiss_spaghetti", b"PRESS"): ("a1", "dismiss_spaghetti", None)}
        for (topic, payload), want in ok.items():
            with self.subTest(topic=topic):
                self.assertEqual(ha_mqtt.parse_command(topic, payload, ("a1",)), want)
        for action, payload in (("fan_part", b"101"), ("speed", b"turbo"), ("opt_unknown", b"ON"),
                                ("read_e1", b"PRESS"), ("temperature", b"200"), ("move", b"PRESS"),
                                ("start", b"PRESS")):
            with self.subTest(action=action):
                self.assertIsNone(ha_mqtt.parse_command(f"pygmy/a1/cmd/{action}", payload, ("a1",)))

    def test_discovery_for_ams_slots_and_controls(self):
        pairs = ha_mqtt.discovery("a1", slots=("A1", "A2"), units=("A",))
        cfg = dict(pairs)
        self.assertIn("homeassistant/sensor/pygmy_a1/ams_a1/config", cfg)
        self.assertIn("homeassistant/sensor/pygmy_a1/ams_a2_remain/config", cfg)
        self.assertIn("homeassistant/sensor/pygmy_a1/ams_a2_state/config", cfg)
        self.assertNotIn("homeassistant/button/pygmy_a1/read_a2/config", cfg)
        self.assertNotIn("homeassistant/sensor/pygmy_a1/ams_a_humidity/config", cfg)
        self.assertIn("homeassistant/select/pygmy_a1/speed/config", cfg)
        self.assertIn("homeassistant/number/pygmy_a1/set_fan_aux/config", cfg)
        self.assertIn("homeassistant/switch/pygmy_a1/opt_bff_spaghetti/config", cfg)
        self.assertIn("homeassistant/camera/pygmy_a1/last_photo/config", cfg)
        self.assertEqual(len({c["unique_id"] for _, c in pairs}), len(pairs))
        ams = cfg["homeassistant/sensor/pygmy_a1/ams_a1/config"]
        self.assertIn("value_json.ams['A1']", ams["value_template"])
        self.assertEqual(cfg["homeassistant/number/pygmy_a1/set_fan_aux/config"]["command_topic"], "pygmy/a1/cmd/fan_aux")
        self.assertEqual(cfg["homeassistant/camera/pygmy_a1/last_photo/config"]["topic"], "pygmy/a1/photo")

    def test_ams_entities_live_on_their_own_device(self):
        cfg = dict(ha_mqtt.discovery("a1", name="AUNA A1", slots=("A1", "B2"), units=("A", "B")))
        for topic, unit in (("homeassistant/sensor/pygmy_a1/ams_a1/config", "A"),
                            ("homeassistant/sensor/pygmy_a1/ams_a1_remain/config", "A"),
                            ("homeassistant/sensor/pygmy_a1/ams_b2_state/config", "B"),
                            ("homeassistant/sensor/pygmy_a1/ams_a_info/config", "A"),
                            ("homeassistant/sensor/pygmy_a1/ams_b_info/config", "B")):
            device = cfg[topic]["device"]
            self.assertEqual(device["identifiers"], [f"pygmy_a1_ams_{unit}"])
            self.assertEqual(device["name"], f"AUNA A1 AMS {unit}")
            self.assertEqual((device["model"], device["via_device"]), ("BMCU", "pygmy_a1"))
            self.assertEqual(device["manufacturer"], "BMCU")
        self.assertEqual(cfg["homeassistant/sensor/pygmy_a1/ams_a1/config"]["name"], "Slot A1")
        self.assertEqual(cfg["homeassistant/sensor/pygmy_a1/ams_a1_remain/config"]["name"], "Slot A1 remaining")
        self.assertEqual(cfg["homeassistant/sensor/pygmy_a1/ams_a_info/config"]["name"], "Raw AMS A info")
        # The printer device carries the rest and has no via_device.
        self.assertEqual(cfg["homeassistant/sensor/pygmy_a1/nozzle/config"]["device"]["identifiers"], ["pygmy_a1"])

    def test_a1_profile_drops_what_it_does_not_have(self):
        a1 = {"fans": ["part"], "options": ["auto_recovery", "sound_enable", "filament_tangle_detect"]}
        cfg = dict(ha_mqtt.discovery("a1", slots=("A1",), units=("A",), profile=a1))
        for topic in ("homeassistant/sensor/pygmy_a1/fan_aux/config",
                      "homeassistant/sensor/pygmy_a1/fan_chamber/config",
                      "homeassistant/number/pygmy_a1/set_fan_aux/config",
                      "homeassistant/number/pygmy_a1/set_fan_chamber/config"):
            self.assertNotIn(topic, cfg)
        self.assertIn("homeassistant/sensor/pygmy_a1/fan/config", cfg)
        self.assertIn("homeassistant/number/pygmy_a1/set_fan_part/config", cfg)
        self.assertIn("homeassistant/switch/pygmy_a1/opt_bff_spaghetti/config", cfg)
        self.assertIn("homeassistant/switch/pygmy_a1/opt_auto_recovery/config", cfg)
        partial = dict(ha_mqtt.discovery("a1", profile={"fans": ["part"], "options": ["sound_enable"]}))
        self.assertIn("homeassistant/switch/pygmy_a1/opt_sound_enable/config", partial)
        self.assertIn("homeassistant/switch/pygmy_a1/opt_bff_spaghetti/config", partial)
        self.assertNotIn("homeassistant/switch/pygmy_a1/opt_auto_recovery/config", partial)
        # No profile: today's full set stays.
        full = dict(ha_mqtt.discovery("a1"))
        for topic in ("homeassistant/sensor/pygmy_a1/fan_aux/config",
                      "homeassistant/sensor/pygmy_a1/fan_chamber/config",
                      "homeassistant/number/pygmy_a1/set_fan_aux/config",
                      "homeassistant/number/pygmy_a1/set_fan_chamber/config"):
            self.assertIn(topic, full)

    def test_flat_state_carries_ams_door_and_options(self):
        status = {"door_open": True, "fans": {"part": 70, "aux": 30, "chamber": 0},
                  "options": {"sound_enable": False, "bff_spaghetti": True},
                  "spaghetti": {"verdict": "warning", "score": 0.42},
                  "ams_units": [{"unit": "A", "humidity": 3, "slots": [
                      {"slot": "A1", "material": "PLA", "name": "Basic", "color": "AC95D5FF", "remain": 80,
                       "active": True, "empty": False, "profile_id": "GFA00"},
                      {"slot": "A2", "empty": True, "active": False}]}]}
        flat = ha_mqtt.flat_state(status)
        self.assertEqual((flat["fan_aux"], flat["fan_chamber"]), (30, 0))
        self.assertNotIn("door", flat)
        self.assertEqual(flat["spaghetti_score"], 0.42)
        self.assertEqual(flat["options"], {"sound_enable": False, "bff_spaghetti": True})
        self.assertEqual(flat["ams"]["A1"], {"material": "PLA", "name": "Basic", "color": "#AC95D5", "remain": 80,
                                             "active": True, "empty": False, "profile_id": "GFA00"})
        self.assertTrue(flat["ams"]["A2"]["empty"])
        self.assertEqual(flat["ams_humidity"], {"A": "C"})
        empty = ha_mqtt.flat_state({})
        self.assertEqual((empty["ams"], empty["ams_humidity"]), ({}, {}))

    def test_finish_publishes_the_photo_before_the_event(self):
        run = dict(STATUS)
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: run, mock.Mock(), snapshot=lambda a: b"JPEG")
        b.client = FakeClient()
        b.publish_states()  # RUNNING: nothing to notify
        run["state"] = "FINISH"
        b.publish_states()
        self.assertEqual([t for t, _, _ in b.client.sent][-2:], ["pygmy/a1/photo", "pygmy/a1/event"])
        self.assertIn(("pygmy/a1/photo", b"JPEG", True), b.client.sent)

    def test_snapshot_error_does_not_swallow_the_event(self):
        run = dict(STATUS)
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: run, mock.Mock(),
                             snapshot=mock.Mock(side_effect=server.ToolError("no camera")))
        b.client = FakeClient()
        b.publish_states()
        run["state"] = "FAILED"
        b.publish_states()
        self.assertEqual(b.client.sent[-1][0], "pygmy/a1/event")

    def test_layout_change_republishes_discovery(self):
        status = dict(STATUS)
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: status, mock.Mock())
        b.client = FakeClient()
        b.publish_states()
        self.assertFalse(any(t.endswith("/ams_a1/config") for t, _, _ in b.client.sent))
        status["ams_units"] = [{"unit": "A", "humidity": 4, "slots": [{"slot": "A1", "empty": True}]}]
        b.publish_states()
        self.assertIn("homeassistant/sensor/pygmy_a1/ams_a1/config",
                      [t for t, _, retained in b.client.sent if retained])
        self.assertEqual(b.layout["a1"], (("A1",), ("A",), (), ()))

    def test_bmcu_unit_retires_its_humidity_and_reader_entities(self):
        status = dict(STATUS)
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: status, mock.Mock())
        b.client = FakeClient()
        b.publish_states()
        b.client.sent.clear()
        status["ams_units"] = [{"unit": "A", "humidity": 4, "slots": [{"slot": "A1", "empty": True}]}]
        b.publish_states()  # the layout gained one BMCU unit: discovery is republished
        cleared = {t for t, p, r in b.client.sent if r and p == ""}
        self.assertTrue(set(ha_mqtt.bmcu_dropped_topics("a1", "A")) <= cleared)
        self.assertNotIn("homeassistant/sensor/pygmy_a1/ams_a1_remain/config", cleared)
        kept = {t for t, p, r in b.client.sent if r and p}
        for topic in ("homeassistant/sensor/pygmy_a1/ams_a1/config",
                      "homeassistant/sensor/pygmy_a1/ams_a1_remain/config",
                      "homeassistant/sensor/pygmy_a1/ams_a1_state/config",
                      "homeassistant/sensor/pygmy_a1/ams_a_info/config"):
            self.assertIn(topic, kept)

    def test_ha_ams_state_drops_impossible_sensor_values(self):
        b = self.bridge()
        bad = b._serial_state({"humidity_raw": 0, "temp": 6503.6}, "a1", "AUNA A1")
        self.assertIsNone(bad["temp"])
        self.assertIsNone(bad["humidity_pct"])
        ok = b._serial_state({"humidity_raw": 29, "temp": 33.0}, "a1", "AUNA A1")
        self.assertEqual((ok["temp"], ok["humidity_pct"]), (33.0, 29))

    def test_profile_learned_later_republishes_discovery(self):
        status = dict(STATUS)
        profile = {}
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: status, mock.Mock(),
                             profile=lambda alias: profile or None)
        b.client = FakeClient()
        b._on_connect(b.client, None, None, mock.Mock(is_failure=False), None)
        self.assertIn("homeassistant/number/pygmy_a1/set_fan_aux/config", [t for t, _, _ in b.client.sent])
        profile.update({"fans": ["part"], "options": ["sound_enable"]})
        b.client.sent.clear()
        b.publish_states()
        topics = [t for t, _, _ in b.client.sent]
        self.assertNotIn("homeassistant/number/pygmy_a1/set_fan_aux/config", topics)
        self.assertIn("homeassistant/switch/pygmy_a1/opt_sound_enable/config", topics)
        self.assertEqual(b.layout["a1"], ((), (), ("part",), ("sound_enable",)))


class OnlyOwnTopics(unittest.TestCase):
    def test_bridge_publishes_only_on_bff_and_its_discovery_topics(self):
        # The broker has no ACL (HA session, 2026-11-11): BFF itself must stay on pygmy/# and its discovery topics.
        b = ha_mqtt.HaBridge("h", 1883, "u", "p", ["a1"], lambda a: STATUS, mock.Mock(), {"a1": "AUNA A1"})
        b.client = FakeClient()
        b._on_connect(b.client, None, None, mock.Mock(is_failure=False), None)
        b.publish_states()
        for topic, _, _ in b.client.sent:
            self.assertTrue(topic.startswith("pygmy/") or topic.startswith("homeassistant/") and "/pygmy_a1/" in topic, topic)
        self.assertEqual(json.loads(b.client.sent[0][1])["device"]["name"], "AUNA A1")


class ServerHaAction(unittest.TestCase):
    def setUp(self):
        server.CFG.update(printers={"a1": 1})

    def test_actions_map_to_server_commands(self):
        done = {"printer": "a1", "action": "x", "outcome": "confirmed", "detail": ""}
        with mock.patch.object(server, "execute", return_value=done) as execute, \
                mock.patch.object(server, "require_support"):
            server.ha_action("a1", "pause", None, "ha-key-123")
            server.ha_action("a1", "stop", None, "ha-key-456")
        self.assertEqual([c.args[:2] for c in execute.call_args_list], [("a1", "pause"), ("a1", "stop")])
        with mock.patch.object(server, "set_light") as light:
            server.ha_action("a1", "light", True, "ha-key-789")
        light.assert_called_once_with(1, True)

    def test_unknown_action_refused(self):
        with mock.patch.object(server, "execute") as execute:
            self.assertRaises(server.ToolError, server.ha_action, "a1", "start", None, "ha-key-123")
        execute.assert_not_called()

    def test_new_actions_map_to_tools(self):
        args = {"printer": "a1"}
        with mock.patch.object(server, "tool_set_print_speed") as speed, \
                mock.patch.object(server, "tool_set_fan") as fan, \
                mock.patch.object(server, "tool_set_print_option") as option, \
                mock.patch.object(server, "tool_clear_errors") as clear, \
                mock.patch.object(server, "tool_read_filament") as read:
            server.ha_action("a1", "speed", 3, "k1")
            server.ha_action("a1", "fan_aux", 40, "k2")
            server.ha_action("a1", "opt_bff_spaghetti", True, "k3")
            server.ha_action("a1", "clear_errors", None, "k4")
            server.ha_action("a1", "read_a1", "A1", "k5")
        speed.assert_called_once_with({**args, "level": 3, "idempotency_key": "k1"})
        fan.assert_called_once_with({**args, "fan": "aux", "percent": 40, "idempotency_key": "k2"})
        option.assert_called_once_with({**args, "option": "bff_spaghetti", "enabled": True, "idempotency_key": "k3"})
        clear.assert_called_once_with({**args, "idempotency_key": "k4"})
        read.assert_called_once_with({**args, "slot": "A1", "idempotency_key": "k5"})

    def test_temperature_and_start_stay_refused(self):
        for action in ("temperature", "start", "move", "load_filament"):
            with self.subTest(action=action):
                self.assertRaises(server.ToolError, server.ha_action, "a1", action, None, "ha-key-123")



class FanAndSpool(unittest.TestCase):
    def test_fan_and_spool_reach_ha(self):
        flat = ha_mqtt.flat_state({"fans": {"part": 70}, "spool": {"material": "PLA", "color": "AC95D5FF"}})
        self.assertEqual((flat["fan"], flat["spool_material"], flat["spool_color"]), (70, "PLA", "#AC95D5"))
        empty = ha_mqtt.flat_state({})
        self.assertEqual((empty["fan"], empty["spool_material"], empty["spool_color"]), (None, None, None))
        spool = dict((t.split("/")[-2], c) for t, c in ha_mqtt.discovery("a1"))["spool"]
        self.assertEqual(spool["json_attributes_topic"], "pygmy/a1/state")

class Notifications(unittest.TestCase):
    RUN = {"state": "RUNNING", "subtask_name": "cube", "end_reason": "none", "alarm": None, "spaghetti": "safe"}

    def ev(self, prev, **cur):
        return [k for k, _ in ha_mqtt.events(prev, {**self.RUN, **cur})]

    def test_finish_notifies_once(self):
        self.assertEqual(self.ev(self.RUN, state="FINISH"), ["finished"])
        done = {**self.RUN, "state": "FINISH"}
        self.assertEqual(self.ev(done, state="FINISH"), [])

    def test_failure_notifies_but_user_stop_is_silent(self):
        self.assertEqual(self.ev(self.RUN, state="FAILED", end_reason="failed"), ["failed"])
        self.assertEqual(self.ev(self.RUN, state="FAILED", end_reason="stopped"), [])

    def test_hands_on_codes_do_not_notify(self):
        status = {"state": "PAUSE", "attention": [{"handling": "persona", "description": "carica filamento"}]}
        self.assertIsNone(ha_mqtt.flat_state(status)["alarm"])
        status["attention"].append({"handling": "sicurezza", "description": "ugello intasato"})
        self.assertEqual(ha_mqtt.flat_state(status)["alarm"], "ugello intasato")
        self.assertEqual(self.ev(self.RUN, alarm="ugello intasato"), ["error"])

    def test_spaghetti_notifies_on_the_edge(self):
        self.assertEqual(self.ev(self.RUN, spaghetti="warning"), ["spaghetti"])
        self.assertEqual(self.ev({**self.RUN, "spaghetti": "warning"}, spaghetti="failure"), [])

    def test_nothing_after_restart_or_for_start(self):
        self.assertEqual(self.ev(None, state="FINISH"), [])
        self.assertEqual(self.ev({**self.RUN, "state": "IDLE"}), [])

    def test_notification_has_no_serial_or_ip(self):
        text = repr(ha_mqtt.events(self.RUN, {**self.RUN, "state": "FINISH"}))
        self.assertNotRegex(text, r"\d+\.\d+\.\d+\.\d+|serial")

    def text(self, prev, lang="it", minutes=None, **cur):
        return [m for _, m in ha_mqtt.events(prev, {**self.RUN, **cur}, lang, minutes)]

    def test_texts_italian(self):
        run = {**self.RUN, "subtask_name": "plate2", "layer_num": 27, "total_layers": 60, "progress": 45}
        self.assertEqual(self.text(run, minutes=72, state="FINISH", spool_material="PLA", total_layers=60, subtask_name="plate2"),
                         ["✅ plate2 finita in 1 h 12 min · PLA · 60 layer. Libera il piatto."])
        self.assertEqual(self.text(run, state="FAILED", end_reason="failed", subtask_name="plate2", layer_num=27, total_layers=60),
                         ["❌ plate2 fallita al 45 % (layer 27 di 60)."])
        self.assertEqual(self.text(run, state="PAUSE", alarm="Ugello intasato"),
                         ["⚠️ Ugello intasato. Stampa in pausa: serve una mano."])
        self.assertEqual(self.text(run, spaghetti="warning", subtask_name="plate2", layer_num=27),
                         ["🍝 Possibili spaghetti su plate2 al layer 27: guarda la camera."])

    def test_spaghetti_pause_after_warning_tells_again(self):
        warned = {**self.RUN, "spaghetti": "warning", "subtask_name": "cube"}
        self.assertEqual(self.text(warned, lang="en", spaghetti="failure", spaghetti_paused=True, subtask_name="cube"),
                         ["🍝 Possible spaghetti on cube: the print is paused. Check the camera."])
        self.assertEqual(self.text(warned, lang="en", spaghetti="failure", subtask_name="cube"), [])

    def test_texts_english_default_and_missing_facts(self):
        self.assertEqual(self.text({**self.RUN, "subtask_name": None}, lang="en", state="FINISH", subtask_name=None),
                         ["✅ Print finished. Clear the plate."])
        self.assertEqual(ha_mqtt.duration(45), "45 min")

    def test_bridge_tracks_print_minutes(self):
        bridge = ha_mqtt.HaBridge("h", 1, "u", "p", ["a1"], None, None)
        clock = iter([0.0, 0.0, 4320.0, 4320.0]).__next__
        self.assertEqual(bridge.track("a1", {"state": "IDLE"}, {"state": "RUNNING"}, clock), 0)
        self.assertEqual(bridge.track("a1", {"state": "RUNNING"}, {"state": "FINISH"}, clock), 72)
        self.assertIsNone(bridge.track("a1", None, {"state": "RUNNING"}))  # started before a restart: unknown

    def test_event_entity_in_discovery(self):
        cfg = dict((t, c) for t, c in ha_mqtt.discovery("a1"))["homeassistant/event/pygmy_a1/event/config"]
        self.assertEqual(cfg["event_types"], list(ha_mqtt.EVENT_TYPES))


if __name__ == "__main__":
    unittest.main()
