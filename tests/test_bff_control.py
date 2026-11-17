"""Command state machine against a fake printer link. Run: python3 -m unittest discover -s tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from bff_mcp import server  # noqa: E402

SERIAL = "03919A000000000"


class Fake:
    """Shared by both printers: the report and every command that left."""
    state = {}
    posts = []
    mode = "ok"  # ok, ignore (sent, never observed), down (not sent)
    NEXT = {"pause": "PAUSE", "resume": "RUNNING", "stop": "FAILED"}

    def snapshot(self):
        return json.loads(json.dumps({**Fake.state, "serial": SERIAL}))

    def command(self, action, **params):
        Fake.posts.append((action, params))
        if Fake.mode == "down":
            return False
        if Fake.mode == "ok":
            if action == "temperature":
                Fake.state["temperatures"][f"{params['heater']}_target"] = params["target"]
            else:
                Fake.state["state"] = Fake.NEXT[action]
        return True


PROFILE = {
    "command_timeout_s": 0.6,
    "supports": {"pause": True, "resume": True, "stop": True},
    "limits": {
        "nozzle_max_c": {"value": 300, "source": "scheda"},
        "bed_max_c": {"value": 100, "source": "scheda"},
        "nozzle_confirm_above_c": {"value": 230, "source": "the user"},
        "bed_confirm_above_c": {"value": 65, "source": "the user"},
    },
}


class ControlTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        server.CFG.pop("control", None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        (tmp / "p2.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1, "p2": 2}, tokens={}, control=True, profiles=str(tmp))
        server.LINKS.update(a1=Fake(), p2=Fake())
        self.addCleanup(server.LINKS.clear)
        self.db_path = str(tmp / "state.db")
        server.open_db(self.db_path)
        Fake.state = {"connected": True, "state": "RUNNING", "temperatures": {"bed_target": 0, "nozzle_target": 0}}
        Fake.posts = []
        Fake.mode = "ok"
        self.n = 0

    def tearDown(self):
        server.DB.close()
        self.tmp.cleanup()

    def key(self):
        self.n += 1
        return f"test-key-{self.n}"

    def call(self, name, printer="a1", **args):
        msg = {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": {"printer": printer, **args}}}
        raw = server.dispatch(msg)
        self.assertNotIn(SERIAL, json.dumps(raw))
        result = raw["result"]
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_pause_is_confirmed_by_observed_state(self):
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertFalse(err)
        self.assertEqual(res["outcome"], "confirmed")
        self.assertEqual(len(Fake.posts), 1)

    def test_false_precondition_sends_nothing(self):
        Fake.state["state"] = "IDLE"
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertIn("rejected", res["errore"])
        self.assertEqual(Fake.posts, [])

    def test_disconnected_sends_nothing(self):
        Fake.state["connected"] = False
        err, _ = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_same_key_replays_without_resending(self):
        key = self.key()
        self.call("pause", idempotency_key=key)
        err, res = self.call("pause", idempotency_key=key)
        self.assertFalse(err)
        self.assertTrue(res["replayed"])
        self.assertEqual(len(Fake.posts), 1)

    def test_key_reused_for_other_command_is_refused(self):
        key = self.key()
        self.call("pause", idempotency_key=key)
        err, res = self.call("resume", idempotency_key=key)
        self.assertTrue(err)
        self.assertIn("idempotency_key", res["errore"])
        self.assertEqual(len(Fake.posts), 1)

    def test_missing_key_is_refused(self):
        err, _ = self.call("pause")
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_timeout_locks_and_never_resends(self):
        Fake.mode = "ignore"
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertIn("unknown", res["errore"])
        err, res = self.call("resume", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertIn("recheck", res["errore"])
        self.assertEqual(len(Fake.posts), 1)

    def test_lock_is_per_printer(self):
        Fake.mode = "ignore"
        self.call("pause", idempotency_key=self.key())
        Fake.mode = "ok"
        Fake.state["state"] = "RUNNING"
        err, _ = self.call("pause", printer="p2", idempotency_key=self.key())
        self.assertFalse(err)

    def test_connection_lost_on_send_is_unknown(self):
        Fake.mode = "down"
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertIn("unknown", res["errore"])

    def test_lock_survives_restart(self):
        Fake.mode = "ignore"
        self.call("pause", idempotency_key=self.key())
        server.DB.close()
        server.open_db(self.db_path)
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertIn("recheck", res["errore"])

    def test_recheck_while_disconnected_keeps_lock(self):
        Fake.mode = "ignore"
        self.call("pause", idempotency_key=self.key())
        Fake.state["connected"] = False
        err, _ = self.call("recheck")
        self.assertTrue(err)
        Fake.state["connected"] = True
        err, res = self.call("pause", idempotency_key=self.key())
        self.assertIn("recheck", res["errore"])

    def test_recheck_reports_command_not_applied_and_unlocks(self):
        Fake.mode = "ignore"
        key = self.key()
        self.call("pause", idempotency_key=key)
        err, res = self.call("recheck")
        self.assertFalse(err)
        self.assertEqual(res["outcome"], "not_applied")
        self.assertEqual(len(Fake.posts), 1)
        Fake.mode = "ok"
        err, _ = self.call("pause", idempotency_key=self.key())
        self.assertFalse(err)
        err, res = self.call("pause", idempotency_key=key)
        self.assertIn("not_applied", res["errore"])

    def test_recheck_with_null_params_unlocks(self):
        server.DB.execute("INSERT INTO commands VALUES (?,?,?,?,?,?)", ("null-key-1", "a1", "temperature", None, "unknown", "esito non osservato"))
        server.DB.execute("INSERT OR REPLACE INTO locks VALUES (?,?)", ("a1", "null-key-1"))
        server.DB.commit()
        err, res = self.call("recheck")
        self.assertFalse(err)
        self.assertEqual(res["outcome"], "not_applied")
        self.assertEqual(server.DB.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)

    def test_recheck_reports_command_applied(self):
        Fake.mode = "ignore"
        self.call("pause", idempotency_key=self.key())
        Fake.state["state"] = "PAUSE"
        _, res = self.call("recheck")
        self.assertEqual(res["outcome"], "confirmed")

    def prepare_stop(self, printer="a1"):
        err, res = self.call("prepare_stop", printer=printer)
        self.assertFalse(err)
        return res["confirm_token"]

    def test_stop_needs_prepare_then_confirm(self):
        token = self.prepare_stop()
        self.assertEqual(Fake.posts, [])
        err, res = self.call("confirm_stop", token=token, idempotency_key=self.key())
        self.assertFalse(err)
        self.assertEqual(res["outcome"], "confirmed")

    def test_token_cannot_be_reused(self):
        token = self.prepare_stop()
        self.call("confirm_stop", token=token, idempotency_key=self.key())
        Fake.state["state"] = "RUNNING"
        err, _ = self.call("confirm_stop", token=token, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(len(Fake.posts), 1)

    def test_confirm_retry_with_same_key_replays(self):
        token, key = self.prepare_stop(), self.key()
        self.call("confirm_stop", token=token, idempotency_key=key)
        err, res = self.call("confirm_stop", token=token, idempotency_key=key)
        self.assertFalse(err)
        self.assertTrue(res["replayed"])
        self.assertEqual(len(Fake.posts), 1)

    def test_expired_token_is_refused(self):
        token = self.prepare_stop()
        server.DB.execute("UPDATE tokens SET expires=0")
        server.DB.commit()
        err, _ = self.call("confirm_stop", token=token, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_token_for_other_printer_is_refused(self):
        token = self.prepare_stop("a1")
        err, _ = self.call("confirm_stop", printer="p2", token=token, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_token_for_other_action_is_refused(self):
        token = self.prepare_stop()
        err, _ = self.call("confirm_temperature", token=token, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_prepare_stop_when_idle_gives_no_token(self):
        Fake.state["state"] = "IDLE"
        err, res = self.call("prepare_stop")
        self.assertTrue(err)
        self.assertNotIn("confirm_token", res)

    def test_temperature_within_ceiling_is_one_step(self):
        err, res = self.call("set_temperature", heater="bed", target=60, idempotency_key=self.key())
        self.assertFalse(err)
        self.assertEqual(res["outcome"], "confirmed")
        self.assertEqual(Fake.state["temperatures"]["bed_target"], 60)

    def test_temperature_above_ceiling_needs_confirm(self):
        err, _ = self.call("set_temperature", heater="bed", target=80, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])
        _, res = self.call("prepare_temperature", heater="bed", target=80)
        err, _ = self.call("confirm_temperature", token=res["confirm_token"], idempotency_key=self.key())
        self.assertFalse(err)
        self.assertEqual(Fake.state["temperatures"]["bed_target"], 80)

    def test_temperature_above_maximum_is_refused_even_with_prepare(self):
        err, res = self.call("prepare_temperature", heater="bed", target=101)
        self.assertTrue(err)
        self.assertNotIn("confirm_token", res)

    def test_bad_temperature_values_are_refused(self):
        for heater, target in (("bed", True), ("bed", 60.5), ("bed", -1), ("bed", "60"), ("chamber", 40)):
            err, _ = self.call("prepare_temperature", heater=heater, target=target)
            self.assertTrue(err, (heater, target))
        self.assertEqual(Fake.posts, [])

    def test_limit_without_source_is_refused(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["limits"]["bed_max_c"]["source"] = ""
        (Path(server.CFG["profiles"]) / "a1.json").write_text(json.dumps(profile))
        err, res = self.call("prepare_temperature", heater="bed", target=60)
        self.assertTrue(err)
        self.assertIn("fonte", res["errore"])

    def test_null_limit_is_refused(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["limits"]["bed_confirm_above_c"]["value"] = None
        (Path(server.CFG["profiles"]) / "a1.json").write_text(json.dumps(profile))
        err, _ = self.call("set_temperature", heater="bed", target=60, idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_unsupported_action_is_refused(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["supports"]["pause"] = False
        (Path(server.CFG["profiles"]) / "a1.json").write_text(json.dumps(profile))
        err, _ = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_missing_profile_is_refused(self):
        (Path(server.CFG["profiles"]) / "a1.json").unlink()
        err, _ = self.call("pause", idempotency_key=self.key())
        self.assertTrue(err)
        self.assertEqual(Fake.posts, [])

    def test_commands_are_hidden_when_control_is_off(self):
        server.CFG["control"] = False
        raw = server.dispatch({"id": 1, "method": "tools/call", "params": {"name": "pause", "arguments": {"printer": "a1", "idempotency_key": self.key()}}})
        self.assertEqual(raw["error"]["code"], -32602)
        listed = {t["name"] for t in server.dispatch({"id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(listed, {"status", "list_printers", "printer_info", "camera_snapshot", "plate_photo", "hms_diagnose", "ready_files", "print_requests", "queue_list"})
        self.assertEqual(Fake.posts, [])


if __name__ == "__main__":
    unittest.main()
