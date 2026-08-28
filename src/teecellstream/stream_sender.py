"""Fragments one H.264 access unit into paced UDP packets, and splits a live Annex-B pipe into access units.

Port of StreamSender.cs (send_access_unit) and of LiveAnnexBSplitter from LiveStreamer.cs (AnnexBSplitter).

fragment packet layout (20-byte header, big-endian, must match stream.c on the PS3):
  [0]='V' [1]='F' [2..5]=frameId [6..7]=fragIndex [8..9]=fragCount [10]=flags(bit0 keyframe)
  [11]=version [12..19]=encoder-exit time (server microseconds, for latency measurement)
every fragment except a frame's last carries exactly FRAGMENT_PAYLOAD_BYTES of data - the PS3 places
fragment i at i * FRAGMENT_PAYLOAD_BYTES and sizes the frame off the last fragment alone.
"""

import struct

from . import log, protocol
from .clock import now_us, sleep_until_us

FRAGMENT_PAYLOAD_BYTES = protocol.FRAGMENT_PAYLOAD_BYTES
FRAGMENT_HEADER_BYTES = protocol.FRAGMENT_HEADER_BYTES
PROTOCOL_VERSION = protocol.PROTOCOL_VERSION

# 'V','F', frameId u32, fragIndex u16, fragCount u16, flags u8, version u8, encoder-exit u64 - all big-endian
_HEADER = struct.Struct(">2sIHHBBQ")
assert _HEADER.size == FRAGMENT_HEADER_BYTES

_START_CODE = b"\x00\x00\x01"
_PICTURE_NAL_TYPES = (1, 5)   # coded slice of a non-IDR / IDR picture

# the PS3 refuses any frame past its FRAME_MAX_BYTES (1MiB): stream.c checks (fragCount-1)*1300 + payloadBytes
# against it on EVERY fragment with that fragment's own payload, so a frame whose full-size fragments overshoot
# the limit loses all of them and never completes - the largest frame that arrives whole is the last multiple
# of the payload size below 1MiB, 806 fragments. sending a bigger one anyway costs the link and stalls the pump
# for the whole paced send (284ms at 30Mbit/s, seconds beyond) for a frame that lands nowhere, so it stays off
# the wire and is said once. the VBV (bufsize 250ms) keeps every real frame far below this; the limit also
# keeps fragIndex/fragCount inside their u16 wire fields, where the original's byte casts would have wrapped.
PS3_FRAME_MAX_BYTES = 1024 * 1024
MAX_UNIT_BYTES = PS3_FRAME_MAX_BYTES // FRAGMENT_PAYLOAD_BYTES * FRAGMENT_PAYLOAD_BYTES   # 1047800
_oversize_reported = False


# pace by SEND RATE, not by a fixed slice of time. spreading every frame over the same window fires a
# keyframe (3-4x a normal frame) as a huge burst - measured at ~400Mbps instantaneous, well past what the
# link absorbs, so packets dropped once per keyframe and each drop froze the picture. at a fixed rate a big
# frame simply takes proportionally longer. the original busy-waited for sub-millisecond precision; Python
# cannot spin without starving every other thread (GIL), so sleep_until_us sleeps to ~150us before the
# due time and spins only that last hair.
#
# blocks the caller for length / send_rate (a 100KB keyframe at 30Mbit/s is ~27ms); a socket error
# propagates to the caller, as in the original - the pump loop owns the decision what to do about it.
def send_access_unit(sock, target: tuple[str, int], frame_id: int, data, keyframe: bool,
                     capture_us: int, send_rate_kbps: int) -> None:
    global _oversize_reported
    view = memoryview(data).cast("B")            # count bytes, whatever buffer the caller hands over
    length = len(view)
    frag_count = (length + FRAGMENT_PAYLOAD_BYTES - 1) // FRAGMENT_PAYLOAD_BYTES   # 0 for an empty unit: nothing leaves
    if length > MAX_UNIT_BYTES:
        if not _oversize_reported:
            _oversize_reported = True
            log.write("live: Frame %d mit %d Bytes überschreitet das PS3-Limit von %d Bytes - verworfen"
                      % (frame_id & 0xFFFFFFFF, length, MAX_UNIT_BYTES))
        return
    frame_id &= 0xFFFFFFFF                       # the wire field is u32; the pump's counter simply wraps
    capture_us &= 0xFFFFFFFFFFFFFFFF
    flags = 1 if keyframe else 0

    # microseconds one fragment's worth of bits takes at the target rate
    start_us = now_us()
    per_fragment_us = (FRAGMENT_HEADER_BYTES + FRAGMENT_PAYLOAD_BYTES) * 8 * 1000 // max(1, send_rate_kbps)
    pack = _HEADER.pack
    sendto = sock.sendto
    for frag_index in range(frag_count):
        if per_fragment_us > 0:
            sleep_until_us(start_us + frag_index * per_fragment_us)
        offset = frag_index * FRAGMENT_PAYLOAD_BYTES
        header = pack(b"VF", frame_id, frag_index, frag_count, flags, PROTOCOL_VERSION, capture_us)
        sendto(header + view[offset:offset + FRAGMENT_PAYLOAD_BYTES], target)


