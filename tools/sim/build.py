"""Build the shareable app simulator: a control panel around a phone running the real app.html on a fake BFF (mock.js).

Usage: python3 tools/sim/build.py OUT.html
Camera photos: tools/sim/cam/<printer>-<n>.jpg (gitignored), cycled like a GIF; without them a drawn bed.
Nothing of the real setup goes in: no token, no IP, no serial, no printer names. Sample printers only.
"""
import base64
import json
import re
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
page = (here.parents[1] / "src" / "pygmy" / "app.html").read_text()
mock = (here / "mock.js").read_text()

# The app's own names and demo printers were the user's; the simulator has only generic models.
# Each substitution is optional: skip it when app.html no longer has the block.
page = re.subn(r"const NAMES = \{[^\n]*\};", "const NAMES = {};", page, count=1)[0]
page = re.subn(r"const DEMO = \{[\s\S]*?\n\};", "const DEMO = {};", page, count=1)[0]
page = re.sub(r"(?i)\b(delta|alpha|mini|bravo)\b", "printer", page)
# Inside the phone frame the app gets the iPhone 17 Pro safe areas (status bar with the Dynamic Island, home bar).
page = re.sub(r"env\(safe-area-inset-top[^)]*\)", "54px", page)
page = re.sub(r"env\(safe-area-inset-bottom[^)]*\)", "34px", page)

photos = {}
for jpg in sorted((here / "cam").glob("*-*.jpg")):
    photos.setdefault(jpg.stem.rsplit("-", 1)[0], []).append("data:image/jpeg;base64," + base64.b64encode(jpg.read_bytes()).decode())
first = page.index("<script>")
page = page[:first] + f"<script>\nwindow.SIM_PHOTOS = {json.dumps(photos)};\n{mock}\n</script>\n" + page[first:]
# A demo key so the login sheet never opens; the fake fetch ignores it.
page = page.replace('try { token = localStorage.getItem("bff_token");', 'token = "simulator"; try {', 1)
page = page.replace('if (!token) askToken();', '', 1)

shell = (here / "shell.html").read_text()
out = shell.replace("__APP__", json.dumps(page).replace("</", "<\\/"), 1)
for secret in (r"\b192\.168\.0\.\d+", r"\b100\.\d+\.\d+\.\d+", r"(?i)\b(delta|alpha|mini|bravo)\b"):
    assert not re.search(secret, out), f"real data in the simulator: {secret}"
Path(sys.argv[1]).write_text(out)
print("ok", len(out), "photos:", {k: len(v) for k, v in photos.items()})
