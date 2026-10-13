# SuperCam / LAUNCHDIGITAL camera protocol (reverse-engineered)

Derived by capturing the vendor Windows client on a bridged tap and replaying it
byte-for-byte against the camera.

## Ports

| Port | Role |
|------|------|
| 3001 | control / probe. 88-byte probe packets carry the ASCII string `video server`. |
| 3000 | media. Each open socket corresponds to one video "value" (0 = visible, 1 = secondary/thermal). |

## The per-session flag (the important bit)

There are **no fixed magic values**. The camera assigns a **fresh 4-byte flag per
session**, delivered in a **56-byte PROBE reply** on port 3001 - the one whose
bytes 28..29 are the marker `01 00`; the flag is at **offset 48..51** of that
reply (`.flag` in `supercam/client.py`). Not every reply carries it, so the
client scans replies for the marker rather than assuming the first one.

The client must echo that live flag in:

* the **8-byte** START packet,
* the **16-byte** CTRL packet.

If you replay a stale flag, the camera accepts *every* message politely (the
register ACK is byte-identical, including its `Neptune` name and MAC) and then
**FINs the media sockets after 1-3 s, sending zero frames**. This is invisible to
any check that only looks at handshake responses - always verify that frames
actually arrive.

## Frames

Both channels deliver fixed-size wrapper blocks of **39448 bytes**:

```
00 00 01 b3 <hdr> "LAUNCHDIGITAL" ... "RAW"|"H264" ...
```

* Visible (value 0): the wrapper carries an `H264` payload - a normal Annex-B
  H.264 stream (1920x1080, ~30 fps, ~1 MB/s).
* Secondary (value 1): the wrapper carries a `RAW` payload - a **160x120,
  16-bit little-endian** thermal image, **38400 bytes at frame offset 88**.
  Header bytes `a0 00` = 160, `78 00` = 120, `10 00` = 16-bit.
  Temperature is `raw / 100` degrees C (offset unverified - calibrate against a
  reference).

Note the stream is **block-aligned**: byte 0 of a capture is not a block start
(observed a 35352-byte preamble), so locate frames by the `00 00 01 b3` magic
**with `RAW`/`H264` at offset 48**, not by naive offset arithmetic.

## Keepalives

The client sends an 8-byte keepalive (`05 00 00 00 cccc cccc <flag> 00 00 00`) on
each media socket roughly every 10 s. Without it the camera stops the stream.

## Reading and writing the camera's settings

Settings are a fixed **376-byte structure**; the camera reports it in the reply to
a control probe (offset 88 of a 458/464-byte reply).

| offset | field |
|--------|-------|
| 0   (24) | device name |
| 24  (16) | IP address |
| 40  (16) | netmask |
| 56  (16) | gateway |
| 72  (16) | DNS |
| 88  (16) | factory/misc field |
| 104 (72) | opaque: ports, MAC, flags - preserve verbatim |
| 176 (96) | firmware string |
| 276 (100)| build date |

**Writing** is a three-step ritual on one control connection, captured from the
vendor client:

```
6c            plain probe        -> camera replies with its settings
6c .. 01 ..7801  WRITE MODE probe -> 6-byte ack
<376 bytes>   the settings block -> ack
6a            COMMIT             -> the camera ECHOES 6a back
```

The camera **reboots** to apply a network change (it beeps when ready, ~2 min).
Two traps learned the hard way: the write-mode probe differs from a plain probe
at offsets 70 and 80/81 (not where you'd guess), and a burst of probes down one
connection makes the camera reset it - each probe needs its own connection.

## Acknowledgement

Reverse-engineered from observed behaviour only; no vendor documentation was
used and no vendor code is redistributed.
