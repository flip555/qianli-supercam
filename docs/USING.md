# Using SuperCam

A task-by-task guide to the web interface. The README covers installing and running;
this covers what to do once it is up.

## Signing in

The relay asks for a username and password by default, and a fresh install serves only
a **setup page** until an account exists.

* **Account** (in the footer) changes the username or password, turns sign-in on or off,
  and manages an optional **guest** account.
* A **guest** may watch, change the view, **measure** (points, boxes and polygons) and take
  snapshots, but cannot change settings, the camera, the calibration or the palette. The
  relay enforces that, not just the page. Regions are shared, so a guest's measurements
  are visible to everyone.
* Sessions are signed cookies, so restarting the relay (an update does) does **not** sign
  you out.
* Lost the password? On the host, in the deployment directory:

  ```bash
  python -m supercam.auth --status     # what is configured
  python -m supercam.auth --reset      # reopen the setup page (sign-in stays on)
  ```


The admin grants capabilities per feature under **Account → Guest permissions**: measure,
snapshot, palette and bands, reference frame, calibration, alarms and the clip window.
Camera settings, the updater, the account page and notification credentials are never
grantable. The relay enforces it per request and the page hides what was not given.

## Choosing a view

| Mode | What you see |
|---|---|
| **Wipe** | Video with a draggable bar revealing the thermal — the easy way to calibrate |
| **PiP** | Video with the thermal in a corner inset (three sizes) |
| **Fusion** | Thermal over the video at an adjustable intensity |
| **Thermal** | Thermal full-frame, with temperature labels |
| **Video** | Visible feed only |

The mode, split position and fusion intensity are remembered per browser. In **Wipe**
and **Fusion** the thermal is drawn *over* the video, so the intensity slider applies
there too.

## Fullscreen, freeze and shortcuts

**Fullscreen** (or `F`) expands the image; **Freeze** (or `Space`) pins the picture and
stops the readout, chart and decoder, so a board can be read without the numbers
drifting under you. The blue **FROZEN** badge confirms it.

| Key | Does |
|---|---|
| `Space` | Freeze / unfreeze |
| `F` | Fullscreen |
| `S` | Snapshot |
| `1`–`5` | Wipe, PiP, Fusion, Thermal, Video |
| `Esc` | Cancel drawing / leave fullscreen |

Typing in a text field never triggers a shortcut.

## Temperature bands

Two independent bands, because they do different jobs:

* **Show** — a clip window. Anything outside is drawn **white**, so you can ignore a
  25 °C room while watching for 35 °C and above.
* **Colours** — what the palette spans. Leave it empty and the colours spread across
  whatever survives the clip; set both ends and the whole palette maps onto that slice.

Each has a dual-thumb slider, number boxes for exact values and an `Auto`. The relay
remembers both across restarts.

## Isotherm

Choose **isotherm** in the Palette list to paint a colour per temperature band.

* A **profile** is a list of rows, each `lower ~ upper` with its own colour; `Add band`
  continues from the previous row, `×` removes one.
* `Save as…` names the current rows; `Delete` removes your own (built-ins are protected).
* `pcba 25-60` and `fine 1C` ship with the project; anything outside the rows is white.

## Measuring

**Measure** → `Point`, `Box` or `Polygon`, then draw on the thermal image.

* **Point** — click once; **Box** — click and drag; **Polygon** — click each vertex, then
  click the first point again or press `Enter` to close (`Esc` cancels)
* Each region gets a row with live **min / average / max**, and a **delta** against
  whichever region is marked as the reference (the radio).
* **CSV** downloads the log: one row per region per second, rotating at 10 MB.
* Each region can carry **its own alarm** — up to 32, each with a separate threshold,
  direction and dwell (see Alarms below).
* Regions are stored on the relay, so they survive restarts and are shared by viewers.

## Reference frame (ΔT)

**Thermal → Reference frame.** With a known-good board in view, press **Set reference**.

* **ΔT view** shows the signed difference: blue colder, white identical, red hotter
  (span selectable, ±1 to ±20 °C)
* The readout switches to ΔT max / average / min
* Each measured region shows a ΔT figure beside its absolute values

The reference (`reference.raw` / `reference.json`) survives restarts; **Clear** removes
it, and snapshots record which reference was in use. The reference is a single frame.

Set the **span** to suit the question: **±1 °C** shows the first degree of heating, **±5 °C**
is a good default for a board, **±20 °C** for something violent. The colour bar in the
corner says what the colours mean, **white at zero** — an all-white image means nothing
has changed since the reference, not that the view is broken. The ΔT palette is its own,
because palettes and isotherm rows describe absolute temperatures while a difference
needs a signed scale.

