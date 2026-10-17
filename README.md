# SuperCam LC-AD11 — live view & thermal

Live video and thermal imaging from a **SuperCam LC-AD11** IP camera — sold as the
**QIANLI Super Cam LC-AD11**, the **SUNSHINE LC-AD11** and the **TB-03 V2.0**. One
camera, several badges: this project should work with any of them, in your browser,
**without the vendor app and without a Windows PC**.

Point it at your network and it finds the camera, connects, and serves a web GUI.

![SuperCam web GUI - wipe comparison with the isotherm palette, band controls and temperature chart](docs/supercam-example.jpg)

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
* **Native 1080p visible feed**, decoded *in the browser* with WebCodecs, so the server
  does no video re-encoding at all. Browsers without WebCodecs — or a plain `http://`
  origin, since WebCodecs requires a secure context — fall back to a small
  server-side MJPEG transcode, which only runs while somebody is watching.
* **Thermal channel decoded** — 160×120, 16-bit — with rainbow / ironbow / white-hot /
  black-hot palettes and an **isotherm mode** that paints a colour per temperature band.
* **Isotherm profiles** — a band editor of `lower ~ upper` rows, each with its own
  colour. Built-in profiles plus your own, saved by name, so a fixed colour scale can be
  returned to for long-running comparisons.
* **Two temperature bands** — *Show* clips what is drawn at all (anything outside is
  white, so a 25 °C room can be ignored while watching for 35 °C and above), and
  *Colours* pins what the palette spans.
* **Temperature readout and history** — max / average / min, the hottest and coldest
  points and two further peaks labelled on the image, plus a **rolling chart** of the
  three series with 1M / 5M / 10M / 1H windows and a clear button.
* **Five view modes** — Wipe, PiP, **Fusion**, Thermal and Video. Fusion blends the
  thermal over the visible feed at an adjustable intensity, and the same intensity
  applies in Wipe and PiP.
* **Alignment controls** — a nudge pad, Rotate, Flip H/V and the scale/offset sliders,
  saved with the calibration, so the visible feed can be registered onto the thermal.
* **Snapshot** — saves what you actually see: the current mode, split, calibration,
  temperature labels and readout, not just one raw feed.
* **Camera status at a glance** — the header dot shows live / connected-but-no-frames /
  searching, so a blank view is never ambiguous.
* **Camera settings** — read and write the camera's own network configuration and
  reboot it to apply, all from the GUI.
* **Auto-discovery** — finds cameras on the local network rather than needing a fixed
  IP, and backs off politely when the camera is missing.
* **Translations** — English, Spanish, French, German and Portuguese, one JSON file per
  language (see below).
* **Sign-in built in** — an admin account and an optional read-only guest, with a
  setup page on first run and a documented recovery command.
* **Looks after itself** — the relay runs under a supervisor with a stall watchdog,
  and can update itself from GitHub releases without leaving the browser.

## Documentation

This README is the front door; the task-by-task guide is
**[docs/USING.md](docs/USING.md)** — signing in, the view modes, bands and isotherms,
measuring, the chart, alignment, alarms, notifications, clips, calibration, snapshots
and the keyboard shortcuts.

