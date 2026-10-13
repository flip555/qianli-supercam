# QIANLI Super Cam LC-AD11 — live view & thermal

Live video and thermal imaging from a **QIANLI Super Cam LC-AD11** IP camera
(also sold as *SuperCam* / *LAUNCHDIGITAL*) — in your browser, **without the
vendor app and without a Windows PC**.

Point it at your network and it finds the camera, connects, and serves a web GUI.

![SuperCam live view - visible and thermal, in Wipe mode](docs/supercam-example.jpg)

## Quick start

```bash
git clone https://github.com/flip555/qianli-supercam
cd qianli-supercam
docker compose up -d --build
```

Then open **`http://<docker-host>:8099/`** — it discovers the camera itself.

Prefer not to use Docker? You need Python 3.10+ and `ffmpeg`:

```bash
pip install -r requirements.txt
./run.sh start          # http://<host>:8099/
```

## What you get

* **No vendor software.** Talks the camera's own protocol directly — no Windows app, no VM.
* **Live 1080p video**, re-encoded on the fly to MJPEG so any browser can play it.
* **Thermal channel decoded** — 160×120, 16-bit — with rainbow / ironbow / white-hot / black-hot palettes.
* **Temperature readout** — max / average / min, with the max, min and up to two further hot spots automatically detected and labelled on the image.
* **Four view modes** — PiP, a draggable **wipe** comparison, or either feed full-frame.
* **Calibration** — scale and offset the video so the thermal and visible images register to each other.
* **Auto-discovery** — finds cameras on the local network instead of needing a fixed IP.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAMERA_HOST` | `auto` | `auto` discovers on the LAN; or pin an address (`192.168.1.85`) |
| `CAMERA_CIDRS` | *(local /24s)* | Comma-separated ranges to sweep, e.g. `192.168.1.0/24` |
| `PORT` | `8099` | Web GUI port |
| `PALETTE` | `rainbow` | `rainbow`, `ironbow`, `whitehot`, `blackhot` |
| `THERMAL_SCALE` / `THERMAL_OFFSET` | `0.01` / `0.0` | Absolute temperature calibration |
| `CALIBRATION_PATH` | `./calibration.json` | Where the image calibration is stored |

**Networking:** the bundled `docker-compose.yml` uses `network_mode: host` so
discovery can sweep your LAN and reach the camera. A bridged-network example is
included in the same file if you'd rather set `CAMERA_HOST` explicitly.

## Finding cameras

```bash
python -m supercam.discover                      # local /24s
python -m supercam.discover --cidr 192.168.1.0/24 --json
```

Cameras listen on TCP 3001 (control) and 3000 (media); each one answers the
probe with its own name, which is how the relay identifies it.

## The web GUI

| Mode | What it shows |
|---|---|
| **PiP** | Video with the thermal in a corner inset (three sizes) |
| **Wipe** | Video with a draggable bar revealing the thermal — the easy way to calibrate |
| **Thermal** | Thermal full-frame, with temperature labels |
| **Video** | Visible feed only |

Under the view: palette, PiP size, the wipe position, and the calibration
sliders (`Video scale`, `X`, `Y`, plus `Centre`). Calibration is stored
server-side and shared with every viewer.

## Status & limitations

* Video is 1080p H.264 decoded and re-encoded to MJPEG (~4× realtime on a small container).
* Absolute thermal temperature is `raw/100`; verify the offset against a known reference before trusting absolute values.
* The visible and thermal lenses have different fields of view, so the wipe needs the one-off calibration above.
* Developed and tested against a single LC-AD11 unit.
* **Setting the camera's own network configuration (e.g. its IP) from this GUI is not supported yet** — that needs the camera's configuration-write protocol, which is not yet mapped.

## Support

If this saved you from installing the vendor app:

* GitHub Sponsors / Ko-fi: **flip555**
* Buy me a coffee: <https://buymeacoffee.com/flip555>

## Updating

The running version is shown in the GUI header. When a newer release is
published, that version becomes an update link - click it and the relay
downloads the release, keeps a timestamped backup of the current deployment in
place, and restarts. Releases are tagged `vX.Y.Z` on GitHub; the version lives
in `supercam/__init__.py`.

## Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it; just keep the copyright
notice. (The reverse-engineering notes in `docs/` describe this project's own
work; the camera firmware and the vendor app are not included or redistributed.)
