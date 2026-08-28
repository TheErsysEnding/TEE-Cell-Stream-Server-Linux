"""Unit tests for tests/fake_ps3.py - the PS3 stand-in that drives the integration test.

The live run (tests/run_integration.sh) proves the whole chain against the real server, but only on the
packets the server happens to produce. These tests feed the stand-in hand-built packets so the corner cases
of handleFragment() (out of order, duplicate, abandoned frame, rejects) and the audio checks are pinned down
deterministically, without any network, and finally run the in-file MockServer self-test on spare ports.

    cd <project> && PYTHONPATH=src python3 -m unittest tests.test_fake_ps3 -v

Nothing here touches the desktop: no portal, no display switch, no input device. The self-test at the end
needs ffmpeg/ffprobe and ~15s; it is skipped without them.
"""

import argparse
import io
import os
import random
import shutil
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_ps3  # noqa: E402

VF = fake_ps3.VF_HEADER
AF = fake_ps3.AF_HEADER
CP = fake_ps3.CP_PACKET
PAYLOAD = fake_ps3.FRAGMENT_PAYLOAD_BYTES

# a synthetic Annex-B access unit: SPS, PPS, IDR - only the NAL types matter to the stand-in
SPS = b"\x00\x00\x00\x01\x67" + b"\x11" * 20
PPS = b"\x00\x00\x00\x01\x68" + b"\x22" * 4
IDR = b"\x00\x00\x00\x01\x65" + b"\x33" * 3000
P_SLICE = b"\x00\x00\x00\x01\x41" + b"\x44" * 700
SEI = b"\x00\x00\x00\x01\x06" + b"\x55" * 10


def fragments_of(access_unit: bytes, frame_id: int, keyframe: bool, capture_us: int | None = None,
                 version: int = fake_ps3.PROTOCOL_VERSION) -> list[bytes]:
    """The server's fragmentation (StreamSender.SendAccessUnit): 1300-byte payloads, the last one shorter."""
    if capture_us is None:
        capture_us = fake_ps3.local_us()
    count = (len(access_unit) + PAYLOAD - 1) // PAYLOAD
    return [VF.pack(b"VF", frame_id, index, count, 1 if keyframe else 0, version, capture_us) + access_unit[index * PAYLOAD:(index + 1) * PAYLOAD]
            for index in range(count)]


def make_fake(tmp: str, padmode: str = "gamepad", **kw) -> fake_ps3.FakePs3:
    fake = fake_ps3.FakePs3(8.0, padmode, tmp, None, 1.0, quiet=True, **kw)
    fake._h264_file = io.BytesIO()
    fake.clock_offset_us = 0       # the packets below are stamped with local_us(), so no conversion
    fake.play_sent_us = fake_ps3.local_us()
    return fake


