"""Local HTTP/HTTPS settings, and a self-signed certificate for LAN use.

WebCodecs needs a secure context. A browser that cannot reach the relay over a public
HTTPS hostname therefore falls back to the MJPEG transcode; a self-signed certificate
fixes that on the LAN - the browser warns once, then the fast path works.
"""
import ipaddress
import json
import os
import socket
import subprocess

DEFAULTS = {"http": True, "https": False, "https_port": 8443, "https_names": ""}


def load(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return cfg
    for k, v in DEFAULTS.items():
        if k not in data:
            continue
        try:
            cfg[k] = bool(data[k]) if isinstance(v, bool) else int(data[k])
        except Exception:
            pass
    cfg["https_names"] = str(data.get("https_names") or "")
    return cfg


def save(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def local_names(extra=""):
    names = {"localhost", "127.0.0.1"}
    try:
        names.add(socket.gethostname())
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(info[4][0])
    except Exception:
        pass
    for n in (extra or "").replace(",", " ").split():
        if n.strip():
            names.add(n.strip())
    return sorted(n for n in names if n)


def _san(names):
    parts = []
    for n in names:
        try:
            ipaddress.ip_address(n)
            parts.append("IP:" + n)
        except ValueError:
            parts.append("DNS:" + n)
    return ",".join(parts)


def ensure_cert(cert_path, key_path, names, days=3650):
    """Create a self-signed certificate if one is not already there."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return False
    folder = os.path.dirname(cert_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key_path, "-out", cert_path, "-days", str(days),
         "-subj", "/CN=supercam-local",
         "-addext", "subjectAltName=" + _san(names)],
        check=True, capture_output=True)
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return True
