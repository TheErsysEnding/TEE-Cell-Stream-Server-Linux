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
# Filler data: padding an encoder emits to hold a constant rate. It carries no picture and belongs to no
# access unit. Measured on a still desktop under NVENC's CBR: 98.5% of the stream was filler, and because
# this splitter attached it to the FRONT of the next unit, one frame grew from 2-3 UDP fragments to 20-25 -
# every one of them another chance to lose a packet, which an intra-refresh stream then decodes straight
# through as a displaced block. Dropped here rather than fragmented and sent.
_FILLER_NAL_TYPE = 12

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
# "the next unit's first NAL" is the part that needs care once a picture may hold more than ONE slice.
# H.264 numbers every slice by the macroblock it starts at, and only the first slice of a picture starts
# at 0. That number is first_mb_in_slice, the very first syntax element of the slice header, coded as
# exp-Golomb - and exp-Golomb writes the value 0 as the single bit 1. So "is this slice the start of a new
# picture" is one bit test on the byte after the NAL header, and nothing has to be parsed.
#
# Without that test every slice after the first would look like the beginning of the next picture, and a
# 4-slice picture would leave here as 4 access units with 4 frame ids. The console would then be handed
# quarter-pictures as if they were whole ones. Multiple slices HAVE been tried before and measured worse
# on the console (1.12.1) - with this splitter underneath, which is reason enough to distrust that result.
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
        # what actually left here, for slice experiments: asked-for and emitted are not the same thing
        self._unit_slices = 0                      # picture NALs in the open unit
        self._slices_seen: dict[int, int] = {}     # slices per picture -> how many pictures had that many
        self._pictures = 0
        self._picture_bytes = 0

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
            if position + 4 >= available:
                # the NAL header byte AND the first byte behind it have to be here: first_mb_in_slice lives
                # in that second byte and decides whether a slice opens a picture or continues one
                self._scan = position
                break

            nal_start = position - 1 if (position > 0 and pending[position - 1] == 0) else position
            nal_type = pending[position + 3] & 0x1F
            is_picture = nal_type in _PICTURE_NAL_TYPES
            # first_mb_in_slice != 0 means this slice continues the picture that is already open. With one
            # slice per picture the test never fires; with several it is the only thing holding them together.
            continues_picture = is_picture and self._unit_has_picture and not pending[position + 4] & 0x80

            if self._unit_start >= 0 and self._unit_has_picture and not continues_picture:
                # a complete access unit ends where the next one's first unit begins
                with memoryview(pending) as window:
                    self._completed = (bytes(window[self._unit_start:nal_start]), self._unit_keyframe)
                self._count_picture(self._unit_slices, nal_start - self._unit_start)
                # drop the consumed bytes; the new unit (if any) restarts at the front of the buffer
                del pending[:nal_start]
                position -= nal_start
                nal_start = 0
                self._unit_start = -1
                self._unit_has_picture = False
                self._unit_keyframe = False
                self._unit_slices = 0
            if nal_type == _FILLER_NAL_TYPE:
                # opens no unit, so its bytes fall in front of the next one's start and are dropped with
                # the next trim. it must not extend the unit it follows either - that is what sent it.
                self._scan = position + 3
                continue
            if self._unit_start < 0:
                self._unit_start = nal_start
                self._unit_has_picture = False
                self._unit_keyframe = False
                self._unit_slices = 0
            if is_picture:
                self._unit_has_picture = True
                self._unit_keyframe |= nal_type == 5
                self._unit_slices += 1
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
        self._count_picture(self._unit_slices, len(result[0]))
        self._pending.clear()
        self._scan = 0
        self._unit_start = -1
        self._unit_has_picture = False
        self._unit_keyframe = False
        self._unit_slices = 0
        return result

    def _count_picture(self, slices: int, size: int) -> None:
        """Book one finished picture. Called where a unit is handed out, so it counts what was SENT."""
        if slices <= 0:
            return              # a unit with no picture in it is not a picture
        self._slices_seen[slices] = self._slices_seen.get(slices, 0) + 1
        self._pictures += 1
        self._picture_bytes += size

    def slice_report(self) -> str:
        """What the encoder actually emitted, per picture. The point of the line is that "-x264-params
        slices=4" is a REQUEST: x264 may hand out fewer (a picture too small to divide) and the console
        only ever sees what came out. Bytes per picture is the second half of the trade - more slices cost
        bits, because nothing may be predicted across a slice boundary and each one carries its own header."""
        if not self._pictures:
            return "sender: keine Bilder gesendet"
        order = sorted(self._slices_seen.items(), key=lambda item: -item[1])
        spread = ", ".join("%d Slices (%dx)" % (slices, pictures) for slices, pictures in order)
        average = self._picture_bytes / self._pictures
        return ("sender: %d Bilder gesendet, %s, Ø %.1f KB je Bild = %.1f Fragmente"
                % (self._pictures, spread, average / 1024.0, average / FRAGMENT_PAYLOAD_BYTES))
