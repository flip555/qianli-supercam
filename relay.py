#!/usr/bin/env python3
"""SuperCam live relay - proven client loop.

Handshake reuses the reverse-engineered client; the read loop is the one that
provably pulls the full stream (~1.2 MB/s). The browser decodes the visible
H.264 itself (WebCodecs on /raw.h264); thermal is served as MJPEG.
"""
import base64, collections, csv, http.server, io, json, os, queue, socket, socketserver, subprocess, sys, threading, time, zipfile
import re
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supercam.client import SuperCam, keepalive, load  # handshake + packet builders
from supercam import thermal as TH
from supercam import settings as CFG
from supercam import reboot as REBOOT
from supercam import update as UPD
from supercam.auth import Auth, check_password
from supercam import auth as authmod
from supercam import authpages
from supercam import regions as REG
from supercam import alarms as AL
from supercam.notify import Notify
from supercam.prerecord import PreRecord
from supercam import __version__ as VERSION
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("CAMERA_HOST", "auto")      # "auto" = discover on the LAN
CIDRS = [c for c in os.environ.get("CAMERA_CIDRS", "").split(",") if c]
PORT = int(os.environ.get("PORT", "8099"))
PALETTE = os.environ.get("PALETTE", "rainbow")
VIDEO_W = os.environ.get("VIDEO_WIDTH", "960")      # MJPEG fallback size
VIDEO_FPS = os.environ.get("VIDEO_FPS", "10")       # MJPEG fallback rate
SCALE = float(os.environ.get("THERMAL_SCALE", "0.01"))
OFFSET = float(os.environ.get("THERMAL_OFFSET", "0.0"))

