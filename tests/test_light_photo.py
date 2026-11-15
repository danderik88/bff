import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bff_mcp import server  # noqa: E402


class LightForPhoto(unittest.TestCase):
    def run_with(self, statuses, control=True):
        calls = []
        seq = iter(statuses)
        last = {}

        def status(_alias):
            nonlocal last
            last = next(seq, last)
            return last

        with mock.patch.object(server, "read_status", side_effect=status), \
                mock.patch.object(server, "alias_of", return_value="p"), \
                mock.patch.object(server, "set_light", side_effect=lambda pid, on: calls.append(on)), \
                mock.patch.object(server, "CFG", {"control": control}), \
                mock.patch.object(server.time, "sleep"):
            result, lit = server.with_light("pid", lambda: "jpeg")
        return result, lit, calls

    def test_unknown_light_state_is_switched_on_then_off(self):
        # Alpha: no lights_report in the push -> chamber_light None; the photo came out dark
        _, lit, calls = self.run_with([{"state": "FINISH", "chamber_light": None}, {"chamber_light": True}])
        self.assertEqual(calls, [True, False])
        self.assertTrue(lit)

    def test_light_off_is_switched_on_then_off(self):
        _, lit, calls = self.run_with([{"state": "IDLE", "chamber_light": False}, {"chamber_light": True}])
        self.assertEqual(calls, [True, False])
        self.assertTrue(lit)

    def test_light_on_is_left_alone(self):
        _, lit, calls = self.run_with([{"state": "IDLE", "chamber_light": True}])
        self.assertEqual(calls, [])
        self.assertTrue(lit)

    def test_never_touched_while_printing_and_reason_logged(self):
        with mock.patch("sys.stderr") as err:
            _, lit, calls = self.run_with([{"state": "RUNNING", "chamber_light": None}])
        self.assertEqual(calls, [])
        self.assertIsNone(lit)
        self.assertIn("RUNNING", "".join(str(c) for c in err.write.call_args_list))

    def test_no_control_reason_logged(self):
        with mock.patch("sys.stderr") as err:
            _, _, calls = self.run_with([{"state": "IDLE", "chamber_light": False}], control=False)
        self.assertEqual(calls, [])
        self.assertIn("control", "".join(str(c) for c in err.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
