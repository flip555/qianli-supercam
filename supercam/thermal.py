"""Thermal frame decode for the SuperCam secondary channel.

Frame layout (verified against a live capture): each frame is exactly
39448 bytes and begins with the vendor wrapper header::

    00 00 01 b3 <hdr> "LAUNCHDIGITAL" ... "RAW" 09 00 7b 00 a0 00 78 00 ...
                                        ^type      ^160    ^120

Pixel payload: **38400 bytes** (160 x 120, uint16 little-endian) starting at
**offset 88**; the rest of the frame is header/padding.
"""
from __future__ import annotations

import math
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


def _banded(lut: bytes, steps: int) -> bytes:
    """Quantise a LUT into `steps` flat colour bands (the isotherm look)."""
    steps = max(2, min(16, int(steps)))
    out = bytearray(256 * 3)
    for i in range(256):
        band = (i * steps) // 256
        centre = (band * 256 + 128) // steps
        out[i * 3:i * 3 + 3] = lut[centre * 3:centre * 3 + 3]
    return bytes(out)


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


def values(frame: bytes):
    """The raw 16-bit thermal values, for region statistics."""
    return struct.unpack_from("<%dH" % N, frame, PAYLOAD_OFFSET)


def decode(frame: bytes, palette: str = "rainbow",
           scale: float = 0.01, offset: float = 0.0,
           clip_lo: float = None, clip_hi: float = None,
           span_lo: float = None, span_hi: float = None,
           iso: dict = None) -> tuple[Image.Image, dict]:
    """Decode one thermal frame.

    Two independent bands, which is how the vendor software works:

    * ``clip_lo``/``clip_hi`` decide what is shown at all - anything outside is
      drawn white, which is how you ignore a 25 C room and look only at what is
      above 35 C.
    * ``span_lo``/``span_hi`` decide what the palette *spans*. Leave either unset
      and it is taken from the data that survives the clip, so the colours always
      use their full range over what you are actually looking at; set both and the
      whole palette maps onto that slice (e.g. 20.0-22.5 C on a board).

    With everything unset this behaves as it always did: colours across the
    frame's own min..max.
    """
    vals = struct.unpack_from("<%dH" % N, frame, PAYLOAD_OFFSET)
    lo, hi = min(vals), max(vals)
    raw = lambda c: None if c is None else int((c - offset) / scale)
    clo, chi = raw(clip_lo), raw(clip_hi)
    shown = [v for v in vals if (clo is None or v >= clo) and (chi is None or v <= chi)]
    base = shown or vals
    slo = raw(span_lo); shi = raw(span_hi)
    if slo is None: slo = min(base)
    if shi is None: shi = max(base)
    if shi <= slo: shi = slo + 1
    span = shi - slo
    lut = LUTS.get(palette, LUTS["rainbow"])
    iso_bands = None
    if palette == "isotherm" and iso:
        try:
            iso_bands = [(float(b["lo"]), float(b["hi"]),
                          bytes(int(b["colour"][k:k + 2], 16) for k in (1, 3, 5)))
                         for b in iso.get("bands", [])
                         if isinstance(b.get("colour"), str) and len(b["colour"]) == 7
                         and b["colour"][0] == "#"]
            if not iso_bands:
                iso_bands = None
        except Exception:
            iso_bands = None
    rgb = bytearray(N * 3)
    for i, v in enumerate(vals):
        if (clo is not None and v < clo) or (chi is not None and v > chi):
            rgb[i * 3:i * 3 + 3] = b"\xff\xff\xff"
            continue
        if iso_bands:
            temp = v * scale + offset
            col = b"\xff\xff\xff"
            for b_lo, b_hi, b_col in iso_bands:
                if b_lo <= temp < b_hi:
                    col = b_col
                    break
            rgb[i * 3:i * 3 + 3] = col
            continue
        b = (v - slo) * 255 // span
        b = 0 if b < 0 else 255 if b > 255 else b
        rgb[i * 3:i * 3 + 3] = lut[b * 3:b * 3 + 3]
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


def render_dt(vals, ref_vals, scale: float = 0.01, span: float = 10.0):
    """Signed difference image: blue = colder than the reference, white = same,
    red = hotter, clamped to +/- span degrees."""
    n = min(len(vals), len(ref_vals))
    lo = int(-span / scale)
    hi = int(span / scale)
    span_raw = (hi - lo) or 1

    def colour(t):
        if t < 0:
            k = -t
            return bytes((int(255 - 215 * k), int(255 - 170 * k), 255))
        return bytes((255, int(255 - 215 * t), int(255 - 215 * t)))

    lut = [colour(2 * i / 255.0 - 1.0) for i in range(256)]
    rgb = bytearray(N * 3)
    dmin = dmax = 0
    dsum = 0
    for i in range(N):
        if i >= n:
            rgb[i * 3:i * 3 + 3] = b"\xff\xff\xff"
            continue
        d = vals[i] - ref_vals[i]
        if d < dmin:
            dmin = d
        if d > dmax:
            dmax = d
        dsum += d
        b = (d - lo) * 255 // span_raw
        b = 0 if b < 0 else 255 if b > 255 else b
        rgb[i * 3:i * 3 + 3] = lut[b]
    img = Image.frombytes("RGB", (W, H), bytes(rgb))
    stats = {"dt_min_c": round(dmin * scale, 2),
             "dt_max_c": round(dmax * scale, 2),
             "dt_avg_c": round((dsum / N) * scale, 2)}
    return img, stats