class ParsingTests(unittest.TestCase):
    def test_parse_big_number_after(self):
        self.assertEqual(fake_ps3.parse_big_number_after(b"TIME 209910199067787", b"TIME "), 209910199067787)
        self.assertEqual(fake_ps3.parse_big_number_after(b"TIME 42abc", b"TIME "), 42)
        self.assertEqual(fake_ps3.parse_big_number_after(b"TIME x", b"TIME "), -1)
        self.assertEqual(fake_ps3.parse_big_number_after(b"TIME", b"TIME "), -1)
        self.assertEqual(fake_ps3.parse_big_number_after(b"CELLSTREAM 1", b"TIME "), -1)

    def test_parse_sinfo(self):
        self.assertEqual(fake_ps3.parse_sinfo(b"SINFO 1280 720 42 1 60 1"), (1280, 720, 42, 1, 60, 1))
        self.assertEqual(fake_ps3.parse_sinfo(b"SINFO 1280 720 42 1 60"), (1280, 720, 42, 1, 60, 0))   # flag optional = keyframes
        self.assertEqual(fake_ps3.parse_sinfo(b"SINFO  1280\t720 42 1 60 0"), (1280, 720, 42, 1, 60, 0))   # strtol skips whitespace
        self.assertIsNone(fake_ps3.parse_sinfo(b"SINFO 0 720 42 1 60 1"))       # every one of the five must be > 0
        self.assertIsNone(fake_ps3.parse_sinfo(b"SINFO 1280 720 42 1"))         # fps missing -> strtol gives 0
        self.assertIsNone(fake_ps3.parse_sinfo(b"SINFX 1280 720 42 1 60 1"))
        self.assertIsNone(fake_ps3.parse_sinfo(b"CELLSTREAM 1"))

    def test_nal_types(self):
        self.assertEqual(fake_ps3.nal_types(SPS + PPS + IDR), [7, 8, 5])
        self.assertEqual(fake_ps3.nal_types(SEI + P_SLICE), [6, 1])
        self.assertEqual(fake_ps3.nal_types(b"\x00\x00\x00\x01"), [])   # a start code with nothing after it
        self.assertEqual(fake_ps3.nal_types(b""), [])

    def test_split_access_units(self):
        data = SPS + PPS + IDR + P_SLICE + SEI + P_SLICE
        units = fake_ps3.split_access_units(data)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0], (SPS + PPS + IDR, True))       # SPS/PPS ride with the picture that follows
        self.assertEqual(units[1], (P_SLICE, False))
        self.assertEqual(units[2], (SEI + P_SLICE, False))         # so does an SEI
        self.assertEqual(b"".join(unit for unit, _ in units), data)   # nothing lost, nothing duplicated
        self.assertEqual(fake_ps3.split_access_units(SPS + PPS), [])   # no picture yet -> no unit

    def test_describe_buttons(self):
        self.assertEqual(fake_ps3.describe_buttons(fake_ps3.BIT_CROSS | fake_ps3.BIT_L1), "cross+L1")
        self.assertEqual(fake_ps3.describe_buttons(0), "")


class RateWindowTests(unittest.TestCase):
    def test_complete_windows_only(self):
        window = fake_ps3.RateWindow()
        start = 1_000_000_000
        for i in range(210):                       # 60/s for 3.5s
            window.add(start + i * 1_000_000 // 60)
        self.assertEqual(window.windows, [60, 60, 60])   # the half-second tail is not a window
        self.assertAlmostEqual(window.average, 60.0)

    def test_empty_gap_windows_count_as_zero(self):
        window = fake_ps3.RateWindow()
        window.add(0)
        window.add(2_500_000)                      # nothing for two and a half seconds
        self.assertEqual(window.windows, [1, 0])

    def test_no_windows(self):
        self.assertEqual(fake_ps3.RateWindow().average, 0.0)

    def test_amount_counts_bytes(self):
        window = fake_ps3.RateWindow()
        for i in range(120):                       # 60 packets of 1000 bytes a second, for two seconds
            window.add(i * 1_000_000 // 60, 1000)
        window.add(2_000_000, 1000)
        self.assertEqual(window.windows, [60000, 60000])


class PadScriptTests(unittest.TestCase):
    def test_gamepad_script(self):
        for t in (1.0, 1.5, 1.99):
            self.assertTrue(fake_ps3.pad_state_at(t, "gamepad")[0] & fake_ps3.BIT_CROSS, t)
        for t in (0.5, 2.0, 2.5):
            self.assertFalse(fake_ps3.pad_state_at(t, "gamepad")[0] & fake_ps3.BIT_CROSS, t)
        for t in (3.0, 3.5, 3.99):
            self.assertTrue(fake_ps3.pad_state_at(t, "gamepad")[0] & fake_ps3.BIT_L1, t)
        self.assertFalse(fake_ps3.pad_state_at(4.0, "gamepad")[0] & fake_ps3.BIT_L1)
        # the sticks sweep the whole int8 range and never leave it
        extremes = [0, 0, 0, 0]
        for frame in range(8 * 60):
            _buttons, *sticks = fake_ps3.pad_state_at(frame / 60, "gamepad")
            for i, value in enumerate(sticks):
                self.assertTrue(-128 <= value <= 127)
                extremes[i] = max(extremes[i], abs(value))
        self.assertGreaterEqual(extremes[0], 120)
        self.assertGreaterEqual(extremes[1], 120)
        self.assertGreaterEqual(extremes[2], 90)

    def test_mouse_script_keeps_hands_off(self):
        """mouse mode lands on the real desktop: no click, and the sticks stay inside DesktopInput's dead zone (16)"""
        for frame in range(8 * 60):
            buttons, *sticks = fake_ps3.pad_state_at(frame / 60, "mouse")
            self.assertFalse(buttons & fake_ps3.BIT_CROSS)
            self.assertLessEqual(max(abs(v) for v in sticks), 15)
        self.assertTrue(fake_ps3.pad_state_at(3.5, "mouse")[0] & fake_ps3.BIT_L1)   # L1 still proves the channel


class PacketLayoutTests(unittest.TestCase):
    def test_cp_packet_matches_stream_c(self):
        packet = CP.pack(b"CP", 0x01020304, 0x0110, -128, 127, -1, 5, 0x1122334455667788)
        self.assertEqual(len(packet), fake_ps3.PAD_PACKET_BYTES)
        self.assertEqual(packet[0:2], b"CP")
        self.assertEqual(packet[2:6], bytes([1, 2, 3, 4]))                 # packetId big-endian
        self.assertEqual(packet[6:8], bytes([0x01, 0x10]))                 # buttons: L1 (bit 8) + cross (bit 4)
        self.assertEqual(packet[8:12], bytes([0x80, 0x7F, 0xFF, 0x05]))    # sticks as (uint8)(int8)
        self.assertEqual(packet[12:20], bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]))

    def test_vf_header_matches_stream_c(self):
        header = VF.pack(b"VF", 0xAABBCCDD, 3, 7, 1, 2, 0x0102030405060708)
        self.assertEqual(len(header), fake_ps3.FRAGMENT_HEADER_BYTES)
        self.assertEqual(header[2:6], bytes([0xAA, 0xBB, 0xCC, 0xDD]))
        self.assertEqual(header[6:8], bytes([0, 3]))
        self.assertEqual(header[8:10], bytes([0, 7]))
        self.assertEqual(header[10], 1)
        self.assertEqual(header[11], 2)
        self.assertEqual(header[12:20], bytes(range(1, 9)))

    def test_af_header_matches_stream_c(self):
        header = AF.pack(b"AF", 9, 240, 0x0102030405060708)
        self.assertEqual(len(header), fake_ps3.AUDIO_HEADER_BYTES)
        self.assertEqual(header[6:8], bytes([0, 240]))
        self.assertEqual(header[8:16], bytes(range(1, 9)))


class FragmentReassemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fake_ps3_test_")
        self.fake = make_fake(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def received(self) -> bytes:
        return self.fake._h264_file.getvalue()

    def test_in_order_keyframe(self):
        au = SPS + PPS + IDR
        for fragment in fragments_of(au, 0, True):
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.frames_complete, 1)
        self.assertEqual(self.fake.frames_incomplete, 0)
        self.assertEqual(self.received(), au)
        self.assertEqual(self.fake.first_au_types, [7, 8, 5])
        self.assertTrue(self.fake.first_au_keyframe_flag)
        self.assertEqual(self.fake.keyframes, 1)
        self.assertEqual(self.fake.fragments, 3)
        self.assertEqual(self.fake.payload_size_violations, 0)
        self.assertEqual(self.fake.version_mismatches, 0)
        self.assertEqual(self.fake.timestamp_implausible, 0)
        self.assertEqual(self.fake.negative_latency_frames, 0)
        self.assertEqual(len(self.fake.network_latency_us), 1)

    def test_out_of_order_and_duplicate(self):
        au = SPS + PPS + IDR
        frags = fragments_of(au, 5, True)
        for fragment in (frags[2], frags[0], frags[2], frags[1]):   # last first, one repeated
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.frames_complete, 1)
        self.assertEqual(self.fake.fragments_duplicate, 1)
        self.assertEqual(self.received(), au)                       # bytes land at their index, not in arrival order

    def test_abandoned_frame_counts_as_incomplete(self):
        frags = fragments_of(SPS + PPS + IDR, 1, True)
        self.fake._handle_fragment(frags[0])                         # frame 1 starts...
        self.fake._handle_fragment(frags[1])
        for fragment in fragments_of(P_SLICE, 2, False):            # ...frame 2 arrives before it finished
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.frames_incomplete, 1)
        self.assertEqual(self.fake.frames_complete, 1)
        self.assertEqual(self.received(), P_SLICE)                   # the torso of frame 1 is never written
        self.assertEqual(self.fake.frame_id_gaps, 0)
        late = frags[2]                                              # frame 1's tail after the fact: a new (partial) frame 1 again
        self.fake._handle_fragment(late)
        self.assertEqual(self.fake.frames_complete, 1)

    def test_frame_id_gap(self):
        for frame_id in (0, 1, 4):
            for fragment in fragments_of(P_SLICE, frame_id, False):
                self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.frames_complete, 3)
        self.assertEqual(self.fake.frame_id_gaps, 2)                 # 2 and 3 never showed up

    def test_rejects_like_the_ps3(self):
        capture = fake_ps3.local_us()
        bad_index = VF.pack(b"VF", 0, 3, 3, 0, 2, capture) + b"x" * 10           # fragIndex >= fragCount
        zero_count = VF.pack(b"VF", 0, 0, 0, 0, 2, capture) + b"x" * 10
        too_many = VF.pack(b"VF", 0, 0, fake_ps3.FRAGMENT_MAX_COUNT + 1, 0, 2, capture) + b"x" * 10
        oversize = VF.pack(b"VF", 0, 0, fake_ps3.FRAGMENT_MAX_COUNT, 0, 2, capture) + b"x" * PAYLOAD   # > FRAME_MAX_BYTES
        header_only = VF.pack(b"VF", 0, 0, 1, 0, 2, capture)
        for packet in (bad_index, zero_count, too_many, oversize, header_only):
            self.fake._handle_fragment(packet)
        self.assertEqual(self.fake.fragments_rejected, 4)            # the header-only packet is dropped before any check
        self.assertEqual(self.fake.fragments, 0)
        self.assertEqual(self.fake.frames_complete, 0)
        self.assertEqual(self.fake.assembly_frame_id, -1)            # nothing started a frame

    def test_version_and_size_violations_are_counted_not_dropped(self):
        au = SPS + PPS + IDR
        frags = fragments_of(au, 0, True, version=1)
        # a 1299-byte middle fragment breaks the PS3's fixed-offset placement: still counted as data here
        short_middle = frags[1][:-1]
        for fragment in (frags[0], short_middle, frags[2]):
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.version_mismatches, 3)
        self.assertEqual(self.fake.payload_size_violations, 1)
        self.assertEqual(self.fake.frames_complete, 1)               # the PS3 would still call it complete

    def test_implausible_timestamp(self):
        far_off = fake_ps3.local_us() + 5_000_000
        for fragment in fragments_of(P_SLICE, 0, False, capture_us=far_off):
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.timestamp_implausible, 1)
        self.assertEqual(self.fake.negative_latency_frames, 1)       # "captured" 5s in the future

    def test_clock_offset_is_applied(self):
        self.fake.clock_offset_us = 123_456_789                       # server clock = ours + offset
        for fragment in fragments_of(P_SLICE, 0, False, capture_us=fake_ps3.local_us() + 123_456_789):
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.timestamp_implausible, 0)
        self.assertEqual(self.fake.negative_latency_frames, 0)

    def test_keyframe_flag_must_match_content(self):
        for fragment in fragments_of(SPS + PPS + IDR, 0, False):    # IDR inside, flag says no
            self.fake._handle_fragment(fragment)
        for fragment in fragments_of(P_SLICE, 1, True):             # no IDR, flag says yes
            self.fake._handle_fragment(fragment)
        for fragment in fragments_of(P_SLICE, 2, False):
            self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.keyframe_flag_mismatches, 2)

    def test_contradicting_frag_count_does_not_raise(self):
        """The console indexes a FRAGMENT_MAX_COUNT array and a 1MiB buffer, so it shrugs this off; so must we.

        Before the fix this raised IndexError out of the receive loop, which killed the run without a report.
        """
        capture = fake_ps3.local_us()
        self.fake._handle_fragment(VF.pack(b"VF", 7, 0, 2, 0, 2, capture) + b"a" * PAYLOAD)
        self.fake._handle_fragment(VF.pack(b"VF", 7, 3, 4, 0, 2, capture) + b"b" * 10)   # index past the announced count
        self.assertEqual(self.fake.frag_count_changes, 1)
        # two fragments is what the frame announced, and the second one called itself the last: complete, with
        # a hole where index 1 should be. the console does the same, only with stale bytes in the hole.
        self.assertEqual(self.fake.frames_complete, 1)
        self.assertEqual(self.received(), b"a" * PAYLOAD + b"b" * 10)
        self.fake._handle_fragment(VF.pack(b"VF", 7, 1, 2, 0, 2, capture) + b"c" * 10)   # the real index 1, too late
        self.assertEqual(self.fake.frames_complete, 1)
        self.assertEqual(self.fake.frag_count_changes, 1)

    def test_no_packet_can_raise(self):
        """Whatever the server sends, the report must still be written: a crash here is a lost test run."""
        rng = random.Random(20260827)
        for _ in range(3000):
            header = VF.pack(b"VF", rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(16),
                             rng.getrandbits(8), rng.getrandbits(8), rng.getrandbits(64))
            self.fake._handle_fragment(header + bytes(rng.getrandbits(8) for _ in range(rng.choice((0, 1, 7, 1300, 1301)))))
        for length in range(0, 24):
            self.fake._handle_fragment(b"VF" + bytes(length))

    def test_fps_windows_and_largest_frame(self):
        for frame_id in range(150):
            for fragment in fragments_of(P_SLICE if frame_id else SPS + PPS + IDR, frame_id, frame_id == 0):
                self.fake._handle_fragment(fragment)
        self.assertEqual(self.fake.frames_complete, 150)
        self.assertEqual(self.fake.largest_frame_bytes, len(SPS + PPS + IDR))
        self.assertEqual(self.fake.video_rate.windows, [])           # all within one second: no complete window yet


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fake_ps3_test_")
        self.fake = make_fake(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def af(self, frames: int, packet_id: int = 0, payload: bytes | None = None, capture_us: int | None = None) -> bytes:
        if payload is None:
            payload = struct.pack(">" + "h" * (frames * 2), *([0x1234, -0x1234] * frames))
        return AF.pack(b"AF", packet_id, frames, fake_ps3.local_us() if capture_us is None else capture_us) + payload

    def test_ainfo_opens_the_feed_once(self):
        self.fake._handle_audio_packet(self.af(240))
        self.assertEqual(self.fake.af_before_ainfo, 1)               # the PS3 drops audio before AINFO
        self.fake._open_audio_feed(b"AINFO 48000 2")
        self.assertTrue(self.fake.audio_feed_open)
        self.assertEqual((self.fake.audio_rate, self.fake.audio_channels), (48000, 2))
        self.fake._open_audio_feed(b"AINFO 48000 2")
        self.fake._open_audio_feed(b"AINFO 44100 2")                # a changed rate mid-stream is noted
        self.assertEqual(self.fake.ainfo_count, 3)
        self.assertEqual(self.fake.ainfo_inconsistent, 1)
        self.assertEqual(self.fake.audio_rate, 48000)                # ...but the feed keeps its first rate, as on the PS3
        self.fake._open_audio_feed(b"AINFO x")                       # unparsable: ignored
        self.assertEqual(self.fake.ainfo_count, 3)

    def test_channel_count_is_read_after_the_rate_whatever_its_length(self):
        for text, rate, channels in ((b"AINFO 8000 2", 8000, 2), (b"AINFO 192000 2", 192000, 2), (b"AINFO 48000 1", 48000, 1)):
            fake = make_fake(self.tmp)
            fake._open_audio_feed(text)
            self.assertEqual((fake.audio_rate, fake.audio_channels), (rate, channels), text)
        fake = make_fake(self.tmp)
        fake._open_audio_feed(b"AINFO  48000 2")                     # no digit right after the prefix: no feed
        self.assertFalse(fake.audio_feed_open)

    def test_valid_packets_parse_big_endian_samples(self):
        self.fake._open_audio_feed(b"AINFO 48000 2")
        for packet_id in range(3):
            self.fake._handle_audio_packet(self.af(240, packet_id))
        self.assertEqual(self.fake.af_total, 3)
        self.assertEqual(self.fake.af_invalid, 0)
        self.assertEqual(self.fake.af_size_mismatch, 0)
        self.assertEqual(self.fake.af_lost, 0)
        self.assertEqual(self.fake.audio_peak, 0x1234)               # 0x12 0x34 on the wire = 4660, not 0x3412
        self.assertEqual(self.fake.af_time_implausible, 0)

    def test_invalid_packets(self):
        self.fake._open_audio_feed(b"AINFO 48000 2")
        self.fake._handle_audio_packet(self.af(513, payload=b"\0" * (513 * 4)))     # > AUDIO_MAX_FRAMES
        self.fake._handle_audio_packet(self.af(0, payload=b"\0" * 4))
        self.fake._handle_audio_packet(self.af(240, payload=b"\0" * (240 * 4 - 2)))  # shorter than it claims
        self.fake._handle_audio_packet(AF.pack(b"AF", 0, 240, 0))                    # header only
        self.assertEqual(self.fake.af_invalid, 4)
        self.fake._handle_audio_packet(self.af(240, payload=b"\0" * (240 * 4 + 2)))  # trailing bytes: tolerated, counted
        self.assertEqual(self.fake.af_invalid, 4)
        self.assertEqual(self.fake.af_size_mismatch, 1)

    def test_lost_packets_and_timestamps(self):
        self.fake._open_audio_feed(b"AINFO 48000 2")
        self.fake._handle_audio_packet(self.af(240, 0))
        self.fake._handle_audio_packet(self.af(240, 4))
        self.assertEqual(self.fake.af_lost, 3)
        self.fake._handle_audio_packet(self.af(240, 5, capture_us=fake_ps3.local_us() - 3_000_000))
        self.assertEqual(self.fake.af_time_implausible, 1)


class BitstreamTests(unittest.TestCase):
    """The PPS bit the whole latency story hangs on (CAVLC vs CABAC), read out of the stream itself."""

    def pps(self, entropy_bit: int, sps_id: int = 0) -> bytes:
        # PPS RBSP: pic_parameter_set_id ue(v), seq_parameter_set_id ue(v), entropy_coding_mode_flag u(1)
        bits = "1" + "1" + str(entropy_bit) + "0000"
        return b"\x00\x00\x00\x01\x68" + bytes([int(bits[:8].ljust(8, "0"), 2)])

    def sps(self, profile: int = 77, level: int = 32) -> bytes:
        return b"\x00\x00\x00\x01\x67" + bytes([profile, 0, level]) + b"\x99" * 8

    def test_entropy_flag(self):
        self.assertEqual(fake_ps3.bitstream_facts(self.sps() + self.pps(0))["entropy"], "cavlc")
        self.assertEqual(fake_ps3.bitstream_facts(self.sps() + self.pps(1))["entropy"], "cabac")

    def test_profile_and_level(self):
        facts = fake_ps3.bitstream_facts(self.sps(100, 42) + self.pps(0))
        self.assertEqual((facts["profile"], facts["level"]), (100, 42))

    def test_missing_parameter_sets_are_reported_not_raised(self):
        self.assertIn("error", fake_ps3.bitstream_facts(IDR))
        self.assertIn("error", fake_ps3.bitstream_facts(self.sps()))       # SPS but no PPS
        self.assertIn("error", fake_ps3.bitstream_facts(b""))

    def test_emulation_prevention_bytes_are_removed(self):
        self.assertEqual(fake_ps3._rbsp(b"\x00\x00\x03\x01\x00\x00\x03\x03"), b"\x00\x00\x01\x00\x00\x03")
        self.assertEqual(fake_ps3._rbsp(b"\x00\x03\x01"), b"\x00\x03\x01")   # only after two zeros

    def test_exp_golomb(self):
        bits = fake_ps3._Bits(bytes([0b00010000, 0b11000000]))   # ue = 7 (000 1 000), then a 1 bit
        self.assertEqual(bits.ue(), 7)
        self.assertEqual(bits.bit(), 0)

    def test_nal_payloads_stops_before_the_next_start_code(self):
        payloads = fake_ps3.nal_payloads(SPS + PPS + IDR, 8)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0], b"\x68" + b"\x22" * 4)   # the PPS, without the next NAL's leading zero