It lives in the repository rather than the GitHub wiki so it is reviewed with the code
and versioned with the release it describes; a wiki mirror is easy to add if you would
rather read it there.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CAMERA_HOST` | `auto` | `auto` discovers on the LAN; or pin an address (`192.168.1.85`) |
| `CAMERA_CIDRS` | *(local /24s)* | Comma-separated ranges to sweep, e.g. `192.168.1.0/24` |
| `CAMERA_FALLBACK_CIDR` / `CAMERA_FALLBACK_IPS` | `192.168.1.0/24` / a few common addresses | Where a factory-fresh camera is looked for |
| `PORT` | `8099` | Web GUI port |
| `PALETTE` | `rainbow` | Default thermal palette (`rainbow`, `ironbow`, `whitehot`, `blackhot`) |
| `VIDEO_WIDTH` / `VIDEO_FPS` | `960` / `10` | Size and rate of the MJPEG fallback encode |
| `THERMAL_SCALE` / `THERMAL_OFFSET` | `0.01` / `0.0` | Absolute temperature calibration |
| `CALIBRATION_PATH` | `./calibration.json` | Where the image calibration is stored |
| `SUPERCAM_REPO` | `flip555/qianli-supercam` | Repository the updater reads releases from |

**Networking:** the bundled `docker-compose.yml` uses `network_mode: host` so
discovery can sweep your LAN and reach the camera. A bridged-network example is
included in the same file if you would rather set `CAMERA_HOST` explicitly.

## Finding cameras

```bash
python -m supercam.discover                      # local /24s
python -m supercam.discover --cidr 192.168.1.0/24 --json
```

Cameras listen on TCP 3001 (control) and 3000 (media); each one answers the probe
with its own name, which is how the relay identifies it.

## The web GUI

The view fills the page and the controls sit in a side panel: **View**, **Thermal**,
**Chart**, **Align** and **Capture**, each collapsible, with the controls that cannot
affect the current view hidden.

| Mode | What it shows |
|---|---|
| **Wipe** | Video with a draggable bar revealing the thermal — the easy way to calibrate (default) |
| **PiP** | Video with the thermal in a corner inset (three sizes) |
| **Fusion** | Thermal over the video at an adjustable intensity |
| **Thermal** | Thermal full-frame, with temperature labels |
| **Video** | Visible feed only |

**Temperature bands** (Thermal panel). With nothing set the colours span each frame's
own min..max, so they slide around as the scene changes. Set **Show** to clip — anything
outside is drawn white, which is how you ignore a 25 °C room and look only at what is
above 35 °C — and **Colours** to pin what the palette spans. `Auto` clears either, and
the relay keeps both across restarts.

**Isotherm** (Thermal panel → Palette → `isotherm`) draws a colour per temperature band.
A profile is a list of rows, each `lower ~ upper` with its own colour; `Add band`
continues from the previous row. `Save as…` names the current bands and `Delete` removes
your own (built-ins are protected). `pcba 25-60` and `fine 1C` ship with the project.

**Chart** keeps the last hour of max / average / min **on the relay**, at one sample a
second, so every viewer sees the same history — including samples from before they
opened the page — with 1M / 5M / 10M / 1H windows. `Clear` empties it for everyone and
is admin-only.

**Align** — a nudge pad (0.5 % a press), Rotate, Flip H, Flip V, and the scale and offset
sliders. `Reset to saved` returns to the stored alignment; `Centre` is the hard 1×/0/0
reset. Calibration is stored server-side and shared with every viewer. A hidden or
throttled browser tab can leave an MJPEG stream showing a stale frame, so the streams are
prodded back to life whenever the tab becomes visible again.

**Capture** — a snapshot of the composed view (mode, split, fusion, calibration,
temperature labels and readout), and the camera settings dialog.

The header shows the running version — and turns into an update link when a newer
release is available — followed by a badge saying whether this browser is decoding the
feed itself (`codec`) or the relay is transcoding it (`mjpeg`).

## Signing in

The relay asks for a username and password by default. On a fresh install there is no
password yet, so the first visit gets a **setup page** and nothing else is served — not
the feed, not the camera settings — until an account exists.

* **Account** (in the footer) changes the username or password, turns sign-in on or off,
  and manages an optional **guest** account.
* A guest may watch, change the view and take snapshots, but cannot change settings, the
  camera, the calibration or the palette. That is enforced by the relay, not merely
  hidden in the page — including the endpoints that take their writes as query strings.
* Sessions are held in memory, so restarting the relay (an update does this) asks
  everyone to sign in again. Passwords are stored as PBKDF2-SHA256 hashes.
* Lost the password? On the host, from the deployment directory:

  ```bash
  python -m supercam.auth --status     # what is configured
  python -m supercam.auth --reset      # reopen the setup page (sign-in stays on)
  python -m supercam.auth --disable    # turn sign-in off entirely — only on a trusted LAN
  ```

`auth.json` (mode 0600) sits beside the other state files and is preserved across
updates.

## Updating

Releases are tagged `vX.Y.Z` on GitHub and the version lives in
`supercam/__init__.py`. When a newer release is published, the version in the GUI
header becomes an update link: clicking it downloads that release, keeps a
timestamped backup of the current deployment beside it, installs the new files
(local state such as calibration, palette and the service script is preserved) and
restarts. A bad release can be rolled back by hand from the `.bak-<timestamp>`
directory.

## Translations

The GUI is translated from **`web/i18n/<lang>.json`** — one flat JSON file per
language, fetched at runtime, no build step. The picker in the footer remembers your
choice; on a first visit it follows the browser's own language.

Currently shipped: **English, Spanish, French, German, Portuguese**. The non-English
files were drafted with AI and are genuinely open to correction — if a phrase reads
wrongly to you, that is a useful contribution, not a nitpick.

Adding a language is one file plus a line in `LANGS` (in `web/index.html`):

```bash
cp web/i18n/en.json web/i18n/xx.json    # translate the values, keep the keys
```

Keys must match `en.json`; anything a translation omits falls back to the key name,
so a partial translation degrades rather than breaking.

## Contributing

Issues and pull requests are welcome — particularly notes or captures from other
units, since this was built against a single one (firmware
`0181_B001_V02.01.01.11`).

* `docs/PROTOCOL.md` documents the camera's control protocol, media framing and
  thermal encoding — enough to talk to one of these without this project.
* `relay.py` is the whole server. `supercam/` holds the camera client, thermal
  decoder, discovery, settings, reboot and updater. `web/index.html` is the entire
  GUI — one file, no build step.
* To cut a release: bump `supercam/__init__.py`, push to `main`, then publish a
  `vX.Y.Z` tag and release. Running copies will offer it in their header.
* Please keep the graceful paths: the WebCodecs feed must fall back to MJPEG, and a
  camera that cannot be found must never become a busy loop.

## Status & limitations

* WebCodecs needs a **secure context** (https, or localhost) and a browser with
  `VideoDecoder` — Chrome/Edge, or Safari 16.4+. Everything else uses the MJPEG
  fallback, which costs roughly half a core while somebody is watching.
* The camera's H.264 stream carries private NAL types; those are stripped before
  the browser sees it, which is why ffmpeg tolerates the stream and a strict
  decoder does not.
* Absolute thermal temperature is `raw/100`; verify the offset against a known
  reference before trusting absolute values.
* The visible and thermal lenses have different fields of view, so the wipe needs
  the one-off calibration above.
* Writing the camera's network settings works, but the unit here would not accept a
  change that moved it to a different `/24` — same-subnet changes apply normally.
  Moving subnets may need the vendor tool.
* Built and tested against **one** unit — a QIANLI Super Cam LC-AD11 on firmware
  `0181_B001_V02.01.01.11`. The same camera is sold as the SUNSHINE LC-AD11 and the
  TB-03 V2.0 and should behave identically, but those have not been tested here.
  Reports from other units (and other firmware revisions) are very welcome.
* The camera's own control interface has no authentication (a blank `root` password
  over telnet). Treat the relay, and the camera, as trusted-LAN only.

## Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it; just keep the copyright
notice. (The reverse-engineering notes in `docs/` describe this project's own work;
the camera firmware and the vendor app are not included or redistributed.)
