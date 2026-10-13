"""Restart the camera over its own Telnet service.

The camera runs a Telnet server (port 23) and accepts ``root`` with a blank
password. Restarting it is how a stored settings change takes effect - the
camera keeps the old address on the wire until it reboots.

Credentials can be overridden with CAMERA_TELNET_USER / CAMERA_TELNET_PASS.
"""
from __future__ import annotations

import os
import re
import socket
import time

USER = os.environ.get("CAMERA_TELNET_USER", "root")
PASSWORD = os.environ.get("CAMERA_TELNET_PASS", "")


def _read(sock, pattern=None, timeout=4.0):
    buf = b""
    end = time.time() + timeout
    sock.settimeout(0.4)
    while time.time() < end:
        try:
            d = sock.recv(8192)
            if not d:
                break
            buf += d
        except (socket.timeout, BlockingIOError, OSError):
            pass
        if pattern and re.search(pattern, buf, re.I):
            break
    return buf


def _refuse_options(greeting: bytes) -> bytes:
    """Answer Telnet option negotiation so the shell is happy."""
    out = bytearray()
    i = 0
    while i < len(greeting):
        if greeting[i] == 0xFF and i + 2 < len(greeting):
            cmd, opt = greeting[i + 1], greeting[i + 2]
            if cmd == 0xFD:
                out += bytes([0xFF, 0xFC, opt])      # DO   -> WONT
            elif cmd == 0xFB:
                out += bytes([0xFF, 0xFE, opt])      # WILL -> DONT
            i += 3
        else:
            i += 1
    return bytes(out)


def reboot(host: str, timeout: float = 12.0, command: str = "sync; reboot -f") -> dict:
    """Log in and restart the camera. Returns {'ok': bool, ...}."""
    try:
        s = socket.create_connection((host, 23), timeout=5)
    except OSError as e:
        return {"ok": False, "error": "telnet connect failed: %s" % type(e).__name__}
    try:
        greeting = _read(s, b"login:", 4.0)
        if b"login:" not in greeting:
            return {"ok": False, "error": "no login prompt"}
        opts = _refuse_options(greeting)
        if opts:
            s.sendall(opts)
        s.sendall(USER.encode() + b"\r\n")
        prompt = _read(s, b"assword|#|\\$", 4.0)
        if b"assword" in prompt:
            s.sendall(PASSWORD.encode() + b"\r\n")
            prompt = _read(s, b"[#\\$]", 5.0)
        if not re.search(rb"[#\$]\s*$", prompt.strip()[-8:] or b" "):
            if b"# " not in prompt and b"~ #" not in prompt:
                return {"ok": False, "error": "login failed"}
        s.sendall(command.encode() + b"\n")
        time.sleep(0.5)
        return {"ok": True, "host": host, "method": "telnet"}
    except OSError as e:
        return {"ok": False, "error": type(e).__name__}
    finally:
        try:
            s.close()
        except Exception:
            pass
