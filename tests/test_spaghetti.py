import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pygmy"))
import spaghetti  # noqa: E402

BAD = [["failure", 0.9, [0, 0, 1, 1]], ["failure", 0.8, [0, 0, 1, 1]]]


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.state, self.calls = "RUNNING", 0
        self.detections = []

        def detect(jpeg):
            self.calls += 1
            if isinstance(self.detections, Exception):
                raise self.detections
            return self.detections
        self.w = spaghetti.Watch(lambda: self.state, lambda: b"jpeg", detect)

    def run_ticks(self, n):
        for _ in range(n):
            self.w.tick()

    def test_runs_only_while_printing(self):
        self.state = "IDLE"
        self.run_ticks(3)
        self.assertEqual(self.calls, 0)
        self.assertIsNone(self.w.verdict)

    def test_one_bad_frame_does_not_warn(self):
        self.run_ticks(40)
        self.detections = BAD
        self.w.tick()
        self.assertEqual(self.w.verdict, "safe")

    def test_lasting_bad_frames_warn(self):
        self.run_ticks(40)
        self.detections = BAD
        self.run_ticks(20)
        self.assertIn(self.w.verdict, ("warning", "failure"))

    def test_warmup_never_warns(self):
        self.detections = BAD
        self.run_ticks(spaghetti.WARMUP_FRAMES)
        self.assertEqual(self.w.verdict, "safe")

    def test_engine_down_is_unknown_not_safe(self):
        self.detections = OSError("down")
        self.w.tick()
        self.assertEqual(self.w.verdict, "unknown")

    def test_no_frame_is_unknown(self):
        self.w.frame = lambda: None
        self.w.tick()
        self.assertEqual((self.w.verdict, self.calls), ("unknown", 0))

    def test_pause_keeps_print_end_resets(self):
        self.run_ticks(5)
        self.state = "PAUSE"
        self.w.tick()
        self.assertEqual(self.w.print.frames, 5)
        self.state = "FINISH"
        self.w.tick()
        self.assertIsNone(self.w.print)

    def test_paused_flag_belongs_to_the_print(self):
        self.run_ticks(1)
        self.w.paused = True
        self.run_ticks(1)
        self.assertIs(self.w.paused, True)  # still the same print
        self.state = "FINISH"
        self.w.tick()
        self.assertIsNone(self.w.paused)  # the next print may pause again
        self.state = "RUNNING"
        self.w.tick()
        self.assertIsNone(self.w.paused)


class FrameTest(unittest.TestCase):
    def test_frame_served_once_and_expires(self):
        n = spaghetti.stash(b"a")
        self.assertEqual(spaghetti.pop(n), b"a")
        self.assertIsNone(spaghetti.pop(n))
        n = spaghetti.stash(b"b", clock=lambda: 0.0)
        self.assertIsNone(spaghetti.pop(n, clock=lambda: 31.0))


if __name__ == "__main__":
    unittest.main()


class DismissTest(unittest.TestCase):
    def test_dismissed_alarm_stays_quiet_until_the_print_ends(self):
        from unittest import mock
        state = ["RUNNING"]
        w = spaghetti.Watch(lambda: state[0], lambda: b"j", lambda j: [])
        with mock.patch.object(spaghetti, "classify", return_value="failure"):
            w.tick()
            self.assertEqual(w.verdict, "failure")
            w.muted = True
            w.tick()
            self.assertEqual(w.verdict, "dismissed")
            state[0] = "FINISH"; w.tick()
            self.assertFalse(w.muted)
            state[0] = "RUNNING"; w.tick()
            self.assertEqual(w.verdict, "failure")


class SwitchTest(unittest.TestCase):
    def test_bff_spaghetti_switch_is_saved_per_printer(self):
        import server
        server.tool_set_print_option({"printer": "a1", "option": "bff_spaghetti", "enabled": False})
        self.assertFalse(server.spaghetti_watch_on("a1"))
        server.tool_set_print_option({"printer": "a1", "option": "bff_spaghetti", "enabled": True})
        self.assertTrue(server.spaghetti_watch_on("a1"))


class BaselineCapTest(unittest.TestCase):
    def test_print_failing_from_the_first_frame_still_alarms(self):
        # BFF restarted mid-print with spaghetti already on the plate: the baseline must not learn it as normal.
        w = spaghetti.Watch(lambda: "RUNNING", lambda: b"j", lambda j: [["failure", 1.2, []]])
        for _ in range(spaghetti.WARMUP_FRAMES + 5):
            w.tick()
        self.assertIn(w.verdict, ("warning", "failure"))

    def test_clean_print_stays_safe(self):
        w = spaghetti.Watch(lambda: "RUNNING", lambda: b"j", lambda j: [["failure", 0.05, []]])
        for _ in range(spaghetti.WARMUP_FRAMES + 5):
            w.tick()
        self.assertEqual(w.verdict, "safe")
