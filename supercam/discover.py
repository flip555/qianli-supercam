"""Find SuperCam / LAUNCHDIGITAL cameras on a network.

The vendor Windows client never broadcasts, so there is nothing to listen for:
discovery means sweeping TCP 3001 (the control port) and confirming a camera by
sending the captured probe packet and reading its reply. Cameras also listen on
TCP 3000 for the actual streams.

    python -m supercam.discover                       # the local /24(s)
    python -m supercam.discover --cidr 172.20.1.0/24  # a specific range
    python -m supercam.discover --cidr 10.0.0.0/24 --json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time

PROBE_PORT = 3001
STREAM_PORT = 3000
MAGIC = b"\x78\xb6\x3a\x12"      # every camera packet starts with this


def local_cidrs(siblings: int = 0) -> list[str]:
    """The /24 around each non-loopback IPv4 address.

    With `siblings`, also include that many /24s either side *within the same
    advertised network* - cameras are often one subnet over from the host.
    """
    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        ip, pfx = m.group(1), int(m.group(2))
        if ip.startswith("127.") or pfx < 16:
            continue
        base = ipaddress.ip_network(f"{ip}/24", strict=False)
        out.append(str(base))
        if siblings and pfx < 24:
            try:
                sibs = [str(x) for x in ipaddress.ip_network(f"{ip}/{pfx}", strict=False).subnets(new_prefix=24)]
                i = sibs.index(str(base))
                out.extend(sibs[max(0, i - siblings): i + siblings + 1])
            except Exception:
                pass
    return sorted(set(out))


def load_probe() -> bytes | None:
    """First control probe from the captured handshake template."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = (os.environ.get("SUPERCAM_TEMPLATE")
            or os.path.normpath(os.path.join(here, os.pardir, "capture", "cli3.txt")))
    try:
        for line in open(path):
            parts = line.strip().split("|")
            if len(parts) >= 5 and parts[2] == str(PROBE_PORT):
                return bytes.fromhex(parts[4])
    except OSError:
        pass
    return None


def _identify(host: str, pkt: bytes, timeout: float) -> dict | None:
    try:
        s = socket.create_connection((host, PROBE_PORT), timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        s.sendall(pkt)
        data, t0 = b"", time.time()
        while time.time() - t0 < timeout and len(data) < 1024:
            try:
                chunk = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            data += chunk
        if not data or not data.startswith(MAGIC):
            return None          # not a camera (e.g. an HTTP server on 3001)
        # the reply opens with a copy of our probe (one byte rewritten - the
        # session flag) followed by the identity block; parse only past it
        body = data[len(pkt):] if len(data) > len(pkt) else data
        name = None
        for raw in re.findall(rb"[\x20-\x7e]{3,}", body):
            t = raw.decode("ascii", "ignore").strip()
            if t and len(set(t)) > 1:          # skip single-character filler
                name = t
                break
        return {"ip": host, "reply_bytes": len(data), "name": name,
                "info": body.hex()}
    finally:
        s.close()


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def scan(cidrs: list[str] | None = None, workers: int = 128,
         timeout: float = 0.4, log=print) -> list[dict]:
    cidrs = cidrs or local_cidrs()
    if not cidrs:
        log("no local /24s found - pass --cidr explicitly")
        return []
    pkt = load_probe()
    if pkt is None:
        raise SystemExit("capture/cli3.txt missing or has no probe packet (set SUPERCAM_TEMPLATE)")
    hosts = [str(h) for c in cidrs for h in ipaddress.ip_network(c, strict=False).hosts()]
    log(f"scanning {len(hosts)} hosts across {', '.join(cidrs)} on tcp/{PROBE_PORT} ...")
    found: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        live = [h for h, ok in zip(hosts, ex.map(lambda h: _open(h, PROBE_PORT, timeout), hosts)) if ok]
    log(f"{len(live)} host(s) with tcp/{PROBE_PORT} open - identifying")
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, max(1, len(live)))) as ex:
        for info in ex.map(lambda h: _identify(h, pkt, timeout * 3), live):
            if info:
                info["stream_port"] = STREAM_PORT
                info["control_port"] = PROBE_PORT
                found.append(info)
                log(f"  camera: {info['ip']}  {info.get('name') or '(no name)'}")
    return found


def find_one(cidrs: list[str] | None = None, log=print) -> dict | None:
    """First camera found. Tries the given ranges (or the local /24s) first,
    then - when no range was specified - a bounded sweep of neighbouring /24s,
    so a camera one subnet over is still found. Never scans a whole /16."""
    found = scan(cidrs, log=log)
    if not found and not cidrs:
        wide = local_cidrs(siblings=4)
        log("nothing on the local /24(s) - widening to %d nearby subnets" % len(wide))
        found = scan(wide, log=log)
    return found[0] if found else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Discover SuperCam / LAUNCHDIGITAL cameras")
    ap.add_argument("--cidr", action="append", default=[],
                    help="CIDR to sweep (repeatable). Default: local /24s")
    ap.add_argument("--workers", type=int, default=128)
    ap.add_argument("--timeout", type=float, default=0.4)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = scan(a.cidr or None, workers=a.workers, timeout=a.timeout,
               log=(lambda *x: None) if a.json else print)
    if a.json:
        print(json.dumps(res, indent=2))
    elif not res:
        print("no cameras found")
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())
