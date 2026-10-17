"""Rolling pre-record buffer, saved when an alarm fires.

Keeps the rendered thermal frames - the same picture the operator was watching, with
the palette that was in use - for a configurable window, so a saved clip contains the
seconds *before* the trigger as well as after. Each clip becomes a zip with the frames
and a meta.json describing why it was kept.

Raw radiometric capture (16-bit frames, re-analysable later) is a separate and heavier
feature; this one is about seeing what happened.
"""
import collections
import json
import os
import shutil
import time
import zipfile


class PreRecord:
    def __init__(self, root, before_s=10, after_s=10, fps=10):
        self.root = root
        self.enabled = True
        self.before = max(0, int(before_s))
        self.after = max(0, int(after_s))
        self.fps = max(1, int(fps))
        self.buf = collections.deque(maxlen=max(20, int(self.fps * max(1, self.before) * 2)))
        self.pending = None

    def configure(self, enabled=None, before_s=None, after_s=None):
        if enabled is not None:
            self.enabled = bool(enabled)
        if before_s is not None:
            self.before = max(0, min(int(before_s), 300))
        if after_s is not None:
            self.after = max(0, min(int(after_s), 300))
        self.buf = collections.deque(maxlen=max(20, int(self.fps * max(1, self.before) * 2)))

    # ---------- frames ----------
    def add(self, ts, jpeg):
        if not self.enabled:
            return
        if not jpeg:
            return
        self.buf.append((ts, jpeg))
        if self.pending:
            self.pending["frames"].append((ts, jpeg))
            if ts >= self.pending["until"]:
                self._finish()

    def trigger(self, ts, meta):
        """Start a clip: everything in the buffer, plus `after` seconds to come."""
        if not self.enabled or self.pending:
            return False
        pre = [(t, j) for (t, j) in self.buf if ts - t <= self.before]
        if not pre:
            return False
        self.pending = {"until": ts + self.after, "meta": dict(meta or {}), "frames": pre}
        return True

    def _finish(self):
        pending, self.pending = self.pending, None
        frames = pending["frames"]
        if not frames:
            return
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(frames[-1][0]))
            os.makedirs(self.root, exist_ok=True)
            tmp = os.path.join(self.root, ".tmp-" + stamp)
            os.makedirs(os.path.join(tmp, "frames"), exist_ok=True)
            for i, (_t, jpeg) in enumerate(frames):
                with open(os.path.join(tmp, "frames", "%05d.jpg" % i), "wb") as f:
                    f.write(jpeg)
            meta = dict(pending["meta"])
            meta.update({"frames": len(frames),
                         "start": round(frames[0][0], 2),
                         "end": round(frames[-1][0], 2),
                         "before_s": self.before, "after_s": self.after})
            with open(os.path.join(tmp, "meta.json"), "w") as f:
                json.dump(meta, f, indent=2)
            target = os.path.join(self.root, stamp + ".zip")
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for name in sorted(os.listdir(os.path.join(tmp, "frames"))):
                    z.write(os.path.join(tmp, "frames", name), "frames/" + name)
                z.write(os.path.join(tmp, "meta.json"), "meta.json")
            shutil.rmtree(tmp, ignore_errors=True)
            print("clip saved: %s (%d frames)" % (target, len(frames)), flush=True)
        except Exception:
            pass

    # ---------- listing ----------
    def list(self, limit=50):
        out = []
        try:
            for name in sorted(os.listdir(self.root), reverse=True):
                if not name.endswith(".zip"):
                    continue
                path = os.path.join(self.root, name)
                st = os.stat(path)
                out.append({"name": name[:-4], "size": st.st_size,
                            "at": round(st.st_mtime, 1)})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out
