#!/usr/bin/env python3
"""fake_ps3 - the PS3's cell-stream app, as far as the network can tell.

A faithful stand-in for upstream/ps3-app/stream.c + net-common.c, used to drive the server end to end
without a console: binds :38311 with a 1 MiB receive buffer, waits for the CELLSTREAM beacon, syncs clocks
with ten TIME probes (lowest round trip wins), sends PLAY until SINFO arrives, reassembles VF fragments
exactly like handleFragment(), re-syncs the clock between packets, repeats PLAY once mid-stream (the console
does that whenever a SINFO goes missing), takes AINFO/AF, sends the pad (CP) 60x/s with a scripted button
pattern plus PADMODE, KEY, CUSTOM and finally STOP - and then listens for the silence that must follow it.

Everything the PS3 would silently swallow is counted and checked here instead: the console tolerates a
server that answers one TIME probe in ten, drifts to 10 Mbit/s CABAC, stalls for half a second or keeps
encoding after STOP - this does not. Afterwards the received Annex-B stream is decoded with ffmpeg, and its
SPS/PPS read directly, before a PASS/FAIL report is printed.

  python3 tests/fake_ps3.py [--duration 8] [--padmode gamepad|mouse] [--keep] [--out DIR] [--key a]
                            [--expect-kbps 6000] [--expect-entropy cavlc] [--resync-interval-ms 30000]
  python3 tests/fake_ps3.py --self-test        # against the in-file MockServer, no real server needed

Exit code 0 = every check passed, 1 = something failed (the report says what).

Deliberately standalone: no teecellstream import. The constants below are the PS3's own (stream.c,
net-common.h), so the server is measured against the console's truth, not against its own definitions.
"""

import argparse
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from array import array
from datetime import datetime, timezone

# section: the PS3's constants (stream.c / net-common.h) - keep in step with the console, never with the server

SERVER_PORT = 38310
CLIENT_PORT = 38311
PACKET_MAX = 1500

SERVER_TIMEOUT_MS = 2000          # once video has flowed, no video for this long = the server is gone
FIRST_FRAME_GRACE_MS = 10000      # the encoder cold-starts in ~1-2s; the first frame gets much longer
CLOCK_RESYNC_INTERVAL_MS = 30000
CLOCK_PROBE_TIMEOUT_MS = 500
PLAY_TRIES = 5
TIME_SYNC_SAMPLES = 10
FRAGMENT_HEADER_BYTES = 20
FRAGMENT_PAYLOAD_BYTES = 1300
FRAME_MAX_BYTES = 1024 * 1024
FRAGMENT_MAX_COUNT = FRAME_MAX_BYTES // FRAGMENT_PAYLOAD_BYTES + 1
AUDIO_HEADER_BYTES = 16
AUDIO_MAX_FRAMES = 512            # the PS3 drops any packet larger than this
PAD_PACKET_BYTES = 20
PAD_RATE_HZ = 60                  # the render loop sends the pad once per rendered frame

# what the test demands on top of what the console tolerates. the PS3 swallows a bad server silently;
# these are the numbers a healthy server on this machine actually produced (see the integration runs),
# left with enough headroom that only a regression - not a busy PC - trips them.
FIRST_FRAME_LIMIT_MS = 3000       # measured 184ms; a rung that had to time out first (5s) must fail here
MIN_AVERAGE_FPS = 55.0            # measured 58.7 average over 7 windows
MIN_WINDOW_FPS = 50               # ... and 57 in the worst of them
MAX_LOST_FRAME_SHARE = 0.005      # measured 0 incomplete, 0 frame-id gaps on loopback
MAX_LOST_FRAMES = 2               # ... and never more than this many, however short the run
MAX_FRAME_GAP_MS = 100            # measured max 20ms. a keyframe that stalls the pump (the GIL trap in
                                  # server.py: a 27ms keyframe stretched past 500ms) shows up only here
MAX_NETWORK_LATENCY_MS = 25.0     # encoder exit -> last fragment, averaged (measured 0.05ms on loopback)
MAX_NETWORK_LATENCY_PEAK_MS = 200.0
MIN_CLOCK_SAMPLES = 8             # of TIME_SYNC_SAMPLES; a beacon may eat one (measured 10/10)
MAX_CLOCK_RTT_MS = 50.0           # measured 0.065ms
MAX_CLOCK_DRIFT_MS = 5.0          # both clocks tick on this machine's monotonic clock (measured < 0.1ms)
POST_STOP_WATCH_S = 1.5           # after STOP the server must fall silent - this is how long we listen
POST_STOP_QUIET_MS = 300          # ... and how late its last video packet may be (measured 14-17ms)
POST_STOP_AUDIO_QUIET_MS = 1000   # audio stops behind the video pump's join, so it is slower (measured 130-235ms)

# StreamSender.cs; stream.c never looks at the version byte, this test does
PROTOCOL_VERSION = 2
EXPECTED_SINFO = (1280, 720, 42, 1, 60)
TIMESTAMP_TOLERANCE_US = 2_000_000   # a server stamp further than this from our synced clock is nonsense

# the server's current defaults (settings.py). they are what the PS3's decoder can actually keep up with:
# CABAC cost the console 38-40ms a frame at 11-13Mbit/s, CAVLC at 6Mbit/s 19-20ms - so a silent fallback
# to the Windows original's CABAC/10Mbit/s is a regression the console feels, and the wire is where it shows.
DEFAULT_EXPECT_KBPS = 6000
DEFAULT_EXPECT_ENTROPY = "cavlc"
BITRATE_CEILING_PERCENT = 140        # -maxrate in encoders.py; the average over the session may not pass it

# pad.h bit positions (PadReceiver.cs ButtonNames order)
BUTTON_NAMES = ("up", "down", "left", "right", "cross", "circle", "square", "triangle",
                "L1", "R1", "L2", "R2", "start", "select", "L3", "R3")
BIT_CROSS = 1 << 4
BIT_L1 = 1 << 8

VF_HEADER = struct.Struct(">2sIHHBBQ")   # 'VF' frameId fragIndex fragCount flags version captureUs
AF_HEADER = struct.Struct(">2sIHQ")      # 'AF' packetId frameCount captureUs
CP_PACKET = struct.Struct(">2sIHbbbbQ")  # 'CP' packetId buttons lx ly rx ry sendUs (server clock)


def local_us() -> int:
    """Our clock, microseconds. The PS3 uses its wall clock; only the offset to the server matters."""
    return time.monotonic_ns() // 1000


def sleep_until_us(due_us: int, clock=local_us, spin_margin_us: int = 150) -> None:
    """Sleeps most of the way, spins only the last hair (Python cannot spin for long without starving threads)."""
    while True:
        remaining = due_us - clock()
        if remaining <= 0:
            return
        if remaining > spin_margin_us:
            time.sleep((remaining - spin_margin_us) / 1_000_000)


def parse_big_number_after(text: bytes, prefix: bytes) -> int:
    """net-common.c parseBigNumberAfter: the unsigned integer right after prefix, -1 if absent."""
    if not text.startswith(prefix):
        return -1
    cursor = len(prefix)
    if cursor >= len(text) or not 48 <= text[cursor] <= 57:
        return -1
    value = 0
    while cursor < len(text) and 48 <= text[cursor] <= 57:
        value = value * 10 + (text[cursor] - 48)
        cursor += 1
    return value


def _strtol(text: bytes, cursor: int) -> tuple[int, int]:
    """strtol(cursor, &cursor, 10): skips whitespace, optional sign, digits; (value, new cursor). 0 when nothing parses."""
    while cursor < len(text) and text[cursor] in b" \t\r\n":
        cursor += 1
    sign = 1
    if cursor < len(text) and text[cursor] in b"+-":
        sign = -1 if text[cursor] == 45 else 1
        cursor += 1
    start = cursor
    while cursor < len(text) and 48 <= text[cursor] <= 57:
        cursor += 1
    if cursor == start:
        return 0, start
    return sign * int(text[start:cursor]), cursor


def parse_sinfo(text: bytes):
    """stream.c parseSinfo: 'SINFO w h level refs fps [intraRefresh]'; five positive values, the flag optional (0 when absent)."""
    if not text.startswith(b"SINFO "):
        return None
    cursor = 6
    values = []
    for _ in range(5):
        value, cursor = _strtol(text, cursor)
        if value <= 0:
            return None
        values.append(value)
    intra, cursor = _strtol(text, cursor)
    return tuple(values) + (intra,)


def describe_buttons(mask: int) -> str:
    return "+".join(name for bit, name in enumerate(BUTTON_NAMES) if mask & (1 << bit))


def _rbsp(nal_payload: bytes) -> bytes:
    """Removes H.264 emulation-prevention bytes (00 00 03 -> 00 00), so the bits can be read as written."""
    out = bytearray()
    zeros = 0
    for byte in nal_payload:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    """Just enough of a bit reader for the SPS/PPS fields we need; raises IndexError past the end."""

    def __init__(self, data: bytes):
        self._data = data
        self._position = 0

    def bit(self) -> int:
        index, shift = divmod(self._position, 8)
        self._position += 1
        return (self._data[index] >> (7 - shift)) & 1

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        """exp-Golomb: leading zeros, then that many bits after the 1."""
        leading = 0
        while self.bit() == 0:
            leading += 1
            if leading > 32:
                raise ValueError("exp-Golomb code too long")
        return (1 << leading) - 1 + (self.bits(leading) if leading else 0)


def nal_payloads(stream: bytes, wanted_type: int, limit: int = 4):
    """The payload bytes (NAL header byte included) of the first `limit` NALs of that type."""
    found = []
    pos = 0
    while len(found) < limit:
        index = stream.find(b"\x00\x00\x01", pos)
        if index < 0 or index + 3 >= len(stream):
            break
        start = index + 3
        end = stream.find(b"\x00\x00\x01", start)
        if end > start and stream[end - 1] == 0:
            end -= 1
        if stream[start] & 0x1F == wanted_type:
            found.append(stream[start:end if end > start else len(stream)])
        pos = start
    return found


