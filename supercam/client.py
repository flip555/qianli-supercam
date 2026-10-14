#!/usr/bin/env python3
"""SuperCam native client (QIANLI / SUNSHINE LC-AD11, TB-03 V2.0) - no Windows app.

Proven recipe: replay the captured client sequence EXACTLY (cli3.txt), with the
LIVE per-session flag (from each 56-byte PROBE reply) substituted into the
8-byte START and 16-byte CTRL, then hold the session with type=0x05 keepalives
every 10 s and read the H.264 continuously.
"""
import socket, time, struct, os, sys, argparse, collections

MAGIC = b"\x78\xb6\x3a\x12"
_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("SUPERCAM_TEMPLATE") or os.path.normpath(
    os.path.join(_HERE, os.pardir, "capture", "cli3.txt"))
if not os.path.exists(SRC):
    SRC = os.environ.get("SUPERCAM_TEMPLATE", SRC)

PROBE_TMPL = (MAGIC + b"video server\x00" + bytes([0xCC]) * 11)
KA_TMPL = (MAGIC + bytes([0xCC]) * 24 + struct.pack("<I", 5) + bytes([0xCC]) * 4)


def keepalive(value):
    return KA_TMPL + struct.pack("<I", value) + struct.pack("<I", 0) + bytes.fromhex("21d36c87")


def load(path=SRC, tmax=6.0):
    rows = []
    for line in open(path):
        p = line.strip().split("|")
        if len(p) < 5 or not p[4]:
            continue
        t = float(p[0])
        if t > tmax:
            continue
        rows.append((t, int(p[1]), int(p[2]), bytes.fromhex(p[4])))
    rows.sort(key=lambda r: r[0])
    keep, last = [], {}
    for r in rows:
        k = (r[1], r[2], r[3])
        if k in last and (r[0] - last[k]) < 0.002:
            continue
        last[k] = r[0]; keep.append(r)
    return keep


class SuperCam:
    def __init__(self, host="192.168.1.64"):
        self.host = host
        self.socks = {}
        self.value_of = {}
        self.probe_val = {}
        self.probe_by_val = {}
        self.flag = {}
        self.buf = collections.defaultdict(bytearray)
        self.tot = collections.Counter()

    def _conn(self, port):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(3); s.connect((self.host, port))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def _parse_flag(self, k):
        b = self.buf[k]
        if len(b) >= 52 and b[28:30] == b"\x01\x00" and k in self.probe_val:
            self.flag[self.probe_val[k]] = bytes(b[48:52])
            return True
        return False

    def _pump(self, k, secs=0.5):
        s = self.socks.get(k)
        if not s or self._parse_flag(k):
            return
        s.settimeout(secs); end = time.time() + secs
        while time.time() < end:
            try:
                d = s.recv(4096)
            except socket.timeout:
                break
            except Exception:
                break
            if not d:
                break
            self.buf[k] += d
            if self._parse_flag(k):
                return

    def handshake(self):
        rows = load()
        t0 = rows[0][0]; start = time.time()
        for t, sp, dp, pl in rows:
            dt = (t - t0) - (time.time() - start)
            if dt > 0:
                time.sleep(dt)
            k = (sp, dp)
            if k not in self.socks:
                try:
                    self.socks[k] = self._conn(dp)
                except Exception as e:
                    self.socks[k] = None; print("open fail", k, e); continue
            s = self.socks[k]
            if s is None:
                continue
            typ = pl[28:30] if len(pl) == 48 else None
            if typ == b"\x01\x00":
                self.probe_val[k] = struct.unpack("<I", pl[36:40])[0]
                self.probe_by_val[self.probe_val[k]] = k
            elif typ == b"\x02\x00":
                self.value_of[k] = struct.unpack("<I", pl[36:40])[0]
            elif len(pl) == 8 and k in self.value_of:
                v = self.value_of[k]
                if v not in self.flag and v in self.probe_by_val:
                    self._pump(self.probe_by_val[v])
                if v in self.flag:
                    pl = self.flag[v] + pl[4:8]
            elif len(pl) == 16:
                cv = struct.unpack("<I", pl[4:8])[0]
                v = 0 if cv == 2 else (1 if cv == 4 else None)
                if v is not None:
                    if v not in self.flag and v in self.probe_by_val:
                        self._pump(self.probe_by_val[v])
                    if v in self.flag:
                        pl = pl[:8] + self.flag[v] + pl[12:16]
            try:
                s.sendall(pl)
            except Exception as e:
                print("send fail", k, e)
        for k in list(self.socks):
            self._pump(k, 0.2)
        print("flags:", {k: v.hex() for k, v in self.flag.items()})
        self.probe_socks = dict(self.probe_by_val)
        # video sockets = biggest-value sockets; identify by captured labels
        self.video = {0: self.socks.get((61872, 3000)), 1: self.socks.get((61873, 3000))}
        return self.flag

    def run(self, seconds=60, out=None, on_frame=None):
        vids = [s for s in self.video.values() if s]
        for s in vids:
            s.setblocking(False)
        files = {}
        if out:
            files[self.video[0]] = open(out, "wb")
            files[self.video[1]] = open(out + ".side", "wb")
        t_end = time.time() + seconds
        t_ka = time.time() + 10
        while time.time() < t_end:
            if time.time() >= t_ka:
                for v, k in self.probe_by_val.items():
                    try: self.socks[k].sendall(keepalive(v))
                    except Exception: pass
                t_ka = time.time() + 10
            for s in vids:
                try:
                    d = s.recv(262144)
                except BlockingIOError:
                    continue
                except Exception:
                    continue
                if not d:
                    continue
                self.tot[s] += len(d)
                if s in files:
                    files[s].write(d)
                if on_frame:
                    on_frame(s, d)
            time.sleep(0.001)
        for f in files.values():
            f.flush(); f.close()
        return self.tot


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.64")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--out", default="/root/native_main.h264")
    a = ap.parse_args()
    c = SuperCam(a.host)
    c.handshake()
    tot = c.run(a.seconds, a.out)
    print("=== totals (bytes) ===")
    for s, n in tot.items():
        print("  ", s.getpeername(), n)
