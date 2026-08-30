"""Tests for teecellstream.audio - safe on a live desktop: they only read the sink monitor and talk to 127.0.0.1.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_audio -v
"""

import atexit
import os
import re
import shutil
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest

# keep the tests' noise out of the user's real settings and log (both modules read these at import)
_TMP = tempfile.mkdtemp(prefix="tee-cst-audio-test-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)   # leave nothing behind on the developer's machine
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))
# ffmpeg -f pulse finds the (PipeWire) pulse server through XDG_RUNTIME_DIR; an agent shell may lack it
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())

from teecellstream import audio, childproc, log, protocol   # noqa: E402
from teecellstream.clock import now_us   # noqa: E402

FFMPEG = shutil.which("ffmpeg")
PULSE_SOCKET = os.path.join(os.environ["XDG_RUNTIME_DIR"], "pulse", "native")
HAVE_PULSE = FFMPEG is not None and os.path.exists(PULSE_SOCKET)
SKIP_REASON = "needs ffmpeg and a pulse/pipewire server at " + PULSE_SOCKET

FRAME_BYTES = 4                     # s16 stereo
CHUNK_FRAMES = 240                  # 5ms at 48kHz
AF_PACKET_BYTES = 16 + CHUNK_FRAMES * FRAME_BYTES


def _log_text() -> str:
    try:
        with open(log.LOG_PATH, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _ffmpeg_wrapper() -> str:
    """A stand-in ffmpeg: hangs for 'tee-hang-source', fails for 'tee-exit-source', else is the real thing."""
    path = os.path.join(_TMP, "ffmpeg-wrapper.sh")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         "case \"$*\" in\n"
                         "  *tee-hang-source*) exec sleep 30 ;;\n"
                         "  *tee-exit-source*) echo 'tee: kaputt' >&2; exit 1 ;;\n"
                         "esac\n"
                         "exec %s \"$@\"\n" % FFMPEG)
        os.chmod(path, 0o755)
    return path