class ThresholdTests(unittest.TestCase):
    """The report's own judgement: numbers a healthy server produces pass, a regression's numbers do not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fake_ps3_test_")
        self.fake = make_fake(self.tmp)
        self.checks = fake_ps3.Checks()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verdict(self, name: str) -> bool:
        return [ok for check, ok, _detail in self.checks.items if check == name][0]

    def test_bitrate_ceiling_catches_a_return_to_10mbit(self):
        self.fake.expect_kbps = 6000
        self.fake.video_bytes = int(6.2e6 / 8 * 8)          # 6.2 Mbit/s over 8s: normal for a busy picture
        self.fake.video_byte_rate.windows = [6_200_000 // 8] * 8
        self.fake._check_bitrate(self.checks, 8.0)
        self.assertTrue(self.verdict("video bitrate"))
        self.checks = fake_ps3.Checks()
        self.fake.video_bytes = int(10_700_000 / 8 * 8)     # the Windows original's 10 Mbit/s
        self.fake.video_byte_rate.windows = [10_700_000 // 8] * 8
        self.fake._check_bitrate(self.checks, 8.0)
        self.assertFalse(self.verdict("video bitrate"))

    def test_loss_recovery_flags_a_keyframe_stream_announced_as_intra(self):
        self.fake.stream_self_heals = True
        self.fake.frames_complete = 480
        self.fake.keyframes = 1
        self.fake._check_loss_recovery(self.checks, 8.0)
        self.assertTrue(self.verdict("loss recovery"))
        self.checks = fake_ps3.Checks()
        self.fake.keyframes = 8                              # one a second: this stream does NOT self-heal
        self.fake._check_loss_recovery(self.checks, 8.0)
        self.assertFalse(self.verdict("loss recovery"))

    def test_loss_recovery_flags_a_keyframe_stream_without_keyframes(self):
        self.fake.stream_self_heals = False
        self.fake.frames_complete = 480
        self.fake.keyframes = 1                              # announced keyframes, sends one an hour
        self.fake._check_loss_recovery(self.checks, 8.0)
        self.assertFalse(self.verdict("loss recovery"))

    def test_clock_resync_must_have_run_when_the_interval_says_so(self):
        self.fake.resync_interval_ms = 2000
        self.fake.resyncs_applied = 0                        # four rounds were due and none completed
        self.fake._check_clock_resync(self.checks, 8.0)
        self.assertFalse(self.verdict("clock re-sync"))
        self.checks = fake_ps3.Checks()
        self.fake.resyncs_applied = 3
        self.fake.offset_drift_us = 400
        self.fake._check_clock_resync(self.checks, 8.0)
        self.assertTrue(self.verdict("clock re-sync"))

    def test_clock_resync_is_not_demanded_at_the_console_interval(self):
        self.fake._check_clock_resync(self.checks, 8.0)      # 30s interval, 8s session: nothing was due
        self.assertTrue(self.verdict("clock re-sync"))

    def test_clock_resync_flags_a_jumped_offset(self):
        self.fake.resync_interval_ms = 2000
        self.fake.resyncs_applied = 3
        self.fake.offset_drift_us = 30_000                   # 30ms: the server's clock is not the one we synced to
        self.fake._check_clock_resync(self.checks, 8.0)
        self.assertFalse(self.verdict("clock re-sync"))

    def test_fps_check_fails_on_one_bad_second(self):
        self.fake.session_seconds = 8.0
        self.fake.first_frame_after_play_ms = 200
        self.fake.first_au_types = [7, 8, 5]
        self.fake.first_au_keyframe_flag = True
        self.fake.frames_complete = 460
        self.fake.frame_gap_max_us = 20_000
        self.fake.network_latency_us = [50] * 460
        self.fake.beacons_during_session = 8
        self.fake.pad_packets_sent = 480
        self.fake.padmodes_sent = 8
        self.fake.video_rate.windows = [60, 60, 60, 20, 60, 60, 60]   # one second at 20fps: judder
        self.fake.keyframes = 1
        self.fake.stream_self_heals = True
        self.fake._evaluate_session(self.checks)
        self.assertFalse(self.verdict("received fps"))
        self.assertTrue(self.verdict("frame gaps"))

    def test_frame_gap_check_fails_on_a_single_stall(self):
        self.fake.frames_complete = 460
        self.fake.frame_gap_max_us = 520_000    # the GIL trap: one keyframe took half a second to pace out
        self.fake.video_rate.windows = [60] * 7
        self.fake.network_latency_us = [50] * 460
        self.fake.beacons_during_session = 8
        self.fake.pad_packets_sent = 480
        self.fake.padmodes_sent = 8
        self.fake._evaluate_session(self.checks)
        self.assertFalse(self.verdict("frame gaps"))
        self.assertTrue(self.verdict("received fps"))

    def test_quiet_after_stop(self):
        self.fake.frames_complete = 460
        self.fake.video_rate.windows = [60] * 7
        self.fake.network_latency_us = [50] * 460
        self.fake.beacons_during_session = 8
        self.fake.pad_packets_sent = 480
        self.fake.padmodes_sent = 8
        self.fake.post_stop_video = 200
        self.fake.post_stop_last_video_ms = 1400.0     # still encoding a second and a half after STOP
        self.fake._evaluate_session(self.checks)
        self.assertFalse(self.verdict("quiet after STOP"))

    def test_audio_may_lag_the_video_after_stop(self):
        """audio_streamer.stop() runs behind live_streamer.stop()'s join, so its tail is longer by design"""
        self.fake.frames_complete = 460
        self.fake.video_rate.windows = [60] * 7
        self.fake.network_latency_us = [50] * 460
        self.fake.beacons_during_session = 8
        self.fake.pad_packets_sent = 480
        self.fake.padmodes_sent = 8
        self.fake.post_stop_last_video_ms = 20.0
        self.fake.post_stop_last_audio_ms = 240.0
        self.fake._evaluate_session(self.checks)
        self.assertTrue(self.verdict("quiet after STOP"))
        self.checks = fake_ps3.Checks()
        self.fake.post_stop_last_audio_ms = 1200.0     # a whole second of sound after the console stopped
        self.fake._evaluate_session(self.checks)
        self.assertFalse(self.verdict("quiet after STOP"))

    def test_beacon_must_keep_going_while_streaming(self):
        self.fake.frames_complete = 460
        self.fake.video_rate.windows = [60] * 7
        self.fake.network_latency_us = [50] * 460
        self.fake.beacons_during_session = 1           # one at the start, then the loop died
        self.fake.pad_packets_sent = 480
        self.fake.padmodes_sent = 8
        self.fake._evaluate_session(self.checks)
        self.assertFalse(self.verdict("beacon while streaming"))


