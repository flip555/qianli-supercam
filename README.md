# QIANLI Super Cam LC-AD11 — open web interface

A small relay and browser interface for the **QIANLI Super Cam LC-AD11** thermal camera
— the same unit sold as the **SUNSHINE LC-AD11** and **TB-03 V2.0**. It speaks the
camera's own protocol directly: no vendor software, no Windows VM, just a live thermal
and visible workstation in a browser tab.

![SuperCam web GUI — wipe comparison with the isotherm palette, band controls and temperature chart](docs/supercam-example.jpg)

## Quick start

```bash
git clone https://github.com/flip555/qianli-supercam.git
cd qianli-supercam
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python relay.py
```

Open `http://localhost:8099`. The first visit asks you to set a username and password,
and **nothing is served until you do** — not the feed, not the camera settings.

Running it as a service, pinning the camera's address and the environment variables are
in **[Getting Started](https://github.com/flip555/qianli-supercam/wiki/Getting-Started)**.

## What it does

* **No vendor software** — talks the camera's protocol directly
* **Native 1080p visible feed**, decoded in the browser with WebCodecs; browsers without
  it fall back to a small MJPEG transcode that only runs while somebody is watching
* **Thermal 160×120, 16-bit** — rainbow / ironbow / white-hot / black-hot, plus an
  isotherm palette with saved profiles and a colour per temperature band
* **Two temperature bands** — clip what is drawn (white outside), and pin what the
  palette spans
* **Measurement** — points and boxes with min / average / max, a reference delta and a
  CSV log
* **Rolling chart** — an hour of max / average / min, shared by every viewer
* **Alarms** — above or below for a dwell in ms, seconds or minutes, latched, notifying
  Discord, Slack or Telegram and saving a clip of the seconds before the trigger
* **Views and alignment** — Wipe, PiP, Fusion, Thermal, Video; nudge, rotate, flip and
  scale the visible feed onto the thermal
* **Two-point thermal calibration** per camera
* **Built in** — sign-in with a guest role whose capabilities the admin grants per
  feature (measure, snapshot, palette, reference, calibration, alarms, clips),
  snapshots, five languages and an in-page updater

## Documentation

Full guide: **[wiki](https://github.com/flip555/qianli-supercam/wiki)** —
[Getting started](https://github.com/flip555/qianli-supercam/wiki/Getting-Started) ·
[Configuration](https://github.com/flip555/qianli-supercam/wiki/Configuration) ·
[Finding cameras](https://github.com/flip555/qianli-supercam/wiki/Finding-Cameras) ·
[Signing in](https://github.com/flip555/qianli-supercam/wiki/Signing-In) ·
[The interface](https://github.com/flip555/qianli-supercam/wiki/The-Interface) ·
[Temperature bands](https://github.com/flip555/qianli-supercam/wiki/Temperature-Bands) ·
[Isotherm](https://github.com/flip555/qianli-supercam/wiki/Isotherm) ·
[Measuring](https://github.com/flip555/qianli-supercam/wiki/Measuring) ·
[The chart](https://github.com/flip555/qianli-supercam/wiki/The-Chart) ·
[Calibration](https://github.com/flip555/qianli-supercam/wiki/Calibration) ·
[Alarms](https://github.com/flip555/qianli-supercam/wiki/Alarms) ·
[Updating](https://github.com/flip555/qianli-supercam/wiki/Updating) ·
[Translations](https://github.com/flip555/qianli-supercam/wiki/Translations) ·
[Protocol](https://github.com/flip555/qianli-supercam/wiki/Protocol) ·
[Status and limitations](https://github.com/flip555/qianli-supercam/wiki/Status-and-Limitations) ·
[Contributing](https://github.com/flip555/qianli-supercam/wiki/Contributing) ·
[Troubleshooting](https://github.com/flip555/qianli-supercam/wiki/Troubleshooting)

The same task-by-task guide is kept in the repository as
**[docs/USING.md](docs/USING.md)** for offline reading and review alongside the code.

## Requirements

Python 3.10+, `ffmpeg`, and a camera reachable on the LAN. Chrome/Edge or Safari 16.4+
for the browser-decoded feed; anything else gets MJPEG. See
[Status and limitations](https://github.com/flip555/qianli-supercam/wiki/Status-and-Limitations)
for what is proven and what is not.

## Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it; just keep the copyright
notice. (The reverse-engineering notes in `docs/` describe this project's own work;
the camera firmware and the vendor app are not included or redistributed.)