def _ffmpeg_lister(name: str, body: str) -> str:
    """A stand-in ffmpeg whose only job is to print a fixed "-sources pulse" listing (or to hang)."""
    path = os.path.join(_TMP, "ffmpeg-lister-%s.sh" % name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n%s\n" % body)
    os.chmod(path, 0o755)
    return path


class _sources:
    """Temporarily replaces the pulse source list the capture tries (and the live enumeration behind it)."""

    def __init__(self, sources, listed=()):
        self.sources = sources
        self.listed = list(listed)

    def __enter__(self):
        self.original = audio.CAPTURE_SOURCES
        self.original_list = audio.list_pulse_sources
        audio.CAPTURE_SOURCES = self.sources
        audio.list_pulse_sources = lambda ffmpeg_path: list(self.listed)

    def __exit__(self, *exc):
        audio.CAPTURE_SOURCES = self.original
        audio.list_pulse_sources = self.original_list


class _env:
    """Temporarily sets environment variables (inherited by the ffmpeg child)."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {key: os.environ.get(key) for key in self.values}
        os.environ.update(self.values)

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _FakeProcess:
    """Just enough Popen for AudioCapture._run_reader: a pipe to read and a poll() that says 'alive'."""

    def __init__(self, read_fd):
        self.stdout = os.fdopen(read_fd, "rb", 0)
        self.stderr = None
        self.returncode = None

    def poll(self):
        return None


def _process_gone(process) -> bool:
    if process is None:
        return True
    if process.poll() is None:
        return False
    try:
        os.kill(process.pid, 0)      # reaped by wait(): the pid no longer exists (barring reuse)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True                      # zombie-free: poll() reaped it; a live pid with the same number is reuse


# ---------------------------------------------------------------------------- pure layout, no ffmpeg

class HeaderLayoutTest(unittest.TestCase):
    def test_af_header_fields(self):
        samples = bytes(range(8))     # two frames of "sound"
        packet = audio.build_af_packet(0x01020304, 2, 0x1122334455667788, samples)
        self.assertEqual(len(packet), 16 + 8)
        self.assertEqual(packet[0], ord("A"))
        self.assertEqual(packet[1], ord("F"))
        self.assertEqual(packet[2:6], bytes([0x01, 0x02, 0x03, 0x04]))           # packetId u32 BE
        self.assertEqual(packet[6:8], bytes([0x00, 0x02]))                       # frameCount u16 BE
        self.assertEqual(packet[8:16], bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]))   # captureUs u64 BE
        self.assertEqual(packet[16:], samples)                                   # payload passes through untouched
        # the same decode the PS3 does (stream.c handleAudioPacket)
        frames = (packet[6] << 8) | packet[7]
        self.assertEqual(frames, 2)
        self.assertLessEqual(frames, protocol.AUDIO_MAX_FRAMES_PER_PACKET)

    def test_packet_id_wraps_to_u32(self):
        packet = audio.build_af_packet((1 << 32) + 5, 240, 0, bytes(240 * 4))
        self.assertEqual(struct.unpack(">I", packet[2:6])[0], 5)
        self.assertEqual(len(packet), AF_PACKET_BYTES)

    def test_ainfo_text(self):
        self.assertEqual(audio.build_ainfo(48000), b"AINFO 48000 2")

    def test_chunk_constants_match_protocol(self):
        self.assertEqual(audio.HEADER_BYTES, 16)
        self.assertEqual(audio.MAX_FRAMES_PER_PACKET, 512)
        self.assertEqual(audio.CHUNK_MS, 5)
        self.assertEqual(min(48000 * 5 // 1000, 512), CHUNK_FRAMES)

    def test_every_chunk_fits_the_ps3s_receive_buffer(self):
        # AUDIO_MAX_FRAMES=512 alone is not the limit: the PS3 recv()s into 1500 bytes (net-common.h
        # PACKET_MAX) and handleAudioPacket then wants packetBytes >= 16 + frames*4, so a 512-frame packet
        # (2064 bytes) is truncated on arrival and dropped WHOLE - all audio lost, not merely capped.
        self.assertEqual(audio.PS3_PACKET_MAX_BYTES, 1500)
        for rate in (44100, 48000, 96000, 192000):
            frames = min(rate * audio.CHUNK_MS // 1000, audio.MAX_FRAMES_PER_PACKET,
                         (audio.PS3_PACKET_MAX_BYTES - audio.HEADER_BYTES) // 4)
            packet = audio.build_af_packet(0, frames, 0, bytes(frames * 4))
            self.assertLessEqual(len(packet), audio.PS3_PACKET_MAX_BYTES, "%dHz overruns the PS3's buffer" % rate)
            self.assertGreater(frames, 0)
            self.assertLessEqual(frames, audio.MAX_FRAMES_PER_PACKET)
            self.assertGreaterEqual(len(packet), audio.HEADER_BYTES + frames * 4)   # the PS3's own check
        self.assertEqual(min(48000 * 5 // 1000, 512, (1500 - 16) // 4), CHUNK_FRAMES)


class SourceChoiceTest(unittest.TestCase):
    """Which pulse source we record. Measured with pw-dump: pulse answers a name it cannot resolve with the
    default CAPTURE device, so a non-monitor name in this list means the PS3 plays the room's microphone."""

    def test_capture_sources_are_monitors_only(self):
        self.assertNotIn("default", audio.CAPTURE_SOURCES)
        for source in audio.CAPTURE_SOURCES:
            self.assertTrue(source == "@DEFAULT_MONITOR@" or source.endswith(audio.MONITOR_SUFFIX),
                            "%r is not a monitor: it would record an input device" % source)

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_list_pulse_sources_parses_ffmpeg_output(self):
        lister = _ffmpeg_lister("ok", "echo 'Auto-detected sources for pulse:'\n"
                                      "echo '  obs_mic [OBS Mic] (none)'\n"
                                      "echo '* alsa_in.thing [Line In] (none)'\n"
                                      "echo '  alsa_out.thing.monitor [Monitor of Thing] (none)'")
        self.assertEqual(audio.list_pulse_sources(lister),
                         ["obs_mic", "alsa_in.thing", "alsa_out.thing.monitor"])

    def test_list_pulse_sources_survives_a_missing_or_wedged_ffmpeg(self):
        self.assertEqual(audio.list_pulse_sources("/nonexistent/tee-ffmpeg"), [])
        lister = _ffmpeg_lister("hang", "exec sleep 30")
        original = audio.SOURCE_LIST_TIMEOUT_S
        audio.SOURCE_LIST_TIMEOUT_S = 0.3
        try:
            started = time.monotonic()
            self.assertEqual(audio.list_pulse_sources(lister), [])
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            audio.SOURCE_LIST_TIMEOUT_S = original

    def test_no_monitor_at_all_means_video_only_never_the_microphone(self):
        capture = audio.AudioCapture("/nonexistent/tee-ffmpeg")
        with _sources(("@DEFAULT_MONITOR@",), listed=["obs_mic", "alsa_input.usb-Some_Mic.analog-stereo"]):
            log_before = len(_log_text())
            self.assertEqual(capture._sources_to_try(), [],
                             "with no playback device every magic name resolves to a capture device")
            self.assertFalse(capture.start())
        text = _log_text()[log_before:]
        self.assertIn("audio: no monitor source present (no playback device), streaming video only", text)
        self.assertNotIn("capturing the speakers", text)

    def test_named_monitors_are_appended_behind_the_magic_names(self):
        capture = audio.AudioCapture("/nonexistent/tee-ffmpeg")
        with _sources(("@DEFAULT_MONITOR@", "@DEFAULT_SINK@.monitor"),
                      listed=["alsa_input.mic", "alsa_output.a.monitor", "alsa_output.b.monitor"]):
            self.assertEqual(capture._sources_to_try(),
                             ["@DEFAULT_MONITOR@", "@DEFAULT_SINK@.monitor",
                              "alsa_output.a.monitor", "alsa_output.b.monitor"])

    def test_an_unaskable_ffmpeg_does_not_disable_audio(self):
        # a listing we could not obtain proves nothing; only a listing WITH sources and WITHOUT monitors does
        capture = audio.AudioCapture("/nonexistent/tee-ffmpeg")
        with _sources(("@DEFAULT_MONITOR@",), listed=[]):
            self.assertEqual(capture._sources_to_try(), ["@DEFAULT_MONITOR@"])


# ---------------------------------------------------------------------------- the ring, fed by hand

class RingTest(unittest.TestCase):
    def setUp(self):
        self.capture = audio.AudioCapture(ffmpeg_path="/nonexistent/ffmpeg")

    def test_read_before_start_is_silence(self):
        data = self.capture.read(240)
        self.assertEqual(data, bytes(960))
        self.assertEqual(self.capture.buffered_frames, 0)

    def test_short_read_is_padded_and_counts_real_frames(self):
        self.capture._write(b"\x01\x02" * 2 * 100)      # 100 frames
        data, real = self.capture.read_frames(240)
        self.assertEqual(real, 100)
        self.assertEqual(len(data), 960)
        self.assertEqual(data[:400], b"\x01\x02" * 200)
        self.assertEqual(data[400:], bytes(560))
        self.assertEqual(self.capture.buffered_frames, 0)

    def test_fifo_order(self):
        self.capture._write(bytes([1, 1, 1, 1]) * 10)
        self.capture._write(bytes([2, 2, 2, 2]) * 10)
        data, real = self.capture.read_frames(15)
        self.assertEqual(real, 15)
        self.assertEqual(data, bytes([1] * 40) + bytes([2] * 20))
        self.assertEqual(self.capture.buffered_frames, 5)

    def test_latency_guard_trims_to_20ms_once_over_60ms(self):
        before = _log_text().count("latency guard")
        for _ in range(7):                                  # 7 x 10ms = 70ms > 60ms
            self.capture._write(bytes(480 * 4))
        self.assertEqual(self.capture.buffered_frames, 960)   # back to 20ms
        self.assertEqual(self.capture.dropped_frames, 7 * 480 - 960)
        for _ in range(5):                                  # trips again ...
            self.capture._write(bytes(480 * 4))
        self.assertEqual(self.capture.buffered_frames, 960)
        self.assertEqual(_log_text().count("latency guard"), before + 1)   # ... but logs only once per session

    def test_buffer_never_exceeds_guard_between_writes(self):
        for _ in range(6):
            self.capture._write(bytes(480 * 4))
            self.assertLessEqual(self.capture.buffered_frames, 2880)

    def test_guard_trims_whole_frames_only(self):
        # a pipe read may end mid-frame; the guard must still drop whole frames, or the head shifts by a
        # byte or three and every later sample is decoded with the wrong byte order (noise until stop)
        frame = bytes([0x10, 0x11, 0x12, 0x13])
        self.capture._write(frame * (7 * 480) + frame[:3])       # 70ms and three bytes of the next frame
        self.assertEqual(self.capture.buffered_frames, 960)      # the partial frame does not count
        self.assertEqual(self.capture.dropped_frames, 7 * 480 - 960)
        data, real = self.capture.read_frames(2)
        self.assertEqual(real, 2)
        self.assertEqual(data, frame * 2, "the head must stay on a frame boundary")
        self.capture._write(frame[3:])                           # the rest of that frame arrives
        data, real = self.capture.read_frames(self.capture.buffered_frames)
        self.assertEqual(real, 959)
        self.assertEqual(data, frame * 959)

    def test_guard_logs_outside_the_ring_lock(self):
        # the sender takes this lock every 5ms; log.write goes to a file and may rotate 2MiB away
        seen = []
        original = audio.log.write

        def spy(message):
            if "latency guard" in message:
                free = self.capture._ring_gate.acquire(blocking=False)
                seen.append(free)
                if free:
                    self.capture._ring_gate.release()
            return original(message)

        audio.log.write = spy
        try:
            for _ in range(7):
                self.capture._write(bytes(480 * 4))
        finally:
            audio.log.write = original
        self.assertEqual(seen, [True], "the guard must not hold the ring lock while writing the log")

    def test_bytes_from_an_abandoned_probe_never_reach_the_next_session(self):
        # a source that was given up on can still have a chunk in flight; it must not land in the ring the
        # next source is about to fill, nor make that source look like it delivered
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(write_fd))
        ghost = _FakeProcess(read_fd)
        self.capture._capturing = True
        self.capture._process = object()        # start() has already moved on to another ffmpeg
        self.capture._first_data.clear()
        reader = threading.Thread(target=self.capture._run_reader, args=(ghost,), daemon=True)
        reader.start()
        os.write(write_fd, bytes(4 * 100))
        reader.join(2.0)
        self.capture._capturing = False
        self.assertFalse(reader.is_alive())
        self.assertEqual(self.capture.buffered_frames, 0)
        self.assertFalse(self.capture._first_data.is_set())

    def test_ring_overflow_trims_whole_frames_only(self):
        frame = bytes([0x20, 0x21, 0x22, 0x23])
        self.capture._ring = bytearray(frame * (48000 - 1) + frame[:1])   # 1s minus a frame, plus a stray byte
        self.capture._write(frame[1:] + frame * 480 + frame[:2])         # spills over the 1s capacity by 1922 bytes
        data, real = self.capture.read_frames(3)
        self.assertEqual(data, frame * 3, "the overflow trim must not shift the head either")
        self.assertEqual(self.capture.buffered_frames, 960 - 3)          # the guard then held it to 20ms


