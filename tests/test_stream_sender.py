"""Tests for stream_sender: the VF fragment header (checked against stream.c's definitions), the pacing, and the
Annex-B splitter - on hand-built NAL streams split at every byte boundary, and on real nvenc output.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_stream_sender -v
Safe to run on a live desktop: only loopback UDP and an ffmpeg lavfi encode in a temp dir.
"""

import array
import atexit
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

_TMP = tempfile.mkdtemp(prefix="tee-cst-stream-sender-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)   # the 13MB fixture must not pile up in the temp dir
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))

from teecellstream import log, protocol, stream_sender  # noqa: E402
from teecellstream.clock import now_us  # noqa: E402
from teecellstream.stream_sender import (  # noqa: E402
    FRAGMENT_HEADER_BYTES, FRAGMENT_PAYLOAD_BYTES, PROTOCOL_VERSION, AnnexBSplitter, send_access_unit,
)

# the PS3's constants, spelled out here on purpose (stream.c) rather than imported, so a drift shows up
PS3_FRAGMENT_HEADER_BYTES = 20
PS3_FRAGMENT_PAYLOAD_BYTES = 1300
PS3_PROTOCOL_VERSION = 2
PS3_FRAME_MAX_BYTES = 1024 * 1024
PS3_FRAGMENT_MAX_COUNT = PS3_FRAME_MAX_BYTES // PS3_FRAGMENT_PAYLOAD_BYTES + 1
# stream.c holds (fragCount-1)*1300 + payloadBytes against FRAME_MAX_BYTES on every fragment, with THAT fragment's
# payload - so the full 1300-byte fragments of an 807-fragment frame all overshoot and the frame never completes.
# the largest frame that arrives whole is the last multiple of the payload size below the limit
PS3_FRAME_MAX_ACCEPTED_BYTES = PS3_FRAME_MAX_BYTES // PS3_FRAGMENT_PAYLOAD_BYTES * PS3_FRAGMENT_PAYLOAD_BYTES   # 1047800


def parse_fragment(packet: bytes) -> dict:
    """handleFragment() from stream.c, byte for byte - deliberately not the sender's struct."""
    assert len(packet) > PS3_FRAGMENT_HEADER_BYTES, "the PS3 drops any VF packet without payload"
    assert packet[0] == ord("V") and packet[1] == ord("F")
    frame_id = (packet[2] << 24) | (packet[3] << 16) | (packet[4] << 8) | packet[5]
    frag_index = (packet[6] << 8) | packet[7]
    frag_count = (packet[8] << 8) | packet[9]
    keyframe = packet[10] & 1
    payload_bytes = len(packet) - PS3_FRAGMENT_HEADER_BYTES
    capture_us = 0
    for i in range(8):
        capture_us = (capture_us << 8) | packet[12 + i]
    # the two sanity checks the PS3 applies before a fragment may touch its assembly
    assert 0 < frag_count <= PS3_FRAGMENT_MAX_COUNT and frag_index < frag_count, "the PS3 drops this fragment"
    assert (frag_count - 1) * PS3_FRAGMENT_PAYLOAD_BYTES + payload_bytes <= PS3_FRAME_MAX_BYTES, "over the PS3's frame limit"
    return {
        "frame_id": frame_id, "frag_index": frag_index, "frag_count": frag_count, "keyframe": keyframe,
        "flags": packet[10], "version": packet[11], "capture_us": capture_us,
        "payload": packet[PS3_FRAGMENT_HEADER_BYTES:],
    }


def reassemble(packets: list[bytes]) -> bytes:
    """Frame reassembly the way stream.c does it: fragment i at i*1300, size from the last fragment alone."""
    frags = [parse_fragment(p) for p in packets]
    count = frags[0]["frag_count"]
    buffer = bytearray(count * PS3_FRAGMENT_PAYLOAD_BYTES)
    last_bytes = -1
    for frag in frags:
        offset = frag["frag_index"] * PS3_FRAGMENT_PAYLOAD_BYTES
        buffer[offset:offset + len(frag["payload"])] = frag["payload"]
        if frag["frag_index"] == count - 1:
            last_bytes = len(frag["payload"])
    assert last_bytes >= 0
    return bytes(buffer[:(count - 1) * PS3_FRAGMENT_PAYLOAD_BYTES + last_bytes])


