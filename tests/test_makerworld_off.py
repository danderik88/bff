"""Public build: no makerworld.py, server.py falls back to makerworld_off and the app hides the sign-in."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
import makerworld_off  # noqa: E402


class MakerWorldOff(unittest.TestCase):
    def test_link_says_use_upload_without_network(self):
        with self.assertRaisesRegex(makerworld_off.LoginFailed, "use Upload"):
            makerworld_off.fetch(None, "https://makerworld.com/en/models/1")
        self.assertFalse(makerworld_off.status(None)["available"])

    def test_same_names_as_the_real_module(self):
        try:
            import makerworld
        except ImportError:
            self.skipTest("build without makerworld.py")
        for name in ("LoginNeeded", "LoginFailed", "fetch", "login_start", "login_code", "status", "logout"):
            self.assertTrue(hasattr(makerworld, name) and hasattr(makerworld_off, name), name)

    def test_server_falls_back_and_app_hides_sign_in(self):
        server = (ROOT / "src/pygmy/server.py").read_text()
        self.assertIn("import makerworld_off as makerworld", server)
        app = (ROOT / "src/pygmy/app.html").read_text()
        self.assertIn("mwStatus.available === false", app)
        self.assertIn("MakerWorld links need sign-in, not in this build", app)


if __name__ == "__main__":
    unittest.main()