# ---------------------------------------------------------------------------- real capture

@unittest.skipUnless(HAVE_PULSE, SKIP_REASON)
class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.capture = audio.AudioCapture(FFMPEG)

    def tearDown(self):
        self.capture.stop()

    def test_start_fills_ring_within_guard(self):
        self.assertTrue(self.capture.start())
        self.assertTrue(self.capture.is_capturing)
        self.assertEqual(self.capture.sample_rate, 48000)
        time.sleep(1.0)
        buffered = self.capture.buffered_frames
        # nobody drained it for a second, so the guard must have held it: > 0 and <= 60ms worth
        self.assertGreater(buffered, 0)
        self.assertLessEqual(buffered, 2880)
        self.assertGreater(self.capture.dropped_frames, 0)     # the guard trimmed (PipeWire streams even silence)
        data = self.capture.read(240)
        self.assertEqual(len(data), 960)
        self.assertIn("capturing the speakers", _log_text())

    def test_real_capture_delivers_48000_frames_per_second(self):
        self.assertTrue(self.capture.start())
        while self.capture.buffered_frames == 0:          # skip ffmpeg's cold start
            time.sleep(0.01)
        self.capture.read_frames(48000)
        started = time.monotonic()
        drained = 0
        while time.monotonic() - started < 3.0:
            drained += self.capture.read_frames(4096)[1]   # drain often: the guard must never trip
            time.sleep(0.01)
        elapsed = time.monotonic() - started
        drained += self.capture.read_frames(48000)[1]
        rate = drained / elapsed
        self.assertAlmostEqual(rate, audio.SAMPLE_RATE, delta=800, msg="captured %.0f frames/s" % rate)
        self.assertEqual(self.capture.dropped_frames, 0, "a drained ring must not trip the latency guard")

    def test_stop_ends_quickly_and_ffmpeg_is_gone(self):
        self.assertTrue(self.capture.start())
        process = self.capture._process
        self.assertIsNotNone(process)
        started = time.monotonic()
        self.capture.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(_process_gone(process))
        self.assertFalse(self.capture.is_capturing)

    def test_falls_back_to_the_next_monitor_when_one_delivers_nothing(self):
        # PipeWire's pulse server maps an unknown source NAME to the default (measured), so a real "no bytes"
        # failure is simulated: the wrapper hangs on the first source and is the real ffmpeg on the second.
        capture = audio.AudioCapture(_ffmpeg_wrapper())
        self.addCleanup(capture.stop)
        with _sources(("tee-hang-source", "@DEFAULT_MONITOR@")):
            started = time.monotonic()
            self.assertTrue(capture.start())
            elapsed = time.monotonic() - started
        self.assertEqual(capture.source, "@DEFAULT_MONITOR@")
        self.assertGreaterEqual(elapsed, audio.START_TIMEOUT_S)
        self.assertLess(elapsed, audio.START_TIMEOUT_S + 2.5)
        text = _log_text()
        self.assertIn("tee-hang-source does not start (no data within 3.0s), trying @DEFAULT_MONITOR@", text)
        time.sleep(0.2)
        self.assertGreater(capture.buffered_frames, 0)
        self.assertTrue(capture.is_capturing)

    def test_falls_back_to_the_next_monitor_when_ffmpeg_exits_early(self):
        capture = audio.AudioCapture(_ffmpeg_wrapper())
        self.addCleanup(capture.stop)
        with _sources(("tee-exit-source", "@DEFAULT_MONITOR@")):
            started = time.monotonic()
            self.assertTrue(capture.start())
            elapsed = time.monotonic() - started
        self.assertEqual(capture.source, "@DEFAULT_MONITOR@")
        self.assertLess(elapsed, 2.0, "an early exit must not wait out the start timeout")
        text = _log_text()
        self.assertIn("tee-exit-source does not start (ffmpeg exited with 1, tee: kaputt), trying @DEFAULT_MONITOR@", text)

    def test_a_list_of_hanging_sources_does_not_multiply_the_stall(self):
        # start() runs on server.py's receive thread under the stream lock: while it waits, the PS3's pad,
        # its TIME probes and its STOP are not read (STREAM_STARTUP_GRACE_MS is 10s). Only the first source
        # is worth the full wait; the rest get FALLBACK_TIMEOUT_S, which is ample for a healthy monitor.
        capture = audio.AudioCapture(_ffmpeg_wrapper())
        self.addCleanup(capture.stop)
        with _sources(("tee-hang-source-a", "tee-hang-source-b", "tee-hang-source-c")):
            started = time.monotonic()
            self.assertFalse(capture.start())
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, audio.START_TIMEOUT_S)
        budget = audio.START_TIMEOUT_S + 2 * audio.FALLBACK_TIMEOUT_S
        self.assertLess(elapsed, budget + 1.5, "three hanging sources took %.1fs" % elapsed)

    def test_at_most_five_sources_are_probed(self):
        capture = audio.AudioCapture("/nonexistent/tee-ffmpeg")
        with _sources(("@DEFAULT_MONITOR@", "@DEFAULT_SINK@.monitor"),
                      listed=["m%d.monitor" % index for index in range(20)]):
            self.assertEqual(len(capture._sources_to_try()), audio.MAX_SOURCES_TO_TRY)

    def test_no_pulse_server_means_video_only(self):
        # real ffmpeg, but no server to talk to: both sources fail at once and start() says so
        with _env(PULSE_SERVER="unix:/nonexistent/tee-pulse-socket"):
            started = time.monotonic()
            self.assertFalse(self.capture.start())
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertIsNone(self.capture.source)
        self.assertFalse(self.capture.is_capturing)
        text = _log_text()
        self.assertIn("audio: could not open the speakers, streaming video only (@DEFAULT_MONITOR@: ffmpeg exited with", text)
        # ffmpeg's last words are in the log (the exact wording is ffmpeg's: "No such process" on 8.0.1)
        self.assertRegex(text, r"@DEFAULT_MONITOR@: ffmpeg exited with \d+, \S.*; @DEFAULT_SINK@\.monitor: ffmpeg exited with \d+, \S")
        self.assertEqual(self.capture.read(240), bytes(960))


