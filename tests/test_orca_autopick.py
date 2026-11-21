import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pygmy"))
import server  # noqa: E402


class AutoMachine(unittest.TestCase):
    M = ["E3K 0.4 nozzle", "E3K 0.6 nozzle", "Voron 0.4 nozzle"]

    def test_name_and_nozzle(self):
        self.assertEqual(server.auto_machine(self.M, ["e3k", None], 0.6), "E3K 0.6 nozzle")

    def test_name_only_when_nozzle_unknown_and_unique(self):
        self.assertEqual(server.auto_machine(self.M, ["voron"], None), "Voron 0.4 nozzle")

    def test_ambiguous_gives_none(self):
        self.assertIsNone(server.auto_machine(self.M, ["e3k"], None))

    def test_single_preset_is_used(self):
        self.assertEqual(server.auto_machine(["My printer"], ["x"], 0.4), "My printer")

    def test_no_match(self):
        self.assertIsNone(server.auto_machine(self.M, ["sovol"], 0.4))


class SignInWindowCloses(unittest.TestCase):
    def test_closes_once_signed_in_but_not_on_a_new_sign_in(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "slicer"))
        import slicerd as s
        proc = mock.Mock(poll=mock.Mock(return_value=None))
        state = {"in": False}
        with mock.patch.object(s, "_spawn", return_value=proc), mock.patch.object(s, "_end"), \
                mock.patch.object(s, "_orca_conf"), mock.patch.object(s, "orca_logged_in", lambda: state["in"]):
            s.start_login()
            s.gui_tick()
            self.assertIsNotNone(s._GUI["login"])
            state["in"] = True
            s.gui_tick()
            self.assertIsNone(s._GUI["login"])
            self.assertIsNotNone(s._GUI["proc"])  # Orca keeps running to sync
            s.start_login()
            s.gui_tick()
            self.assertIsNotNone(s._GUI["login"])
            s.stop_login()


if __name__ == "__main__":
    unittest.main()
