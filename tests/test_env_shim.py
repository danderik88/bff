import os
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


class EnvShim(unittest.TestCase):
    """Users write PYGMY_*; the server still reads BFF_*."""

    def run_shim(self, env):
        src = (SRC / "pygmy" / "server.py").read_text()
        block = src[src.index("for _k, _v in list(os.environ.items()):"):].split("\n\n")[0]
        out = subprocess.run([sys.executable, "-c", "import os\n" + block + "\nprint(os.environ.get('BFF_MCP_BIND'))"],
                             env={**env, "PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
        return out.stdout.strip()

    def test_pygmy_sets_bff(self):
        self.assertEqual(self.run_shim({"PYGMY_MCP_BIND": "10.0.0.5"}), "10.0.0.5")

    def test_existing_bff_wins(self):
        self.assertEqual(self.run_shim({"PYGMY_MCP_BIND": "10.0.0.5", "BFF_MCP_BIND": "10.0.0.9"}), "10.0.0.9")