# incremental Annex-B access-unit splitter for a live pipe: push encoder bytes in, take complete access
# units out. parameter units (SPS/PPS/SEI) attach to the picture that follows; a picture unit closes the
# unit, and the unit ends where the next unit's first NAL begins - so a unit only comes out once the
# encoder has begun the next one (a live pipe never needs the last one; flush() exists for files/tests).
#
# start codes are found with bytes.find (C speed): at 10Mbit/s that is ~1000 NALs a second, and walking
# the buffer byte by byte in Python was measured far too slow to keep up with the encoder.
class AnnexBSplitter:
    def __init__(self):
        self._pending = bytearray()
        self._scan = 0               # where the next start-code search begins
        self._unit_start = -1        # offset of the open unit's first NAL in _pending, -1 = no unit open
        self._unit_has_picture = False
        self._unit_keyframe = False
        self._completed: tuple[bytes, bool] | None = None

    def push(self, data) -> None:
        self._pending += data

    def take_access_unit(self) -> tuple[bytes, bool] | None:
        """One complete access unit as (bytes, keyframe), or None until the next NAL has begun."""
        pending = self._pending
        find = pending.find
        while self._completed is None:
            position = find(_START_CODE, self._scan)
            available = len(pending)
            if position < 0:
                # no start code yet; keep the last two bytes in view - they may be the front of a split one
                self._scan = max(self._scan, available - 2)
                break
            if position + 3 >= available:
                self._scan = position   # start code seen but its NAL header byte has not arrived: resume right here
                break

            nal_start = position - 1 if (position > 0 and pending[position - 1] == 0) else position
            nal_type = pending[position + 3] & 0x1F
            is_picture = nal_type in _PICTURE_NAL_TYPES

            if self._unit_start >= 0 and self._unit_has_picture:
                # a complete access unit ends where the next one's first unit begins
                with memoryview(pending) as window:
                    self._completed = (bytes(window[self._unit_start:nal_start]), self._unit_keyframe)
                # drop the consumed bytes and restart the new unit at the front of the buffer
                del pending[:nal_start]
                position -= nal_start
                self._unit_start = 0
                self._unit_has_picture = False
                self._unit_keyframe = False
            if self._unit_start < 0:
                self._unit_start = nal_start
                self._unit_has_picture = False
                self._unit_keyframe = False
            if is_picture:
                self._unit_has_picture = True
                self._unit_keyframe |= nal_type == 5
            self._scan = position + 3

        result = self._completed
        self._completed = None
        return result

    def flush(self) -> tuple[bytes, bool] | None:
        """End of stream: the next complete unit if one is still waiting, else the open trailing unit if it holds a
        picture, else None. Not used on the live pipe (the original never flushed either) - for files and tests."""
        unit = self.take_access_unit()
        if unit is not None:
            return unit
        if self._unit_start < 0 or not self._unit_has_picture:
            return None
        result = (bytes(self._pending[self._unit_start:]), self._unit_keyframe)
        self._pending.clear()
        self._scan = 0
        self._unit_start = -1
        self._unit_has_picture = False
        self._unit_keyframe = False
        return result