class SelfTestTests(unittest.TestCase):
    """The whole client against the in-file MockServer, on spare ports so a running server is not in the way."""

    @unittest.skipIf(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, "ffmpeg/ffprobe missing")
    def test_mock_server_round_trip(self):
        for port in (48310, 48311):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                self.skipTest("udp :%d in use" % port)
            finally:
                probe.close()
        tmp = tempfile.mkdtemp(prefix="fake_ps3_selftest_")
        options = argparse.Namespace(duration=8.0, padmode="gamepad", keep=False, out=tmp, key="a", beacon_timeout=10.0,
                                     client_port=48311, server_port=48310, allow_no_audio=False,
                                     resync_interval_ms=2000,   # the console's 30s would never fire inside the run
                                     expect_kbps=fake_ps3.DEFAULT_EXPECT_KBPS, expect_entropy=fake_ps3.DEFAULT_EXPECT_ENTROPY)
        checks = fake_ps3.Checks()
        try:
            fake = fake_ps3.run_self_test(options, checks)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNotNone(fake)
        names = [name for name, _ok, _detail in checks.items]
        for expected in ("beacon", "clock sync", "clock sync quality", "PLAY -> SINFO", "first AU keyframe", "received fps",
                         "frame gaps", "lost frames", "fragment sizes", "protocol version", "timestamps", "network latency",
                         "loss recovery", "video bitrate", "beacon while streaming", "clock re-sync", "quiet after STOP",
                         "AINFO", "audio packets/s", "ffprobe", "entropy coder", "SPS vs SINFO level",
                         "picture not black", "mock: CP rate", "mock: cross 1s..2s", "mock: L1 3s..4s", "mock: KEY",
                         "mock: CUSTOM 4", "mock: STOP", "mock: clean"):
            self.assertIn(expected, names)
        self.assertEqual(checks.failed, [], "\n".join("%s: %s" % (name, detail) for name, ok, detail in checks.items if not ok))
        self.assertGreaterEqual(fake.frames_complete, 400)
        self.assertGreaterEqual(fake.fragments, fake.frames_complete)


if __name__ == "__main__":
    unittest.main()
