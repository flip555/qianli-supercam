"""SuperCam ("Neptune") proprietary protocol — validated 2026-10-13.

Pure client-side implementation: nothing is written to the camera.
Derived by replaying a live capture of the vendor Windows client
(`PCB_Client.exe` / `NetClient.dll`).

Transport: two TCP ports.
  * 3000  "video server"     — probe/init/keepalive + stream
  * 3001  "video server"     — registration/control

Handshake must be issued STRICTLY SEQUENTIALLY (one connection at a time,
in captured order) or the camera accepts the connection then drops it.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"\x78\xb6\x3a\x12"
SERVICE = b"video server"          # 12 bytes, no NUL
SERIAL = b"888888"                 # 6 bytes, repeated twice in register
PAD = 0xCC

VIDEO_PORT = 3000
CTRL_PORT = 3001


# --------------------------------------------------------------------------
# 48-byte protocol packets (port 3000)
# --------------------------------------------------------------------------
def probe(ip: str, port: int = VIDEO_PORT, value: int = 0,
          sid: bytes = b"\x21\xd3\x6c\x87") -> bytes:
    """type=0x01 — open a video stream pair (value 0 = main, 1 = secondary)."""
    ipb = bytes(int(x) for x in ip.split("."))
    return (MAGIC + SERVICE + b"\x00" + bytes([PAD]) * 11
            + struct.pack("<H", 1)
            + ipb + struct.pack("<H", port)
            + struct.pack("<I", value)
            + struct.pack("<I", 0x28)
            + sid)


def init(value: int = 0, channel: int = 8,
         sid: bytes = b"\x21\xd3\x6c\x87") -> bytes:
    """type=0x02 — bind a video socket to a channel."""
    return (MAGIC + bytes([PAD]) * 24
            + struct.pack("<H", 2)
            + bytes([PAD]) * 6
            + struct.pack("<I", value)
            + struct.pack("<I", channel)
            + sid)


def type14(value: int = 0, sid: bytes = b"\x21\xd3\x6c\x87") -> bytes:
    """type=0x14 — post-registration handshake on a video socket."""
    return (MAGIC + bytes([PAD]) * 24
            + struct.pack("<I", 0x14)
            + bytes([PAD]) * 4
            + struct.pack("<I", value)
            + struct.pack("<I", 0)
            + sid)


def keepalive3000(value: int = 0, sid: bytes = b"\x21\xd3\x6c\x87") -> bytes:
    """type=0x05 — periodic keepalive on port 3000 (~every 10 s)."""
    return (MAGIC + bytes([PAD]) * 24
            + struct.pack("<I", 0x05)
            + bytes([PAD]) * 4
            + struct.pack("<I", value)
            + struct.pack("<I", 0)
            + sid)


def serial_packet() -> bytes:
    """40-byte serial handshake sent right after PROBE."""
    return (SERIAL + b"\x00" + bytes([PAD]) * 13) * 2


def start(flag: int = 0x33, value: int = 1) -> bytes:
    """8-byte 'start stream' packet sent on a video socket after INIT.

    `flag` matches the flag carried by the 16-byte control message on 3001.
    This is the packet whose absence stopped the camera streaming.
    """
    return struct.pack("<I", flag) + struct.pack("<I", value)


# --------------------------------------------------------------------------
# Port 3001 — registration / control
# --------------------------------------------------------------------------
def register(cmd: int = 0x65, flags: int = 1,
             sid: bytes = b"\x21\xd3\x6c\x87") -> bytes:
    """88-byte registration/keepalive packet."""
    return (MAGIC + SERVICE + bytes(12) + SERIAL + bytes(14) + SERIAL
            + bytes(14) + struct.pack("<Q", cmd) + struct.pack("<Q", flags)
            + sid)


def ctrl(value: int, flag: int, port: int = VIDEO_PORT) -> bytes:
    """16-byte control message: announce a stream flag on a port."""
    return (struct.pack("<I", 3) + struct.pack("<I", value)
            + struct.pack("<I", flag) + struct.pack("<I", port))


KEEPALIVE_CTRL = struct.pack("<II", 1, 2)     # 8-byte 3001 keepalive
START_FLAGS = (0x33, 0x35)                    # per-stream flags seen live


# --------------------------------------------------------------------------
# Framing (camera -> client)
# --------------------------------------------------------------------------
FRAME_START = b"\x00\x00\x01\xb3"     # each image frame begins with this
FRAME_SIZE = 39448                    # observed fixed frame size
THERMAL_W, THERMAL_H = 160, 120
THERMAL_BYTES = THERMAL_W * THERMAL_H * 2   # 16-bit raw = 38400


@dataclass
class Frame:
    index: int
    raw: bytes            # full 39448-byte frame
    thermal: bytes | None  # 38400-byte 16-bit thermal block, if located


def iter_frames(buf: bytes):
    """Yield complete 39448-byte frames from a stream buffer.

    Returns (frames, remainder). The camera emits fixed-size frames whose
    first 4 bytes are FRAME_START; the thermal payload is the large
    ~38400-byte block inside each frame.
    """
    frames = []
    i = 0
    while True:
        j = buf.find(FRAME_START, i)
        if j < 0:
            break
        end = j + FRAME_SIZE
        if end > len(buf):
            break
        frames.append(buf[j:end])
        i = end
    # remainder starts at the last (partial) frame start, if any
    k = buf.rfind(FRAME_START)
    rem = buf[k:] if k >= 0 and (len(buf) - k) < FRAME_SIZE else b""
    return frames, rem


def find_thermal(frame: bytes) -> bytes | None:
    """Locate the 16-bit 160x120 thermal block inside a frame.

    Heuristic: the largest run of bytes between 00 00 01 markers that is at
    least THERMAL_BYTES long, trimmed to THERMAL_BYTES.
    """
    marks = []
    i = 0
    while True:
        j = frame.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        marks.append(j)
        i = j + 3
    marks.append(len(frame))
    for a, b in zip(marks, marks[1:]):
        if (b - a) >= THERMAL_BYTES:
            return frame[a: a + THERMAL_BYTES]
    return None
