# BFF

Run your Bambu Lab printers **LAN-only** from your phone: live camera, controls, notifications and
"Print now" with server-side slicing. Everything stays on your network; no printer cloud account is used.

> BFF is an independent project. It is **not affiliated with, endorsed by or supported by Bambu Lab**.
> "Bambu Lab" and printer model names are trademarks of their owners and are used only to say which
> printers BFF works with. BFF only uses the interfaces the printers offer in LAN-only / Developer Mode
> (MQTT, FTPS to the SD card or USB stick, camera).

Status: early, invite-only preview. Tested by hand on A1, A1 mini, P1P, P1S and P2S, with AMS, AMS 2 Pro and AMS lite.

## What it does

- **Web app** (installable on iPhone/Android): printer status, live camera with pinch to zoom, light, temperatures,
  speed, fans, motion, AMS and spools, AMS drying where the printer supports it, errors explained in plain words.
- **Print now**: upload an STL, 3MF or STEP file, or paste a Printables link; pick printer and spools (up to 16 colours);
  BFF slices it with the OrcaSlicer command line on your server and starts it after a plate photo and your OK.
  Per-printer queue and a Files screen.
- **Safety**: every action that moves or heats the printer is checked against the printer state and asks for a confirmation;
  optional spaghetti detection that warns you (it never pauses on its own).
- **Notifications**: Web Push and Telegram (print done, failed, errors, low spool, spaghetti, printer offline).
- **Home Assistant**: entities and events through MQTT discovery, camera as RTSP.
- **AI agents**: an MCP server, so an agent can read the printers and, with your confirmation, start or stop prints.
- **Add printer** from the app: finds printers on the network; pair more phones with a QR code.

## What you need

- A small always-on Linux host on the same network as the printers (BFF runs in a Debian LXC container; a VM or mini PC works too).
  Python 3 standard library only.
- Printers in **LAN-only mode with Developer Mode on** (printer screen: Settings, Network).
- **A USB stick or SD card (FAT32) in every printer.** BFF sends prints to it over FTPS. Newer printers (P2S, H2 series)
  keep files in internal memory that only the vendor software can reach, so they need one too.
- Optional: go2rtc and ffmpeg for camera streams, a second container for the slicer (OrcaSlicer CLI), Tailscale for remote access.

## Install

`deploy/` holds the systemd units and config templates, `deploy/slicer/` the slicer container.
A step-by-step guide comes with the public release; until then, ask in the repo discussions.

Tests: `python3 -m unittest discover -s tests`

## Not included

MakerWorld links: downloading from MakerWorld needs a sign-in through a non-public account API, so this build leaves it out.
Download the model from MakerWorld yourself and use Upload.

## License

MIT, see `LICENSE`.