_lock = threading.Lock()
CAL_PATH = (os.environ.get("CALIBRATION_PATH")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json"))
PALETTE_PATH = (os.environ.get("PALETTE_PATH")
                or os.path.join(os.path.dirname(CAL_PATH), "palette.txt"))
_palette = {"name": PALETTE}          # live palette - the GUI can change it


TRANGE_PATH = os.path.join(os.path.dirname(CAL_PATH), "trange.json")
AUTH = Auth(os.environ.get("SUPERCAM_AUTH_PATH")
            or os.path.join(os.path.dirname(CAL_PATH), "auth.json"))
TCAL_PATH = os.path.join(os.path.dirname(CAL_PATH), "thermal_cal.json")
REFERENCE_RAW = os.path.join(os.path.dirname(CAL_PATH), "reference.raw")
REFERENCE_JSON = os.path.join(os.path.dirname(CAL_PATH), "reference.json")
REFERENCES_DIR = os.path.join(os.path.dirname(CAL_PATH), "references")
_ref = None          # {"raw", "vals", "at", "stats", "scale", "offset"}
_dt_span = 5.0       # degrees either side of zero, for the delta palette
_tcal = {"scale": SCALE, "offset": OFFSET}      # factory values unless calibrated


def _load_tcal():
    global _tcal
    try:
        with open(TCAL_PATH) as f:
            d = json.load(f)
        sc, off = float(d["scale"]), float(d["offset"])
        if 0.0001 <= abs(sc) <= 1.0 and -200 <= off <= 200:
            _tcal = {"scale": sc, "offset": off}
            print("thermal calibration: scale %g offset %g" % (sc, off), flush=True)
    except Exception:
        pass


def _save_tcal():
    try:
        with open(TCAL_PATH, "w") as f:
            json.dump(_tcal, f)
    except Exception:
        pass


def _fit_tcal(points):
    """Two (raw, true) pairs -> scale and offset, with sanity limits."""
    (r1, t1), (r2, t2) = points[0], points[1]
    r1, t1, r2, t2 = float(r1), float(t1), float(r2), float(t2)
    if abs(r2 - r1) < 50:
        raise ValueError("the two points are too close together (need a real span)")
    scale = (t2 - t1) / (r2 - r1)
    offset = t1 - scale * r1
    if not (0.0001 <= abs(scale) <= 1.0) or not (-200 <= offset <= 200):
        raise ValueError("that would give an implausible calibration")
    return scale, offset


_load_tcal()


def _load_reference():
    global _ref, _dt_span
    try:
        with open(REFERENCE_RAW, "rb") as f:
            raw = f.read()
        with open(REFERENCE_JSON) as f:
            meta = json.load(f)
        _ref = {"raw": raw, "vals": TH.values(raw), **meta}
        _dt_span = float(meta.get("span") or 5.0)
        print("reference frame restored (%.0f)" % (_ref.get("at") or 0), flush=True)
    except Exception:
        _ref = None


_load_reference()


def _slug(name):
    return "".join(c for c in (name or "").strip().lower() if c.isalnum() or c in "-_")[:24]


def _ref_meta(name, raw, stats):
    return {"name": name, "at": round(time.time(), 1), "stats": stats,
            "scale": _tcal["scale"], "offset": _tcal["offset"]}


def _ref_save_named(name, raw, stats):
    slug = _slug(name)
    if not slug:
        raise ValueError("give the reference a name")
    os.makedirs(REFERENCES_DIR, exist_ok=True)
    meta = _ref_meta(slug, raw, stats)
    with open(os.path.join(REFERENCES_DIR, slug + ".raw"), "wb") as f:
        f.write(raw)
    with open(os.path.join(REFERENCES_DIR, slug + ".json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    return slug, meta


def _ref_load_named(slug):
    slug = _slug(slug)
    if not slug:
        raise ValueError("no reference named")
    with open(os.path.join(REFERENCES_DIR, slug + ".raw"), "rb") as f:
        raw = f.read()
    with open(os.path.join(REFERENCES_DIR, slug + ".json")) as f:
        meta = json.load(f)
    return {"raw": raw, "vals": TH.values(raw), **meta}


def _snap_paths(name):
    base = os.path.join(SNAPSHOT_DIR, os.path.basename(name or ""))
    return base + ".raw", base + ".json"


def _ref_list():
    out = []
    try:
        for n in sorted(os.listdir(REFERENCES_DIR)):
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(REFERENCES_DIR, n)) as f:
                    meta = json.load(f)
            except Exception:
                continue
            out.append({"name": n[:-5], "at": meta.get("at"), "stats": meta.get("stats")})
    except Exception:
        pass
    return out

_hist = collections.deque(maxlen=3600)   # (ts, min, max, mean) at ~1 Hz, shared by viewers
_hist_last = 0.0

REGIONS_PATH = os.path.join(os.path.dirname(CAL_PATH), "regions.json")
REGIONS_CSV = os.path.join(os.path.dirname(CAL_PATH), "regions.csv")
_regions = []
_csv_last = 0.0


def _sanitise_regions(items):
    out, seen_ref = [], False
    for r in (items or [])[:REG.MAX_REGIONS]:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind")
        if kind not in ("point", "rect", "poly"):
            continue
        pts = r.get("pts")
        if not isinstance(pts, list) or not pts:
            continue
        try:
            pts = [[int(round(float(q[0]))), int(round(float(q[1])))] for q in pts][:8]
        except Exception:
            continue
        ref = bool(r.get("ref")) and not seen_ref
        seen_ref = seen_ref or ref
        out.append({"id": str(r.get("id") or "r%d" % (len(out) + 1))[:32],
                    "kind": kind, "pts": pts,
                    "name": str(r.get("name") or "")[:40], "ref": ref})
    return out


def _load_regions():
    global _regions
    try:
        with open(REGIONS_PATH) as f:
            _regions = _sanitise_regions(json.load(f))
        print("regions restored: %d" % len(_regions), flush=True)
    except Exception:
        _regions = []


def _save_regions():
    try:
        with open(REGIONS_PATH, "w") as f:
            json.dump(_regions, f)
    except Exception:
        pass


def _maybe_csv(results):
    global _csv_last
    now = time.time()
    if now - _csv_last < 1.0:
        return
    _csv_last = now
    try:
        if os.path.exists(REGIONS_CSV) and os.path.getsize(REGIONS_CSV) > 10 * 1024 * 1024:
            os.replace(REGIONS_CSV, REGIONS_CSV + ".1")
        new = not os.path.exists(REGIONS_CSV)
        with open(REGIONS_CSV, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "region", "name", "kind", "min_c", "max_c", "avg_c", "delta_c"])
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            for r in results:
                w.writerow([ts, r.get("id"), r.get("name"), r.get("kind"),
                            r.get("min_c"), r.get("max_c"), r.get("avg_c"), r.get("delta_c", "")])
    except Exception:
        pass


_load_regions()

ALARMS_PATH = os.path.join(os.path.dirname(CAL_PATH), "alarms.json")
NOTIFY_PATH = os.path.join(os.path.dirname(CAL_PATH), "notifications.json")
ALARM_LOG = os.path.join(os.path.dirname(CAL_PATH), "alarms.log")
_alarms = []
_alarm_events = collections.deque(maxlen=50)
NOTIFY = Notify(NOTIFY_PATH)


def _sanitise_alarms(items):
    out = []
    for r in (items or [])[:16]:
        if not isinstance(r, dict):
            continue
        try:
            threshold = float(r.get("threshold"))
            dwell = int(float(r.get("dwell_ms", 0)))
        except Exception:
            continue
        out.append({
            "id": str(r.get("id") or "a%d" % (len(out) + 1))[:32],
            "name": str(r.get("name") or "")[:40],
            "scope": "region" if r.get("scope") == "region" else "frame",
            "region_id": str(r.get("region_id") or "")[:32],
            "metric": "avg" if r.get("metric") == "avg" else "max",
            "above": bool(r.get("above", True)),
            "threshold": threshold,
            "dwell_ms": max(0, min(dwell, 3600000)),
            "enabled": bool(r.get("enabled", True)),
        })
    return out


def _load_alarms():
    global _alarms
    try:
        with open(ALARMS_PATH) as f:
            _alarms = _sanitise_alarms(json.load(f))
        print("alarms restored: %d" % len(_alarms), flush=True)
    except Exception:
        _alarms = []


def _save_alarms():
    try:
        with open(ALARMS_PATH, "w") as f:
            json.dump(_alarms, f)
    except Exception:
        pass


def _dwell_text(ms):
    ms = int(ms or 0)
    if ms >= 60000 and ms % 60000 == 0:
        return "%d min" % (ms // 60000)
    if ms >= 1000 and ms % 1000 == 0:
        return "%d s" % (ms // 1000)
    return "%d ms" % ms


def _on_alarms(fired):
    for rule in fired:
        where = ("region " + str(rule.get("region_id"))) if rule.get("scope") == "region" else "whole frame"
        text = ("SuperCam alarm: %s - %s %s %.1f C (threshold %.1f C, held %s)"
                % (rule.get("name") or rule.get("id"), where, rule.get("metric", "max"),
                   float(rule.get("value", 0.0)), float(rule.get("threshold", 0.0)),
                   _dwell_text(rule.get("dwell_ms"))))
        with _lock:
            _alarm_events.append({"t": round(time.time(), 1), "rule": rule.get("id"), "text": text})
        try:
            with open(ALARM_LOG, "a") as f:
                f.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            pass
        NOTIFY.send(list(NOTIFY.cfg.keys()), text)
        try:
            PREREC.trigger(time.time(), {"rule": rule.get("id"), "name": rule.get("name"),
                                         "text": text})
        except Exception:
            pass
        print("alarm fired: %s" % text, flush=True)


_load_alarms()

SNAPSHOT_DIR = os.path.join(os.path.dirname(CAL_PATH), "snapshots")
PREREC_PATH = os.path.join(os.path.dirname(CAL_PATH), "prerecord.json")
PREREC_DIR = os.path.join(os.path.dirname(CAL_PATH), "captures")
PREREC = PreRecord(PREREC_DIR)


def _load_prerec():
    try:
        with open(PREREC_PATH) as f:
            d = json.load(f)
        PREREC.configure(d.get("enabled", True), d.get("before_s", 10), d.get("after_s", 10))
        print("pre-record: %s, %ds before / %ds after"
              % ("on" if PREREC.enabled else "off", PREREC.before, PREREC.after), flush=True)
    except Exception:
        pass


def _save_prerec():
    try:
        with open(PREREC_PATH, "w") as f:
            json.dump({"enabled": PREREC.enabled,
                       "before_s": PREREC.before, "after_s": PREREC.after}, f)
    except Exception:
        pass


_load_prerec()

_trange = {"clip_lo": None, "clip_hi": None,   # shown at all (None = no bound)
           "span_lo": None, "span_hi": None,   # palette span   (None = automatic)
           "iso": None}                         # isotherm band table (base/step/colours)

ISO_PATH = os.path.join(os.path.dirname(CAL_PATH), "isotherm.json")
ISO_PCBA = [{"lo": 25.0, "hi": 30.0, "colour": "#1f6feb"},
            {"lo": 30.0, "hi": 35.0, "colour": "#16c4c4"},
            {"lo": 35.0, "hi": 40.0, "colour": "#8b5cf6"},
            {"lo": 40.0, "hi": 45.0, "colour": "#e8b48c"},
            {"lo": 45.0, "hi": 50.0, "colour": "#ffe14d"},
            {"lo": 50.0, "hi": 55.0, "colour": "#ff9f1a"},
            {"lo": 55.0, "hi": 60.0, "colour": "#ff3b30"}]
ISO_BUILTIN = {"pcba 25-60": {"bands": ISO_PCBA},
               "fine 1C":    {"bands": [{"lo": 25.0 + i, "hi": 26.0 + i,
                                         "colour": c} for i, c in enumerate(
                                        ["#1f6feb", "#2f8be0", "#16c4c4", "#7fd6b0",
                                         "#b9e08a", "#ffe14d", "#ffb347", "#ff8c42",
                                         "#ff5e3a", "#ff3b30"])]}}
_iso = {"current": "pcba 8", "profiles": dict(ISO_BUILTIN)}


def _load_iso():
    try:
        with open(ISO_PATH) as f:
            t = json.load(f)
        for k, v in (t.get("profiles") or {}).items():
            _iso["profiles"][k] = v
        if t.get("current") in _iso["profiles"]:
            _iso["current"] = t["current"]
    except Exception:
        pass


def _save_iso():
    try:
        with open(ISO_PATH, "w") as f:
            json.dump(_iso, f)
    except Exception:
        pass


def _apply_iso(name):
    p = _iso["profiles"].get(name)
    if not p:
        return False
    _trange["iso"] = {"bands": list(p.get("bands") or ISO_PCBA)}
    _iso["current"] = name
    _save_iso()
    return True


_load_iso()
if _iso["profiles"].get(_iso["current"]):
    _apply_iso(_iso["current"])


def _load_trange():
    try:
        with open(TRANGE_PATH) as f:
            t = json.load(f)
        for k in ("clip_lo", "clip_hi", "span_lo", "span_hi"):
            if t.get(k) is not None:
                _trange[k] = float(t[k])
        print("thermal bands restored: clip %s-%s, span %s-%s"
              % tuple(str(_trange[k]) for k in ("clip_lo", "clip_hi", "span_lo", "span_hi")),
              flush=True)
    except Exception:
        pass


_load_trange()


def _load_palette():
    """Restore the palette chosen in the GUI (falls back to the default)."""
    try:
        with open(PALETTE_PATH) as f:
            name = f.read().strip()
        if (name in TH.LUTS or name == "isotherm"):
            _palette["name"] = name
            print("palette restored: %s" % name, flush=True)
    except Exception:
        pass


_load_palette()


def _load_cal():
    try:
        with open(CAL_PATH) as f:
            c = json.load(f)
    except Exception:
        c = {}
    return {"ts": float(c.get("ts", 1.0) or 1.0),
            "tx": float(c.get("tx", 0.0) or 0.0),
            "ty": float(c.get("ty", 0.0) or 0.0)}
_state = {"vjpeg": None, "vts": 0.0, "tjpeg": None, "tts": 0.0,
          "stats": None, "connected": False, "vbytes": 0, "tbytes": 0, "frames": 0,
          "dtjpeg": None, "dtts": 0.0, "dtstats": None, "regions": []}
_client = None                 # the live camera client (for settings read/write)
_cfg_cache = {"at": 0.0, "cfg": None, "key": None}
_cfg_lock = threading.RLock()   # re-entrant: the write path re-reads the config
_raw_subs: "set[queue.Queue]" = set()
_raw_lock = threading.Lock()
_vin: "queue.Queue[bytes]" = queue.Queue(maxsize=3)
_venc_synced = False
_verr = ""
_vrestarts = 0
_vframes = 0
_vproc = None
_vwriter_started = False
_vdropped = 0
_vstalls = 0
_vid = {"subs": 0}          # viewers of /video.mjpg
_hits = {"raw": 0, "mjpeg": 0, "rawconn": 0}   # which path clients take
_vsub_lock = threading.Lock()


def _start_video_ffmpeg() -> None:
    global _vproc, _venc_synced
    _venc_synced = False          # re-arm: wait for the next SPS for this process
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-fflags", "+genpts+igndts", "-err_detect", "ignore_err",
           "-analyzeduration", "500000", "-probesize", "200000",
           "-f", "h264", "-i", "-",
           "-vf", "scale=%s:-2" % VIDEO_W, "-r", VIDEO_FPS, "-q:v", "6",
           "-f", "mjpeg", "-flush_packets", "1", "pipe:1"]
    _vproc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=open("/tmp/ffvideo.log", "ab"))
    threading.Thread(target=_video_reader, args=(_vproc,), daemon=True).start()


def _video_reader(p) -> None:
    buf = bytearray()
    try:
        while True:
            d = p.stdout.read(65536)
            if not d:
                break
            buf += d
            while True:
                i = buf.find(b"\xff\xd8")
                if i < 0:
                    if len(buf) > 2_000_000:
                        del buf[:-2]
                    break
                j = buf.find(b"\xff\xd9", i + 2)
                if j < 0:
                    if i:
                        del buf[:i]
                    break
                jpeg = bytes(buf[i:j + 2])
                del buf[:j + 2]
                global _vframes
                _vframes += 1
                with _lock:
                    _state["vjpeg"] = jpeg
                    _state["vts"] = time.time()
    except Exception:
        pass


def _video_writer() -> None:
    """Feed camera H.264 to the encoder - but only while somebody is watching.

    The h264 -> MJPEG transcode is by far the biggest CPU cost here, and it used
    to run around the clock whether or not anyone had the page open. With no
    viewers there is nothing to encode, so the encoder is stopped (and the camera
    stream is still drained, so the camera link itself is unaffected).
    """
    global _vproc, _verr, _vrestarts
    idle_since = None
    while True:
        data = _vin.get()
        with _vsub_lock:
            watching = _vid["subs"] > 0
        if not watching:
            if _vproc is not None:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > 20:
                    try:
                        _vproc.kill()
                    except Exception:
                        pass
                    _vproc = None
                    print("no viewers left - encoder stopped", flush=True)
            else:
                idle_since = idle_since or time.time()
            continue
        idle_since = None
        try:
            if _vproc is None or _vproc.poll() is not None:
                _vrestarts += 1
                _start_video_ffmpeg()
            _vproc.stdin.write(data)
            _vproc.stdin.flush()
        except Exception as e:
            _verr = repr(e)
            try:
                if _vproc:
                    _vproc.stdin.close()
            except Exception:
                pass
            _vproc = None


def _log(msg: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _camera_reachable() -> bool:
    """Quick probe: is the camera answering on its control port right now?"""
    host = getattr(_client, "host", None) or HOST
    if not host or host in ("auto", "discover"):
        return False
    try:
        s = socket.create_connection((host, 3001), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _stall_watchdog() -> None:
    """Recover the video pipeline when frames stop arriving while connected.

    A camera reboot leaves this wedged with no error at all: ffmpeg stays alive
    and its input simply goes quiet, so nothing raises and the view goes blank
    until the service is restarted. Three responses, in increasing force:

      * the camera is still sending bytes but no JPEGs come out -> the encoder is
        wedged, so restart it;
      * the camera has gone silent -> its session is dead, so drop the sockets and
        let camera_loop rebuild it;
      * the camera is up and answering yet frames still will not flow after
        several attempts -> the session is beyond saving, so exit and let the
        supervisor start a clean process.

    Every step is throttled, and the last resort only fires while the camera is
    genuinely reachable, so it can never fight a camera that is still booting.
    """
    global _vproc, _vrestarts, _vstalls
    last_action = 0.0
    actions = 0
    hist = collections.deque(maxlen=24)          # (time, camera bytes)
    while True:
        time.sleep(4)
        with _lock:
            connected = bool(_state.get("connected"))
            vts = _state.get("vts") or 0.0
            vbytes = _state.get("vbytes") or 0
        hist.append((time.time(), vbytes))
        if not connected or vts <= 0:
            continue
        age = time.time() - vts
        if age < 12:
            actions = 0                            # frames are flowing again
            continue
        if time.time() - last_action < 15:
            continue                               # still cooling down
        old = next((vb for (t, vb) in hist if time.time() - t <= 20), vbytes)
        sending = vbytes != old
        last_action = time.time()
        _vstalls += 1
        actions += 1
        if sending:
            try:
                if _vproc:
                    _vproc.kill()
            except Exception:
                pass
            _vproc = None
            _vrestarts += 1
            _start_video_ffmpeg()
            print("%s video stalled %.0fs with data flowing - encoder restarted (stalls=%d)"
                  % (time.strftime("%H:%M:%S"), age, _vstalls), flush=True)
        else:
            print("%s camera silent %.0fs - dropping the session so it rebuilds (stalls=%d)"
                  % (time.strftime("%H:%M:%S"), age, _vstalls), flush=True)
            if _client is not None:
                for sock in list(_client.socks.values()):
                    try:
                        sock.close()
                    except Exception:
                        pass
        if actions >= 3 and _camera_reachable():
            if actions >= 6:
                print("%s pipeline stuck for %.0fs with the camera up after %d attempts "
                      "- exiting so the supervisor starts it clean"
                      % (time.strftime("%H:%M:%S"), age, actions), flush=True)
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(2)
            # First try the cheaper remedy: drop the camera session so the stream is
            # rebuilt. Exiting the process after three encoder restarts used to happen
            # constantly, and every exit signed every viewer out.
            print("%s pipeline stuck for %.0fs with the camera up - reconnecting the session"
                  % (time.strftime("%H:%M:%S"), age), flush=True)
            if _client is not None:
                for sock in list(_client.socks.values()):
                    try:
                        sock.close()
                    except Exception:
                        pass


def _thermal_worker(client, sock, data):
    frames = client.dec.feed(data)
    if not frames:
        return
    try:
        img, stats = TH.decode(frames[-1], _palette["name"], _tcal["scale"], _tcal["offset"],
                               _trange["clip_lo"], _trange["clip_hi"],
                               _trange["span_lo"], _trange["span_hi"],
                               _trange["iso"])
        out = io.BytesIO(); img.save(out, format="JPEG", quality=80)
        jpeg = out.getvalue()
    except Exception:
        return
    global _hist_last
    try:
        if _regions or _alarms or _ref:
            vals = TH.values(frames[-1])
            ref_vals = _ref["vals"] if _ref else None
            res = []
            if _regions:
                ref_id = next((r["id"] for r in _regions if r.get("ref")), None)
                res = REG.stats_for(vals, TH.W, TH.H, _regions, _tcal["scale"],
                                    _tcal["offset"], ref_id, ref_vals)
                with _lock:
                    _state["regions"] = res
                _maybe_csv(res)
            if _alarms:
                fired = AL.evaluate(_alarms, stats, res, time.time())
                if fired:
                    _on_alarms(fired)
            if ref_vals:
                dtimg, dtstats = TH.render_dt(vals, ref_vals, _tcal["scale"], _dt_span)
                out2 = io.BytesIO()
                dtimg.save(out2, format="JPEG", quality=80)
                with _lock:
                    _state["dtjpeg"] = out2.getvalue()
                    _state["dtts"] = time.time()
                    _state["dtstats"] = dtstats
    except Exception:
        pass
    try:
        PREREC.add(time.time(), jpeg)
    except Exception:
        pass
    with _lock:
        _state["tjpeg"] = jpeg; _state["tts"] = time.time()
        _state["traw"] = frames[-1]
        _state["stats"] = stats; _state["frames"] += 1
        now = time.time()
        if now - _hist_last >= 1.0:
            _hist_last = now
            _hist.append((round(now, 1), stats["min_c"], stats["max_c"], stats["mean_c"]))


def stream_loop(client) -> None:
    """The proven tight loop: read only the stream sockets, keepalive every 10s."""
    vids = [s for s in (client.video.get(0), client.video.get(1)) if s]
    for s in vids:
        s.setblocking(False)
    valve = {client.video.get(0): 0, client.video.get(1): 1}
    t_ka = time.time() + 10
    while True:
        if time.time() >= t_ka:
            for v, k in client.probe_by_val.items():
                s = client.socks.get(k)
                if s:
                    try:
                        s.sendall(keepalive(v))
                    except OSError:
                        pass
            t_ka = time.time() + 10
        for s in vids:
            try:
                d = s.recv(262144)
            except (BlockingIOError, OSError):
                continue
            if not d:
                continue
            v = valve.get(s)
            if v == 0:
                global _vwriter_started, _vdropped, _venc_synced
                with _lock:
                    _state["vbytes"] += len(d)
                if not _venc_synced:
                    _i = d.find(b"\x00\x00\x01\x67")
                    if _i < 0:
                        d_enc = None
                    else:
                        _venc_synced = True
                        d_enc = d[_i:]
                else:
                    d_enc = d
                if d_enc is not None:
                    if not _vwriter_started:
                        _vwriter_started = True
                        threading.Thread(target=_video_writer, daemon=True).start()
                    try:
                        _vin.put_nowait(d_enc)
                    except queue.Full:
                        _vdropped += 1
                with _raw_lock:
                    subs = list(_raw_subs)
                for q in subs:
                    try:
                        q.put_nowait(d)
                    except queue.Full:
                        pass
            elif v == 1:
                with _lock:
                    _state["tbytes"] += len(d)
                _thermal_worker(client, s, d)
        time.sleep(0.001)


def _control_sockets(c):
    """The camera's control (3001) sockets, paired with their probe packets."""
    out = []
    for _t, sp, dp, pl in load():
        if dp == 3001 and len(pl) == 88:
            s = c.socks.get((sp, dp))
            if s:
                out.append(((sp, dp), s, pl))
    return out


def _read_config(timeout: float = 2.0, fresh: bool = False):
    """Return (config_dict, key).

    The handshake already receives the camera's config reply, so that buffered
    copy is used first - it costs nothing and never disturbs the stream. Only if
    it is missing do we actively probe, and we remember which socket answered.
    """
    global _cfg_cache
    with _cfg_lock:
        c = _client
        if not c:
            raise RuntimeError("camera not connected")

        # 1) free: anything the handshake already read (skipped when we must see
        #    the camera's *current* settings, e.g. to verify a write)
        for k, b in ([] if fresh else list(c.buf.items())):
            found = CFG.find(bytes(b))
            if found:
                cfg = CFG.parse(found[1])
                _cfg_cache = {"at": time.time(), "cfg": cfg, "key": k}
                return cfg, k

        # 2) ask - previous answering socket first, then the rest
        socks = _control_sockets(c)
        if not socks:
            raise RuntimeError("no control sockets on the live session")
        prev = (_cfg_cache or {}).get("key")
        socks.sort(key=lambda t: 0 if t[0] == prev else 1)
        for _k, s, pl in socks:
            try:
                s.settimeout(0.3)
                s.sendall(pl)
            except OSError:
                pass
        blob = {}
        t0 = time.time()
        while time.time() - t0 < timeout:
            for k, s, _pl in socks:
                try:
                    d = s.recv(8192)
                except (socket.timeout, BlockingIOError, OSError):
                    continue
                if d:
                    blob[k] = blob.get(k, b"") + d
            for k, b in blob.items():
                found = CFG.find(b)
                if found:
                    cfg = CFG.parse(found[1])
                    _cfg_cache = {"at": time.time(), "cfg": cfg, "key": k}
                    return cfg, k
            time.sleep(0.05)

        # 3) last resort: last known good values, so the UI still works
        if _cfg_cache.get("cfg"):
            return _cfg_cache["cfg"], _cfg_cache.get("key")
        raise RuntimeError("no configuration reply from the camera")


_dead_socks = set()
SELECT = bytes.fromhex("0100000002000000")   # enters config mode on a control socket
D7 = bytes.fromhex(
    "78b63a12766964656f2073657276657200000000000000000000000038383838383800"
    "000000000000000000000000003838383838380000000000000000000000000000d700"
    "010000000000000000000800000021d36c87")
SELECT8 = bytes.fromhex("0100000002000000")
COMMIT = bytes.fromhex("78b63a12766964656f20736572766572000000000000000000000000383838383838000000000000000000000000000038383838383800000000000000000000000000006a00000000000000000000000000000021d36c87")   # the app's one-off commit/reboot packet


# Captured verbatim from the vendor client's own settings write (see
# docs/PROTOCOL.md). Deriving these by patching a plain probe got the offsets
# wrong, so they are stored exactly as observed.
WRITE_MODE = bytes.fromhex(
    "78b63a12766964656f2073657276657200000000000000000000000038383838383800"
    "0000000000000000000000000038383838383800000000000000000000000000006c00"
    "010000000000000000007801000021d36c87")
D7 = bytes.fromhex(
    "78b63a12766964656f2073657276657200000000000000000000000038383838383800"
    "000000000000000000000000003838383838380000000000000000000000000000d700"
    "010000000000000000000800000021d36c87")
SELECT8 = bytes.fromhex("0100000002000000")
COMMIT = bytes.fromhex(
    "78b63a12766964656f2073657276657200000000000000000000000038383838383800"
    "0000000000000000000000000038383838383800000000000000000000000000006a00"
    "0000000000000000000000000021d36c87")


def _probe_variants():
    """(plain probe, write-mode probe, commit packet) - exactly as captured."""
    probes = [pl for (_t, _sp, dp, pl) in load() if dp == 3001 and len(pl) == 88]
    base = next((p for p in probes if len(p) > 69 and p[68] == 0x6C), probes[0])
    return base, WRITE_MODE, COMMIT


def _write_config_fresh(host: str, block: bytes, want_ip: str, budget: float = 12.0):
    """Write settings exactly the way the vendor client does:

        connect -> 6c probe (read settings) -> 6c+01 (write mode)
                -> 376-byte block -> 6a commit (camera echoes 6a)

    Then verify by reading the settings back.
    """
    t0 = time.time()
    base, wmode, commit = _probe_variants()
    try:
        s = socket.socket()
        s.settimeout(2.0)
        s.connect((host, 3001))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        return {"applied": False, "attempts": [{"step": "connect", "result": type(e).__name__}]}

    def exchange(payload, label, wait=0.45, expect_len=None, expect_op=None):
        """send a packet and wait for the reply the vendor app expects, so
        replies cannot be mis-attributed to the following step."""
        s.sendall(payload)
        time.sleep(wait)
        buf = b""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                s.settimeout(0.25)
                d = s.recv(8192)
            except (socket.timeout, BlockingIOError, OSError):
                d = b""
            if d:
                buf += d
            if expect_len and len(buf) >= expect_len:
                break
            if expect_op and len(buf) >= 70 and buf[68:70] == expect_op:
                break
        return buf

    steps = []
    try:
        r = exchange(base, "read", expect_len=440)
        steps.append({"step": "read-settings", "reply_bytes": len(r)})
        # NOTE: replaying the app's extra per-connection setup (a second 6c probe,
        # the d7 probe, the 8-byte select) made the camera store NOTHING, so the
        # simpler sequence is kept. See docs/PROTOCOL.md ("Known gap").
        r = exchange(wmode, "write-mode", expect_len=6)
        steps.append({"step": "write-mode", "reply_bytes": len(r)})
        r = exchange(block, "block", expect_len=6)
        steps.append({"step": "settings", "reply_bytes": len(r)})
        r = exchange(commit, "commit", wait=0.6, expect_op=b"\x6a\x00")
        acked = len(r) >= 88 and r[68:70] == b"\x6a\x00"
        steps.append({"step": "commit", "reply_bytes": len(r), "acknowledged": acked})
        time.sleep(2.0)          # let the camera settle before the socket closes
    except OSError as e:
        steps.append({"step": "send", "result": type(e).__name__})
        try:
            s.close()
        except Exception:
            pass
        return {"applied": False, "attempts": steps}
    try:
        s.close()
    except Exception:
        pass

    for _ in range(3):
        time.sleep(1.5)
        if time.time() - t0 > budget:
            break
        try:
            cfg, _k = _read_config(timeout=1.2, fresh=True)
            if cfg.get("ip") == want_ip:
                return {"applied": True, "attempts": steps, "now": cfg.get("ip")}
            steps.append({"verify": "still " + str(cfg.get("ip"))})
            break
        except Exception as e:
            steps.append({"verify_error": str(e)})
    return {"applied": False, "attempts": steps, "elapsed": round(time.time() - t0, 2)}


def _write_config(block: bytes, key, want_ip: str, budget: float = 5.0):
    """Send the config block to at most a couple of control sockets and report
    honestly what happened. Bounded: never takes longer than `budget` seconds."""
    t0 = time.time()
    with _cfg_lock:
        c = _client
        if not c:
            raise RuntimeError("camera not connected")
        socks = _control_sockets(c)
        socks.sort(key=lambda t: 0 if t[0] == key else 1)
        attempts = []
        for k, s, _pl in socks[:3]:
            if k in _dead_socks:
                continue
            if time.time() - t0 > budget:
                break
            try:
                s.settimeout(0.8)
                # mirror the vendor app: enter config mode, poke, then write
                s.sendall(SELECT)
                time.sleep(0.12)
                if _pl:
                    s.sendall(_pl)
                time.sleep(0.12)
                s.sendall(block)
                attempts.append({"socket": list(k), "result": "sent"})
            except OSError as e:
                _dead_socks.add(k)
                attempts.append({"socket": list(k), "result": type(e).__name__})
                continue
            time.sleep(0.5)
            try:
                cfg, _k2 = _read_config(timeout=1.2, fresh=True)
                if cfg.get("ip") == want_ip:
                    return {"applied": True, "attempts": attempts, "now": cfg.get("ip")}
                attempts[-1]["after"] = cfg.get("ip")
                break            # camera answered and the value did not change
            except Exception as e:
                attempts[-1]["after_error"] = str(e)
        return {"applied": False, "attempts": attempts, "elapsed": round(time.time() - t0, 2)}


def camera_loop() -> None:
    delay = 8
    while True:
        found = False
        try:
            host = HOST
            if host in ("", "auto", "discover"):
                from supercam import discover as _d
                cam = _d.find_one(CIDRS or None)
                if not cam:
                    raise RuntimeError("no camera found - set CAMERA_HOST or CAMERA_CIDRS")
                host = cam["ip"]
                print("discovered camera: %s %s" % (host, cam.get("name") or ""), flush=True)
            c = SuperCam(host)
            c.dec = TH.ThermalDecoder()
            c.handshake()
            found = True
            global _client
            _client = c
            with _lock:
                _state["connected"] = True
            print("connected (flags %s)" % {k: v.hex() for k, v in c.flag.items()}, flush=True)
            stream_loop(c)
        except Exception as e:
            print("camera loop error:", repr(e), flush=True)
        finally:
            with _lock:
                _state["connected"] = False
            try:
                for s in list(getattr(c, "socks", {}).values()):
                    s.close()
            except Exception:
                pass
        # back off when the camera is missing (a flaky camera used to mean a full
        # 2540-host sweep every 8 seconds); reset once it is back
        if found:
            delay = 8
        else:
            delay = min(delay * 2, 300)
            print("camera not found - retrying in %ds" % delay, flush=True)
        time.sleep(delay)


def _page() -> bytes:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "web", "index.html"), "rb") as f:
        return f.read()


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _jpegs(self, key, ts):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = 0.0
        while True:
            with _lock:
                j = _state.get(key); t = _state.get(ts, 0.0)
            if j and t != last:
                last = t
                try:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
                except OSError:
                    return
            time.sleep(0.02)

    # ---------- authentication plumbing ----------
    # Which guest permission a write needs. Anything absent stays admin-only,
    # including the camera, the updater, the account and notification credentials.
    GUEST_CAPS = {"/regions": "measure", "/snapshots": "snapshot",
                  "/palette": "display", "/trange": "display", "/isotherm": "display",
                  "/reference": "reference", "/references": "reference",
                  "/tcal": "calibration", "/alarms": "alarms",
                  "/prerecord": "prerecord"}

    API_PATHS = ("/health", "/version", "/thermal.json", "/history.json", "/camera",
                 "/reboot", "/update", "/calibration", "/palette", "/trange", "/isotherm",
                 "/whoami", "/regions", "/regions.csv", "/alarms", "/notify",
                 "/captures", "/captures.zip", "/prerecord", "/tcal",
                 "/history.csv", "/snapshots", "/snapshot.zip",
                 "/reference", "/dt.json", "/dt.mjpg",
                 "/references", "/compare.json", "/dt.jpg",
                 "/reanalyze.jpg", "/reanalyze.json")

    def _is_api(self, p):
        return p in self.API_PATHS or p.startswith(("/i18n/", "/raw", "/video", "/stream"))

    # These endpoints take their writes as GET query strings (the GUI calls them
    # that way), so "POST means write" is not enough to keep a guest read-only.
    QUERY_WRITES = ("/palette", "/trange", "/isotherm", "/calibration", "/history.json",
                    "/regions.csv")

    def _is_write(self, p):
        if self.command == "POST":
            return True
        if p in self.QUERY_WRITES:
            return bool(urllib.parse.urlparse(self.path).query)
        return False

    def _cookie(self, name="supercam_session"):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _set_cookie(self, token):
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        self.send_header("Set-Cookie",
                         "supercam_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                         % (token, 30 * 86400, secure))

    def _clear_cookie(self):
        self.send_header("Set-Cookie", "supercam_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def _page(self, html, status=200):
        b = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json_out(self, status, obj):
        b = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except OSError:
            pass          # the browser hung up; nothing to salvage

    def _redirect(self, to, token=None):
        self.send_response(303)
        if token:
            self._set_cookie(token)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(n).decode("utf-8", "replace") if n else ""
        except Exception:
            return ""

    def _form(self):
        """Form fields from a urlencoded body (the only form encoding sent by the GUI)."""
        return {k: v[0] for k, v in urllib.parse.parse_qs(self._body()).items()}

    def _route_public(self, p):
        """Sign-in, first-run setup and sign-out: reachable without a session."""
        if p == "/login":
            if self.command == "POST":
                f = self._form()
                token = AUTH.login(f.get("username", ""), f.get("password", ""))
                if token:
                    self._redirect("/", token)
                else:
                    self._page(authpages.login("Username or password not recognised."))
            elif AUTH.session(self._cookie()):
                self._redirect("/")
            else:
                self._page(authpages.login())
            return True
        if p == "/setup":
            if not AUTH.needs_setup():
                self._redirect("/")
            elif self.command == "POST":
                f = self._form()
                user = (f.get("username") or "admin").strip()
                pw = f.get("password") or ""
                if len(pw) < 8:
                    self._page(authpages.setup("Use at least 8 characters."))
                elif pw != f.get("confirm"):
                    self._page(authpages.setup("The two passwords do not match."))
                else:
                    AUTH.set_credentials(user, pw)
                    self._redirect("/", AUTH.login(user, pw))
            else:
                self._page(authpages.setup())
            return True
        if p == "/logout":
            AUTH.logout(self._cookie())
            self.send_response(303)
            self._clear_cookie()
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _gate(self, p):
        """Answer the request when it is not allowed; True means it was handled."""
        if not AUTH.enabled():
            self.role = "admin"
            return False
        if AUTH.needs_setup():
            if p in ("/", "/index.html"):
                self._page(authpages.setup())
            else:
                self._json_out(401, {"ok": False, "error": "setup required", "setup": True})
            return True
        sess = AUTH.session(self._cookie())
        if not sess:
            if self.command == "POST" or self._is_api(p):
                self._json_out(401, {"ok": False, "error": "login required"})
            else:
                self._page(authpages.login())
            return True
        self.role = sess["role"]
        self.user = sess.get("user") or AUTH.cfg.get("username")
        if p == "/notify" and self.role != "admin":
            self._json_out(403, {"ok": False, "error": "admin only"})
            return True
        if self._is_write(p) and self.role != "admin":
            cap = self.GUEST_CAPS.get(p)
            if not (cap and AUTH.can(self.role, cap)):
                self._json_out(403, {"ok": False,
                                     "error": "not permitted for this account"})
                return True
        return False

    def _route_authed(self, p):
        if p == "/whoami":
            self._json_out(200, {"ok": True, "role": self.role,
                                 "username": getattr(self, "user", None) or AUTH.cfg.get("username"),
                                 "auth": AUTH.enabled(), "perms": AUTH.perms()})
            return True
        if p == "/account":
            if self.role != "admin":
                self._json_out(403, {"ok": False, "error": "admin only"})
                return True
            msg, err = "", False
            if self.command == "POST":
                f = self._form()
                action = f.get("action", "")
                if action == "username":
                    new = (f.get("username") or "").strip()[:40]
                    if new:
                        AUTH.cfg["username"] = new
                        AUTH.save()
                        msg = "Username changed."
                    else:
                        msg, err = "Username cannot be empty.", True
                elif action == "password":
                    if not check_password(f.get("current", ""), AUTH.cfg.get("password") or ""):
                        msg, err = "Current password is not right.", True
                    elif len(f.get("new") or "") < 8:
                        msg, err = "Use at least 8 characters.", True
                    elif f.get("new") != f.get("confirm"):
                        msg, err = "The two passwords do not match.", True
                    else:
                        AUTH.set_credentials(AUTH.cfg.get("username"), f["new"])
                        msg = "Password changed."
                elif action == "login":
                    AUTH.set_enabled(f.get("enabled") == "1")
                    msg = "Sign-in is %s." % ("required" if AUTH.enabled() else "off")
                elif action == "perms":
                    AUTH.set_perms({cap: ("p_" + cap) in f for cap in authmod.GUEST_PERMS})
                    msg = "Guest permissions updated."
                elif action == "guest":
                    on = f.get("enabled") == "1"
                    pw = f.get("password") or ""
                    if on and len(pw) < 8 and not (AUTH.cfg.get("guest") or {}).get("password"):
                        msg, err = "Guest password needs at least 8 characters.", True
                    else:
                        AUTH.set_guest(on, pw or None)
                        msg = "Guest account %s." % ("enabled" if on else "removed")
            self._page(authpages.account(AUTH.cfg.get("username"), self.role,
                                         AUTH.enabled(),
                                         bool((AUTH.cfg.get("guest") or {}).get("enabled")),
                                         msg, err, AUTH.perms()))
            return True
        return False

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        p = self.path.split("?")[0]
        global _regions, _ref, _dt_span
        self.role = "admin"
        try:
            if self._route_public(p):
                return
            if self._gate(p):
                return
            if self._route_authed(p):
                return
        except Exception as exc:
            self._json_out(500, {"ok": False, "error": "auth: %r" % (exc,)})
            return
        if p in ("/", "/index.html"):
            # which front door a browser came through decides whether it can use
            # WebCodecs at all (it needs a secure context), so log it
            _log("page: host=%s proto=%s ua=%s" % (self.headers.get("Host", "?"),
                                                   self.headers.get("X-Forwarded-Proto", "http"),
                                                   self.headers.get("User-Agent", "?")[:40]))
            b = _page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif p == "/raw.h264":
            _hits["rawconn"] += 1
            _t0, _nb = time.time(), 0
            _log("raw.h264 client connected")
            q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
            with _raw_lock:
                _raw_subs.add(q)
            self.send_response(200)
            self.send_header("Content-Type", "video/h264")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    try:
                        _d = q.get(timeout=5)
                        _nb += len(_d)
                        self.wfile.write(_d)
                    except queue.Empty:
                        continue
                    except OSError:
                        break
            finally:
                _log("raw.h264 client left: %.0fs, %d KB" % (time.time()-_t0, _nb//1024))
                with _raw_lock:
                    _raw_subs.discard(q)
        elif p == "/reanalyze.jpg":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = os.path.basename((q.get("name") or [""])[0])
            rawp, metap = _snap_paths(name)
            if not name or not os.path.exists(rawp):
                self._json_out(404, {"ok": False, "error": "no stored frame for that snapshot"})
                return
            try:
                with open(rawp, "rb") as f:
                    raw = f.read()
                with open(metap) as f:
                    meta = json.load(f)
                sc = float(meta.get("scale") or _tcal["scale"])
                off = float(meta.get("offset") or _tcal["offset"])
                pal = (q.get("palette") or [meta.get("palette") or _palette["name"]])[0]

                def opt(key, default):
                    try:
                        if q.get(key) and q[key][0] != "":
                            return float(q[key][0])
                    except Exception:
                        pass
                    return default
                clip_lo = opt("clip_lo", _trange["clip_lo"])
                clip_hi = opt("clip_hi", _trange["clip_hi"])
                span_lo = opt("span_lo", _trange["span_lo"])
                span_hi = opt("span_hi", _trange["span_hi"])
                iso = _trange.get("iso") if pal == "isotherm" else None
                img, _st = TH.decode(raw, pal, sc, off, clip_lo, clip_hi, span_lo, span_hi, iso)
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=85)
            except Exception as exc:
                self._json_out(500, {"ok": False, "error": str(exc)})
                return
            data = out.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/reanalyze.json":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = os.path.basename((q.get("name") or [""])[0])
            rawp, metap = _snap_paths(name)
            if not name or not os.path.exists(rawp):
                self._json_out(404, {"ok": False, "error": "no stored frame for that snapshot"})
                return
            try:
                with open(rawp, "rb") as f:
                    raw = f.read()
                with open(metap) as f:
                    meta = json.load(f)
                vals = TH.values(raw)
                sc = float(meta.get("scale") or _tcal["scale"])
                off = float(meta.get("offset") or _tcal["offset"])
                ref_id = next((r["id"] for r in _regions if r.get("ref")), None)
                regions = REG.stats_for(vals, TH.W, TH.H, _regions, sc, off, ref_id) \
                    if _regions else []
            except Exception as exc:
                self._json_out(500, {"ok": False, "error": str(exc)})
                return
            self._json_out(200, {"ok": True, "name": name, "at": meta.get("at"),
                                 "recorded": meta.get("stats"), "regions": regions,
                                 "scale": sc, "offset": off,
                                 "palette": meta.get("palette")})
        elif p == "/references":
            if self.command == "POST":
                f = self._form()
                action = f.get("action", "save")
                slug = _slug(f.get("name"))
                if action == "save":
                    with _lock:
                        raw = _state.get("traw")
                        st = _state.get("stats")
                    if not raw:
                        self._json_out(409, {"ok": False, "error": "no thermal frame yet"})
                        return
                    try:
                        slug, meta = _ref_save_named(slug, raw, st)
                    except Exception as exc:
                        self._json_out(400, {"ok": False, "error": str(exc)})
                        return
                    # a saved reference also becomes the active one
                    with open(REFERENCE_RAW, "wb") as fh:
                        fh.write(raw)
                    with open(REFERENCE_JSON, "w") as fh:
                        json.dump(meta, fh, indent=2, default=str)
                    _ref = {"raw": raw, "vals": TH.values(raw), **meta}
                    print("reference saved: %s" % slug, flush=True)
                elif action == "select":
                    try:
                        _ref = _ref_load_named(slug)
                    except Exception:
                        self._json_out(404, {"ok": False, "error": "no such reference"})
                        return
                    with open(REFERENCE_RAW, "wb") as fh:
                        fh.write(_ref["raw"])
                    with open(REFERENCE_JSON, "w") as fh:
                        json.dump(_ref_meta(slug, _ref["raw"], _ref.get("stats")), fh,
                                  indent=2, default=str)
                    print("reference active: %s" % slug, flush=True)
                elif action == "delete":
                    for ext in (".raw", ".json"):
                        try:
                            os.remove(os.path.join(REFERENCES_DIR, slug + ext))
                        except Exception:
                            pass
                    if (_ref or {}).get("name") == slug:
                        _ref = None
                        with _lock:
                            _state["dtstats"] = None
            self._json_out(200, {"ok": True, "active": (_ref or {}).get("name"),
                                 "items": _ref_list()})
        elif p == "/compare.json":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            a, b = _slug((q.get("a") or [""])[0]), _slug((q.get("b") or [""])[0])
            try:
                ra, rb = _ref_load_named(a), _ref_load_named(b)
            except Exception:
                self._json_out(404, {"ok": False, "error": "name two saved references"})
                return
            n = min(len(ra["vals"]), len(rb["vals"]))
            deltas = [rb["vals"][i] - ra["vals"][i] for i in range(n)]
            sc = _tcal["scale"]
            self._json_out(200, {"ok": True, "a": a, "b": b, "samples": n,
                                 "stats": {"dt_min_c": round(min(deltas) * sc, 2),
                                           "dt_max_c": round(max(deltas) * sc, 2),
                                           "dt_avg_c": round(sum(deltas) / n * sc, 2)}})
        elif p == "/dt.jpg":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            a, b = _slug((q.get("a") or [""])[0]), _slug((q.get("b") or [""])[0])
            try:
                ra, rb = _ref_load_named(a), _ref_load_named(b)
            except Exception:
                self._json_out(404, {"ok": False, "error": "name two saved references"})
                return
            img, _ = TH.render_dt(rb["vals"], ra["vals"], _tcal["scale"])
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85)
            data = out.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/reference":
            if self.command == "POST":
                form = self._form()
                if form.get("span") is not None:
                    try:
                        _dt_span = max(0.5, min(float(form.get("span")), 50.0))
                    except Exception:
                        self._json_out(400, {"ok": False, "error": "span must be a number"})
                        return
                    if _ref:
                        meta = dict(_ref)
                        meta["span"] = _dt_span
                        with open(REFERENCE_JSON, "w") as fh:
                            json.dump({k: meta.get(k) for k in
                                       ("name", "at", "stats", "scale", "offset", "span")},
                                      fh, indent=2, default=str)
                    self._json_out(200, {"ok": True, "span": _dt_span})
                    return
                if form.get("clear"):
                    _ref = None
                    for path in (REFERENCE_RAW, REFERENCE_JSON):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                    with _lock:
                        _state["dtstats"] = None
                    self._json_out(200, {"ok": True, "present": False})
                    return
                with _lock:
                    raw = _state.get("traw")
                    st = _state.get("stats")
                if not raw:
                    self._json_out(409, {"ok": False, "error": "no thermal frame yet"})
                    return
                try:
                    meta = {"at": round(time.time(), 1), "stats": st,
                            "scale": _tcal["scale"], "offset": _tcal["offset"]}
                    with open(REFERENCE_RAW, "wb") as fh:
                        fh.write(raw)
                    with open(REFERENCE_JSON, "w") as fh:
                        json.dump(meta, fh, indent=2, default=str)
                    _ref = {"raw": raw, "vals": TH.values(raw), **meta}
                    print("reference frame captured", flush=True)
                except Exception as exc:
                    self._json_out(500, {"ok": False, "error": str(exc)})
                    return
                self._json_out(200, {"ok": True, "present": True, "at": meta["at"]})
                return
            meta = _ref or {}
            self._json_out(200, {"ok": True, "present": bool(_ref),
                                 "at": meta.get("at"), "stats": meta.get("stats"),
                                 "span": _dt_span})
        elif p == "/dt.json":
            with _lock:
                dts = _state.get("dtstats")
            meta = _ref or {}
            self._json_out(200, {"ok": True, "present": bool(_ref), "at": meta.get("at"),
                                 "stats": dts})
        elif p == "/dt.mjpg":
            self._jpegs("dtjpeg", "dtts")
        elif p == "/stream.mjpg":
            self._jpegs("tjpeg", "tts")
        elif p == "/video.mjpg":
            with _vsub_lock:
                _vid["subs"] += 1
                _hits["mjpeg"] += 1
            try:
                self._jpegs("vjpeg", "vts")
            finally:
                with _vsub_lock:
                    _vid["subs"] -= 1
        elif p == "/video.jpg":
            with _lock:
                j = _state["vjpeg"]
            if not j:
                self.send_response(503); self.send_header("Content-Length","0"); self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(j)))
                self.end_headers(); self.wfile.write(j)
        elif p == "/history.csv":
            with _lock:
                rows = list(_hist)
            lines = ["time,min_c,max_c,avg_c"]
            for ts, mn, mx, mean in rows:
                lines.append("%s,%.2f,%.2f,%.2f"
                             % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), mn, mx, mean))
            data = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="temperature.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p.startswith("/snapshots/"):
            name = os.path.basename(p[len("/snapshots/"):])
            path = os.path.join(SNAPSHOT_DIR, name)
            if not name.endswith(".jpg") or not os.path.exists(path):
                self._json_out(404, {"ok": False, "error": "no such snapshot"})
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception:
                self._json_out(500, {"ok": False, "error": "cannot read snapshot"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/snapshots":
            if self.command == "POST":
                try:
                    body = json.loads(self._body() or "{}")
                except Exception:
                    body = {}
                target = os.path.basename(str(body.get("delete") or ""))
                if target:
                    removed = []
                    for ext in (".jpg", ".raw", ".json"):
                        try:
                            os.remove(os.path.join(SNAPSHOT_DIR, target + ext))
                            removed.append(ext)
                        except Exception:
                            pass
                    self._json_out(200 if removed else 404,
                                   {"ok": bool(removed), "deleted": target, "removed": removed})
                    return
                image = body.get("image") or ""
                if not image.startswith("data:image/jpeg;base64,"):
                    self._json_out(400, {"ok": False, "error": "expected a jpeg data URL"})
                    return
                try:
                    jpeg = base64.b64decode(image.split(",", 1)[1])
                except Exception:
                    self._json_out(400, {"ok": False, "error": "bad image data"})
                    return
                stamp = time.strftime("%Y%m%d-%H%M%S")
                try:
                    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
                    base = os.path.join(SNAPSHOT_DIR, stamp)
                    with open(base + ".jpg", "wb") as f:
                        f.write(jpeg)
                    with _lock:
                        raw = _state.get("traw")
                        now_stats = _state.get("stats")
                    # the video layer on its own, so re-analysis has a still to show
                    video = body.get("video") or ""
                    if video.startswith("data:image/jpeg;base64,"):
                        try:
                            with open(base + "-video.jpg", "wb") as f:
                                f.write(base64.b64decode(video.split(",", 1)[1]))
                        except Exception:
                            pass
                    if raw:
                        with open(base + ".raw", "wb") as f:
                            f.write(raw)
                    with _lock:
                        region_stats = list(_state.get("regions") or [])
                    meta = {"at": round(time.time(), 1), "stats": now_stats,
                            "palette": _palette["name"], "bands": dict(_trange),
                            "scale": _tcal["scale"], "offset": _tcal["offset"],
                            "user": getattr(self, "user", None),
                            "raw_bytes": len(raw) if raw else 0,
                            "regions": list(_regions), "region_stats": region_stats,
                            "reference_at": (_ref or {}).get("at")}
                    with open(base + ".json", "w") as f:
                        json.dump(meta, f, indent=2, default=str)
                except Exception as exc:
                    self._json_out(500, {"ok": False, "error": str(exc)})
                    return
                print("snapshot saved: %s" % stamp, flush=True)
                self._json_out(200, {"ok": True, "name": stamp})
                return
            items = []
            try:
                for name in sorted(os.listdir(SNAPSHOT_DIR), reverse=True):
                    if not name.endswith(".jpg"):
                        continue
                    base = os.path.join(SNAPSHOT_DIR, name[:-4])
                    st = os.stat(base + ".jpg")
                    items.append({"name": name[:-4], "size": st.st_size,
                                  "at": round(st.st_mtime, 1),
                                  "raw": os.path.exists(base + ".raw"),
                                  "video": os.path.exists(base + "-video.jpg")})
                    if len(items) >= 60:
                        break
            except Exception:
                pass
            self._json_out(200, {"ok": True, "snapshots": items})
        elif p == "/snapshot.zip":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = os.path.basename((q.get("name") or [""])[0])
            base = os.path.join(SNAPSHOT_DIR, name)
            if not name or not os.path.exists(base + ".jpg"):
                self._json_out(404, {"ok": False, "error": "no such snapshot"})
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for ext in (".jpg", ".raw", ".json"):
                    if os.path.exists(base + ext):
                        z.write(base + ext, name + ext)
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % name)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/tcal":
            if self.command == "POST":
                try:
                    body = json.loads(self._body() or "{}")
                except Exception:
                    body = {}
                if body.get("reset"):
                    _tcal["scale"], _tcal["offset"] = SCALE, OFFSET
                    _save_tcal()
                    self._json_out(200, {"ok": True, "scale": _tcal["scale"],
                                         "offset": _tcal["offset"], "reset": True})
                    return
                try:
                    pts = body.get("points") or []
                    if len(pts) != 2:
                        raise ValueError("two points are required")
                    sc, off = _fit_tcal(pts)
                except Exception as exc:
                    self._json_out(400, {"ok": False, "error": str(exc)})
                    return
                _tcal["scale"], _tcal["offset"] = sc, off
                _save_tcal()
                print("thermal calibration -> scale %g offset %g" % (sc, off), flush=True)
            self._json_out(200, {"ok": True, "scale": _tcal["scale"],
                                 "offset": _tcal["offset"],
                                 "factory": {"scale": SCALE, "offset": OFFSET}})
        elif p == "/captures":
            self._json_out(200, {"ok": True, "clips": PREREC.list()})
        elif p == "/captures.zip":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = os.path.basename((q.get("name") or [""])[0])
            path = os.path.join(PREREC_DIR, name + ".zip") if name else ""
            if not name or not os.path.exists(path):
                self._json_out(404, {"ok": False, "error": "no such clip"})
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except Exception:
                self._json_out(500, {"ok": False, "error": "cannot read clip"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % name)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/prerecord":
            if self.command == "POST":
                f = self._form()
                PREREC.configure(("enabled" in f and f.get("enabled") == "1"),
                                 int(float(f.get("before_s") or PREREC.before)),
                                 int(float(f.get("after_s") or PREREC.after)))
                _save_prerec()
            self._json_out(200, {"ok": True, "enabled": PREREC.enabled,
                                 "before_s": PREREC.before, "after_s": PREREC.after,
                                 "clips": PREREC.list()})
        elif p == "/alarms":
            if self.command == "POST":
                global _alarms
                try:
                    items = json.loads(self._body() or "[]")
                except Exception:
                    items = None
                if not isinstance(items, list):
                    self._json_out(400, {"ok": False, "error": "expected a JSON list"})
                    return
                _alarms = _sanitise_alarms(items)
                _save_alarms()
                print("alarms -> %d" % len(_alarms), flush=True)
            with _lock:
                events = list(_alarm_events)
            self._json_out(200, {"ok": True, "alarms": _alarms, "events": events})
        elif p == "/notify":
            if self.command == "POST":
                f = self._form()
                channel = f.get("channel", "")
                if f.get("test"):
                    NOTIFY.send([channel] if channel in NOTIFY.cfg else [],
                                "SuperCam test message - notifications are working.")
                    self._json_out(200, {"ok": True, "tested": channel,
                                         "channels": NOTIFY.status()})
                    return
                if channel in NOTIFY.cfg and f.get("clear"):
                    NOTIFY.cfg.pop(channel, None)
                    NOTIFY.save()
                    self._json_out(200, {"ok": True, "channels": NOTIFY.status()})
                    return
                ok = NOTIFY.set(channel, {k: v for k, v in f.items() if k != "channel"})
                self._json_out(200 if ok else 400,
                               {"ok": ok, "channels": NOTIFY.status()})
                return
            self._json_out(200, {"ok": True, "channels": NOTIFY.status()})
        elif p == "/regions":
            if self.command == "POST":
                try:
                    items = json.loads(self._body() or "[]")
                except Exception:
                    items = None
                if not isinstance(items, list):
                    self._json_out(400, {"ok": False, "error": "expected a JSON list"})
                    return
                _regions = _sanitise_regions(items)
                _save_regions()
                with _lock:
                    _state["regions"] = []
                print("regions -> %d" % len(_regions), flush=True)
            with _lock:
                res = list(_state.get("regions") or [])
            self._json_out(200, {"ok": True, "regions": _regions, "stats": res})
        elif p == "/regions.csv":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "clear" in q:
                try:
                    open(REGIONS_CSV, "w").close()
                except Exception:
                    pass
            try:
                with open(REGIONS_CSV, "rb") as f:
                    data = f.read()
            except Exception:
                data = b"time,region,name,kind,min_c,max_c,avg_c,delta_c\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="regions.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif p == "/history.json":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "clear" in q:
                with _lock:
                    _hist.clear()
            try:
                since = float((q.get("since") or ["0"])[0] or 0)
            except Exception:
                since = 0.0
            with _lock:
                samples = [list(x) for x in _hist if x[0] >= since]
            out = json.dumps({"ok": True, "samples": samples}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        elif p == "/thermal.json":
            with _lock:
                s = dict(_state)
            body = ('{"stats":%s,"frames":%d,"connected":%s,"vbytes":%d}'
                    % (__import__("json").dumps(s["stats"]), s["frames"],
                       str(s["connected"]).lower(), s["vbytes"])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/reboot":
            if self.command != "POST":
                out = json.dumps({"ok": False, "error": "POST only"}).encode()
                self.send_response(405)
            else:
                host = _client.host if _client else None
                if not host:
                    out = json.dumps({"ok": False, "error": "camera not connected"}).encode()
                    self.send_response(503)
                else:
                    res = REBOOT.reboot(host)
                    out = json.dumps(res).encode()
                    self.send_response(200 if res.get("ok") else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/camera":
            if self.command == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    want = json.loads(self.rfile.read(n) or b"{}")
                    cfg, key = _read_config()
                    ip = want.get("ip") or cfg["ip"]
                    if not CFG.validate_ip(ip):
                        raise ValueError("invalid IP address")
                    block = CFG.set_fields(bytes.fromhex(cfg["_raw"]),
                                           ip=ip,
                                           mask=want.get("mask") or None,
                                           gw=want.get("gw") or None,
                                           dns=want.get("dns") or None)
                    try:
                        res = _write_config_fresh(_client.host if _client else "", block, ip)
                    except Exception as _e:
                        res = _write_config(block, key, ip)
                    out = json.dumps({"ok": res["applied"], "requested": ip,
                                      "detail": res,
                                      "note": "a network change makes the camera reboot"}).encode()
                    self.send_response(200 if res["applied"] else 502)
                except Exception as e:
                    out = json.dumps({"ok": False, "error": str(e)}).encode()
                    self.send_response(400)
            else:
                try:
                    cfg, _key = _read_config()
                    out = json.dumps({"ok": True, **{k: v for k, v in cfg.items() if k != "_raw"}}).encode()
                    self.send_response(200)
                except Exception as e:
                    out = json.dumps({"ok": False, "error": str(e)}).encode()
                    self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/calibration":
            if self.command == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    cal = {"ts": max(0.1, min(10.0, float(body.get("ts", 1.0)))),
                           "tx": max(-100.0, min(100.0, float(body.get("tx", 0.0)))),
                           "ty": max(-100.0, min(100.0, float(body.get("ty", 0.0))))}
                    with open(CAL_PATH, "w") as f:
                        json.dump(cal, f)
                    out = json.dumps(cal).encode()
                    self.send_response(200)
                except Exception as e:
                    out = json.dumps({"error": str(e)}).encode()
                    self.send_response(400)
            else:
                out = json.dumps(_load_cal()).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p.startswith("/i18n/"):
            name = p[len("/i18n/"):]
            if (name.endswith(".json") and name[:-5].replace("-", "").isalnum()
                    and len(name) < 24):
                fp = os.path.join(HERE, "web", "i18n", name)
                try:
                    with open(fp, "rb") as f:
                        out = f.read()
                    self.send_response(200)
                except OSError:
                    out = b'{}'
                    self.send_response(404)
            else:
                out = b'{}'
                self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/version":
            out = json.dumps({"ok": True, "version": VERSION, "repo": UPD.REPO}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/update":
            ok_flag = False
            if self.command == "POST":
                try:
                    rel = UPD.latest()
                    if not rel.get("tarball"):
                        raise RuntimeError("no release found")
                    res = UPD.apply(rel["tarball"], HERE)
                    ok_flag = bool(res.get("ok"))
                    out = json.dumps({"ok": ok_flag, "from": VERSION, "to": rel["tag"],
                                      "detail": res}).encode()
                    code = 200 if ok_flag else 500
                except Exception as e:
                    out, code = json.dumps({"ok": False, "error": str(e)}).encode(), 502
                self.send_response(code)
            else:
                try:
                    rel = UPD.latest()
                    out = json.dumps({"ok": True, "current": VERSION, "latest": rel["tag"],
                                      "name": rel["name"], "url": rel["url"],
                                      "notes": rel["notes"][:1200],
                                      "available": UPD.is_newer(rel["tag"], VERSION)}).encode()
                    self.send_response(200)
                except Exception as e:
                    out = json.dumps({"ok": False, "error": str(e)}).encode()
                    self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            try:
                self.wfile.flush()
            except Exception:
                pass
            if ok_flag:
                print("release installed - exiting so the supervisor restarts cleanly", flush=True)
                threading.Timer(1.5, lambda: os._exit(3)).start()
            return
        elif p == "/trange":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "auto" in q:
                for k in _trange:
                    _trange[k] = None
                changed = True
            else:
                # Only the bounds actually supplied are touched: sending one must
                # not silently clear the others (that made a half-filled pair look
                # like it kept resetting itself to automatic).
                supplied = {k: q[k][0] for k in ("clip_lo", "clip_hi", "span_lo", "span_hi") if k in q}
                iso = dict(_trange["iso"] or {"bands": list(ISO_PCBA)})
                if "bands" in q:
                    try:
                        bs = json.loads(q["bands"][0])
                        okb = [b for b in bs
                               if isinstance(b, dict)
                               and isinstance(b.get("colour"), str)
                               and len(b.get("colour", "")) == 7
                               and b.get("lo") is not None and b.get("hi") is not None]
                        if okb:
                            iso["bands"] = [{"lo": float(b["lo"]), "hi": float(b["hi"]),
                                             "colour": b["colour"]} for b in okb[:32]]
                    except Exception:
                        pass
                _trange["iso"] = iso
                for alias, key in (("min", "span_lo"), ("max", "span_hi")):
                    if alias in q and key not in supplied:
                        supplied[key] = q[alias][0]
                changed = bool(supplied)
                try:
                    for k, v in supplied.items():
                        _trange[k] = None if v in ("", "auto") else float(v)
                except Exception as e:
                    out = json.dumps({"ok": False, "error": str(e)}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers(); self.wfile.write(out); return
            if changed:
                try:
                    with open(TRANGE_PATH, "w") as fh:
                        json.dump(_trange, fh)
                except Exception:
                    pass
            out = json.dumps(dict({"ok": True}, **_trange)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        elif p == "/isotherm":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = True
            if "apply" in q:
                ok = _apply_iso(q["apply"][0])
            elif "save" in q:
                name = (q["save"][0] or "").strip()[:40]
                if name:
                    _iso["profiles"][name] = dict(_trange["iso"] or {"bands": list(ISO_PCBA)})
                    _iso["current"] = name
                    _save_iso()
                else:
                    ok = False
            elif "delete" in q:
                name = q["delete"][0]
                if name in _iso["profiles"] and name not in ISO_BUILTIN:
                    del _iso["profiles"][name]
                    if _iso["current"] == name:
                        _iso["current"] = next(iter(ISO_BUILTIN))
                        _apply_iso(_iso["current"])
                    _save_iso()
                else:
                    ok = False
            out = json.dumps({"ok": ok, "current": _iso["current"],
                              "profiles": _iso["profiles"],
                              "iso": _trange["iso"]}).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        elif p == "/palette":
            name = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    .get("name") or [None])[0]
            if name is None:
                out = json.dumps({"ok": True, "palette": _palette["name"],
                                  "available": sorted(list(TH.LUTS) + ["isotherm"])}).encode()
                self.send_response(200)
            elif name in TH.LUTS or name == "isotherm":
                _palette["name"] = name
                try:
                    with open(PALETTE_PATH, "w") as fh:
                        fh.write(name)
                except Exception:
                    pass
                print("palette -> %s" % name, flush=True)
                out = json.dumps({"ok": True, "palette": name}).encode()
                self.send_response(200)
            else:
                out = json.dumps({"ok": False, "error": "unknown palette",
                                  "available": sorted(list(TH.LUTS) + ["isotherm"])}).encode()
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/health":
            with _lock:
                s = dict(_state)
            body = ('{"connected":%s,"frames":%d,"vbytes":%d,"tbytes":%d,"vdropped":%d,"vjpeg":%s,"vframes":%d,"vq":%d,"verr":"%s","vrestarts":%d,"vstalls":%d,"vsubs":%d,"rawconn":%d,"mjpegconn":%d}'
                    % (str(s["connected"]).lower(), s["frames"], s["vbytes"], s["tbytes"], _vdropped, str(bool(s["vjpeg"])).lower(), _vframes, _vin.qsize(), _verr.replace('"',"'"), _vrestarts, _vstalls, _vid["subs"], _hits["rawconn"], _hits["mjpeg"])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Thread(target=_stall_watchdog, daemon=True).start()
    print("relay2 on :%d" % PORT, flush=True)
    S(("0.0.0.0", PORT), H).serve_forever()