# ---------------------------------------------------------------------------- what the PS3 unpacks

class _SineCapture(audio.AudioCapture):
    """The real ffmpeg and the real pipe, but a known waveform instead of the speakers (no pulse needed).

    pan puts the same 1kHz tone on both channels with opposite sign, so left/right cannot be confused.
    """

    def _sources_to_try(self):
        return ["tee-sine"]

    def _capture_args(self, source):
        return [self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-re",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=%d:duration=6" % audio.SAMPLE_RATE,
                "-af", "pan=stereo|c0=0.5*c0|c1=-0.5*c0",
                "-ac", str(audio.CHANNELS), "-ar", str(audio.SAMPLE_RATE), "-f", "s16be", "pipe:1"]


@unittest.skipUnless(FFMPEG is not None, "needs ffmpeg")
class PayloadLayoutTest(unittest.TestCase):
    """The payload, decoded exactly the way stream.c handleAudioPacket decodes it."""

    def test_payload_is_big_endian_s16_left_then_right_at_48khz(self):
        capture = _SineCapture(FFMPEG)
        self.addCleanup(capture.stop)
        self.assertTrue(capture.start())
        # drain as it arrives: letting 100ms pile up would trip the latency guard and cut the tone in half
        frames = 4800                                   # 100ms
        collected = bytearray()
        deadline = time.monotonic() + 8.0
        while len(collected) < frames * 4 and time.monotonic() < deadline:
            chunk, real = capture.read_frames(2048)
            collected += chunk[:real * 4]
            time.sleep(0.005)
        self.assertEqual(capture.dropped_frames, 0, "the guard trimmed: the tone would have a hole in it")
        data = bytes(collected[:frames * 4])
        self.assertEqual(len(data), frames * 4)
        packet = audio.build_af_packet(7, frames, 123, data)

        # the PS3's own acceptance test (stream.c: handleAudioPacket)
        self.assertGreater(len(packet), protocol.AUDIO_HEADER_BYTES)
        count = (packet[6] << 8) | packet[7]
        self.assertTrue(0 < count <= protocol.AUDIO_MAX_FRAMES_PER_PACKET or count == frames)
        self.assertGreaterEqual(len(packet), protocol.AUDIO_HEADER_BYTES + frames * 4)

        values = struct.unpack(">%dh" % (frames * 2), data)
        left, right = values[0::2], values[1::2]
        peak = max(abs(value) for value in left)
        self.assertGreater(peak, 500, "no signal came through the pipe")
        # same tone inverted on the right channel: proves L comes first and the channels are not swapped
        self.assertLessEqual(max(abs(a + b) for a, b in zip(left, right)), 2)
        # a 1kHz sine sampled at 48kHz moves at most peak*2*pi*1000/48000 per sample
        step = max(abs(b - a) for a, b in zip(left, left[1:]))
        self.assertLess(step, peak * 0.14 + 4, "samples jump by %d: the payload is not big-endian s16" % step)
        swapped = struct.unpack("<%dh" % (frames * 2), data)[0::2]
        self.assertGreater(max(abs(b - a) for a, b in zip(swapped, swapped[1:])), step * 4,
                           "the step check would pass on a byte-swapped payload too - it must not")
        crossings = sum(1 for a, b in zip(left, left[1:]) if (a < 0) != (b < 0))
        self.assertAlmostEqual(crossings / 2 / (frames / audio.SAMPLE_RATE), 1000, delta=60,
                               msg="the tone came back at the wrong pitch: the 48kHz path is off")