class FragmentHeaderTest(unittest.TestCase):
    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)   # a whole paced frame may land before we read
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(2.0)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        self.target = self.rx.getsockname()

    def tearDown(self):
        self.rx.close()
        self.tx.close()

    def _collect(self, expected: int) -> list[bytes]:
        packets = [self.rx.recv(4096) for _ in range(expected)]
        self.rx.settimeout(0.05)
        with self.assertRaises(socket.timeout, msg="more packets than fragments"):
            self.rx.recv(4096)
        return packets

    def test_constants_match_the_ps3(self):
        self.assertEqual(FRAGMENT_HEADER_BYTES, PS3_FRAGMENT_HEADER_BYTES)
        self.assertEqual(FRAGMENT_PAYLOAD_BYTES, PS3_FRAGMENT_PAYLOAD_BYTES)
        self.assertEqual(PROTOCOL_VERSION, PS3_PROTOCOL_VERSION)
        self.assertEqual(protocol.FRAGMENT_HEADER_BYTES, PS3_FRAGMENT_HEADER_BYTES)

    def test_header_byte_layout(self):
        data = random.Random(7).randbytes(3000)   # 1300 + 1300 + 400
        capture_us = 0x0102030405060708
        send_access_unit(self.tx, self.target, 0x0A0B0C0D, data, True, capture_us, 30000)
        packets = self._collect(3)

        for index, packet in enumerate(packets):
            self.assertEqual(packet[0:2], b"VF")
            self.assertEqual(packet[2:6], bytes([0x0A, 0x0B, 0x0C, 0x0D]))      # frameId u32 big-endian
            self.assertEqual(packet[6:8], bytes([0, index]))                     # fragIndex u16
            self.assertEqual(packet[8:10], bytes([0, 3]))                        # fragCount u16
            self.assertEqual(packet[10], 1)                                      # flags: bit0 keyframe
            self.assertEqual(packet[11], 2)                                      # version
            self.assertEqual(packet[12:20], bytes([1, 2, 3, 4, 5, 6, 7, 8]))     # encoder-exit u64 big-endian
            parsed = parse_fragment(packet)
            self.assertEqual(parsed["frame_id"], 0x0A0B0C0D)
            self.assertEqual(parsed["capture_us"], capture_us)
            self.assertEqual(parsed["keyframe"], 1)
        self.assertEqual(len(packets[0]), 20 + 1300)
        self.assertEqual(len(packets[1]), 20 + 1300)
        self.assertEqual(len(packets[2]), 20 + 400)
        self.assertEqual(packets[0][20:], data[0:1300])
        self.assertEqual(packets[1][20:], data[1300:2600])
        self.assertEqual(packets[2][20:], data[2600:])
        self.assertEqual(reassemble(packets), data)

    def test_all_but_last_fragment_carry_exactly_1300_bytes(self):
        data = random.Random(8).randbytes(1300 * 7 + 1)
        send_access_unit(self.tx, self.target, 3, data, False, now_us(), 30000)
        packets = self._collect(8)
        for packet in packets[:-1]:
            self.assertEqual(len(packet) - 20, 1300)
        self.assertEqual(len(packets[-1]) - 20, 1)
        self.assertEqual([parse_fragment(p)["frag_index"] for p in packets], list(range(8)))
        self.assertEqual({parse_fragment(p)["frag_count"] for p in packets}, {8})
        self.assertEqual(reassemble(packets), data)

    def test_frame_that_is_an_exact_multiple_of_1300(self):
        data = random.Random(9).randbytes(2600)
        send_access_unit(self.tx, self.target, 4, data, False, now_us(), 30000)
        packets = self._collect(2)   # no empty third fragment: the PS3 would drop it anyway
        self.assertEqual([len(p) - 20 for p in packets], [1300, 1300])
        self.assertEqual({parse_fragment(p)["frag_count"] for p in packets}, {2})
        self.assertEqual(reassemble(packets), data)

    def test_one_byte_frame(self):
        send_access_unit(self.tx, self.target, 5, b"\x65", False, now_us(), 30000)
        packets = self._collect(1)
        parsed = parse_fragment(packets[0])
        self.assertEqual(len(packets[0]), 21)
        self.assertEqual((parsed["frag_index"], parsed["frag_count"]), (0, 1))
        self.assertEqual(parsed["payload"], b"\x65")
        self.assertEqual(reassemble(packets), b"\x65")

    def test_empty_unit_sends_nothing(self):
        send_access_unit(self.tx, self.target, 6, b"", True, now_us(), 30000)
        self._collect(0)

    def test_keyframe_flag_clear_for_normal_frames(self):
        send_access_unit(self.tx, self.target, 7, b"abc", False, now_us(), 30000)
        parsed = parse_fragment(self._collect(1)[0])
        self.assertEqual(parsed["flags"], 0)
        self.assertEqual(parsed["keyframe"], 0)
        self.assertEqual(parsed["version"], PS3_PROTOCOL_VERSION)

    def test_frame_id_is_masked_to_u32(self):
        send_access_unit(self.tx, self.target, (1 << 32) + 9, b"x", False, now_us(), 30000)
        self.assertEqual(parse_fragment(self._collect(1)[0])["frame_id"], 9)
        send_access_unit(self.tx, self.target, 0xFFFFFFFF, b"x", False, now_us(), 30000)
        packet = self._collect(1)[0]
        self.assertEqual(packet[2:6], b"\xff\xff\xff\xff")

    def test_capture_us_round_trips_as_u64(self):
        stamp = now_us()
        self.assertGreater(stamp, 6 * 365 * 24 * 3600 * 1_000_000)   # the clock counts from 2020, not from process start
        send_access_unit(self.tx, self.target, 8, b"x", False, stamp, 30000)
        self.assertEqual(parse_fragment(self._collect(1)[0])["capture_us"], stamp)

    def test_accepts_memoryview_and_bytearray(self):
        data = bytearray(random.Random(10).randbytes(1500))
        send_access_unit(self.tx, self.target, 9, memoryview(data), False, now_us(), 30000)
        self.assertEqual(reassemble(self._collect(2)), bytes(data))
        send_access_unit(self.tx, self.target, 10, data, False, now_us(), 30000)
        self.assertEqual(reassemble(self._collect(2)), bytes(data))

    def test_counts_bytes_not_items_for_any_buffer(self):
        # a buffer whose items are wider than a byte must still be fragmented by its BYTE length
        items = array.array("I", [0x01020304, 0x05060708, 0x090A0B0C])
        send_access_unit(self.tx, self.target, 11, memoryview(items), False, now_us(), 30000)
        packets = self._collect(1)
        self.assertEqual(len(packets[0]), 20 + 12)
        self.assertEqual(reassemble(packets), items.tobytes())

    def test_unit_above_the_ps3_frame_limit_is_dropped_and_said_once(self):
        # stream.c refuses any frame past FRAME_MAX_BYTES (1MiB) before it touches the assembly, so sending one only
        # stalls the pump for the whole paced send. it stays off the wire and the log says so once
        stream_sender._oversize_reported = False
        sent = []

        class FakeSocket:
            def sendto(self, packet, target):
                sent.append(bytes(packet))

        too_big = bytes(PS3_FRAME_MAX_ACCEPTED_BYTES + 1)
        send_access_unit(FakeSocket(), self.target, 14, too_big, True, now_us(), 10_000_000_000)
        send_access_unit(FakeSocket(), self.target, 15, too_big, True, now_us(), 10_000_000_000)
        self.assertEqual(sent, [])
        self.assertEqual(log.get_recent().count("überschreitet das PS3-Limit"), 1)
        self.assertIn("live: Frame 14 mit %d Bytes" % len(too_big), log.get_recent())
        # ... while the largest frame the PS3 accepts still goes out whole, and passes its checks fragment by fragment
        largest = memoryview(bytes(PS3_FRAME_MAX_ACCEPTED_BYTES))
        send_access_unit(FakeSocket(), self.target, 16, largest, False, now_us(), 10_000_000_000)
        self.assertEqual(len(sent), PS3_FRAGMENT_MAX_COUNT - 1)                   # 806 full fragments
        last = parse_fragment(sent[-1])
        self.assertEqual((last["frag_index"], last["frag_count"]), (PS3_FRAGMENT_MAX_COUNT - 2, PS3_FRAGMENT_MAX_COUNT - 1))
        self.assertEqual(reassemble(sent), bytes(largest))

    def test_the_ps3_refuses_the_full_fragments_of_a_frame_just_below_1mib(self):
        # why the sender's limit is 806 fragments and not "1MiB": stream.c's per-fragment size check counts a full
        # payload for every fragment but the last, so the non-last fragments of an 807-fragment frame all fail it
        sent = []

        class FakeSocket:
            def sendto(self, packet, target):
                sent.append(bytes(packet))

        stream_sender._oversize_reported = True   # not the point here
        with unittest.mock.patch.object(stream_sender, "MAX_UNIT_BYTES", PS3_FRAME_MAX_BYTES):
            send_access_unit(FakeSocket(), self.target, 19, bytes(PS3_FRAME_MAX_BYTES), False, now_us(), 10_000_000_000)
        self.assertEqual(len(sent), PS3_FRAGMENT_MAX_COUNT)                       # 807 on the wire ...
        with self.assertRaises(AssertionError):
            parse_fragment(sent[0])                                              # ... but the PS3 drops every full one
        parse_fragment(sent[-1])                                                 # and keeps only the short last one

    def test_no_fragment_leaves_before_it_is_due(self):
        # the point of pacing: at no moment may packets leave faster than the send rate, or a keyframe goes out as
        # the burst the original measured at ~400Mbit/s. each fragment's send moment is held against its own due
        # time (start + i * per_fragment_us, and start is at or after `before`), not just the frame's total
        stamps = []

        class StampingSocket:
            def sendto(self, packet, target):
                stamps.append(now_us())

        per_fragment_us = (20 + 1300) * 8 * 1000 // protocol.SEND_RATE_KBPS
        before = now_us()
        send_access_unit(StampingSocket(), self.target, 17, bytes(1300 * 40), False, before, protocol.SEND_RATE_KBPS)
        self.assertEqual(len(stamps), 40)
        for index, stamp in enumerate(stamps):
            self.assertGreaterEqual(stamp - before, index * per_fragment_us, "fragment %d left early" % index)

    def test_pacing_holds_beside_a_pacing_sibling_thread(self):
        # the audio streamer paces its own packets on the same clock (sleep, then a 150us spin that holds the GIL)
        # every 5ms. a fragment waking into that spin waits for the GIL; that must cost the frame a few ms at most,
        # not the hundreds measured with the default 5ms switch interval and a busy Python thread
        from teecellstream.clock import sleep_until_us
        stop = threading.Event()
        sink = ("127.0.0.1", 9)

        def audio_like():
            due = now_us()
            while not stop.is_set():
                due += 5000
                sleep_until_us(due, 150)
                self.tx.sendto(b"AF" + bytes(14 + 240 * 4), sink)

        sibling = threading.Thread(target=audio_like, name="test-audio-like", daemon=True)
        sibling.start()
        try:
            data = random.Random(15).randbytes(100 * 1024)
            worst = 0.0
            for _ in range(5):
                started = time.perf_counter()
                send_access_unit(self.tx, self.target, 18, data, True, now_us(), protocol.SEND_RATE_KBPS)
                worst = max(worst, time.perf_counter() - started)
                self.assertEqual(reassemble(self._collect((len(data) + 1299) // 1300)), data)
                self.rx.settimeout(2.0)
        finally:
            stop.set()
            sibling.join(1.0)
        self.assertLess(worst, 0.060, "a pacing sibling thread stretched a 27ms frame to %.1fms" % (worst * 1000))

    def test_pacing_spreads_a_big_frame_over_the_send_rate(self):
        data = random.Random(11).randbytes(100 * 1024)
        frag_count = (len(data) + 1299) // 1300                                  # 79
        per_fragment_us = (20 + 1300) * 8 * 1000 // protocol.SEND_RATE_KBPS       # 352us at 30000 kbps
        self.assertEqual(per_fragment_us, 352)
        floor_s = (frag_count - 1) * per_fragment_us / 1_000_000                  # 27.456ms: last fragment's due time

        started = time.perf_counter()
        send_access_unit(self.tx, self.target, 12, data, True, now_us(), protocol.SEND_RATE_KBPS)
        elapsed = time.perf_counter() - started
        packets = self._collect(frag_count)
        self.assertEqual(reassemble(packets), data)
        self.assertGreaterEqual(elapsed, 0.025, "sent faster than the send rate allows: %.1fms" % (elapsed * 1000))
        self.assertGreaterEqual(elapsed, floor_s * 0.98, "sent faster than the send rate allows: %.1fms" % (elapsed * 1000))
        self.assertLess(elapsed, 0.060, "pacing overshoots badly: %.1fms for a %.1fms frame" % (elapsed * 1000, floor_s * 1000))

        # the same frame at an absurd rate goes out as fast as loopback takes it: the delay above IS the pacing
        started = time.perf_counter()
        send_access_unit(self.tx, self.target, 13, data, True, now_us(), 10_000_000_000)
        unpaced = time.perf_counter() - started
        self.assertEqual(reassemble(self._collect(frag_count)), data)
        self.assertLess(unpaced, 0.020, "unpaced send took %.1fms" % (unpaced * 1000))
        print("\n  pacing: 100KB @ %d kbps in %.2fms (ideal %.2fms), unpaced %.2fms"
              % (protocol.SEND_RATE_KBPS, elapsed * 1000, floor_s * 1000, unpaced * 1000))


# ---------------------------------------------------------------- splitter on hand-built streams

def nal(nal_type: int, payload: bytes, ref_idc: int = 3, four_byte: bool = True) -> bytes:
    start = b"\x00\x00\x00\x01" if four_byte else b"\x00\x00\x01"
    return start + bytes([(ref_idc << 5) | nal_type]) + payload


def body(seed: int, length: int) -> bytes:
    """NAL payload bytes without any zero, so no accidental start code (real streams use emulation prevention)."""
    rng = random.Random(seed)
    return bytes(rng.randrange(1, 256) for _ in range(length))


def scan_nal_types(data: bytes) -> list[int]:
    """Independent NAL type listing for checking access units (start codes of either length)."""
    types = []
    pos = 0
    while True:
        i = data.find(b"\x00\x00\x01", pos)
        if i < 0 or i + 3 >= len(data):
            return types
        types.append(data[i + 3] & 0x1F)
        pos = i + 3


# SPS PPS SEI IDR | SEI P | P | SEI P  (mixed 3- and 4-byte start codes, a picture with a trailing zero byte)
SYNTHETIC_UNITS = [
    (nal(7, body(1, 12)) + nal(8, body(2, 5)) + nal(6, body(3, 9), ref_idc=0, four_byte=False) + nal(5, body(4, 40)), True),
    (nal(6, body(5, 7), ref_idc=0, four_byte=False) + nal(1, body(6, 33), ref_idc=2), False),
    (nal(1, body(7, 21), ref_idc=2, four_byte=False), False),
    (nal(6, body(8, 3), ref_idc=0) + nal(1, body(9, 17) + b"\x00", ref_idc=2), False),
]
SYNTHETIC_STREAM = b"".join(unit for unit, _keyframe in SYNTHETIC_UNITS)


def split_with(splitter: AnnexBSplitter, chunks) -> list[tuple[bytes, bool]]:
    units = []
    for chunk in chunks:
        splitter.push(chunk)
        while (unit := splitter.take_access_unit()) is not None:
            units.append(unit)
    tail = splitter.flush()
    if tail is not None:
        units.append(tail)
    return units


class AnnexBSplitterSyntheticTest(unittest.TestCase):
    def test_whole_stream_in_one_push(self):
        splitter = AnnexBSplitter()
        splitter.push(SYNTHETIC_STREAM)
        taken = []
        while (unit := splitter.take_access_unit()) is not None:
            taken.append(unit)
        self.assertEqual(taken, SYNTHETIC_UNITS[:3], "the last unit stays open until the next NAL begins")
        self.assertEqual(splitter.flush(), SYNTHETIC_UNITS[3])
        self.assertIsNone(splitter.flush())
        self.assertIsNone(splitter.take_access_unit())

    def test_keyframe_only_when_an_idr_slice_is_inside(self):
        units = split_with(AnnexBSplitter(), [SYNTHETIC_STREAM])
        self.assertEqual([keyframe for _data, keyframe in units], [True, False, False, False])
        self.assertEqual(scan_nal_types(units[0][0]), [7, 8, 6, 5])
        self.assertEqual(scan_nal_types(units[1][0]), [6, 1])
        self.assertEqual(scan_nal_types(units[2][0]), [1])
        self.assertEqual(scan_nal_types(units[3][0]), [6, 1])

    def test_every_split_point_gives_the_same_units(self):
        for cut in range(len(SYNTHETIC_STREAM) + 1):
            units = split_with(AnnexBSplitter(), [SYNTHETIC_STREAM[:cut], SYNTHETIC_STREAM[cut:]])
            self.assertEqual(units, SYNTHETIC_UNITS, "cut at byte %d" % cut)

    def test_byte_by_byte(self):
        units = split_with(AnnexBSplitter(), (SYNTHETIC_STREAM[i:i + 1] for i in range(len(SYNTHETIC_STREAM))))
        self.assertEqual(units, SYNTHETIC_UNITS)

    def test_random_chunks_repeated_stream(self):
        stream = SYNTHETIC_STREAM * 50
        expected = SYNTHETIC_UNITS * 50
        rng = random.Random(3)
        chunks = []
        pos = 0
        while pos < len(stream):
            size = rng.randint(1, 97)
            chunks.append(stream[pos:pos + size])
            pos += size
        units = split_with(AnnexBSplitter(), chunks)
        self.assertEqual(units, expected)
        self.assertEqual(b"".join(data for data, _keyframe in units), stream)

    def test_parameter_sets_without_a_picture_never_come_out(self):
        splitter = AnnexBSplitter()
        splitter.push(nal(7, body(1, 12)) + nal(8, body(2, 5)))
        self.assertIsNone(splitter.take_access_unit())
        self.assertIsNone(splitter.flush())
        # ... they attach to the picture that follows
        splitter.push(nal(5, body(4, 40)) + nal(1, body(6, 3)))
        data, keyframe = splitter.take_access_unit()
        self.assertTrue(keyframe)
        self.assertEqual(scan_nal_types(data), [7, 8, 5])

    def test_bytes_before_the_first_start_code_are_dropped(self):
        units = split_with(AnnexBSplitter(), [b"\x11\x22\x33" + SYNTHETIC_STREAM])
        self.assertEqual(units, SYNTHETIC_UNITS)

    def test_pending_buffer_does_not_grow_with_consumed_units(self):
        splitter = AnnexBSplitter()
        for _ in range(200):
            splitter.push(SYNTHETIC_STREAM)
            while splitter.take_access_unit() is not None:
                pass
        self.assertLess(len(splitter._pending), 2 * len(SYNTHETIC_STREAM))

    def test_push_accepts_memoryview(self):
        units = split_with(AnnexBSplitter(), [memoryview(SYNTHETIC_STREAM)])
        self.assertEqual(units, SYNTHETIC_UNITS)

    def test_near_miss_byte_patterns_stay_inside_the_nal(self):
        # emulation prevention leaves 00 00 03 xx, 00 00 02 and lone 00 01 in real payloads; trailing zeros after
        # a NAL belong to the byte stream and only ONE of them is taken as the 4-byte start code's leading zero
        # (so a trailing zero before a 3-byte code would migrate into the next unit, as in the original)
        tricky = b"\x00\x00\x03\x01" + b"\x00\x01" + b"\x00\x00\x02" + b"\x00\x00\x03\x00\x00\x03" + b"\x01\x00"
        units = [
            (nal(7, tricky) + nal(8, b"\x00\x00\x03\x01", four_byte=False) + nal(5, tricky + body(1, 30) + tricky), True),
            (nal(6, b"\x00\x01\x00\x01", ref_idc=0) + nal(1, tricky, ref_idc=2, four_byte=False) + b"\x00", False),
            (nal(1, b"\x00\x00\x03" + body(2, 9), ref_idc=2), False),
            # an SEI after a picture already belongs to the NEXT unit
            (nal(6, b"\x00\x00\x03\x02", ref_idc=0, four_byte=False) + nal(1, body(3, 5), ref_idc=2, four_byte=False), False),
        ]
        stream = b"".join(unit for unit, _keyframe in units)
        for cut in range(len(stream) + 1):
            self.assertEqual(split_with(AnnexBSplitter(), [stream[:cut], stream[cut:]]), units, "cut at byte %d" % cut)
        self.assertEqual(split_with(AnnexBSplitter(), (stream[i:i + 1] for i in range(len(stream)))), units)

    def test_matches_a_literal_port_of_the_original_on_random_streams(self):
        # LiveAnnexBSplitter.TakeAccessUnit byte for byte, as the oracle: on zero-heavy random streams (plenty of
        # real and near-miss start codes, garbage NAL types, split codes) both must hand out the same units at the
        # same push, and keep the same open tail
        class Original:
            def __init__(self):
                self.pending = bytearray()
                self.scan = 0
                self.unit_start = -1
                self.has_picture = False
                self.keyframe = False
                self.completed = None

            def push(self, data):
                self.pending += data

            def take(self):
                p = self.pending
                while self.completed is None and self.scan + 3 < len(p):
                    if not (p[self.scan] == 0 and p[self.scan + 1] == 0 and p[self.scan + 2] == 1):
                        self.scan += 1
                        continue
                    nal_start = self.scan - 1 if (self.scan > 0 and p[self.scan - 1] == 0) else self.scan
                    nal_type = p[self.scan + 3] & 0x1F
                    if self.unit_start >= 0 and self.has_picture:
                        self.completed = (bytes(p[self.unit_start:nal_start]), self.keyframe)
                        del p[:nal_start]
                        self.scan -= nal_start
                        self.unit_start, self.has_picture, self.keyframe = 0, False, False
                    if self.unit_start < 0:
                        self.unit_start, self.has_picture, self.keyframe = nal_start, False, False
                    if nal_type in (1, 5):
                        self.has_picture = True
                        self.keyframe |= nal_type == 5
                    self.scan += 3
                result, self.completed = self.completed, None
                return result

        alphabet = [0, 0, 0, 0, 1, 1, 2, 3, 0x65, 0x41, 0x67, 0x68, 0x06, 0x09, 0x25, 0xE0, 0xFF, 0x1F, 0x05]
        for seed in range(1500):
            rng = random.Random(seed)
            stream = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
            ours, original = AnnexBSplitter(), Original()
            pos = 0
            while pos < len(stream):
                chunk = stream[pos:pos + rng.randint(1, 9)]
                pos += len(chunk)
                ours.push(chunk)
                original.push(chunk)
                while True:
                    unit, expected = ours.take_access_unit(), original.take()
                    self.assertEqual(unit, expected, "seed %d after byte %d" % (seed, pos))
                    if unit is None:
                        break
            tail = bytes(ours._pending[ours._unit_start:]) if ours._unit_start >= 0 else None
            expected_tail = bytes(original.pending[original.unit_start:]) if original.unit_start >= 0 else None
            self.assertEqual(tail, expected_tail, "seed %d open unit" % seed)
            self.assertEqual((ours._unit_has_picture, ours._unit_keyframe), (original.has_picture, original.keyframe), "seed %d" % seed)

    def test_pending_allocation_stays_bounded(self):
        # del buf[:n] must really give the memory back (CPython compacts the bytearray), not just hide it
        unit = nal(6, body(1, 30), ref_idc=0) + nal(1, body(2, 20000), ref_idc=2)
        splitter = AnnexBSplitter()
        peak = 0
        for _ in range(500):
            splitter.push(unit[:7000])
            splitter.push(unit[7000:])
            while splitter.take_access_unit() is not None:
                pass
            peak = max(peak, sys.getsizeof(splitter._pending))
        self.assertLess(peak, 4 * len(unit))


# ---------------------------------------------------------------- splitter on real encoder output

FIXTURE_SECONDS = 10
FIXTURE_FRAMES = FIXTURE_SECONDS * protocol.FPS


def nvenc_fixture_args(ffmpeg: str, path: str) -> list[str]:
    """The exact nvenc intra-refresh arguments from SPEC.md, fed from a synthetic 720p60 source."""
    kbps, fps = protocol.KBPS, protocol.FPS
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d:duration=%d" % (protocol.WIDTH, protocol.HEIGHT, fps, FIXTURE_SECONDS),
        "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-rc", "vbr", "-pix_fmt", "yuv420p",
        "-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * protocol.REFRESH_MAX_RATE_PERCENT // 100),
        "-bufsize", "%dk" % (kbps * protocol.REFRESH_BUFFER_MS // 1000), "-bf", "0", "-refs", "1",
        "-g", str(protocol.ANCHOR_KEYFRAME_SECONDS * fps), "-intra-refresh", "1", "-single-slice-intra-refresh", "1",
        "-color_range", "tv", "-colorspace", "bt709", "-forced-idr", "1", "-f", "h264", "-flush_packets", "1", path,
    ]


class AnnexBSplitterNvencTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise unittest.SkipTest("ffmpeg fehlt")
        cls.path = os.path.join(_TMP, "fixture-720p60-%ds.h264" % FIXTURE_SECONDS)
        try:
            result = subprocess.run(nvenc_fixture_args(ffmpeg, cls.path), capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise unittest.SkipTest("ffmpeg h264_nvenc: %s" % error)
        if result.returncode != 0 or not os.path.exists(cls.path) or os.path.getsize(cls.path) == 0:
            raise unittest.SkipTest("h264_nvenc nicht nutzbar: " + result.stderr.strip()[-300:])
        with open(cls.path, "rb") as handle:
            cls.data = handle.read()

    def test_splits_nvenc_stream_into_600_access_units_fast(self):
        data = self.data
        rng = random.Random(1234)
        splitter = AnnexBSplitter()
        units = []
        pos = 0
        started = time.perf_counter()
        while pos < len(data):
            size = rng.randint(1, 70000)
            splitter.push(data[pos:pos + size])
            pos += size
            while (unit := splitter.take_access_unit()) is not None:
                units.append(unit)
        tail = splitter.flush()
        elapsed = time.perf_counter() - started
        if tail is not None:
            units.append(tail)

        self.assertEqual(len(units), FIXTURE_FRAMES)
        first_data, first_keyframe = units[0]
        self.assertTrue(first_keyframe)
        first_types = scan_nal_types(first_data)
        self.assertEqual(first_types[0], 7, "the PS3 configures its decoder from the first unit's SPS")
        self.assertIn(8, first_types)
        self.assertIn(5, first_types)
        for index, (unit_data, keyframe) in enumerate(units):
            types = scan_nal_types(unit_data)
            pictures = [t for t in types if t in (1, 5)]
            self.assertEqual(len(pictures), 1, "unit %d holds %d pictures: %s" % (index, len(pictures), types))
            if index > 0:
                self.assertFalse(keyframe, "unit %d flagged keyframe" % index)
                self.assertNotIn(5, types, "unit %d carries an IDR: %s" % (index, types))
                self.assertNotIn(7, types, "unit %d carries an SPS: %s" % (index, types))
        self.assertEqual(b"".join(unit_data for unit_data, _keyframe in units), data)

        throughput = len(data) / elapsed / 1_000_000
        print("\n  splitter: %.1f MB nvenc stream, %d units, %d random chunks -> %.0f MB/s"
              % (len(data) / 1e6, len(units), len(data) // 35000, throughput))
        self.assertGreater(throughput, 50, "splitter too slow: %.1f MB/s" % throughput)

    def test_64k_pump_chunks_match_random_chunks(self):
        # the live pump reads its pipe in 64KiB blocks; the result must not depend on the chunking
        data = self.data
        chunked = split_with(AnnexBSplitter(), (data[i:i + 65536] for i in range(0, len(data), 65536)))
        whole = split_with(AnnexBSplitter(), [data])
        self.assertEqual(len(chunked), FIXTURE_FRAMES)
        self.assertEqual(chunked, whole)


if __name__ == "__main__":
    unittest.main()
