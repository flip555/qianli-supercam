"""Read and write the camera's own network configuration.

The camera exposes its settings as a fixed 376-byte structure. A control probe
gets a reply that starts with an 88-byte echo of the probe, followed by that
structure; writing it back (same 376 bytes, sent raw on a control socket)
reconfigures the camera.

Field offsets inside the 376-byte block:

    0    24   device name      (ASCII, NUL padded)
    24   16   IP address
    40   16   netmask
    56   16   gateway
    72   16   DNS
    88   16   factory/misc field (observed 235.1.1.1)
    104  72   opaque: ports, MAC tail, flags  - preserved verbatim
    176  96   firmware string (repeated twice)
    276  100  build date (repeated twice)
"""
from __future__ import annotations

import re

BLOCK_LEN = 376
ECHO_LEN = 88
DOTTED = re.compile(rb"^\d{1,3}(?:\.\d{1,3}){3}$")

FIELDS = [
    ("name", 0, 24),
    ("ip", 24, 16),
    ("mask", 40, 16),
    ("gw", 56, 16),
    ("dns", 72, 16),
    ("misc", 88, 16),
    ("firmware", 176, 48),
    ("date", 276, 48),
]


def _str(block: bytes, off: int, size: int) -> str:
    return block[off:off + size].split(b"\x00")[0].decode("ascii", "ignore").strip()


def parse(block: bytes) -> dict:
    """Decode a 376-byte configuration block into named fields."""
    out = {name: _str(block, off, size) for name, off, size in FIELDS}
    out["_raw"] = block.hex()
    return out


def find(blob: bytes):
    """Locate the config block inside a control reply.

    Returns (offset, block) or None. The block either stands alone (a write) or
    follows the 88-byte probe echo (a read reply).
    """
    for off in (ECHO_LEN, 0):
        if len(blob) < off + 40:
            continue
        ip = blob[off + 24:off + 40].split(b"\x00")[0]
        name = blob[off:off + 24].split(b"\x00")[0]
        if DOTTED.match(ip) and name and len(name) > 1:
            block = bytearray(blob[off:off + BLOCK_LEN])
            if len(block) < BLOCK_LEN:              # replies are slightly short
                block += b"\x00" * (BLOCK_LEN - len(block))
            return off, bytes(block)
    return None


def set_fields(block: bytes, **changes) -> bytes:
    """Return a copy of the block with the named fields replaced.

    Only the string fields are touched, so everything opaque (MAC, ports,
    firmware, flags) is preserved byte-for-byte.
    """
    out = bytearray(block[:BLOCK_LEN])
    if len(out) < BLOCK_LEN:
        out += b"\x00" * (BLOCK_LEN - len(out))
    for name, value in changes.items():
        if value is None:
            continue
        for fname, off, size in FIELDS:
            if fname != name:
                continue
            raw = str(value).encode("ascii")[:size - 1]
            out[off:off + size] = raw + b"\x00" * (size - len(raw))
    return bytes(out)


def validate_ip(value: str) -> bool:
    parts = str(value).split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 and str(int(p)) == p for p in parts)
    except ValueError:
        return False