**Save as…** keeps the current frame under a name, and as many as you like. Pick two in
the compare row and press **Compare** to see ΔT between them (min / average / max) with
the thermal image showing the difference, B minus A. Run a known-good board, save
`good-10min`, run the suspect, save `bad-10min`, compare — the shared surroundings cancel
out and the difference is the suspect's own heating.

### Comparing two saved references

**Save as…** keeps the current frame under a name, and as many as you like. Pick two in
the compare row and press **Compare** to see ΔT between them (min / average / max), with
the thermal image showing the difference, B minus A. Run a known-good board, save
`good-10min`, run the suspect, save `bad-10min`, compare — the shared surroundings cancel
out and the difference is that board's own heating.

## Re-analysing a stored frame

Press **Open** on a snapshot to return to that frame. It renders with that snapshot's own
scale and offset but your current palette, bands and isotherm, and a green RE-ANALYSIS
badge confirms it. The readout shows the statistics recorded at the time, your current
regions are measured against the stored frame, and **Refresh** / **Back to live** control
it — the picture was a view, the raw frame is the measurement.

## The chart

An hour of **max / average / min**, kept by the relay at one sample a second, so every
viewer sees the same history — including samples from before they opened the page.
Choose a 1M / 5M / 10M / 1H window; **CSV** downloads the retained hour as
`temperature.csv`; **Clear** empties it for everyone (admin only).

## Aligning the feeds

**Align** moves the *visible* feed onto the thermal, which never moves:

* A nudge pad (0.5 % a press), **Rotate**, **Flip H**, **Flip V**
* `Video scale`, `X`, `Y` sliders
* `Reset to saved` returns to the stored alignment, `Centre` is the hard 1×/0/0 reset

Calibration is stored server-side and shared with every viewer. Snapshots include it.

## Alarms

**Alarm** → `Add alarm`. Each rule watches either:

* the **whole frame**, or **one of your regions** (including a polygon — it measures only
* the **max** or the **average**,

and fires when it goes **above** or **below** a threshold **for a chosen time** — the
dwell takes **ms, seconds or minutes**. A fired rule stays latched until the reading
clears 0.5 °C past the threshold, so a noisy sensor cannot flap. Firings appear as a
toast, in `alarms.log`, and through notifications.

## Notifications

Under **Alarm → Notifications**:

| Channel | What it needs |
|---|---|
| Discord | A webhook URL |
| Slack | A webhook URL |
| Telegram | A bot token and a chat id |

Paste them there, not into a chat window — credentials are stored `0600` and never
returned to the page; it only reports *configured · host* or *configured · chat 12345*.
**Test** sends a real message.

## Clips

**Alarm → Clips** keeps a rolling buffer of the thermal frame, so when an alarm fires
the saved clip contains the seconds **before** it as well as after.

* Set the **before** and **after** windows in seconds and `Save`
* Each clip is a zip of frames plus a `meta.json` naming the rule, the reading and the
  window; download it from the list
* Settings persist in `prerecord.json`, clips in `captures/`

## Calibrating the temperatures

If the readings are offset (or plain wrong), calibrate against two references:

1. Put something of known temperature in view and measure it with a **point** or **box**
2. **Thermal → Two-point calibration**: pick that region, type its true temperature,
   `Add point`
3. Repeat with a second reference **well away** from the first — the wider the span, the
   better the slope
4. `Apply`; the panel shows the resulting scale and offset

`Factory` restores the values from `THERMAL_SCALE` / `THERMAL_OFFSET`.

## Snapshots

**Capture → Snapshot** saves what you actually see — the current mode, split, fusion,
calibration, temperature labels and readout — and does two things: downloads a JPEG, and
stores the snapshot on the relay with its **raw 16-bit thermal frame** and a metadata
sidecar (palette, bands, calibration, stats). Stored snapshots appear as a gallery under
Capture, each downloadable as a zip of picture, raw frame and metadata. The raw frame is
what makes a snapshot re-analysable later; the in-GUI re-analysis view is not built yet.

## Updating

The header shows the running version and turns into an update link when a newer release
exists. Updates preserve your account, calibration, regions, alarms, notifications and
clips.

## If something looks wrong

* **The badge says `mjpeg`, not `codec`.** WebCodecs needs a secure context: open the
  page over `https`, or on `localhost`. A plain `http://<ip>` always falls back.
* **The picture is still.** Check the **FROZEN** badge, then `Space`.
* **Readings are off by a constant, or drift.** Two-point calibration, then check the
  slope with the far reference.
* **You want ΔT between a reference board and this one.** Use **Thermal → Reference
  frame**: take the reference on the known-good board, then switch on ΔT view. The
  reference is a single frame and the span is selectable (±1 to ±20 °C); re-take it if the scene or
  the palette changes.
* **A tab left alone comes back to the login page.** That should no longer happen —
  sessions are signed cookies now — so tell the maintainer if it does.
* **Locked out.** `python -m supercam.auth --reset` on the host.