def bitstream_facts(stream: bytes) -> dict:
    """What the H.264 stream itself says: profile/level from the SPS, entropy coder from the PPS.

    The PS3 sizes its decoder from the SPS (openDecoderForStream) and pays for the entropy coder on its
    SPUs, so both are worth reading out of the bytes rather than trusting SINFO or the server's settings.
    """
    facts: dict = {}
    sps = nal_payloads(stream, 7, 1)
    pps = nal_payloads(stream, 8, 1)
    if not sps:
        facts["error"] = "no SPS in the stream"
        return facts
    try:
        data = _rbsp(sps[0][1:])
        facts["profile"] = data[0]
        facts["level"] = data[2]
        if not pps:
            facts["error"] = "no PPS in the stream"
            return facts
        bits = _Bits(_rbsp(pps[0][1:]))
        bits.ue()   # pic_parameter_set_id
        bits.ue()   # seq_parameter_set_id
        facts["entropy"] = "cabac" if bits.bit() else "cavlc"
    except (IndexError, ValueError) as error:
        facts["error"] = "unreadable SPS/PPS: %r" % (error,)
    return facts


def nal_types(access_unit: bytes) -> list[int]:
    """NAL unit types in an Annex-B access unit (start code 00 00 01, type = next byte & 0x1F)."""
    types = []
    pos = 0
    while True:
        index = access_unit.find(b"\x00\x00\x01", pos)
        if index < 0 or index + 3 >= len(access_unit):
            return types
        types.append(access_unit[index + 3] & 0x1F)
        pos = index + 3


class RateWindow:
    """Per-second totals, like the PS3's stats window: complete one-second windows only, the tail is dropped.

    `amount` counts bytes as easily as packets (the PS3's own window counts both: windowBytes, windowDecodedFrames).
    """

    def __init__(self):
        self.start_us = None
        self.count = 0
        self.windows: list[int] = []

    def add(self, now_us: int, amount: int = 1) -> None:
        if self.start_us is None:
            self.start_us = now_us
        while now_us - self.start_us >= 1_000_000:
            self.windows.append(self.count)
            self.count = 0
            self.start_us += 1_000_000
        self.count += amount

    @property
    def average(self) -> float:
        return sum(self.windows) / len(self.windows) if self.windows else 0.0