# ---------------------------------------------------------------------------- the streamer against a local "PS3"

def _receive_until(receiver, seconds_of_af: float, wall_timeout: float):
    """Collects datagrams until the AF timestamps span `seconds_of_af` (or wall_timeout passes)."""
    packets = []
    first_ts = None
    deadline = time.monotonic() + wall_timeout
    receiver.settimeout(0.5)
    while time.monotonic() < deadline:
        try:
            data, _sender = receiver.recvfrom(4096)
        except socket.timeout:
            continue
        packets.append(data)
        if data[:2] == b"AF":
            ts = struct.unpack(">Q", data[8:16])[0]
            if first_ts is None:
                first_ts = ts
            elif ts - first_ts >= seconds_of_af * 1_000_000:
                break
    return packets


@unittest.skipUnless(HAVE_PULSE, SKIP_REASON)
class StreamerTest(unittest.TestCase):
    def setUp(self):
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(("127.0.0.1", 0))
        self.receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.target = self.receiver.getsockname()
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_sock.bind(("127.0.0.1", 0))
        self.streamer = audio.AudioStreamer(self.server_sock, FFMPEG)

    def tearDown(self):
        self.streamer.stop()
        self.server_sock.close()
        self.receiver.close()

    def test_ainfo_then_paced_af_packets(self):
        self.streamer.start(self.target)
        packets = _receive_until(self.receiver, seconds_of_af=2.0, wall_timeout=5.0)
        self.streamer.stop()

        self.assertGreater(len(packets), 0)
        self.assertEqual(packets[0], b"AINFO 48000 2", "the very first datagram announces the rate")

        af = [p for p in packets if p[:2] == b"AF"]
        infos = [p for p in packets if p.startswith(b"AINFO")]
        self.assertGreaterEqual(len(af), 300, "expected ~2s of packets, got %d" % len(af))
        for index, packet in enumerate(af):
            self.assertEqual(len(packet), AF_PACKET_BYTES)
            packet_id = struct.unpack(">I", packet[2:6])[0]
            self.assertEqual(packet_id, index, "packet ids must be consecutive from 0 (no loss on loopback)")
            self.assertEqual(struct.unpack(">H", packet[6:8])[0], CHUNK_FRAMES)
        stamps = [struct.unpack(">Q", p[8:16])[0] for p in af]
        for earlier, later in zip(stamps, stamps[1:]):
            self.assertGreater(later, earlier, "capture timestamps must increase")

        span_s = (stamps[-1] - stamps[0]) / 1_000_000
        self.assertGreaterEqual(span_s, 1.9)
        rate = (len(af) - 1) / span_s
        self.assertAlmostEqual(rate, 200, delta=15, msg="pacing off: %.1f packets/s" % rate)
        # every 5ms is 5000us apart on average; the pacing is absolute so there is no drift
        mean_gap_us = (stamps[-1] - stamps[0]) / (len(af) - 1)
        self.assertAlmostEqual(mean_gap_us, 5000, delta=300)
        # AINFO once at start plus once per second (packet 0, 200, 400)
        self.assertGreaterEqual(len(infos), 3)
        self.assertLessEqual(len(infos), 5)
        for info in infos:
            self.assertEqual(info, b"AINFO 48000 2")

        text = _log_text()
        self.assertIn("audio: streaming 48000Hz stereo to 127.0.0.1:%d (1536kbps)" % self.target[1], text)
        self.assertIn("packets sent", text)

    def test_repeated_play_from_same_target_does_not_restart(self):
        self.streamer.start(self.target)
        thread = self.streamer._send_thread
        process = self.streamer.capture._process
        self.streamer.start(self.target)          # the PS3 repeats PLAY until SINFO arrives
        self.assertIs(self.streamer._send_thread, thread)
        self.assertIs(self.streamer.capture._process, process)
        self.assertTrue(self.streamer.is_streaming)

    def test_send_error_ends_session_so_next_play_restarts_audio(self):
        self.streamer.start(self.target)
        time.sleep(0.3)
        first_process = self.streamer.capture._process
        self.server_sock.close()                  # every further sendto raises: the loop must end and say so
        deadline = time.monotonic() + 1.0
        while self.streamer.is_streaming and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.streamer.is_streaming)
        self.assertIn("audio: sending aborted", _log_text())
        # the same PS3 (same port) sends PLAY again: that is a new session now, not a repeat to ignore
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_sock.bind(("127.0.0.1", 0))
        self.streamer.sock = self.server_sock
        self.streamer.start(self.target)
        self.assertTrue(self.streamer.is_streaming)
        self.assertIsNot(self.streamer.capture._process, first_process)
        self.assertTrue(_process_gone(first_process))
        self.receiver.settimeout(1.0)
        data, _sender = self.receiver.recvfrom(4096)
        self.assertEqual(data, b"AINFO 48000 2")

    def test_ten_start_stop_cycles_leak_nothing(self):
        def open_fds():
            return len(os.listdir("/proc/self/fd"))

        def audio_threads():
            return sorted(thread.name for thread in threading.enumerate() if thread.name.startswith("audio"))

        self.streamer.start(self.target)          # warm-up: childproc's spawner thread appears once, for good
        time.sleep(0.2)
        self.streamer.stop()
        time.sleep(0.2)
        fds_before = open_fds()
        processes = []
        cycles = 10
        for cycle in range(cycles):
            self.streamer.start(self.target)
            self.assertTrue(self.streamer.is_streaming, "cycle %d" % cycle)
            processes.append(self.streamer.capture._process)
            time.sleep(0.25)
            self.streamer.stop()
            self.assertFalse(self.streamer.is_streaming)
        self.assertEqual(len(set(id(p) for p in processes)), cycles, "every session ran its own ffmpeg")
        for process in processes:
            self.assertTrue(_process_gone(process))
        deadline = time.monotonic() + 1.0                 # the pipe readers end at EOF, a moment after stop()
        while audio_threads() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(audio_threads(), [])
        self.assertEqual(open_fds(), fds_before, "pipes left open")
        self.assertEqual(childproc.children(), [])
        self.receiver.settimeout(0.3)
        infos = 0
        while True:
            try:
                data, _sender = self.receiver.recvfrom(4096)
            except socket.timeout:
                break
            infos += data.startswith(b"AINFO")
        self.assertGreaterEqual(infos, cycles + 1, "every session must have announced itself")

    def test_ffmpeg_dying_mid_stream_is_logged_and_the_clock_keeps_going(self):
        self.streamer.start(self.target)
        time.sleep(0.3)
        process = self.streamer.capture._process
        killed_at_us = now_us()
        os.kill(process.pid, signal.SIGKILL)      # our own child only
        deadline = time.monotonic() + 1.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(process.poll(), "must be reaped: no zombie")
        self.assertFalse(self.streamer.capture.is_capturing)
        # like the original: the send loop carries on with silence so the PS3's feed does not stall
        self.assertTrue(self.streamer.is_streaming)
        self.receiver.settimeout(0.3)
        fresh = 0                                  # packets stamped well after the kill, not the backlog
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            try:
                data, _sender = self.receiver.recvfrom(4096)
            except socket.timeout:
                continue
            if data[:2] == b"AF" and struct.unpack(">Q", data[8:16])[0] > killed_at_us + 100_000:
                fresh += 1
        self.assertGreater(fresh, 30, "packets must keep coming after the capture died")
        self.assertIn("audio: capture aborted (ffmpeg gone", _log_text())
        started = time.monotonic()
        self.streamer.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(self.streamer.is_streaming)

    def test_a_session_that_ends_by_itself_takes_its_ffmpeg_with_it(self):
        # the send loop dying (socket gone) used to leave ffmpeg capturing into a ring nobody drains: a
        # pulse stream held and CPU burnt until the watchdog's stop, or for ever if the PS3 never returns
        self.streamer.start(self.target)
        time.sleep(0.3)
        process = self.streamer.capture._process
        self.assertIsNotNone(process)
        self.server_sock.close()
        deadline = time.monotonic() + 2.0
        while self.streamer.is_streaming and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.streamer.is_streaming)
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(_process_gone(process), "the capture must stop with the session")
        self.assertFalse(self.streamer.capture.is_capturing)
        self.assertEqual(childproc.children(), [])

    def test_a_send_thread_that_cannot_start_leaves_no_ffmpeg_behind(self):
        real_thread = threading.Thread

        class _RefusesToStart(real_thread):
            def start(self):
                if self.name == "audio-send":
                    raise RuntimeError("can't start new thread")
                return real_thread.start(self)

        threading.Thread = _RefusesToStart
        try:
            self.streamer.start(self.target)
        finally:
            threading.Thread = real_thread
        self.assertFalse(self.streamer.is_streaming)
        self.assertIsNone(self.streamer._send_thread)
        self.assertFalse(self.streamer.capture.is_capturing)
        self.assertEqual(childproc.children(), [], "the capture's ffmpeg must not be left running")
        self.assertIn("audio: the sending thread will not start, streaming video only", _log_text())
        self.streamer.start(self.target)          # and the next PLAY still works
        self.assertTrue(self.streamer.is_streaming)

    def test_stop_ends_within_a_second_and_kills_ffmpeg(self):
        self.streamer.start(self.target)
        time.sleep(0.3)
        process = self.streamer.capture._process
        thread = self.streamer._send_thread
        self.assertIsNotNone(process)
        started = time.monotonic()
        self.streamer.stop()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(_process_gone(process))
        self.assertFalse(self.streamer.is_streaming)
        # nothing more arrives after stop
        self.receiver.settimeout(0.3)
        drained = 0
        while True:
            try:
                self.receiver.recvfrom(4096)
                drained += 1
            except socket.timeout:
                break
        time.sleep(0.2)
        self.receiver.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            self.receiver.recvfrom(4096)


class StreamerWithoutCaptureTest(unittest.TestCase):
    """The capture failing must never stop the video: start() returns quietly, nothing is sent, the reason is logged."""

    def test_missing_ffmpeg_streams_video_only(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            streamer = audio.AudioStreamer(server_sock, "/nonexistent/tee-ffmpeg")
            log_before = len(_log_text())
            streamer.start(receiver.getsockname())      # must not raise
            self.assertFalse(streamer.is_streaming)
            self.assertIsNone(streamer._send_thread)
            receiver.settimeout(0.2)
            with self.assertRaises(socket.timeout):
                receiver.recvfrom(4096)
            text = _log_text()[log_before:]              # only what this start() wrote
            self.assertIn("audio: ffmpeg will not start, streaming video only", text)
            self.assertNotIn("versuche default", text, "a missing binary is not a source problem: no fallback round")
            streamer.stop()                              # idempotent, harmless
        finally:
            server_sock.close()
            receiver.close()


if __name__ == "__main__":
    unittest.main()
