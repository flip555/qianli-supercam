"""Self-update from GitHub releases.

The repository is public, so no credentials are involved. Only files shipped in
the release are installed: local state (calibration, palette, the service
script, pid files) is preserved, and the previous deployment is copied aside as
a timestamped backup so a bad release can be rolled back by hand.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.request

REPO = os.environ.get("SUPERCAM_REPO", "flip555/qianli-supercam")
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
UA = {"User-Agent": "supercam-relay"}

# deployment-owned state: never overwrite these with release content
KEEP = {"calibration.json", "palette.txt", "svc.sh", "svc.pid", "relay.pid",
        "__pycache__"}


def _get(url, timeout=15):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def latest(timeout=15) -> dict:
    with _get(API_LATEST, timeout) as r:
        j = json.load(r)
    return {"tag": j.get("tag_name") or "", "name": j.get("name") or "",
            "notes": (j.get("body") or "").strip(), "tarball": j.get("tarball_url") or "",
            "url": j.get("html_url") or "", "published": j.get("published_at") or ""}


def parts(tag) -> tuple:
    out = []
    for chunk in (tag or "").lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if digits:
            out.append(int(digits))
    return tuple(out)


def is_newer(latest_tag, current) -> bool:
    """True only when the release is a strictly newer version."""
    return parts(latest_tag) > parts(current)


def apply(tarball_url, dest, timeout=120) -> dict:
    """Install a release tarball over ``dest``, preserving local state."""
    dest = os.path.abspath(dest)
    tmp = tempfile.mkdtemp(prefix="supercam-update-")
    try:
        arc = os.path.join(tmp, "release.tar.gz")
        with _get(tarball_url, timeout) as r, open(arc, "wb") as f:
            shutil.copyfileobj(r, f)
        with tarfile.open(arc) as tf:
            tf.extractall(os.path.join(tmp, "x"))
        inner = [os.path.join(tmp, "x", d) for d in os.listdir(os.path.join(tmp, "x"))]
        root = inner[0] if inner else None
        if (not root or not os.path.isfile(os.path.join(root, "relay.py"))
                or not os.path.isdir(os.path.join(root, "supercam"))):
            return {"ok": False, "error": "release does not look like this project"}
        backup = "%s.bak-%s" % (dest, time.strftime("%Y%m%d-%H%M%S"))
        if os.path.isdir(dest):
            shutil.copytree(dest, backup, symlinks=True)
        installed = []
        for name in sorted(os.listdir(root)):
            if name in KEEP:
                continue
            src, dst = os.path.join(root, name), os.path.join(dest, name)
            if os.path.isdir(src):
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            installed.append(name)
        return {"ok": True, "installed": installed, "backup": backup}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