class Checks:
    """The report: every check with its verdict and the numbers behind it."""

    def __init__(self):
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.items.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [name for name, ok, _detail in self.items if not ok]

    def print_report(self) -> None:
        print()
        print("=" * 78)
        for name, ok, detail in self.items:
            print("%s  %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
        print("=" * 78)
        passed = len(self.items) - len(self.failed)
        print("RESULT: %s (%d/%d checks passed)" % ("PASS" if not self.failed else "FAIL", passed, len(self.items)))
        if self.failed:
            print("failed: " + ", ".join(self.failed))


def log(message: str) -> None:
    print("[cst] " + message, flush=True)


# section: the pad script
#
# what the "player" does, as a function of time since the stream started. gamepad mode drives only the
# server's virtual Xbox pad, so the full pattern is safe. in mouse mode every button and stick lands on the
# REAL desktop (a cross press is a left click, a stick sweep drags the pointer across the screen), so that
# script keeps its hands off: no cross, and stick values inside DesktopInput's dead zone (16), which still
# exercises the packet path and shows up in the server's 2s pad report without moving anything.

def pad_state_at(t: float, padmode: str) -> tuple[int, int, int, int, int]:
    buttons = 0
    if 3.0 <= t < 4.0:
        buttons |= BIT_L1
    if padmode == "gamepad":
        if 1.0 <= t < 2.0:
            buttons |= BIT_CROSS
        amplitude_left, amplitude_right = 127.0, 100.0
    else:
        amplitude_left = amplitude_right = 10.0   # below the mouse dead zone: the pointer never moves
    lx = int(round(amplitude_left * math.sin(2 * math.pi * t / 4.0)))
    ly = int(round(amplitude_left * math.cos(2 * math.pi * t / 4.0)))
    rx = int(round(amplitude_right * math.sin(2 * math.pi * t / 3.0)))
    ry = int(round(amplitude_right * math.cos(2 * math.pi * t / 3.0)))
    clamp = lambda v: max(-128, min(127, v))   # noqa: E731
    return buttons, clamp(lx), clamp(ly), clamp(rx), clamp(ry)


# section: the fake PS3 (port of runStreamSession and friends)

class FakePs3:
    def __init__(self, duration_s: float, padmode: str, out_dir: str, key: str | None,
                 beacon_timeout_s: float, client_port: int = CLIENT_PORT, quiet: bool = False,
                 resync_interval_ms: int = CLOCK_RESYNC_INTERVAL_MS, expect_kbps: int = DEFAULT_EXPECT_KBPS,
                 expect_entropy: str = DEFAULT_EXPECT_ENTROPY):
        self.duration_s = duration_s
        self.padmode = padmode
        self.out_dir = out_dir
        self.key = key
        self.beacon_timeout_s = beacon_timeout_s
        self.client_port = client_port
        self.quiet = quiet
        # the console re-syncs every 30s, far longer than a test run: a shorter interval is the only way to
        # exercise the probe-between-packets path (and the server's TIME handler under a running stream)
        self.resync_interval_ms = resync_interval_ms
        self.expect_kbps = expect_kbps
        self.expect_entropy = expect_entropy

        self.sock: socket.socket | None = None
        self.server_address = None
        self.h264_path = os.path.join(out_dir, "fake_ps3_received.h264")
        self._h264_file = None

        # clock sync (syncServerClock / updateClockSync / handleTimeReply)
        self.clock_offset_us = 0
        self.sync_samples_ok = 0
        self.best_rtt_us = None
        self.resync_round_start_us = 0
        self.resync_probe_sent_us = 0
        self.resync_best_rtt_us = None
        self.resync_best_offset_us = 0
        self.resync_probes_left = 0
        self.resyncs_applied = 0
        self.initial_offset_us = 0
        self.offset_drift_us = 0

        # handshake
        self.beacon_after_s = None
        self.sinfo = None
        self.play_attempts = 0
        self.stream_self_heals = False
        # the console repeats PLAY until SINFO arrives, so a server that treats a repeat as a NEW session
        # tears the running stream down and builds it again. one repeat mid-session proves it does not.
        self.replay_play_at_us = 0
        self.replay_play_sent = False
        self.sinfo_after_replay = 0

        # reassembly (handleFragment)
        self.assembly_frame_id = -1
        self.assembly_frag_count = 0
        self.assembly_frags_received = 0
        self.assembly_last_frag_bytes = -1
        self.assembly_frag_seen: list[bool] = []
        self.assembly_fragments: list[bytes | None] = []
        self.assembly_keyframe = False
        self.assembly_capture_local_us = 0

        # video statistics
        self.frames_complete = 0
        self.frames_incomplete = 0
        self.fragments = 0
        self.fragments_duplicate = 0
        self.fragments_rejected = 0
        self.video_bytes = 0
        self.version_mismatches = 0
        self.payload_size_violations = 0
        self.timestamp_implausible = 0
        self.negative_latency_frames = 0
        self.keyframe_flag_mismatches = 0
        self.keyframes = 0
        self.first_au_types: list[int] | None = None
        self.first_au_keyframe_flag = False
        self.first_frame_after_play_ms = None
        self.largest_frame_bytes = 0
        self.network_latency_us: list[int] = []
        self.frame_ids_seen = 0
        self.frame_id_gaps = 0
        self.last_frame_id = -1
        self.frag_count_changes = 0
        self.video_rate = RateWindow()
        self.video_byte_rate = RateWindow()      # wire bytes per second: the bitrate the PS3's radio sees
        self.frame_gap_max_us = 0
        self.frame_gap_at_s = 0.0
        self._last_complete_us = 0

        # audio
        self.audio_feed_open = False
        self.audio_rate = 0
        self.audio_channels = 0
        self.ainfo_count = 0
        self.ainfo_inconsistent = 0
        self.af_total = 0
        self.af_before_ainfo = 0
        self.af_invalid = 0
        self.af_size_mismatch = 0
        self.af_lost = 0
        self.af_last_id = -1
        self.af_time_implausible = 0
        self.audio_peak = 0
        self.audio_rate_window = RateWindow()

        # session
        self.session_start_us = 0
        self.play_sent_us = 0
        self.other_packets = 0
        self.beacons_during_session = 0
        self.sinfo_repeats = 0
        self.oversize_packets = 0
        self.server_gone_reason = None
        self.stops_sent = 0
        self.session_seconds = 0.0
        self.post_stop_video = 0
        self.post_stop_audio = 0
        self.post_stop_last_video_ms = None
        self.post_stop_last_audio_ms = None

        # pad thread
        self.pad_packet_id = 0
        self.pad_packets_sent = 0
        self.padmodes_sent = 0
        self.key_sent = False
        self.custom_sent = False
        self._pad_thread: threading.Thread | None = None
        self._pad_stop = threading.Event()
        self._pad_error = None

    def say(self, message: str) -> None:
        if not self.quiet:
            log(message)

    # ---------------------------------------------------------------- net-common.c

    def _open_client_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)   # absorb a keyframe burst even if the reader hiccups
        effective = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)   # Linux reports double and clamps to rmem_max
        if effective < 1024 * 1024:
            self.say("SO_RCVBUF 1MB clamped by the kernel to %d bytes (net.core.rmem_max)" % effective)
        sock.bind(("0.0.0.0", self.client_port))
        self.sock = sock

    def _set_receive_timeout(self, milliseconds: int) -> None:
        self.sock.settimeout(milliseconds / 1000)

    def _recv(self):
        """One recv: (packet, sender) or (None, None) on timeout / ICMP noise, like a recv() returning <= 0."""
        try:
            return self.sock.recvfrom(2048)
        except (socket.timeout, OSError):
            return None, None

    def _drain_socket(self) -> None:
        """discard queued packets so an earlier step can't pollute the next"""
        self._set_receive_timeout(50)
        while self._recv()[0] is not None:
            pass

    def _discover_server(self) -> bool:
        self._set_receive_timeout(500)
        started = local_us()
        deadline = started + int(self.beacon_timeout_s * 1_000_000)
        while local_us() < deadline:
            packet, sender = self._recv()
            if packet is None:
                continue
            if packet.startswith(b"CELLSTREAM"):
                self.server_address = sender
                self.beacon_after_s = (local_us() - started) / 1e6
                self.say("beacon %r from %s:%d after %.2fs" % (packet[:20].decode("ascii", "replace"), sender[0], sender[1], self.beacon_after_s))
                return True
        return False

    # ---------------------------------------------------------------- clock sync

    def _sync_server_clock(self) -> bool:
        """pairs the server's clock with ours; the sample with the lowest round trip has the least one-way skew"""
        self._drain_socket()
        self._set_receive_timeout(500)
        best_rtt = None
        for _sample in range(TIME_SYNC_SAMPLES):
            sent_us = local_us()
            self.sock.sendto(b"TIME", self.server_address)
            reply, _sender = self._recv()
            if reply is None:
                continue
            server_us = parse_big_number_after(reply, b"TIME ")
            if server_us < 0:
                continue   # a beacon (or a stray SINFO) ate this sample - the PS3 loses it the same way
            now_us = local_us()
            rtt_us = now_us - sent_us
            self.sync_samples_ok += 1
            if best_rtt is None or rtt_us < best_rtt:
                best_rtt = rtt_us
                self.clock_offset_us = server_us - (sent_us + rtt_us // 2)   # the reply was stamped ~halfway through
            time.sleep(0.02)
        self.best_rtt_us = best_rtt
        if best_rtt is not None:
            self.say("clock synced, best round trip %.3fms (%d/%d samples), offset %d us" %
                     (best_rtt / 1000, self.sync_samples_ok, TIME_SYNC_SAMPLES, self.clock_offset_us))
        return best_rtt is not None

    def _update_clock_sync(self) -> None:
        """the 30s re-sync: a probe goes out between packets, its reply is picked up like any other packet"""
        now = local_us()
        if self.resync_probes_left == 0 and now - self.resync_round_start_us >= self.resync_interval_ms * 1000:
            self.resync_round_start_us = now
            self.resync_probes_left = TIME_SYNC_SAMPLES
            self.resync_best_rtt_us = None
        if self.resync_probe_sent_us and now - self.resync_probe_sent_us > CLOCK_PROBE_TIMEOUT_MS * 1000:
            self.resync_probe_sent_us = 0   # a probe that never came back: forget it
            self.resync_probes_left -= 1
        if self.resync_probes_left > 0 and self.resync_probe_sent_us == 0:
            self.resync_probe_sent_us = now
            self.sock.sendto(b"TIME", self.server_address)

    def _handle_time_reply(self, reply: bytes) -> None:
        if not self.resync_probe_sent_us:
            return
        server_us = parse_big_number_after(reply, b"TIME ")
        if server_us < 0:
            return
        rtt_us = local_us() - self.resync_probe_sent_us
        if self.resync_best_rtt_us is None or rtt_us < self.resync_best_rtt_us:
            self.resync_best_rtt_us = rtt_us
            self.resync_best_offset_us = server_us - (self.resync_probe_sent_us + rtt_us // 2)
        self.resync_probe_sent_us = 0
        self.resync_probes_left -= 1
        if self.resync_probes_left <= 0 and self.resync_best_rtt_us is not None:
            self.resync_probes_left = 0
            self.clock_offset_us = self.resync_best_offset_us
            self.resyncs_applied += 1
            self.offset_drift_us = self.clock_offset_us - self.initial_offset_us
            self.say("clock re-synced, best round trip %.3fms, offset moved %d us since the first sync"
                     % (self.resync_best_rtt_us / 1000, self.offset_drift_us))

    # ---------------------------------------------------------------- session setup

    def _request_play(self) -> bool:
        """sends PLAY (with retries) and waits for the SINFO reply describing the stream"""
        self._set_receive_timeout(1000)
        for _attempt in range(PLAY_TRIES):
            self.play_attempts += 1
            self.play_sent_us = local_us()
            self.sock.sendto(b"PLAY", self.server_address)
            packet, _sender = self._recv()
            if packet is None:
                continue
            parsed = parse_sinfo(packet)
            if parsed is not None:
                self.sinfo = parsed
                return True
        return False

    # ---------------------------------------------------------------- fragment reassembly (handleFragment)

    def _handle_fragment(self, packet: bytes) -> None:
        packet_bytes = len(packet)
        if packet_bytes <= FRAGMENT_HEADER_BYTES:
            return
        _magic, frame_id, frag_index, frag_count, flags, version, capture_us = VF_HEADER.unpack_from(packet)
        keyframe = flags & 1
        payload_bytes = packet_bytes - FRAGMENT_HEADER_BYTES

        if frag_count <= 0 or frag_count > FRAGMENT_MAX_COUNT or frag_index >= frag_count:
            self.fragments_rejected += 1
            return
        if (frag_count - 1) * FRAGMENT_PAYLOAD_BYTES + payload_bytes > FRAME_MAX_BYTES:
            self.fragments_rejected += 1
            return

        # the checks the PS3 does not make: the header's version, and the fixed fragment size it relies on
        if version != PROTOCOL_VERSION:
            self.version_mismatches += 1
        if frag_index < frag_count - 1:
            if payload_bytes != FRAGMENT_PAYLOAD_BYTES:
                self.payload_size_violations += 1
        elif payload_bytes < 1 or payload_bytes > FRAGMENT_PAYLOAD_BYTES:
            self.payload_size_violations += 1

        # a newer frame started before this one completed: its data is now missing from the stream, so drop the
        # incomplete frame (an intra-refresh stream decodes on through the damage; a keyframe stream would wait)
        if frame_id != self.assembly_frame_id:
            if self.assembly_frame_id >= 0 and self.assembly_frags_received > 0:
                self.frames_incomplete += 1
            self.assembly_frame_id = frame_id
            self.assembly_frag_count = frag_count
            self.assembly_frags_received = 0
            self.assembly_last_frag_bytes = -1
            self.assembly_keyframe = bool(keyframe)
            self.assembly_capture_local_us = capture_us - self.clock_offset_us   # server clock -> ours
            self.assembly_frag_seen = [False] * frag_count
            self.assembly_fragments = [None] * frag_count
            if abs(local_us() - self.assembly_capture_local_us) > TIMESTAMP_TOLERANCE_US:
                self.timestamp_implausible += 1
            if self.last_frame_id >= 0 and frame_id > self.last_frame_id + 1:
                self.frame_id_gaps += frame_id - self.last_frame_id - 1
            self.last_frame_id = frame_id
            self.frame_ids_seen += 1
        # a later fragment of the same frame announcing a different fragCount: the console shrugs (its seen
        # array is FRAGMENT_MAX_COUNT long and its frame buffer 1MiB, both indexed by the fragment's own
        # index), so this must not raise here either - it is a server bug to report, not a client crash.
        if frag_index >= len(self.assembly_frag_seen):
            self.frag_count_changes += 1
            grow = frag_index + 1 - len(self.assembly_frag_seen)
            self.assembly_frag_seen.extend([False] * grow)
            self.assembly_fragments.extend([None] * grow)
        elif frag_count != self.assembly_frag_count:
            self.frag_count_changes += 1
        if self.assembly_frag_seen[frag_index]:
            self.fragments_duplicate += 1
            return
        self.assembly_frag_seen[frag_index] = True
        self.assembly_frags_received += 1
        self.assembly_fragments[frag_index] = packet[FRAGMENT_HEADER_BYTES:]
        if frag_index == frag_count - 1:
            self.assembly_last_frag_bytes = payload_bytes
        self.fragments += 1
        self.video_bytes += packet_bytes
        self.video_byte_rate.add(local_us(), packet_bytes)

        if self.assembly_frags_received != self.assembly_frag_count or self.assembly_last_frag_bytes < 0:
            return

        # frame complete: what the PS3 would hand to its decode thread goes to the file instead. the console
        # sizes the frame by formula ((count-1)*1300 + last) over its fixed-offset buffer, so a short middle
        # fragment leaves it decoding stale bytes; here the shortfall is already counted as a size violation.
        access_unit = b"".join(fragment for fragment in self.assembly_fragments if fragment is not None)
        complete_us = local_us()
        self.assembly_frame_id = -1
        self._on_access_unit(access_unit, self.assembly_keyframe, self.assembly_capture_local_us, complete_us)

    def _on_access_unit(self, access_unit: bytes, keyframe: bool, capture_local_us: int, complete_us: int) -> None:
        if self.frames_complete == 0:
            self.first_au_types = nal_types(access_unit)
            self.first_au_keyframe_flag = keyframe
            self.first_frame_after_play_ms = (complete_us - self.play_sent_us) / 1000
            self.say("first frame %d bytes, %.0fms after PLAY, NAL types %s, keyframe flag %d" %
                     (len(access_unit), self.first_frame_after_play_ms, self.first_au_types, keyframe))
        # the gap between two finished frames: what the console would show as a freeze. the per-second fps
        # average hides a single long stall (a keyframe whose paced send lost the GIL was measured at >500ms
        # for a 27ms frame), so the worst gap is tracked in its own right.
        if self._last_complete_us:
            gap = complete_us - self._last_complete_us
            if gap > self.frame_gap_max_us:
                self.frame_gap_max_us = gap
                self.frame_gap_at_s = (complete_us - self.session_start_us) / 1e6
        self._last_complete_us = complete_us
        self.frames_complete += 1
        self.video_rate.add(complete_us)
        self.largest_frame_bytes = max(self.largest_frame_bytes, len(access_unit))
        if keyframe:
            self.keyframes += 1
        # keyframe flag must say what the bytes say (the PS3 builds its decoder on the first flagged frame)
        has_idr = 5 in nal_types(access_unit)
        if has_idr != keyframe:
            self.keyframe_flag_mismatches += 1
        # a frame cannot arrive before it was captured: if it claims to, the clocks have come apart
        latency = complete_us - capture_local_us
        if latency < 0:
            self.negative_latency_frames += 1
        else:
            self.network_latency_us.append(latency)
        self._h264_file.write(access_unit)

    # ---------------------------------------------------------------- audio

    def _open_audio_feed(self, info: bytes) -> None:
        rate = parse_big_number_after(info, b"AINFO ")
        if rate <= 0:
            return   # openAudioFeed: no digits right after the prefix means no feed, whatever follows
        cursor = 6
        while cursor < len(info) and 48 <= info[cursor] <= 57:
            cursor += 1   # step over the rate's own digits, however many it has
        channels, _cursor = _strtol(info, cursor)
        self.ainfo_count += 1
        if self.audio_feed_open:
            if rate != self.audio_rate or channels != self.audio_channels:
                self.ainfo_inconsistent += 1
            return
        self.audio_feed_open = True
        self.audio_rate = rate
        self.audio_channels = channels
        self.say("audio: %dHz, %d channels" % (rate, channels))

    def _handle_audio_packet(self, packet: bytes) -> None:
        self.af_total += 1
        if not self.audio_feed_open:
            self.af_before_ainfo += 1   # the PS3 drops these: no feed to push them into yet
            return
        packet_bytes = len(packet)
        if packet_bytes <= AUDIO_HEADER_BYTES:
            self.af_invalid += 1
            return
        _magic, packet_id, frames, capture_us = AF_HEADER.unpack_from(packet)
        if frames <= 0 or frames > AUDIO_MAX_FRAMES or packet_bytes < AUDIO_HEADER_BYTES + frames * 4:
            self.af_invalid += 1
            return
        if packet_bytes != AUDIO_HEADER_BYTES + frames * 4:
            self.af_size_mismatch += 1   # the PS3 tolerates trailing bytes; still worth knowing
        samples = array("h")
        samples.frombytes(packet[AUDIO_HEADER_BYTES:AUDIO_HEADER_BYTES + frames * 4])
        if sys.byteorder == "little":
            samples.byteswap()   # the wire is big-endian
        self.audio_peak = max(self.audio_peak, max(samples), -min(samples))
        if self.af_last_id >= 0 and packet_id > self.af_last_id + 1:
            self.af_lost += packet_id - self.af_last_id - 1
        self.af_last_id = packet_id
        if abs(local_us() - (capture_us - self.clock_offset_us)) > TIMESTAMP_TOLERANCE_US:
            self.af_time_implausible += 1
        self.audio_rate_window.add(local_us())

    # ---------------------------------------------------------------- the controller up-channel

    def _server_clock_us(self) -> int:
        return local_us() + self.clock_offset_us   # our clock -> the server's

    def _send_pad_state(self, buttons: int, lx: int, ly: int, rx: int, ry: int) -> None:
        packet = CP_PACKET.pack(b"CP", self.pad_packet_id & 0xFFFFFFFF, buttons & 0xFFFF, lx, ly, rx, ry, self._server_clock_us())
        self.sock.sendto(packet, self.server_address)
        self.pad_packet_id += 1
        self.pad_packets_sent += 1

    def _send_pad_mode(self) -> None:
        self.sock.sendto(b"PADMODE gamepad" if self.padmode == "gamepad" else b"PADMODE mouse", self.server_address)
        self.padmodes_sent += 1

    def _send_custom_command(self, slot: int) -> None:
        self.sock.sendto(("CUSTOM %d" % slot).encode("ascii"), self.server_address)

    def _send_keystroke(self, key: str) -> None:
        self.sock.sendto(b"KEY " + key.encode("latin-1")[:1], self.server_address)

    def _run_pad_thread(self) -> None:
        """the render loop's share: the pad at 60Hz, PADMODE every second, and the scripted one-offs"""
        try:
            self._send_pad_mode()   # tell the server which device BEFORE the first pad packet lands on it
            frame = 0
            while not self._pad_stop.is_set():
                due = self.session_start_us + frame * 1_000_000 // PAD_RATE_HZ
                sleep_until_us(due)
                if self._pad_stop.is_set():
                    break
                t = (local_us() - self.session_start_us) / 1e6
                self._send_pad_state(*pad_state_at(t, self.padmode))
                if frame and frame % PAD_RATE_HZ == 0:
                    self._send_pad_mode()
                if self.key is not None and not self.key_sent and t >= 5.0:
                    self.key_sent = True
                    self._send_keystroke(self.key)
                    self.say("sent KEY %r" % self.key)
                if not self.custom_sent and t >= 6.0:
                    self.custom_sent = True
                    self._send_custom_command(4)
                    self.say("sent CUSTOM 4")
                frame += 1
        except Exception as error:   # noqa: BLE001 - report it, never let the thread die silently
            self._pad_error = repr(error)

    # ---------------------------------------------------------------- the stream session

    def run(self, checks: Checks) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        try:
            self._open_client_socket()
        except OSError as error:
            checks.add("socket", False, "bind :%d failed: %s (another fake PS3 or the real app?)" % (self.client_port, error))
            return
        try:
            self._run_session(checks)
        finally:
            # whatever went wrong in there, the pad thread must not outlive the socket it sends on
            self._pad_stop.set()
            if self._pad_thread is not None:
                self._pad_thread.join(2.0)
            if self._h264_file is not None:
                self._h264_file.close()
            self.sock.close()

    def _run_session(self, checks: Checks) -> None:
        if not checks.add("beacon", self._discover_server(),
                          "no CELLSTREAM beacon on :%d within %.0fs" % (self.client_port, self.beacon_timeout_s) if self.server_address is None
                          else "from %s:%d after %.2fs" % (self.server_address[0], self.server_address[1], self.beacon_after_s)):
            return

        synced = self._sync_server_clock()
        if not checks.add("clock sync", synced, "no TIME reply" if not synced else
                          "%d/%d samples, best round trip %.3fms, offset %d us" % (self.sync_samples_ok, TIME_SYNC_SAMPLES, self.best_rtt_us / 1000, self.clock_offset_us)):
            return
        # the console is happy with one answered probe out of ten; a server that answers TIME only now and
        # then would still give it wildly skewed latency figures, so the test wants nearly all of them
        rtt_ms = self.best_rtt_us / 1000
        checks.add("clock sync quality", self.sync_samples_ok >= MIN_CLOCK_SAMPLES and rtt_ms <= MAX_CLOCK_RTT_MS,
                   "%d/%d TIME replies (need >= %d), best round trip %.3fms (need <= %.0fms)" %
                   (self.sync_samples_ok, TIME_SYNC_SAMPLES, MIN_CLOCK_SAMPLES, rtt_ms, MAX_CLOCK_RTT_MS))
        self._drain_socket()

        if not checks.add("PLAY -> SINFO", self._request_play(), "no SINFO after %d PLAY" % PLAY_TRIES if self.sinfo is None
                          else "SINFO %s after %d PLAY" % (" ".join(str(v) for v in self.sinfo), self.play_attempts)):
            return
        width, height, level, refs, fps, intra = self.sinfo
        checks.add("SINFO values", (width, height, level, refs, fps) == EXPECTED_SINFO and intra in (0, 1),
                   "%dx%d level %d refs %d fps %d intraRefresh %d (expected %s, intra 0/1)" % (width, height, level, refs, fps, intra, " ".join(str(v) for v in EXPECTED_SINFO)))
        self.stream_self_heals = intra != 0
        self.say("server offers %dx%d at %dfps, loss recovery = %s" % (width, height, fps, "intra refresh" if self.stream_self_heals else "keyframes"))

        self.initial_offset_us = self.clock_offset_us
        self._h264_file = open(self.h264_path, "wb")
        self.session_start_us = local_us()
        self.resync_round_start_us = self.session_start_us
        self._pad_thread = threading.Thread(target=self._run_pad_thread, name="fake-ps3-pad", daemon=True)
        self._pad_thread.start()

        self._set_receive_timeout(500)
        last_video_us = local_us()
        end_us = self.session_start_us + int(self.duration_s * 1_000_000)
        self.replay_play_at_us = self.session_start_us + int(self.duration_s * 500_000)   # halfway
        while local_us() < end_us:
            packet, _sender = self._recv()
            if packet is not None:
                length = len(packet)
                if length > PACKET_MAX:
                    self.oversize_packets += 1   # the PS3's buffer is PACKET_MAX: this would be truncated there
                if length > FRAGMENT_HEADER_BYTES and packet[0] == 0x56 and packet[1] == 0x46:      # 'V' 'F'
                    self._handle_fragment(packet)
                    last_video_us = local_us()
                elif length > AUDIO_HEADER_BYTES and packet[0] == 0x41 and packet[1] == 0x46:       # 'A' 'F'
                    self._handle_audio_packet(packet)
                elif 6 < length < PACKET_MAX and packet[0] == 0x41 and packet[1] == 0x49:            # 'A' 'I'
                    self._open_audio_feed(packet)
                elif 5 < length < PACKET_MAX and packet[0] == 0x54 and packet[1] == 0x49:            # 'T' 'I'
                    self._handle_time_reply(packet)
                elif packet.startswith(b"CELLSTREAM"):
                    self.beacons_during_session += 1   # the server keeps looking for PS3s while it streams
                elif packet.startswith(b"SINFO "):
                    self.sinfo_repeats += 1            # SINFO is sent 3x per PLAY; the extras land here
                    if self.replay_play_sent:
                        self.sinfo_after_replay += 1
                else:
                    self.other_packets += 1            # ignored, as on the PS3

            # halfway through, do what the console does when a SINFO went missing: send PLAY again. the
            # stream must carry on untouched (the frame-gap check is what would catch a restart).
            if not self.replay_play_sent and local_us() >= self.replay_play_at_us:
                self.replay_play_sent = True
                self.sock.sendto(b"PLAY", self.server_address)
                self.say("repeated PLAY (the console does this until SINFO arrives)")

            # the server went away: video is the signal - it flows continuously while a server is alive
            idle_limit_ms = SERVER_TIMEOUT_MS if self.frames_complete > 0 else FIRST_FRAME_GRACE_MS
            if local_us() - last_video_us > idle_limit_ms * 1000:
                self.server_gone_reason = ("no video for %dms - server gone" % SERVER_TIMEOUT_MS if self.frames_complete > 0
                                           else "no first frame in %dms" % FIRST_FRAME_GRACE_MS)
                self.say("stream: " + self.server_gone_reason)
                break
            self._update_clock_sync()

        self.session_seconds = (local_us() - self.session_start_us) / 1e6
        stop_us = local_us()
        for _ in range(3):
            self.sock.sendto(b"STOP", self.server_address)
            self.stops_sent += 1
        # stop the render loop sending the pad before the socket goes away underneath it
        self._pad_stop.set()
        self._pad_thread.join(2.0)
        self._h264_file.close()
        self._h264_file = None
        self._watch_after_stop(stop_us)
        self.say("stopping (received %d complete frames, %d incomplete, %d audio packets)" % (self.frames_complete, self.frames_incomplete, self.af_total))
        self._evaluate_session(checks)

    def _watch_after_stop(self, stop_us: int) -> None:
        """A STOP the server ignores leaves the desktop switched and the encoder running: listen for silence.

        Not something the PS3 checks (it is on its way back to WAITING), which is exactly why it belongs here.
        """
        self._set_receive_timeout(100)
        deadline = stop_us + int(POST_STOP_WATCH_S * 1_000_000)
        while local_us() < deadline:
            packet, _sender = self._recv()
            if packet is None or len(packet) < 2:
                continue
            head = packet[:2]
            if head == b"VF":
                self.post_stop_video += 1
                self.post_stop_last_video_ms = (local_us() - stop_us) / 1000
            elif head == b"AF":
                self.post_stop_audio += 1
                self.post_stop_last_audio_ms = (local_us() - stop_us) / 1000

    # ---------------------------------------------------------------- what the numbers say

    def _evaluate_session(self, checks: Checks) -> None:
        seconds = self.session_seconds or self.duration_s
        checks.add("session", self.server_gone_reason is None and self._pad_error is None,
                   self.server_gone_reason or self._pad_error or "ran %.1fs, STOP sent x%d" % (seconds, self.stops_sent))
        checks.add("first frame", self.first_frame_after_play_ms is not None and self.first_frame_after_play_ms <= FIRST_FRAME_LIMIT_MS,
                   "none" if self.first_frame_after_play_ms is None else
                   "%.0fms after PLAY (need <= %dms; the console would wait %dms, but a rung that had to time out first takes 5s)"
                   % (self.first_frame_after_play_ms, FIRST_FRAME_LIMIT_MS, FIRST_FRAME_GRACE_MS))
        types = self.first_au_types or []
        checks.add("first AU keyframe", self.first_au_keyframe_flag and 7 in types and 8 in types and 5 in types,
                   "flag %d, NAL types %s (need SPS 7 + PPS 8 + IDR 5)" % (self.first_au_keyframe_flag, types))
        windows = self.video_rate.windows
        worst = min(windows) if windows else 0
        checks.add("received fps", bool(windows) and self.video_rate.average >= MIN_AVERAGE_FPS and worst >= MIN_WINDOW_FPS,
                   "%d frames in %d complete windows: avg %.1f (need >= %.0f), min %d (need >= %d), max %d; per second %s" %
                   (self.frames_complete, len(windows), self.video_rate.average, MIN_AVERAGE_FPS, worst, MIN_WINDOW_FPS,
                    max(windows) if windows else 0, windows))
        # a single long gap is invisible in the per-second average and very visible on the console
        gap_ms = self.frame_gap_max_us / 1000
        checks.add("frame gaps", self.frames_complete > 1 and gap_ms <= MAX_FRAME_GAP_MS,
                   "worst gap between two frames %.1fms at t=%.1fs (need <= %dms; 60fps is 16.7ms)" %
                   (gap_ms, self.frame_gap_at_s, MAX_FRAME_GAP_MS))
        total = self.frames_complete + self.frames_incomplete
        lost = self.frames_incomplete + self.frame_id_gaps
        share = lost / total if total else 1.0
        checks.add("lost frames", total > 0 and (lost <= MAX_LOST_FRAMES or share <= MAX_LOST_FRAME_SHARE),
                   "%d incomplete + %d never arrived = %d of %d (%.2f%%, need <= %d or %.1f%%); %d fragments, %d duplicate, %d rejected" %
                   (self.frames_incomplete, self.frame_id_gaps, lost, total, share * 100, MAX_LOST_FRAMES,
                    MAX_LOST_FRAME_SHARE * 100, self.fragments, self.fragments_duplicate, self.fragments_rejected))
        checks.add("fragment sizes", self.payload_size_violations == 0 and self.frag_count_changes == 0,
                   "%d violations of 'exactly %d bytes except the last', %d fragments whose fragCount contradicted the frame's first, in %d fragments; largest frame %d bytes" %
                   (self.payload_size_violations, FRAGMENT_PAYLOAD_BYTES, self.frag_count_changes, self.fragments, self.largest_frame_bytes))
        checks.add("protocol version", self.version_mismatches == 0, "%d fragments with version != %d" % (self.version_mismatches, PROTOCOL_VERSION))
        latency = self.network_latency_us
        mean_ms = (sum(latency) / len(latency) / 1000) if latency else 0.0
        peak_ms = (max(latency) / 1000) if latency else 0.0
        checks.add("timestamps", self.timestamp_implausible == 0 and self.negative_latency_frames == 0,
                   "%d implausible (> +-2s from synced clock), %d negative-latency frames over %d frames" %
                   (self.timestamp_implausible, self.negative_latency_frames, len(latency)))
        # encoder exit -> last fragment of the frame: the stage the console calls "network". on this machine
        # it is the pacer's own time, so a regression in the pacing shows here before it shows on the console
        checks.add("network latency", bool(latency) and mean_ms <= MAX_NETWORK_LATENCY_MS and peak_ms <= MAX_NETWORK_LATENCY_PEAK_MS,
                   "avg %.2fms (need <= %.0f), max %.2fms (need <= %.0f) over %d frames" %
                   (mean_ms, MAX_NETWORK_LATENCY_MS, peak_ms, MAX_NETWORK_LATENCY_PEAK_MS, len(latency)))
        checks.add("keyframe flags", self.keyframe_flag_mismatches == 0,
                   "%d frames where flag and IDR content disagree; %d keyframes in %d frames" % (self.keyframe_flag_mismatches, self.keyframes, self.frames_complete))
        self._check_loss_recovery(checks, seconds)
        self._check_bitrate(checks, seconds)
        checks.add("packet sizes", self.oversize_packets == 0, "%d packets over %d bytes (the PS3 would truncate them); %d SINFO repeats, %d other ignored packets" %
                   (self.oversize_packets, PACKET_MAX, self.sinfo_repeats, self.other_packets))
        # the beacon must keep going while a stream runs, or the next PS3 (or this one after a reconnect)
        # never finds the server again. it reaches us twice a second here: 255.255.255.255 and the NIC's own
        # broadcast address both loop back.
        checks.add("beacon while streaming", self.beacons_during_session >= max(1, int(seconds) - 1),
                   "%d beacons in %.1fs (need >= %d)" % (self.beacons_during_session, seconds, max(1, int(seconds) - 1)))
        self._check_clock_resync(checks, seconds)
        # the PS3 stops listening the moment it sends STOP, so nobody but this test ever notices a server
        # that carries on encoding (and leaves the desktop at 1280x720)
        video_quiet = self.post_stop_last_video_ms is None or self.post_stop_last_video_ms <= POST_STOP_QUIET_MS
        audio_quiet = self.post_stop_last_audio_ms is None or self.post_stop_last_audio_ms <= POST_STOP_AUDIO_QUIET_MS
        checks.add("quiet after STOP", video_quiet and audio_quiet,
                   "%d video + %d audio packets in the %.1fs after STOP, last video %s (need <= %dms), last audio %s (need <= %dms)" %
                   (self.post_stop_video, self.post_stop_audio, POST_STOP_WATCH_S,
                    "none" if self.post_stop_last_video_ms is None else "%.0fms" % self.post_stop_last_video_ms,
                    POST_STOP_QUIET_MS,
                    "none" if self.post_stop_last_audio_ms is None else "%.0fms" % self.post_stop_last_audio_ms,
                    POST_STOP_AUDIO_QUIET_MS))
        checks.add("repeated PLAY", not self.replay_play_sent or self.sinfo_after_replay >= 1,
                   "PLAY repeated at t=%.1fs, %d SINFO came back (the server must answer it and NOT restart the "
                   "stream - a restart shows up as a frame gap)" % (self.duration_s / 2, self.sinfo_after_replay))
        checks.add("pad sent", self.pad_packets_sent >= PAD_RATE_HZ * max(0.0, self.duration_s - 0.5) and self.padmodes_sent >= 1,
                   "%d CP packets (60/s), %d PADMODE %s, KEY %s, CUSTOM 4 %s" %
                   (self.pad_packets_sent, self.padmodes_sent, self.padmode, ("sent" if self.key_sent else ("not sent" if self.key else "off (use --key)")),
                    "sent" if self.custom_sent else "not sent (duration < 6s)"))

    def _check_loss_recovery(self, checks: Checks, seconds: float) -> None:
        """SINFO's intra flag decides whether the console decodes on through a loss - so the stream had
        better be built the way the flag says. An intra-refresh stream carries one anchor IDR every few
        hundred frames (measured: 1 in 458); a keyframe stream one per sweep second (-g fps)."""
        if seconds < 2.5 or self.frames_complete < 60:
            checks.add("loss recovery", True, "run too short to judge the keyframe cadence")
            return
        if self.stream_self_heals:
            allowed = 1 + int(seconds / 4) + 1
            checks.add("loss recovery", self.keyframes <= allowed,
                       "SINFO says intra refresh: %d keyframes in %.1fs (need <= %d; a keyframe stream would send one per second)"
                       % (self.keyframes, seconds, allowed))
        else:
            needed = max(1, int(seconds / 2))
            checks.add("loss recovery", self.keyframes >= needed,
                       "SINFO says keyframes: %d in %.1fs (need >= %d, the encoder's -g is one sweep second)"
                       % (self.keyframes, seconds, needed))

    def _check_bitrate(self, checks: Checks, seconds: float) -> None:
        """What actually went over the wire, against the rate the server is configured for. The PS3's decoder
        is what the bitrate limits (38-40ms a frame at 11-13Mbit/s), so a silent return to the Windows
        original's 10Mbit/s is a regression - and one only the wire can see."""
        ceiling = self.expect_kbps * BITRATE_CEILING_PERCENT // 100
        windows = self.video_byte_rate.windows
        average_kbps = (self.video_bytes * 8 / seconds / 1000) if seconds > 0 else 0.0
        worst_kbps = (max(windows) * 8 / 1000) if windows else 0.0
        checks.add("video bitrate", average_kbps <= ceiling * 1.10 and worst_kbps <= ceiling * 1.35,
                   "avg %.0fkbit/s, worst second %.0fkbit/s, %d bytes in %.1fs (configured %dk, -maxrate %dk; "
                   "a still test picture undershoots by design, so only the ceiling is checked)" %
                   (average_kbps, worst_kbps, self.video_bytes, seconds, self.expect_kbps, ceiling))

    def _check_clock_resync(self, checks: Checks, seconds: float) -> None:
        """The 30s re-sync never fires inside a test run, so run_integration.sh shortens the interval: this is
        the only place the probe-between-packets path (and the server answering TIME mid-stream) is exercised."""
        rounds_due = int(seconds * 1000 // max(1, self.resync_interval_ms))
        if rounds_due < 1:
            checks.add("clock re-sync", True, "not exercised: interval %dms, session %.1fs (--resync-interval-ms shortens it)"
                       % (self.resync_interval_ms, seconds))
            return
        drift_ms = self.offset_drift_us / 1000
        checks.add("clock re-sync", self.resyncs_applied >= 1 and abs(drift_ms) <= MAX_CLOCK_DRIFT_MS,
                   "%d re-syncs applied in %.1fs (interval %dms, %d due), offset moved %.3fms (need <= %.0fms)" %
                   (self.resyncs_applied, seconds, self.resync_interval_ms, rounds_due, drift_ms, MAX_CLOCK_DRIFT_MS))

    def evaluate_audio(self, checks: Checks, allow_no_audio: bool) -> None:
        windows = self.audio_rate_window.windows
        average = self.audio_rate_window.average
        seconds = self.session_seconds or self.duration_s
        # AINFO is repeated once a second for the whole stream on purpose: the PS3 may still be finishing the
        # video handshake when the first one lands, and a missed announcement means no sound all session
        expected_ainfo = max(1, int(seconds) - 1)
        seen = self.ainfo_count >= expected_ainfo and self.audio_channels == 2 and self.audio_rate > 0
        checks.add("AINFO", seen or (allow_no_audio and self.ainfo_count == 0),
                   ("%d AINFO in %.1fs (need >= %d, one a second), %dHz %d channels%s" %
                    (self.ainfo_count, seconds, expected_ainfo, self.audio_rate, self.audio_channels,
                     ", inconsistent x%d" % self.ainfo_inconsistent if self.ainfo_inconsistent else ""))
                   if self.ainfo_count else "no AINFO at all" + (" (allowed)" if allow_no_audio else ""))
        checks.add("audio packets/s", (bool(windows) and 180 <= average <= 220) or (allow_no_audio and self.af_total == 0),
                   "%d AF packets, %d complete windows: avg %.1f/s (need ~200: 180..220), per second %s; %d before AINFO, %d lost" %
                   (self.af_total, len(windows), average, windows, self.af_before_ainfo, self.af_lost))
        checks.add("audio packets valid", self.af_invalid == 0 and self.af_size_mismatch == 0 and self.af_time_implausible == 0
                   and self.ainfo_inconsistent == 0 and self.af_before_ainfo == 0 and self.af_lost == 0,
                   "%d invalid (frames 1..%d, length), %d length mismatches, %d implausible stamps, %d before AINFO, %d lost; peak sample %d" %
                   (self.af_invalid, AUDIO_MAX_FRAMES, self.af_size_mismatch, self.af_time_implausible,
                    self.af_before_ainfo, self.af_lost, self.audio_peak))


# section: decoding what arrived (ffmpeg / ffprobe)

def read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def run_command(args: list[str], timeout: float = 60.0, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return -1, "", str(error)
    return completed.returncode, completed.stdout, completed.stderr


def ffprobe_video_stream(path: str) -> dict:
    code, out, err = run_command(["ffprobe", "-v", "error", "-show_streams", "-select_streams", "v:0", "-of", "json", path])
    if code != 0:
        return {"error": err.strip()[-200:] or "ffprobe failed"}
    try:
        streams = json.loads(out).get("streams") or []
    except ValueError:
        return {"error": "ffprobe printed no JSON"}
    return streams[0] if streams else {"error": "no video stream found"}


def extract_frame_png(h264_path: str, frame_index: int, png_path: str) -> str | None:
    """decodes the stream and writes frame `frame_index` (decode order) as PNG; the error text on failure"""
    code, _out, err = run_command(["ffmpeg", "-v", "error", "-y", "-i", h264_path, "-vf", "select=eq(n\\,%d)" % frame_index,
                                   "-frames:v", "1", "-fps_mode", "passthrough", png_path], timeout=120.0)
    if code != 0:
        return err.strip()[-300:] or "ffmpeg failed"
    if not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
        return "ffmpeg wrote no frame (stream shorter than %d frames?)" % (frame_index + 1)
    return None


def luma_via_signalstats(png_path: str) -> tuple[float, float] | None:
    """(mean, max) luma of the PNG via ffmpeg's signalstats; run in the file's directory so the lavfi graph needs no escaping"""
    code, out, _err = run_command(["ffprobe", "-v", "error", "-f", "lavfi", "-i", "movie=%s,signalstats" % os.path.basename(png_path),
                                   "-show_entries", "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.YMAX", "-of", "json"],
                                  cwd=os.path.dirname(png_path) or ".")
    if code != 0:
        return None
    try:
        tags = json.loads(out)["frames"][0]["tags"]
        return float(tags["lavfi.signalstats.YAVG"]), float(tags["lavfi.signalstats.YMAX"])
    except (ValueError, KeyError, IndexError):
        return None


def luma_via_gdkpixbuf(png_path: str) -> tuple[float, float] | None:
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(png_path)
    except Exception:   # noqa: BLE001 - no gi, no loader: the caller falls back
        return None
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels, stride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    return _luma_of_rows(pixels, width, height, channels, stride)


def _luma_of_rows(pixels: bytes, width: int, height: int, channels: int, stride: int) -> tuple[float, float]:
    total = 0.0
    peak = 0.0
    for row in range(height):
        line = pixels[row * stride:row * stride + width * channels]
        if channels >= 3:
            r, g, b = line[0::channels], line[1::channels], line[2::channels]
            total += 0.299 * sum(r) + 0.587 * sum(g) + 0.114 * sum(b)
            peak = max(peak, 0.299 * max(r) + 0.587 * max(g) + 0.114 * max(b))
        else:
            total += sum(line[0::channels])
            peak = max(peak, max(line[0::channels]))
    return total / (width * height), peak


def luma_via_plain_png(png_path: str) -> tuple[float, float] | None:
    """last resort: a minimal PNG decoder (8-bit grey/RGB/RGBA, filters 0-4, no interlace) - slow but dependency-free"""
    try:
        data = read_bytes(png_path)
    except OSError:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = 0
    channels = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or interlace != 0:
                return None
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour)
            if channels is None:
                return None
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if not width or not channels:
        return None
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    previous = bytearray(stride)
    rows = bytearray()
    offset = 0
    for _row in range(height):
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 0xFF
        rows += line
        previous = line
    return _luma_of_rows(bytes(rows), width, height, channels, stride)


def analyse_stream(fake: FakePs3, checks: Checks, keep: bool) -> None:
    path = fake.h264_path
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size == 0:
        checks.add("ffprobe", False, "no stream data received")
        return
    info = ffprobe_video_stream(path)
    checks.add("ffprobe", info.get("width") == 1280 and info.get("height") == 720 and info.get("codec_name") == "h264",
               info.get("error") or "%sx%s, codec %s, profile %s, level %s (%d bytes)" %
               (info.get("width"), info.get("height"), info.get("codec_name"), info.get("profile"), info.get("level"), size))

    # the entropy coder is not in SINFO and not in ffprobe's output - it is one bit in the PPS, and it is
    # what took the console's decode from 38-40ms a frame to 19-20ms. read it out of the stream itself.
    facts = bitstream_facts(read_bytes(path)[:262144])
    entropy = facts.get("entropy")
    checks.add("entropy coder", entropy == fake.expect_entropy or (fake.expect_entropy == "any" and entropy in ("cavlc", "cabac")),
               facts.get("error") or "PPS says %s, expected %s (--expect-entropy; CABAC costs the PS3 ~2x the decode time)"
               % (entropy, fake.expect_entropy))
    # SINFO's level is what the PS3 reserves decoder memory from; too small and the decoder is mis-sized
    # (it produced black), so it must cover the level the encoder actually wrote into the SPS.
    sinfo_level = fake.sinfo[2] if fake.sinfo else 0
    stream_level = facts.get("level")
    checks.add("SPS vs SINFO level", stream_level is not None and sinfo_level >= stream_level,
               "stream SPS says level %s, profile %s; SINFO promised %s (must cover it)" % (stream_level, facts.get("profile"), sinfo_level))

    frame_index = min(30, max(0, fake.frames_complete - 1))
    png_path = os.path.join(fake.out_dir, "fake_ps3_frame%d.png" % frame_index)
    error = extract_frame_png(path, frame_index, png_path)
    if not checks.add("decode frame %d" % frame_index, error is None, error or "PNG written: " + png_path):
        return

    luma = luma_via_signalstats(png_path)
    method = "signalstats"
    if luma is None:
        luma, method = luma_via_gdkpixbuf(png_path), "GdkPixbuf"
    if luma is None:
        luma, method = luma_via_plain_png(png_path), "plain PNG decode"
    if luma is None:
        checks.add("picture not black", False, "could not measure the PNG's luma")
    else:
        mean, peak = luma
        # the server's own test source (videotestsrc pattern=ball) is a small white ball on black: after the
        # limited->full range conversion its mean luma is ~0.3, so "not black" also accepts a bright spot
        checks.add("picture not black", mean > 10 or peak >= 128,
                   "mean luma %.1f, max %.0f via %s (need mean > 10 or max >= 128)" % (mean, peak, method))
    if not keep:
        try:
            os.remove(path)
        except OSError:
            pass


# section: MockServer - a Python stand-in for the real server, so this client can test itself
#
# speaks the server's side of the protocol from a real h264 file: beacons, answers TIME and PLAY (SINFO x3),
# paces VF fragments like StreamSender.SendAccessUnit, sends 5ms AF packets plus AINFO once a second, and
# records every CP / PADMODE / KEY / CUSTOM / STOP it receives instead of replaying it anywhere.

_EPOCH_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
_SERVER_START_US = int((time.time() - _EPOCH_2020) * 1_000_000)
_SERVER_START_MONOTONIC_NS = time.monotonic_ns()


def server_now_us() -> int:
    """the server's clock (clock.py): microseconds since 2020-01-01 UTC, ticking on the monotonic clock"""
    return _SERVER_START_US + (time.monotonic_ns() - _SERVER_START_MONOTONIC_NS) // 1000


def split_access_units(data: bytes) -> list[tuple[bytes, bool]]:
    """whole-file version of LiveAnnexBSplitter: SPS/PPS/SEI attach to the picture that follows; a picture NAL closes the unit"""
    starts = []
    pos = 0
    while True:
        index = data.find(b"\x00\x00\x01", pos)
        if index < 0 or index + 3 >= len(data):
            break
        nal_start = index - 1 if index > 0 and data[index - 1] == 0 else index
        starts.append((nal_start, data[index + 3] & 0x1F))
        pos = index + 3
    units = []
    unit_start = None
    has_picture = keyframe = False
    for nal_start, nal_type in starts:
        if unit_start is not None and has_picture:
            units.append((data[unit_start:nal_start], keyframe))
            unit_start, has_picture, keyframe = nal_start, False, False
        if unit_start is None:
            unit_start = nal_start
        if nal_type in (1, 5):
            has_picture = True
            keyframe = keyframe or nal_type == 5
    if unit_start is not None and has_picture:
        units.append((data[unit_start:], keyframe))   # a file ends; a live pipe would wait for the next NAL
    return units


def send_access_unit(sock: socket.socket, target, frame_id: int, data: bytes, keyframe: bool, capture_us: int, send_rate_kbps: int) -> None:
    """StreamSender.SendAccessUnit: 20-byte header, 1300-byte payloads, fragments paced at the send rate"""
    frag_count = (len(data) + FRAGMENT_PAYLOAD_BYTES - 1) // FRAGMENT_PAYLOAD_BYTES
    per_fragment_us = (FRAGMENT_HEADER_BYTES + FRAGMENT_PAYLOAD_BYTES) * 8 * 1000 // max(1, send_rate_kbps)
    start_us = server_now_us()
    for frag_index in range(frag_count):
        sleep_until_us(start_us + frag_index * per_fragment_us, clock=server_now_us)
        header = VF_HEADER.pack(b"VF", frame_id & 0xFFFFFFFF, frag_index, frag_count, 1 if keyframe else 0, PROTOCOL_VERSION, capture_us)
        sock.sendto(header + data[frag_index * FRAGMENT_PAYLOAD_BYTES:(frag_index + 1) * FRAGMENT_PAYLOAD_BYTES], target)


class MockServer:
    FPS = 60
    SEND_RATE_KBPS = DEFAULT_EXPECT_KBPS * 3   # live_streamer keeps the original's 3x ratio to the video rate
    AUDIO_RATE = 48000
    AUDIO_CHUNK_FRAMES = 240   # 5ms at 48kHz

    def __init__(self, video_path: str, server_port: int = SERVER_PORT, client_port: int = CLIENT_PORT, intra_refresh: int = 1):
        self.video_path = video_path
        self.server_port = server_port
        self.client_port = client_port
        self.intra_refresh = intra_refresh
        self.sock: socket.socket | None = None
        self._running = False
        self._streaming = False
        self._target = None
        self._stream_start_us = 0
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        # what the fake PS3 sent us, for the self-test's assertions
        self.cp_count = 0
        self.cp_lost = 0
        self.cp_last_id = -1
        self.cp_trip_us: list[int] = []
        self.cp_last_buttons = 0
        self.button_events: list[tuple[float, str, str]] = []   # (t since stream start, "pressed"/"released", names)
        self.stick_extremes = [0, 0, 0, 0]                        # max |lx| |ly| |rx| |ry| seen
        self.padmodes: list[str] = []
        self.keys: list[str] = []
        self.customs: list[int] = []
        self.stops = 0
        self.plays = 0
        self.frames_sent = 0
        self.audio_packets_sent = 0
        self.errors: list[str] = []

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        sock.bind(("0.0.0.0", self.server_port))
        sock.settimeout(0.2)
        self.sock = sock
        self._running = True
        for name, target in (("mock-beacon", self._run_beacon), ("mock-receive", self._run_receive)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._running = False
        self._streaming = False
        for thread in self._threads:
            thread.join(3.0)
        if self.sock is not None:
            self.sock.close()

    def _run_beacon(self) -> None:
        # the real server hits every interface's broadcast address. the mock stays on loopback on purpose: a
        # broadcast beacon on :38311 would make a real PS3 on the LAN connect to this mock instead of the server
        target = ("127.0.0.1", self.client_port)
        while self._running:
            try:
                self.sock.sendto(b"CELLSTREAM 1", target)
            except OSError:
                pass
            time.sleep(1.0)

    def _run_receive(self) -> None:
        while self._running:
            try:
                packet, sender = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            try:
                self._handle(packet, sender)
            except Exception as error:   # noqa: BLE001
                self.errors.append("receive: %r" % error)

    def _handle(self, packet: bytes, sender) -> None:
        if len(packet) >= PAD_PACKET_BYTES and packet[0] == 0x43 and packet[1] == 0x50:
            self._handle_pad(packet)
            return
        text = packet.decode("ascii", "replace")
        if text.startswith("TIME"):
            self.sock.sendto(("TIME %d" % server_now_us()).encode("ascii"), sender)
        elif text.startswith("PLAY"):
            self.plays += 1
            info = ("SINFO 1280 720 42 1 %d %d" % (self.FPS, self.intra_refresh)).encode("ascii")
            for _ in range(3):
                self.sock.sendto(info, sender)
            with self._lock:
                if self._streaming:
                    return   # a repeated PLAY: answered, not restarted
                self._streaming = True
                self._target = sender
                self._stream_start_us = server_now_us()
            for name, target in (("mock-video", self._run_video), ("mock-audio", self._run_audio)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)
        elif text.startswith("PADMODE "):
            self.padmodes.append(text[8:])
        elif text.startswith("KEY ") and len(packet) >= 5:
            self.keys.append(chr(packet[4]))
        elif text.startswith("CUSTOM "):
            self.customs.append(int(text[7:].strip()))
        elif text.startswith("STOP"):
            self.stops += 1
            self._streaming = False
        else:
            self.errors.append("unknown packet %r" % text[:40])

    def _handle_pad(self, packet: bytes) -> None:
        _magic, packet_id, buttons, lx, ly, rx, ry, sent_us = CP_PACKET.unpack_from(packet)
        if self.cp_last_id >= 0 and packet_id > self.cp_last_id + 1:
            self.cp_lost += packet_id - self.cp_last_id - 1
        self.cp_last_id = packet_id
        self.cp_count += 1
        self.cp_trip_us.append(server_now_us() - sent_us)
        for i, value in enumerate((lx, ly, rx, ry)):
            self.stick_extremes[i] = max(self.stick_extremes[i], abs(value))
        if buttons != self.cp_last_buttons:
            t = (server_now_us() - self._stream_start_us) / 1e6
            pressed = describe_buttons(buttons & ~self.cp_last_buttons)
            released = describe_buttons(self.cp_last_buttons & ~buttons)
            if pressed:
                self.button_events.append((t, "pressed", pressed))
            if released:
                self.button_events.append((t, "released", released))
            self.cp_last_buttons = buttons

    def _run_video(self) -> None:
        try:
            units = split_access_units(read_bytes(self.video_path))
            if not units:
                self.errors.append("video file has no access units")
                return
            start_us = server_now_us()
            frame_id = 0
            while self._streaming and self._running:
                sleep_until_us(start_us + frame_id * 1_000_000 // self.FPS, clock=server_now_us)
                data, keyframe = units[frame_id % len(units)]   # loops; the file starts with the IDR, so a wrap is a clean restart
                send_access_unit(self.sock, self._target, frame_id, data, keyframe, server_now_us(), self.SEND_RATE_KBPS)
                frame_id += 1
                self.frames_sent = frame_id
        except Exception as error:   # noqa: BLE001
            self.errors.append("video: %r" % error)

    def _run_audio(self) -> None:
        try:
            info = ("AINFO %d 2" % self.AUDIO_RATE).encode("ascii")
            chunk_us = self.AUDIO_CHUNK_FRAMES * 1_000_000 // self.AUDIO_RATE
            packets_per_second = 1_000_000 // chunk_us
            # a 440Hz tone, so the receiver's sample parsing has something to measure
            tone = array("h", [int(8000 * math.sin(2 * math.pi * 440 * n / self.AUDIO_RATE)) for n in range(self.AUDIO_RATE)])
            self.sock.sendto(info, self._target)
            start_us = server_now_us()
            packet_id = 0
            position = 0
            while self._streaming and self._running:
                sleep_until_us(start_us + packet_id * chunk_us, clock=server_now_us)
                frames = array("h")
                for n in range(self.AUDIO_CHUNK_FRAMES):
                    sample = tone[(position + n) % len(tone)]
                    frames.append(sample)
                    frames.append(sample)
                position += self.AUDIO_CHUNK_FRAMES
                if sys.byteorder == "little":
                    frames.byteswap()
                header = AF_HEADER.pack(b"AF", packet_id & 0xFFFFFFFF, self.AUDIO_CHUNK_FRAMES, server_now_us())
                self.sock.sendto(header + frames.tobytes(), self._target)
                if packet_id % packets_per_second == 0:
                    self.sock.sendto(info, self._target)
                packet_id += 1
                self.audio_packets_sent = packet_id
        except Exception as error:   # noqa: BLE001
            self.errors.append("audio: %r" % error)


def generate_fixture(path: str, seconds: float, kbps: int = DEFAULT_EXPECT_KBPS, entropy: str = DEFAULT_EXPECT_ENTROPY) -> str:
    """encodes a moving test picture with the server's own arguments (encoders.py): the CURRENT defaults -
    6 Mbit/s CAVLC, and -g as the intra-refresh SWEEP (60), not the Windows original's 10 Mbit/s CABAC and
    -g 216000, which measured as no refresh at all. x264 when there is no NVIDIA card."""
    fps = 60
    rate = ["-b:v", "%dk" % kbps, "-maxrate", "%dk" % (kbps * 140 // 100), "-bufsize", "%dk" % (kbps * 250 // 1000), "-bf", "0", "-refs", "1"]
    coder = ["-coder", entropy] if entropy in ("cabac", "cavlc") else []
    source = ["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=%d:duration=%.1f" % (fps, seconds)]
    nvenc = source + ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-rc", "vbr", "-pix_fmt", "yuv420p"] + coder + \
        ["-delay", "0"] + rate + \
        ["-g", str(fps), "-intra-refresh", "1", "-single-slice-intra-refresh", "1", "-color_range", "tv", "-colorspace", "bt709",
         "-forced-idr", "1", "-f", "h264", path]
    x264 = source + ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                     "-x264-params", "sliced-threads=0:slices=1:intra-refresh=1"] + coder + rate + ["-g", str(fps), "-f", "h264", path]
    for name, args in (("h264_nvenc", nvenc), ("libx264", x264)):
        code, _out, err = run_command(["ffmpeg"] + args, timeout=120.0)
        if code == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return name
        log("fixture with %s failed: %s" % (name, err.strip()[-200:]))
    raise RuntimeError("ffmpeg could not encode the fixture")


def _make_fake_ps3(options, out_dir: str, key: str | None) -> FakePs3:
    """One place where the command line becomes a client, so the self-test and the real run stay identical."""
    return FakePs3(options.duration, options.padmode, out_dir, key, options.beacon_timeout, options.client_port,
                   resync_interval_ms=getattr(options, "resync_interval_ms", CLOCK_RESYNC_INTERVAL_MS),
                   expect_kbps=getattr(options, "expect_kbps", DEFAULT_EXPECT_KBPS),
                   expect_entropy=getattr(options, "expect_entropy", DEFAULT_EXPECT_ENTROPY))


def run_self_test(options, checks: Checks) -> FakePs3 | None:
    out_dir = options.out or tempfile.mkdtemp(prefix="fake_ps3_selftest_")
    os.makedirs(out_dir, exist_ok=True)
    fixture = os.path.join(out_dir, "mock_source.h264")
    expect_entropy = getattr(options, "expect_entropy", DEFAULT_EXPECT_ENTROPY)
    encoder = generate_fixture(fixture, options.duration + 3.0, getattr(options, "expect_kbps", DEFAULT_EXPECT_KBPS),
                               "cavlc" if expect_entropy == "any" else expect_entropy)
    units = split_access_units(read_bytes(fixture))
    log("self-test: fixture %s via %s, %d access units, %d bytes" % (fixture, encoder, len(units), os.path.getsize(fixture)))
    checks.add("fixture", len(units) >= 60 and units[0][1] and 7 in nal_types(units[0][0]),
               "%d AUs via %s, first AU keyframe=%s NAL types %s" % (len(units), encoder, units[0][1] if units else None, nal_types(units[0][0]) if units else []))

    mock = MockServer(fixture, options.server_port, options.client_port)
    try:
        mock.start()
    except OSError as error:
        checks.add("mock server", False, "bind :%d failed: %s - is the real server running? use --server-port/--client-port" % (options.server_port, error))
        return None
    key = options.key if options.key is not None else "a"   # the mock only records the key, so the script's KEY a is safe here
    fake = _make_fake_ps3(options, out_dir, key)
    try:
        fake.run(checks)
        time.sleep(0.3)   # let the STOPs land before reading the mock's tally
    finally:
        mock.stop()
    fake.evaluate_audio(checks, options.allow_no_audio)
    analyse_stream(fake, checks, options.keep)

    # the mock's view: did the fake PS3 say what the script says it should, when it should?
    expected_cp = PAD_RATE_HZ * options.duration
    checks.add("mock: CP rate", 0.9 * expected_cp <= mock.cp_count <= 1.1 * expected_cp + PAD_RATE_HZ and mock.cp_lost == 0,
               "%d CP packets in %.1fs (expected ~%d), %d lost" % (mock.cp_count, options.duration, expected_cp, mock.cp_lost))
    trips = sorted(mock.cp_trip_us)
    median = trips[len(trips) // 2] if trips else None
    checks.add("mock: CP send time", median is not None and -5000 <= median <= 20000 and trips[0] >= -20000,
               "trip PS3->server median %.2fms, min %.2fms, max %.2fms (the CP stamp is in the server's clock)" %
               ((median or 0) / 1000, (trips[0] if trips else 0) / 1000, (trips[-1] if trips else 0) / 1000))

    def event_time(kind: str, name: str):
        for t, what, names in mock.button_events:
            if what == kind and name in names.split("+"):
                return t
        return None

    if options.padmode == "gamepad":
        cross_down, cross_up = event_time("pressed", "cross"), event_time("released", "cross")
        checks.add("mock: cross 1s..2s", cross_down is not None and cross_up is not None and abs(cross_down - 1.0) < 0.25 and abs(cross_up - 2.0) < 0.25,
                   "pressed at %s, released at %s (want 1.0 / 2.0 +-0.25)" % (cross_down, cross_up))
        checks.add("mock: sticks sweep", mock.stick_extremes[0] >= 120 and mock.stick_extremes[1] >= 120 and mock.stick_extremes[2] >= 90,
                   "max |lx| %d |ly| %d |rx| %d |ry| %d" % tuple(mock.stick_extremes))
    else:
        checks.add("mock: no cross in mouse mode", event_time("pressed", "cross") is None and max(mock.stick_extremes) <= 15,
                   "cross pressed at %s, stick max %s (mouse mode must not click or move)" % (event_time("pressed", "cross"), max(mock.stick_extremes)))
    l1_down, l1_up = event_time("pressed", "L1"), event_time("released", "L1")
    checks.add("mock: L1 3s..4s", l1_down is not None and l1_up is not None and abs(l1_down - 3.0) < 0.25 and abs(l1_up - 4.0) < 0.25,
               "pressed at %s, released at %s (want 3.0 / 4.0 +-0.25)" % (l1_down, l1_up))
    wanted_mode = options.padmode
    checks.add("mock: PADMODE", len(mock.padmodes) >= int(options.duration) - 1 and all(mode == wanted_mode for mode in mock.padmodes),
               "%d x %s" % (len(mock.padmodes), sorted(set(mock.padmodes))))
    checks.add("mock: KEY", mock.keys == [key], "received %r (want [%r] at t=5s)" % (mock.keys, key))
    checks.add("mock: CUSTOM 4", mock.customs == [4], "received %r (want [4] at t=6s)" % mock.customs)
    checks.add("mock: STOP", mock.stops >= 1, "%d STOP received, %d PLAY" % (mock.stops, mock.plays))
    checks.add("mock: clean", not mock.errors, "; ".join(mock.errors) if mock.errors else "%d frames and %d audio packets sent, no errors" % (mock.frames_sent, mock.audio_packets_sent))
    if not options.keep:
        try:
            os.remove(fixture)
        except OSError:
            pass
    return fake


# section: command line

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="stands in for the PS3's cell-stream app and checks what the server sends")
    parser.add_argument("--duration", type=float, default=8.0, help="seconds to stream after SINFO (default 8; the script needs >= 7)")
    parser.add_argument("--padmode", choices=("gamepad", "mouse"), default="gamepad", help="PADMODE to announce (mouse keeps its hands off the real desktop)")
    parser.add_argument("--keep", action="store_true", help="keep the received .h264 file")
    parser.add_argument("--out", help="directory for the .h264 and the PNG (default: a fresh temp dir)")
    parser.add_argument("--key", help="send 'KEY <char>' at t=5s. off by default: the server TYPES it into whatever window has focus")
    parser.add_argument("--beacon-timeout", type=float, default=30.0, help="seconds to wait for the CELLSTREAM beacon (default 30)")
    parser.add_argument("--client-port", type=int, default=CLIENT_PORT, help="port to bind (the PS3 uses %d)" % CLIENT_PORT)
    parser.add_argument("--server-port", type=int, default=SERVER_PORT, help="self-test only: port the mock server binds")
    parser.add_argument("--allow-no-audio", action="store_true", help="do not fail when the server sends no audio (no capture device)")
    parser.add_argument("--resync-interval-ms", type=int, default=CLOCK_RESYNC_INTERVAL_MS,
                        help="clock re-sync interval (the console uses %d; shorten it to exercise the path inside a short run)" % CLOCK_RESYNC_INTERVAL_MS)
    parser.add_argument("--expect-kbps", type=int, default=DEFAULT_EXPECT_KBPS,
                        help="the server's configured video bitrate (default %d); the wire average may not pass its -maxrate" % DEFAULT_EXPECT_KBPS)
    parser.add_argument("--expect-entropy", choices=("cavlc", "cabac", "any"), default=DEFAULT_EXPECT_ENTROPY,
                        help="entropy coder the PPS must announce (default %s - what the PS3's decoder can keep up with)" % DEFAULT_EXPECT_ENTROPY)
    parser.add_argument("--self-test", action="store_true", help="run against the in-file MockServer instead of a real server")
    options = parser.parse_args(argv)

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg and ffprobe are required", file=sys.stderr)
        return 1
    if options.duration < 7.0:
        log("note: --duration below 7s skips part of the script (cross 1-2s, L1 3-4s, KEY 5s, CUSTOM 6s)")
    if options.padmode == "mouse":
        log("mouse mode: the server drives the REAL pointer and keyboard - no cross press, sticks kept inside the dead zone")

    checks = Checks()
    if options.self_test:
        fake = run_self_test(options, checks)
    else:
        out_dir = options.out or tempfile.mkdtemp(prefix="fake_ps3_")
        fake = _make_fake_ps3(options, out_dir, options.key)
        fake.run(checks)
        if fake.sinfo is not None:
            fake.evaluate_audio(checks, options.allow_no_audio)
            analyse_stream(fake, checks, options.keep)
    if fake is not None:
        log("output directory: " + fake.out_dir + (" (h264 kept)" if options.keep else ""))
    checks.print_report()
    return 0 if not checks.failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
