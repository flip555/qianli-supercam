#!/usr/bin/env python3
"""SuperCam live relay - proven client loop.

Handshake reuses the reverse-engineered client; the read loop is the one that
provably pulls the full stream (~1.2 MB/s). The browser decodes the visible
H.264 itself (WebCodecs on /raw.h264); thermal is served as MJPEG.
"""
import collections, http.server, io, os, queue, socket, socketserver, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supercam.client import SuperCam, keepalive, load  # handshake + packet builders
from supercam import thermal as TH
from supercam import settings as CFG
from supercam import reboot as REBOOT
from supercam import update as UPD
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
_trange = {"clip_lo": None, "clip_hi": None,   # shown at all (None = no bound)
           "span_lo": None, "span_hi": None}   # palette span   (None = automatic)


def _load_trange():
    import json
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
        if name in TH.LUTS:
            _palette["name"] = name
            print("palette restored: %s" % name, flush=True)
    except Exception:
        pass


_load_palette()


def _load_cal():
    import json
    try:
        with open(CAL_PATH) as f:
            c = json.load(f)
    except Exception:
        c = {}
    return {"ts": float(c.get("ts", 1.0) or 1.0),
            "tx": float(c.get("tx", 0.0) or 0.0),
            "ty": float(c.get("ty", 0.0) or 0.0)}
_state = {"vjpeg": None, "vts": 0.0, "tjpeg": None, "tts": 0.0,
          "stats": None, "connected": False, "vbytes": 0, "tbytes": 0, "frames": 0}
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
            print("video stalled %.0fs with data flowing - encoder restarted (stalls=%d)"
                  % (age, _vstalls), flush=True)
        else:
            print("camera silent %.0fs - dropping the session so it rebuilds (stalls=%d)"
                  % (age, _vstalls), flush=True)
            if _client is not None:
                for sock in list(_client.socks.values()):
                    try:
                        sock.close()
                    except Exception:
                        pass
        if actions >= 3 and _camera_reachable():
            print("pipeline stuck for %.0fs with the camera up after %d attempts "
                  "- exiting so the supervisor starts it clean" % (age, actions), flush=True)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(2)


def _thermal_worker(client, sock, data):
    frames = client.dec.feed(data)
    if not frames:
        return
    try:
        img, stats = TH.decode(frames[-1], _palette["name"], SCALE, OFFSET,
                               _trange["clip_lo"], _trange["clip_hi"],
                               _trange["span_lo"], _trange["span_hi"])
        out = io.BytesIO(); img.save(out, format="JPEG", quality=80)
        jpeg = out.getvalue()
    except Exception:
        return
    with _lock:
        _state["tjpeg"] = jpeg; _state["tts"] = time.time()
        _state["stats"] = stats; _state["frames"] += 1


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
                j = _state[key]; t = _state[ts]
            if j and t != last:
                last = t
                try:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
                except OSError:
                    return
            time.sleep(0.02)

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
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
            import json
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
            import json
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
            import json
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
            import json
            out = json.dumps({"ok": True, "version": VERSION, "repo": UPD.REPO}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif p == "/update":
            import json
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
            import json, urllib.parse
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
        elif p == "/palette":
            import json, urllib.parse
            name = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    .get("name") or [None])[0]
            if name is None:
                out = json.dumps({"ok": True, "palette": _palette["name"],
                                  "available": sorted(TH.LUTS)}).encode()
                self.send_response(200)
            elif name in TH.LUTS:
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
                                  "available": sorted(TH.LUTS)}).encode()
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
