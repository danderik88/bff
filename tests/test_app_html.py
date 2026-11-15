"""Task 5: the Print now tab is four numbered steps, a queue card and a Files screen."""
import unittest
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "bff_mcp" / "app.html").read_text()


class AppHtml(unittest.TestCase):
    def test_print_now_steps_present(self):
        for id_ in ("step1", "step2", "step3", "step4", "queue", "filesview"):
            self.assertIn(f'id="{id_}"', PAGE)
        self.assertNotIn("Thingiverse", PAGE)
        self.assertNotIn("Bambu", PAGE)


if __name__ == "__main__":
    unittest.main()
