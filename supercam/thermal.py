"""Thermal frame decode for the SuperCam secondary channel.

Frame layout (verified against a live capture): each frame is exactly
39448 bytes and begins with the vendor wrapper header::

    00 00 01 b3 <hdr> "LAUNCHDIGITAL" ... "RAW" 09 00 7b 00 a0 00 78 00 ...
                                        ^type      ^160    ^120

Pixel payload: **38400 bytes** (160 x 120, uint16 little-endian) starting at
**offset 88**; the rest of the frame is header/padding.
"""
from __future__ import annotations

import struct

from PIL import Image

W, H = 160, 120
N = W * H
FRAME_SIZE = 39448
PAYLOAD_OFFSET = 88
PAYLOAD_BYTES = N * 2
MAGIC = b"\x00\x00\x01\xb3"
RAW_MARK = 48          # "RAW" sits at magic+48


def _ramp(stops: list[tuple[int, tuple[int, int, int]]]) -> bytes:
    """Build a 256-entry RGB LUT from (position, (r,g,b)) stops."""
    lut = bytearray(256 * 3)
    for i in range(256):
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= i <= p1:
                t = 0 if p1 == p0 else (i - p0) / (p1 - p0)
                for k in range(3):
                    lut[i * 3 + k] = int(c0[k] + (c1[k] - c0[k]) * t)
                break
        else:
            lut[i * 3:i * 3 + 3] = bytes(stops[-1][1])
    return bytes(lut)


LUTS = {
    # perceptual "iron"-ish rainbow, cool -> hot
    "rainbow": _ramp([(0, (0, 0, 40)), (48, (0, 90, 200)), (110, (0, 200, 160)),
                      (170, (240, 220, 40)), (215, (250, 120, 20)), (256, (255, 255, 255))]),
    "whitehot": _ramp([(0, (0, 0, 0)), (256, (255, 255, 255))]),
    "blackhot": _ramp([(0, (255, 255, 255)), (256, (0, 0, 0))]),
    "ironbow": _ramp([(0, (0, 0, 0)), (64, (90, 0, 130)), (128, (200, 50, 20)),
                      (192, (250, 180, 30)), (256, (255, 255, 255))]),
}


class ThermalDecoder:
    """Reassembles 39448-byte thermal frames from a byte stream."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                if len(self.buf) > FRAME_SIZE:
                    del self.buf[:-FRAME_SIZE]
                break
            if len(self.buf) < i + RAW_MARK + 3:
                del self.buf[:i]
                break
            if self.buf[i + RAW_MARK:i + RAW_MARK + 3] != b"RAW":
                del self.buf[:i + 4]          # false positive
                continue
            if len(self.buf) < i + FRAME_SIZE:
                if i:
                    del self.buf[:i]
                break
            out.append(bytes(self.buf[i:i + FRAME_SIZE]))
            del self.buf[:i + FRAME_SIZE]
        return out


def _peaks(vals, n: int = 3, min_sep: int = 14, margin: int = 3):
    """Up to n hottest spots, kept apart so we label distinct regions.
    A border margin skips sensor-edge artefacts."""
    out = []
    for i in sorted(range(N), key=lambda k: vals[k], reverse=True):
        x, y = i % W, i // W
        if x < margin or y < margin or x >= W - margin or y >= H - margin:
            continue
        if all((x - px) ** 2 + (y - py) ** 2 >= min_sep ** 2 for px, py, _ in out):
            out.append((x, y, vals[i]))
        if len(out) >= n:
            break
    return out


def decode(frame: bytes, palette: str = "rainbow",
           scale: float = 0.01, offset: float = 0.0,
           tmin: float = None, tmax: float = None) -> tuple[Image.Image, dict]:
    """Decode one thermal frame.

    With no range the colours span the frame's own min..max (automatic contrast).
    Given ``tmin``/``tmax`` in degrees, the ramp is pinned to that band instead and
    everything outside it is drawn white - so a narrow band can be picked out of a
    scene without the colours sliding around as the scene changes.
    """
    vals = struct.unpack_from("<%dH" % N, frame, PAYLOAD_OFFSET)
    lo, hi = min(vals), max(vals)
    if tmin is None or tmax is None or tmax <= tmin:
        clo, chi = lo, hi          # automatic
    else:
        clo, chi = int((tmin - offset) / scale), int((tmax - offset) / scale)
    span = (chi - clo) or 1
    lut = LUTS.get(palette, LUTS["rainbow"])
    rgb = bytearray(N * 3)
    for i, v in enumerate(vals):
        if v < clo or v > chi:
            rgb[i * 3:i * 3 + 3] = b"\xff\xff\xff"
        else:
            b = (v - clo) * 255 // span
            rgb[i * 3:i * 3 + 3] = lut[(0 if b < 0 else 255 if b > 255 else b) * 3:
                                       (0 if b < 0 else 255 if b > 255 else b) * 3 + 3]
    img = Image.frombytes("RGB", (W, H), bytes(rgb))
    hot = max(range(N), key=lambda i: vals[i])
    cold = min(range(N), key=lambda i: vals[i])
    peaks = _peaks(vals)
    stats = {
        "min_c": round(lo * scale + offset, 2),
        "max_c": round(hi * scale + offset, 2),
        "mean_c": round((sum(vals) / N) * scale + offset, 2),
        "hot": [hot % W, hot // W],
        "cold": [cold % W, cold // W],
        "peaks": [{"x": x, "y": y, "c": round(v * scale + offset, 2)} for x, y, v in peaks],
        "w": W, "h": H,
    }
    return img, stats
