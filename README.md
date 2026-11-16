<h1 align="center">Pygmy</h1>

<p align="center">
  Your Bambu Lab printers on your phone, <b>without the cloud</b>.<br>
  Live camera, controls, notifications and one-tap printing. Everything stays on your network.
</p>

<p align="center">
  <img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Docker" src="https://img.shields.io/badge/install-Docker-2496ED">
  <img alt="LAN only" src="https://img.shields.io/badge/cloud-none-brightgreen">
  <img alt="Status" src="https://img.shields.io/badge/status-early%20preview-orange">
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-a-quick-tour">Tour</a> ·
  <a href="#-whats-in-the-box">Features</a> ·
  <a href="#-good-to-know">Good to know</a> ·
  <a href="#-support">Support</a>
</p>

> [!NOTE]
> **Work in progress.** Pygmy is young and changes often, Print now most of all. Watch the first layers of
> every print and [open an issue](https://github.com/danderik88/pygmy/issues) when something looks off.

## 👀 A quick tour

<table>
<tr>
<td width="62%">

### Every printer at a glance

Progress, temperatures, fans, speed, light and the live camera (pinch to zoom). Spools and AMS units with
their colours. When something goes wrong you get the error in plain words, not a hex code.

Works with Bambu Lab printers in LAN mode, with AMS, AMS 2 Pro, AMS lite and BMCU. Tested by hand on
**A1, A1 mini, P1P, P1S and P2S**; H2D so far read-only plus pause / resume / stop. On other models only
moving the axes by hand stays off, until their safe area is mapped. More filament profiles are on the way.

</td>
<td width="38%" align="center"><img src="assets/status.gif" width="240" alt="Printer status"></td>
</tr>
<tr>
<td width="38%" align="center"><img src="assets/printnow.gif" width="240" alt="Print now"></td>
<td width="62%">

### Print now

Drop an STL, 3MF or STEP file, or paste a Printables link. Pick the printer and the spools (up to 16 colours).
Pygmy slices it on your own server with the OrcaSlicer command line, shows you a photo of the plate and starts
only after your OK.

Each printer has its own queue, and there is a Files screen.

</td>
</tr>
<tr>
<td width="62%">

### AMS drying

Start and stop filament drying from the app on the printers that can do it over LAN
(P2S and H2 series with an AMS 2 Pro). The others show the drying state.

</td>
<td width="38%" align="center"><img src="assets/drying.gif" width="240" alt="AMS drying"></td>
</tr>
<tr>
<td width="38%" align="center"><img src="assets/addprinter.gif" width="240" alt="Add printer"></td>
<td width="62%">

### Add a printer in a minute

Pygmy listens on your network and lists the printers it finds. Pick one, type its access code, done.
Want the app on another phone too? Show a QR code from the first one.

</td>
</tr>
</table>

<sub>Recorded in the built-in simulator (`tools/sim`) with sample printers; the camera frames are real.</sub>

## 📦 What's in the box

|   |   |
|---|---|
| 📱 **Web app** | Installable on iPhone and Android, works over your LAN or Tailscale. |
| 🛡️ **Safety checks** | Every action that moves or heats a printer is checked against its state and asks you first. |
| 🔔 **Notifications** | Web Push and Telegram: print done, failed, errors, low spool, printer offline. |
| 🏠 **Home Assistant** | Entities and events through MQTT discovery, camera as RTSP. |
| 🤖 **AI agents** | An MCP server: an agent can read your printers and, with your OK, start or stop prints. |
| 🧩 **Klipper too** | Sovol SV08 and IdeaFormer IR3 V2 through Moonraker (work in progress, see below). |

## 🚀 Quick start

**You need:**

- Printers in **LAN-only mode with Developer Mode on** (printer screen: Settings, Network), and each printer's **access code** (same screen).
- **A USB stick or SD card (FAT32) in every printer.** Pygmy sends prints to it. P2S and H2 keep files in internal memory that only the vendor software can reach, so they need one too.
- An always-on **Linux x86_64** box on the same network (mini PC, NAS, VM) with [Docker](https://docs.docker.com/engine/install/).
  A Raspberry Pi works too, without Print now (OrcaSlicer has no Linux ARM build).

**Then:**

```sh
git clone https://github.com/danderik88/pygmy && cd pygmy
cp .env.example .env
sed -i "s/CHANGE_ME_IP/192.168.1.50/" .env          # <- this machine's IP (or its Tailscale IP)
for k in TOKEN SLICER RTSP; do sed -i "s/CHANGE_ME_$k/$(openssl rand -hex 24)/" .env; done
docker compose up -d --build                         # first build ~5 min (downloads OrcaSlicer)
echo "http://$(grep ^BFF_MCP_BIND= .env | cut -d= -f2):8765/app#k=$(grep ^BFF_MCP_TOKENS= .env | cut -d= -f3)"
```

Open the printed link on your phone, tap **Add printer**, then add the app to your home screen
(Share, Add to Home Screen). Try **Print now** with a small STL.

Update: `git pull && docker compose up -d --build` · Logs: `docker compose logs -f bff` ·
Data (printers, queue, files) lives in the `bff-data` volume · ARM / no Print now: `docker compose up -d --build bff go2rtc`

> [!WARNING]
> Developer Mode turns off the vendor's cloud authentication. Keep the printers and Pygmy on a trusted network and
> **never expose Pygmy's port to the internet**. For remote access use Tailscale or a VPN (below).

## 🔧 Good to know

<details>
<summary><b>Remote access and notifications</b> (Tailscale)</summary>

Install [Tailscale](https://tailscale.com) on the machine, set `BFF_MCP_BIND` to its Tailscale IP, then
`sudo tailscale serve --bg http://<tailscale-ip>:8765`. The app is then at `https://<machine>.<tailnet>.ts.net/app`
from anywhere, and Web Push notifications work (they need HTTPS).

</details>

<details>
<summary><b>Printing with an AI agent</b> (Claude Code or any MCP client)</summary>

| You have | How you print |
|---|---|
| **No agent** | **Print now** in the app. Pygmy slices on your server and asks for your OK. Works today. |
| **An AI agent** | The agent slices with an OrcaSlicer MCP server of your choice and hands the file to Pygmy's MCP; Pygmy shows the plate photo and starts only after your OK. |

Connect an agent (one token per agent: add `,agent=<another 48-char token>` to `BFF_MCP_TOKENS` and restart):

```sh
claude mcp add --transport http bff http://<bff-ip>:8765/ --header "Authorization: Bearer <token>"
```

Coming next: pick a model in the app and your own agent slices it instantly with your OrcaSlicer profiles.

</details>

<details>
<summary><b>Klipper printers</b> (work in progress)</summary>

Pygmy drives Klipper printers through Moonraker's documented HTTP API: status, pause / resume / cancel,
temperatures, chamber light, webcam still, Print now with OrcaSlicer's own profile. An ACE Pro shows up as a spool unit.
Supported so far: Sovol SV08 (`KLIPPER-SV08`) and IdeaFormer IR3 V2 (`KLIPPER-IR3V2`, conservative temperature limits).
One filament per print.

They show up by themselves under **+ Add printer** when Moonraker answers on port 7125 (Mainsail or Fluidd running means it does); otherwise use **Klipper: enter IP**. Temperature limits come from the printer's own printer.cfg. Manual alternative: add an entry to `printers.json` in the data volume and restart Pygmy:

    "sv08": {"host": "192.168.1.60", "port": 7125, "name": "SV08", "model": "KLIPPER-SV08", "api_key": ""}

</details>

<details>
<summary><b>Moving the axes</b> (Expert mode)</summary>

Day-to-day printing never needs it. To jog the head by hand: gear icon, **Advanced**, **Expert mode**. A **Motion**
card appears on the printer screen (home first, then move in small steps). Only on models tested so far
(A1, A1 mini, P1P, P1S, P2S) and only with `BFF_MCP_CONTROL=1`.

The printer does not report the head position over LAN, so Pygmy only knows it after homing and cannot see what
is on the bed. Clear the bed, stay next to the printer, and **never drive a P1 into its front-left corner (X0 Y0)**:
the head hits the filament cutter.

</details>

<details>
<summary><b>Without Docker</b></summary>

- Proxmox: `deploy/` holds the systemd units (`bff-mcp.service`, `go2rtc.service`) and `deploy/slicer/provision.sh`
  creates the slicer as an LXC container. Same settings as `.env.example`.
- No root: `deploy/user/install.sh user@host` installs Pygmy and the slicer as `systemd --user` services with the
  OrcaSlicer flatpak (needs Tailscale, `uv` and a display on the host).

</details>

<details>
<summary><b>Not included: MakerWorld</b></summary>

Downloading from MakerWorld needs a sign-in through a non-public account API, so this build leaves it out.
Download the model from MakerWorld yourself and use Upload.

</details>

## 🙏 Thanks

- [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) (AGPL-3.0): Pygmy runs its unmodified command line and its official
  Bambu Lab profiles in a separate container; no OrcaSlicer code is copied into Pygmy.
- [go2rtc](https://github.com/AlexxIT/go2rtc) for the camera streams.
- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) and the community that documented the LAN interfaces.
- [tommasobbianchi](https://github.com/tommasobbianchi) for the Klipper/Moonraker backend, SV08 slicing and the rootless installer.
- [tlabcustomaudio](https://github.com/tlabcustomaudio) for H2D support, and the beta testers for their patience and feedback.

## ☕ Support

If Pygmy saved you a trip to the printer, you can [buy me a coffee](https://paypal.me/merighidaniel).
Bug reports and tests on printers we don't have help just as much.

## 🛠️ Contributing

Tests: `python3 -m unittest discover -s tests`. Pull requests welcome; only documented LAN interfaces
(no vendor plugin, no reimplemented proprietary protocols).

## ⚖️ Disclaimer and license

Pygmy is an independent project, **not affiliated with, endorsed by or supported by Bambu Lab**. "Bambu Lab" and
printer model names are trademarks of their owners, used only to say which printers Pygmy works with. It uses only
the interfaces the printers offer in LAN-only / Developer Mode (MQTT, FTPS to the SD card or USB stick, camera):
no vendor plugin, no cloud.

**Use at your own risk.** A printer heats up and moves: never leave a print you started remotely completely
unwatched. The software is provided as is, without warranty; the authors are not liable for damage to printers,
prints, property or warranty. MIT license, see `LICENSE`.
