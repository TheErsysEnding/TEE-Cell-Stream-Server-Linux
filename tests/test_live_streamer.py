"""live_streamer.py end to end: a gst test pattern -> ffmpeg -> splitter -> paced VF fragments -> a local UDP
receiver that reassembles frames the way stream.c on the PS3 does. Plus the PLAY-repeat rule, the fuse, the
encoder fallback, and the pump surviving a dead link.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_live_streamer -v
Safe on a live desktop: the picture comes from videotestsrc, nothing is captured, no portal, no mode switch.
"""

import errno
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_SCRATCH = tempfile.mkdtemp(prefix="tee-cst-test-live-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_SCRATCH, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_SCRATCH, "server.log"))
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from teecellstream import childproc, encoders, log, protocol   # noqa: E402
from teecellstream import live_streamer as live_streamer_module   # noqa: E402
from teecellstream.clock import now_us, sleep_until_us   # noqa: E402
from teecellstream.live_streamer import LiveStreamer   # noqa: E402

try:
    from teecellstream import capture as real_capture   # noqa: E402 - pulls in portal.py, which needs gi
except ImportError:
    real_capture = None

FFMPEG = shutil.which("ffmpeg")
GST = shutil.which("gst-launch-1.0")
W, H, FPS = protocol.WIDTH, protocol.HEIGHT, protocol.FPS
FRAME_BYTES = W * H * 3 // 2
HEADER = struct.Struct(">2sIHHBBQ")


# ---------------------------------------------------------------- stand-in captures (mirror the SPEC feed() contract)

class GstTestCapture:
    """videotestsrc (ball) -> I420 frames on a pipe; a reader keeps the newest frame; feed() hands the newest
    frame to ffmpeg exactly every 1/fps s, unchanged frames included, until stop().

    Records which thread called start()/feed() and whether an ffmpeg was already running at start(), so a
    test can check the contract: capture first, then ffmpeg; feed() on its own thread."""

    name = "test"
    needs_scale = False

    def __init__(self):
        self.captured_fps = 0
        self.gst_pid = None
        self.start_thread = None
        self.feed_thread = None
        self.ffmpeg_running_at_start = None
        self._process = None
        self._latest = None
        self._latest_gate = threading.Lock()
        self._stop = threading.Event()
        self._first_frame = threading.Event()
        self._reader = None

    def start(self, width, height, fps):
        self.start_thread = threading.current_thread().name
        self.ffmpeg_running_at_start = any(os.path.basename(str(child.args[0])) == "ffmpeg" for child in childproc.children())
        self._width, self._height, self._fps = width, height, fps
        self._frame_bytes = width * height * 3 // 2
        caps = "video/x-raw,format=I420,width=%d,height=%d,framerate=%d/1" % (width, height, fps)
        args = ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "pattern=ball", "!", caps,
                "!", "fdsink", "fd=1", "sync=false"]
        self._process = childproc.popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.gst_pid = self._process.pid
        self._reader = threading.Thread(target=self._read_frames, name="test-capture-reader", daemon=True)
        self._reader.start()
        return True

    def ffmpeg_input_args(self):
        return ["-probesize", "32", "-analyzeduration", "0", "-f", "rawvideo", "-pix_fmt", "yuv420p",
                "-video_size", "%dx%d" % (self._width, self._height), "-framerate", str(self._fps), "-i", "pipe:0"]

    def _read_frames(self):
        fd = self._process.stdout.fileno()
        frame = bytearray(self._frame_bytes)
        while not self._stop.is_set():
            filled = 0
            while filled < self._frame_bytes:
                try:
                    data = os.read(fd, self._frame_bytes - filled)
                except OSError:
                    return
                if not data:
                    return
                frame[filled:filled + len(data)] = data
                filled += len(data)
            with self._latest_gate:
                self._latest = bytes(frame)
            self._first_frame.set()

    def feed(self, ffmpeg_stdin):
        self.feed_thread = threading.current_thread().name
        if ffmpeg_stdin is None:
            return
        while not self._first_frame.wait(0.05):   # only start once there is a frame to give
            if self._stop.is_set():
                return
        fd = ffmpeg_stdin.fileno()
        period_us = 1_000_000 // self._fps
        due = now_us()
        while not self._stop.is_set():
            with self._latest_gate:
                view = memoryview(self._latest)
            try:
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            except OSError:   # BrokenPipe: ffmpeg is gone
                return
            due += period_us
            sleep_until_us(due)

    def stop(self):
        self._stop.set()
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join(2.0)
            process.stdout.close()


class HighMotionCapture(GstTestCapture):
    """Pure noise instead of the ball: every bitrate the window offers is then really spent, so a change
    of it shows up as a change in the bytes that reach the receiver."""

    name = "test-noise"

    def start(self, width, height, fps):
        self.start_thread = threading.current_thread().name
        self.ffmpeg_running_at_start = False
        self._width, self._height, self._fps = width, height, fps
        self._frame_bytes = width * height * 3 // 2
        caps = "video/x-raw,format=I420,width=%d,height=%d,framerate=%d/1" % (width, height, fps)
        args = ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "pattern=snow", "!", caps,
                "!", "fdsink", "fd=1", "sync=false"]
        self._process = childproc.popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.gst_pid = self._process.pid
        self._reader = threading.Thread(target=self._read_frames, name="test-capture-reader", daemon=True)
        self._reader.start()
        return True


class FailingInputCapture:
    """A capture whose ffmpeg input does not exist: ffmpeg exits at once with no frames."""

    name = "broken"
    needs_scale = False
    captured_fps = 0

    def start(self, width, height, fps):
        return True

    def ffmpeg_input_args(self):
        return ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", "1280x720", "-framerate", "60",
                "-i", "/nonexistent/tee-cst-no-such-input"]

    def feed(self, ffmpeg_stdin):
        return

    def stop(self):
        pass


class SilentCapture:
    """Comes up, then never delivers a frame (a capture backend that hangs). ffmpeg sits on its empty
    stdin and writes nothing, so only the first-frame watchdog can end this rung."""

    name = "stumm"
    needs_scale = False
    captured_fps = 0

    def __init__(self):
        self._stop = threading.Event()

    def start(self, width, height, fps):
        return True

    def ffmpeg_input_args(self):
        return ["-probesize", "32", "-analyzeduration", "0", "-f", "rawvideo", "-pix_fmt", "yuv420p",
                "-video_size", "%dx%d" % (W, H), "-framerate", str(FPS), "-i", "pipe:0"]

    def feed(self, ffmpeg_stdin):
        self._stop.wait()      # blocks until stop(), exactly as the SPEC's feed() contract says

    def stop(self):
        self._stop.set()


class SlowStartCapture(FailingInputCapture):
    """start() takes a while (a portal dialog would): lets a test stop() the streamer mid-startup."""

    name = "slow"

    def start(self, width, height, fps):
        time.sleep(0.5)
        return True


class FailFastThenSlowCapture(FailingInputCapture):
    """First rung fails at once (ffmpeg finds no input), the second rung's capture then takes its time."""

    name = "fail-then-slow"
    starts = 0

    def start(self, width, height, fps):
        FailFastThenSlowCapture.starts += 1
        if FailFastThenSlowCapture.starts % 2 == 0:
            time.sleep(1.0)
        return True


# ---------------------------------------------------------------- the receiver (reassembly as stream.c does it)

class Reassembler:
    def __init__(self):
        self.frames = []           # (frame_id, keyframe, bytes, version, encoder_exit_us)
        self.incomplete = 0
        self.bad_fragment_sizes = 0
        self._id = None

    def handle(self, packet):
        if len(packet) <= protocol.FRAGMENT_HEADER_BYTES or packet[:2] != b"VF":
            return
        _tag, frame_id, frag_index, frag_count, flags, version, exit_us = HEADER.unpack(packet[:protocol.FRAGMENT_HEADER_BYTES])
        payload = packet[protocol.FRAGMENT_HEADER_BYTES:]
        if frag_count <= 0 or frag_index >= frag_count:
            return
        if frag_index < frag_count - 1 and len(payload) != protocol.FRAGMENT_PAYLOAD_BYTES:
            self.bad_fragment_sizes += 1   # the PS3 places fragment i at i*1300: anything else corrupts the frame
        if frame_id != self._id:
            if self._id is not None and self._received > 0:
                self.incomplete += 1
            self._id, self._count, self._received, self._last = frame_id, frag_count, 0, -1
            self._keyframe, self._version, self._exit_us = flags & 1, version, exit_us
            self._seen = bytearray(frag_count)
            self._buf = bytearray(frag_count * protocol.FRAGMENT_PAYLOAD_BYTES)
        if self._seen[frag_index]:
            return
        self._seen[frag_index] = 1
        self._received += 1
        offset = frag_index * protocol.FRAGMENT_PAYLOAD_BYTES
        self._buf[offset:offset + len(payload)] = payload
        if frag_index == frag_count - 1:
            self._last = len(payload)
        if self._received != self._count or self._last < 0:
            return
        size = (frag_count - 1) * protocol.FRAGMENT_PAYLOAD_BYTES + self._last
        self.frames.append((frame_id, bool(self._keyframe), bytes(self._buf[:size]), self._version, self._exit_us))
        self._id = None


def _nal_types(access_unit):
    types_found = []
    position = 0
    while True:
        position = access_unit.find(b"\x00\x00\x01", position)
        if position < 0 or position + 3 >= len(access_unit):
            return types_found
        types_found.append(access_unit[position + 3] & 0x1F)
        position += 3


def _process_state(pid):
    """None when the pid is gone, otherwise the state letter from /proc (Z = zombie)."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


@unittest.skipUnless(FFMPEG and GST, "ffmpeg und gst-launch-1.0 werden gebraucht")
class LiveStreamerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.available = encoders.detect_available(FFMPEG)
        if not cls.available:
            raise unittest.SkipTest("kein Encoder auf diesem PC")
        cls.best = cls.available[0]

    def setUp(self):
        self.receiver = self._open_receiver()
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(("127.0.0.1", 0))
        self.target = self.receiver.getsockname()
        self.failures = []
        self.streamer = None

    def tearDown(self):
        if self.streamer is not None:
            self.streamer.stop()
        self.receiver.close()
        self.sender.close()

    @staticmethod
    def _open_receiver():
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.2)
        return receiver

    def _make(self, encoder_list, create_capture=GstTestCapture, loss_recovery="intra", stream_size=None):
        self.streamer = LiveStreamer(self.sender, FFMPEG, FPS, protocol.KBPS, W, H, protocol.SEND_RATE_KBPS,
                                     create_capture, lambda: list(encoder_list), lambda: loss_recovery, self.failures.append,
                                     stream_size=stream_size)
        return self.streamer

    def test_sinfo_announces_the_chosen_size_and_the_session_pins_it(self):
        """SINFO leaves before the encoder starts, so a size changed in the window in between would leave the
        two disagreeing. The session pins it; the next one picks the change up."""
        chosen = [(1536, 864)]
        streamer = self._make([], create_capture=GstTestCapture, stream_size=lambda: chosen[0])
        target = self.receiver.getsockname()
        streamer.send_stream_info(target)
        self.assertEqual(b"SINFO 1536 864", self.receiver.recv(256)[:14], "vor dem Start gilt die Einstellung")

        streamer.start(target)
        try:
            while True:                       # drain the three SINFO repeats start() sends
                packet = self.receiver.recv(2048)
                if packet.startswith(b"SINFO"):
                    self.assertEqual(b"SINFO 1536 864", packet[:14])
                    break
            chosen[0] = (1280, 720)           # changed mid-session: the running one must not follow
            streamer.send_stream_info(target)
            while True:
                packet = self.receiver.recv(2048)
                if packet.startswith(b"SINFO"):
                    self.assertEqual(b"SINFO 1536 864", packet[:14], "die laufende Sitzung behält ihre Größe")
                    break
        finally:
            streamer.stop()

    def test_an_unknown_size_falls_back_to_the_constructor(self):
        streamer = self._make([], stream_size=lambda: (999, 111))
        self.assertEqual((W, H), streamer._current_size())

    def _collect(self, seconds, reassembler, texts, stop_at_first_vf=False, receiver=None):
        """Receives for `seconds` (or until the first VF when asked); returns when the first VF arrived."""
        receiver = receiver or self.receiver
        deadline = time.monotonic() + seconds
        first_vf = None
        while time.monotonic() < deadline:
            try:
                packet, _sender = receiver.recvfrom(4096)
            except socket.timeout:
                continue
            if packet[:2] == b"VF":
                if first_vf is None:
                    first_vf = time.monotonic()
                reassembler.handle(packet)
                if stop_at_first_vf:
                    return first_vf
            else:
                texts.append(packet.decode("ascii", "replace"))
        return first_vf

    def _wait_until_idle(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while self.streamer.is_streaming and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.streamer.is_streaming, "Pumpe läuft noch. Log:\n" + log.get_recent())

    def _wait_for_ffmpeg(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session = self.streamer._session
            if session is not None and session.process is not None:
                return session.process
            time.sleep(0.02)
        self.fail("ffmpeg wurde nicht gestartet. Log:\n" + log.get_recent())

    # (3) the whole path, 3 seconds of 720p60
    def test_streams_three_seconds_to_a_udp_receiver(self):
        streamer = self._make([self.best])
        texts, frames = [], Reassembler()
        streamer.start(self.target)
        self.assertTrue(streamer.is_streaming)

        first_vf = self._collect(10.0, frames, texts, stop_at_first_vf=True)
        self.assertIsNotNone(first_vf, "kein VF-Fragment innerhalb 10 s. Log:\n" + log.get_recent())
        self.assertEqual(texts[:3], ["SINFO 1280 720 42 1 60 1"] * 3, "SINFO muss 3x vor dem ersten Frame kommen")
        self._collect(3.0, frames, texts)

        session = streamer._session
        ffmpeg_pid = session.process.pid
        capture = session.capture
        gst_pid = capture.gst_pid
        self.assertNotIn(_process_state(ffmpeg_pid), (None, "Z"))
        self.assertNotIn(_process_state(gst_pid), (None, "Z"))

        started = time.monotonic()
        streamer.stop()
        stop_took = time.monotonic() - started
        self.assertLess(stop_took, 4.0, "stop() brauchte %.1f s" % stop_took)
        self.assertFalse(streamer.is_streaming)

        # capture first, then ffmpeg; the feed on its own thread (SPEC live_streamer.py)
        self.assertEqual(capture.start_thread, "live-pump")
        self.assertFalse(capture.ffmpeg_running_at_start, "ffmpeg lief schon, bevor die Aufnahme gestartet war")
        self.assertEqual(capture.feed_thread, "live-feed")

        self.assertGreaterEqual(len(frames.frames), 150, "nur %d Frames in 3 s" % len(frames.frames))
        self.assertEqual(frames.incomplete, 0)
        self.assertEqual(frames.bad_fragment_sizes, 0)
        ids = [frame[0] for frame in frames.frames]
        self.assertEqual(ids[0], 0)
        self.assertEqual(ids, list(range(len(ids))), "Frame-IDs müssen lückenlos hochzählen")
        self.assertTrue(all(frame[3] == protocol.PROTOCOL_VERSION for frame in frames.frames))
        first_id, first_keyframe, first_au, _version, first_exit_us = frames.frames[0]
        self.assertTrue(first_keyframe, "erstes AU muss ein Keyframe sein")
        self.assertIn(7, _nal_types(first_au), "SPS fehlt im ersten AU")
        self.assertIn(8, _nal_types(first_au), "PPS fehlt im ersten AU")
        self.assertIn(5, _nal_types(first_au), "IDR fehlt im ersten AU")
        self.assertFalse(any(frame[1] for frame in frames.frames[1:]), "Intra-Refresh: nach dem ersten kein weiteres Keyframe")
        self.assertLess(abs(first_exit_us - now_us()), 60 * 1_000_000)   # stamped with the shared clock, not zero
        # encoder-exit stamps rise with the frames
        stamps = [frame[4] for frame in frames.frames]
        self.assertEqual(stamps, sorted(stamps))

        # the stream is real H.264 that decodes at the right size
        path = os.path.join(_SCRATCH, "received-%s.h264" % self.best.kind)
        with open(path, "wb") as handle:
            for frame in frames.frames:
                handle.write(frame[2])
        probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height,nb_read_frames", "-of", "csv=p=0", path], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "%d,%d,%d" % (W, H, len(frames.frames)), probe.stderr)

        # no zombies, no orphans: ffmpeg and gst are gone and reaped
        for name, pid in (("ffmpeg", ffmpeg_pid), ("gst-launch", gst_pid)):
            self.assertIsNone(_process_state(pid), "%s (pid %d) lebt noch: Zustand %s" % (name, pid, _process_state(pid)))
        self.assertEqual(childproc.children(), [])

        recent = log.get_recent()
        self.assertIn("live: versuche Encoder " + self.best.name, recent)
        self.assertRegex(recent, r"live: erstes Frame \d+ ms nach Encoder-Start gesendet")
        self.assertRegex(recent, r"live: \d+ Frames gesendet")
        self.assertIn("live: Stream an 127.0.0.1:%d beendet" % self.target[1], recent)
        self.assertEqual(self.failures, [])

    # the same path through the real capture.py test backend (what the integration run uses)
    @unittest.skipUnless(real_capture is not None, "capture.py braucht gi")
    def test_streams_with_the_real_test_capture(self):
        streamer = self._make([self.best], create_capture=real_capture.TestCapture)
        texts, frames = [], Reassembler()
        streamer.start(self.target)
        first_vf = self._collect(10.0, frames, texts, stop_at_first_vf=True)
        self.assertIsNotNone(first_vf, "kein VF-Fragment innerhalb 10 s. Log:\n" + log.get_recent())
        self._collect(1.0, frames, texts)
        capture = streamer._session.capture
        self.assertEqual(capture.name, "test")
        started = time.monotonic()
        streamer.stop()
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertGreaterEqual(len(frames.frames), 40, "nur %d Frames in 1 s" % len(frames.frames))
        self.assertTrue(frames.frames[0][1], "erstes AU muss ein Keyframe sein")
        self.assertEqual(frames.incomplete, 0)
        self.assertEqual(childproc.children(), [], "Kindprozesse übrig")
        recent = log.get_recent()
        self.assertIn("capture: test gestartet", recent)
        self.assertIn("capture: sende Bilder an ffmpeg", recent)
        self.assertIn("capture: test gestoppt", recent)
        self.assertEqual(self.failures, [])

    # (4) the PS3 repeats PLAY until SINFO arrives: answer, never restart
    def test_repeated_play_only_resends_sinfo(self):
        streamer = self._make([self.best])
        before = log.get_recent().count("live: versuche Encoder")
        streamer.start(self.target)
        pump = streamer._pump_thread
        pid_before = self._wait_for_ffmpeg().pid
        streamer.start(self.target)
        streamer.start(self.target)
        self.assertIs(streamer._pump_thread, pump)

        # everything since the first PLAY is still queued in the receiver: 3 SINFO per PLAY, then video
        texts, frames = [], Reassembler()
        self._collect(1.5, frames, texts)
        self.assertEqual(texts, ["SINFO 1280 720 42 1 60 1"] * 9, texts)
        self.assertGreater(len(frames.frames), 0, "kein Frame. Log:\n" + log.get_recent())
        self.assertTrue(streamer.is_streaming)
        self.assertEqual(streamer._session.process.pid, pid_before, "ffmpeg wurde neu gestartet")
        self.assertEqual(log.get_recent().count("live: versuche Encoder") - before, 1)
        streamer.stop()

    # a PLAY from a different address is a new session: the old one is stopped first
    def test_play_from_another_address_restarts(self):
        streamer = self._make([self.best])
        before = log.get_recent().count("live: versuche Encoder")
        streamer.start(self.target)
        first_pump = streamer._pump_thread
        first_process = self._wait_for_ffmpeg()
        texts, frames = [], Reassembler()
        self.assertIsNotNone(self._collect(10.0, frames, texts, stop_at_first_vf=True), log.get_recent())

        other = self._open_receiver()
        self.addCleanup(other.close)
        streamer.start(other.getsockname())
        self.assertIsNot(streamer._pump_thread, first_pump)
        self.assertEqual(streamer._session.target, other.getsockname())
        self.assertIsNotNone(first_process.poll(), "der alte ffmpeg läuft noch")
        other_texts, other_frames = [], Reassembler()
        self.assertIsNotNone(self._collect(10.0, other_frames, other_texts, stop_at_first_vf=True, receiver=other),
                             "kein Frame an das neue Ziel. Log:\n" + log.get_recent())
        self.assertEqual(other_texts[:3], ["SINFO 1280 720 42 1 60 1"] * 3)
        self.assertTrue(other_frames.frames[0][1] if other_frames.frames else True)
        streamer.stop()
        recent = log.get_recent()
        self.assertEqual(recent.count("live: versuche Encoder") - before, 2)
        self.assertIn("live: Stream an 127.0.0.1:%d beendet" % self.target[1], recent)
        self.assertEqual(self.failures, [])

    # the PS3's link drops (ENETUNREACH on sendto): log once, carry on - the pump must not die with
    # is_streaming stuck at True (the watchdog ends the session when the pad packets stop)
    def test_pump_survives_a_dead_link(self):
        real_send = live_streamer_module.send_access_unit
        dead_ids = set(range(5, 15))

        def flaky_send(sock, target, frame_id, data, keyframe, capture_us, send_rate_kbps):
            if frame_id in dead_ids:
                raise OSError(errno.ENETUNREACH, "Network is unreachable")
            real_send(sock, target, frame_id, data, keyframe, capture_us, send_rate_kbps)

        live_streamer_module.send_access_unit = flaky_send
        self.addCleanup(setattr, live_streamer_module, "send_access_unit", real_send)

        streamer = self._make([self.best])
        texts, frames = [], Reassembler()
        streamer.start(self.target)
        self.assertIsNotNone(self._collect(10.0, frames, texts, stop_at_first_vf=True), log.get_recent())
        self._collect(1.0, frames, texts)
        self.assertTrue(streamer.is_streaming, "die Pumpe starb am Sendefehler. Log:\n" + log.get_recent())
        self.assertIsNone(streamer._session.process.poll())
        ids = [frame[0] for frame in frames.frames]
        self.assertEqual(ids[:5], [0, 1, 2, 3, 4])
        self.assertFalse(dead_ids & set(ids), "Frames, die nicht gesendet werden konnten, kamen trotzdem an")
        self.assertIn(15, ids, "nach dem Ausfall ging es nicht weiter")
        self.assertGreater(max(ids), 40)
        streamer.stop()
        self.assertEqual(log.get_recent().count("live: Senden an 127.0.0.1 fehlgeschlagen: [Errno %d] Network is unreachable"
                                               % errno.ENETUNREACH), 1, "genau ein Log-Eintrag für den Sendefehler")
        self.assertEqual(self.failures, [])

    # (5) the fuse: three PLAYs on an encoder that will not start -> one callback, with a reason
    def test_fuse_trips_after_three_failed_starts(self):
        broken = encoders.VideoEncoder("x264", "Kaputt (Test)", [], True)
        streamer = self._make([broken], create_capture=FailingInputCapture)
        for attempt in (1, 2, 3):
            streamer.start(self.target)
            self._wait_until_idle()
            if attempt < 3:
                self.assertEqual(self.failures, [], "Sicherung zu früh ausgelöst (Versuch %d)" % attempt)
        self.assertEqual(len(self.failures), 1)
        self.assertIn("kein Encoder startet", self.failures[0])
        self.assertIn("3-mal in Folge", self.failures[0])
        recent = log.get_recent()
        self.assertIn("live: versuche Encoder Kaputt (Test)", recent)
        self.assertIn("live: Encoder lieferte keine Frames. ffmpeg sagte:", recent)
        self.assertIn("tee-cst-no-such-input", recent, "ffmpegs Fehlertext fehlt im Log")
        self.assertEqual(childproc.children(), [])

        # re-armed: the count starts over, so a single new failure does not trip it again
        streamer.reset_failures()
        streamer.start(self.target)
        self._wait_until_idle()
        self.assertEqual(len(self.failures), 1)

    def test_stop_during_startup_is_not_a_failed_start(self):
        # deviation from the original (which counted a STOP before the first frame as a failed start): a
        # portal dialog can take longer than the watchdog's grace, and three of those must not trip the fuse
        streamer = self._make([self.best], create_capture=SlowStartCapture)
        for _ in range(3):
            streamer.start(self.target)
            time.sleep(0.1)
            streamer.stop()
            self.assertFalse(streamer.is_streaming)
        self.assertEqual(self.failures, [])
        self.assertEqual(streamer._failed_starts, 0)

    def test_capture_that_will_not_start_trips_the_fuse_with_its_own_reason(self):
        class NoCapture:
            name = "none"
            needs_scale = False
            captured_fps = 0

            def start(self, width, height, fps):
                return False

            def ffmpeg_input_args(self):
                return []

            def feed(self, ffmpeg_stdin):
                return

            def stop(self):
                pass

        streamer = self._make([self.best, self.best], create_capture=NoCapture)
        before = log.get_recent().count("live: versuche Encoder")
        for _ in range(3):
            streamer.start(self.target)
            self._wait_until_idle()
        self.assertEqual(len(self.failures), 1)
        self.assertIn("Bildschirmaufnahme startet nicht", self.failures[0])
        # one attempt per PLAY: a dead capture is not retried on the next encoder
        self.assertEqual(log.get_recent().count("live: versuche Encoder") - before, 3)

    def test_falls_back_to_the_next_encoder(self):
        vaapi = encoders.LADDER[1]
        if vaapi in self.available:
            self.skipTest("VA-API funktioniert hier, kein natürlicher Fehlschlag")
        streamer = self._make([vaapi, self.best])
        texts, frames = [], Reassembler()
        streamer.start(self.target)
        first_vf = self._collect(15.0, frames, texts, stop_at_first_vf=True)
        self.assertIsNotNone(first_vf, "kein Frame vom Ersatz-Encoder. Log:\n" + log.get_recent())
        # SINFO described the first candidate (VA-API: no intra refresh) - the PS3 reads it only before streaming
        self.assertEqual(texts[:3], ["SINFO 1280 720 42 1 60 0"] * 3)
        # ... so the rung that actually streams must keep that promise: periodic keyframes, not an
        # intra-refresh stream the PS3 would freeze on after its first loss
        self._collect(1.5, frames, texts)
        streamer.stop()
        keyframe_ids = [frame[0] for frame in frames.frames if frame[1]]
        self.assertIn(60, keyframe_ids, "der Ersatz-Encoder muss im Keyframe-Modus laufen, wie angekündigt: %r" % keyframe_ids[:5])
        recent = log.get_recent()
        self.assertIn("live: versuche Encoder " + vaapi.name, recent)
        self.assertIn("live: Encoder lieferte keine Frames", recent)
        self.assertIn("live: versuche Encoder " + self.best.name, recent)
        self.assertEqual(childproc.children(), [])
        self.assertEqual(self.failures, [])

    def test_keyframe_mode_sends_sinfo_flag_zero(self):
        streamer = self._make([self.best], loss_recovery="keyframe")
        texts, frames = [], Reassembler()
        streamer.start(self.target)
        self.assertIsNotNone(self._collect(10.0, frames, texts, stop_at_first_vf=True), log.get_recent())
        self._collect(1.5, frames, texts)
        streamer.stop()
        self.assertEqual(texts[:3], ["SINFO 1280 720 42 1 60 0"] * 3)
        keyframe_ids = [frame[0] for frame in frames.frames if frame[1]]
        self.assertEqual(keyframe_ids[:2], [0, 60], "Keyframe-Modus: IDR jede Sekunde erwartet, bekam %r" % keyframe_ids[:5])

    # a rung that genuinely came to nothing counts even if a STOP lands during the next one - otherwise a
    # ladder that fails slower than the watchdog's grace would never trip the fuse
    def test_genuine_failure_before_a_stop_still_counts(self):
        broken = encoders.VideoEncoder("x264", "Kaputt (Test)", [], True)
        FailFastThenSlowCapture.starts = 0
        streamer = self._make([broken, broken], create_capture=FailFastThenSlowCapture)
        before = log.get_recent().count("live: Encoder lieferte keine Frames")
        for attempt in (1, 2, 3):
            streamer.start(self.target)
            deadline = time.monotonic() + 5.0
            while log.get_recent().count("live: Encoder lieferte keine Frames") < before + attempt and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)   # the second rung is now inside its (slow) capture start
            streamer.stop()
            self._wait_until_idle()
            self.assertEqual(streamer._failed_starts, attempt)   # only the server's re-arm resets it
        self.assertEqual(len(self.failures), 1)
        self.assertIn("kein Encoder startet", self.failures[0])

    # a pump that dies from an unexpected error must not leave ffmpeg or the capture (gst-launch, the
    # share session) behind - nothing else would ever stop them
    def test_pump_exception_leaves_nothing_behind(self):
        real_spawn = live_streamer_module._spawn_ffmpeg

        def boom(args, raw_pipe):
            raise RuntimeError("boom (Test)")

        live_streamer_module._spawn_ffmpeg = boom
        self.addCleanup(setattr, live_streamer_module, "_spawn_ffmpeg", real_spawn)
        streamer = self._make([self.best])
        streamer.start(self.target)
        pump = streamer._pump_thread
        self._wait_until_idle()
        pump.join(5.0)
        self.assertFalse(pump.is_alive())
        self.assertIn("live: Pumpe abgebrochen: RuntimeError('boom (Test)')", log.get_recent())
        self.assertEqual(childproc.children(), [], "gst-launch blieb übrig")
        self.assertFalse(any(thread.name.startswith("test-capture") for thread in threading.enumerate()))
        self.assertEqual(self.failures, [])


    # ---------------------------------------------------------------- what the window changes between streams

    def _make_with_knobs(self, knobs, create_capture=HighMotionCapture):
        """A streamer wired the way server.py wires it: bitrate and entropy coder come from callables that
        read the settings again, so the window can change them between two connects."""
        self.streamer = LiveStreamer(self.sender, FFMPEG, FPS, protocol.KBPS, W, H, protocol.SEND_RATE_KBPS,
                                     create_capture, lambda: [self.best], lambda: "intra", self.failures.append,
                                     lambda: knobs["kbps"], lambda: knobs["coder"])
        return self.streamer

    def _one_stream(self, streamer, seconds=2.0):
        """Runs one session and reports what it did: ffmpeg's arguments, the pacing rate the sender was
        given, and how many video bytes actually reached the receiver per second."""
        seen_rates = []
        real_send = live_streamer_module.send_access_unit

        def watched_send(sock, target, frame_id, data, keyframe, capture_us, send_rate_kbps):
            seen_rates.append(send_rate_kbps)
            real_send(sock, target, frame_id, data, keyframe, capture_us, send_rate_kbps)

        while True:   # anything still queued from the previous session would be counted here
            try:
                self.receiver.recvfrom(4096)
            except socket.timeout:
                break

        live_streamer_module.send_access_unit = watched_send
        try:
            streamer.start(self.target)
            args = None
            deadline = time.monotonic() + 15.0
            first = None
            payload = 0
            while time.monotonic() < deadline:
                try:
                    packet, _sender = self.receiver.recvfrom(4096)
                except socket.timeout:
                    continue
                if packet[:2] != b"VF":
                    continue
                if first is None:
                    first = time.monotonic()
                    deadline = first + seconds
                    args = list(streamer._session.process.args)
                    continue
                payload += len(packet) - protocol.FRAGMENT_HEADER_BYTES
            self.assertIsNotNone(first, "kein Video. Log:\n" + log.get_recent())
            streamer.stop()
        finally:
            live_streamer_module.send_access_unit = real_send
        self.assertTrue(seen_rates, "der Sender wurde nie aufgerufen")
        self.assertEqual(len(set(seen_rates)), 1, "die Paketrate darf sich mitten im Stream nicht ändern")
        return args, seen_rates[0], payload * 8 / 1000.0 / seconds

    def test_bitrate_and_entropy_coder_are_read_again_at_every_start(self):
        knobs = {"kbps": 4000, "coder": "cavlc"}
        streamer = self._make_with_knobs(knobs)

        low_args, low_rate, low_kbps = self._one_stream(streamer)
        self.assertEqual(low_args[low_args.index("-b:v") + 1], "4000k")
        self.assertEqual(low_args[low_args.index("-maxrate") + 1], "5600k")
        self.assertEqual(low_args[low_args.index("-coder") + 1], "cavlc")
        self.assertEqual(low_rate, 12000, "Paketrate = 3x Videorate, wie SEND_RATE_KBPS = KBPS * 3")

        knobs.update(kbps=12000, coder="cabac")
        high_args, high_rate, high_kbps = self._one_stream(streamer)
        self.assertEqual(high_args[high_args.index("-b:v") + 1], "12000k")
        self.assertEqual(high_args[high_args.index("-maxrate") + 1], "16800k")
        self.assertEqual(high_args[high_args.index("-coder") + 1], "cabac")
        self.assertEqual(high_rate, 36000)

        # and it is not only the command line: the change reaches the wire
        self.assertGreater(high_kbps, low_kbps * 1.6,
                           "Bitrate 4000 -> 12000 kbit/s, gemessen aber %.0f -> %.0f kbit/s" % (low_kbps, high_kbps))
        self.assertGreater(low_kbps, 1500, "bei 4000 kbit/s kamen nur %.0f an" % low_kbps)

    def test_plain_numbers_and_none_still_work(self):
        """The constructor keeps taking values instead of callables (what every other test does), and the
        packet rate keeps the constructor's ratio to the video rate whichever way it is given."""
        def make(video_kbps, entropy_coder):
            return LiveStreamer(self.sender, FFMPEG, FPS, protocol.KBPS, W, H, protocol.SEND_RATE_KBPS,
                                GstTestCapture, lambda: [self.best], lambda: "intra", self.failures.append,
                                video_kbps, entropy_coder)

        plain = make(None, None)
        self.assertEqual((plain._current_kbps(), plain._current_send_rate_kbps()), (protocol.KBPS, protocol.SEND_RATE_KBPS))
        self.assertEqual(plain._current_entropy_coder(), "auto")

        number = make(6000, "cavlc")
        self.assertEqual((number._current_kbps(), number._current_send_rate_kbps()), (6000, 18000))
        self.assertEqual(number._current_entropy_coder(), "cavlc")

        called = make(lambda: 4000, lambda: "cabac")
        self.assertEqual((called._current_kbps(), called._current_send_rate_kbps()), (4000, 12000))
        self.assertEqual(called._current_entropy_coder(), "cabac")

        # nonsense from a broken settings file must not reach ffmpeg's command line as "-coder None"
        for nonsense in (None, "", "unsinn", 7):
            self.assertEqual(make(None, lambda value=nonsense: value)._current_entropy_coder(), "auto")

    # ---------------------------------------------------------------- staying clean over many sessions

    def test_ten_start_stop_cycles_leave_nothing_behind(self):
        streamer = self._make([self.best])
        before = {thread.name for thread in threading.enumerate()}
        open_fds = len(os.listdir("/proc/self/fd"))
        pids = []
        for cycle in range(10):
            streamer.start(self.target)
            process = self._wait_for_ffmpeg()
            pids.append((process.pid, streamer._session.capture.gst_pid))
            frames = Reassembler()
            self.assertIsNotNone(self._collect(10.0, frames, [], stop_at_first_vf=True),
                                 "Runde %d ohne Frame. Log:\n" % cycle + log.get_recent())
            self._collect(0.3, frames, [])   # let the whole chain really run, not just come up
            started = time.monotonic()
            streamer.stop()
            self.assertLess(time.monotonic() - started, 4.0, "stop() in Runde %d zu langsam" % cycle)
            self.assertFalse(streamer.is_streaming)
            self.assertEqual(childproc.children(), [], "Runde %d: Kindprozesse übrig" % cycle)

        for cycle, (ffmpeg_pid, gst_pid) in enumerate(pids):
            self.assertIsNone(_process_state(ffmpeg_pid), "ffmpeg aus Runde %d lebt noch" % cycle)
            self.assertIsNone(_process_state(gst_pid), "gst-launch aus Runde %d lebt noch" % cycle)

        # the pump, feed, stderr and first-frame threads must all be joined, not piling up
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            leftover = {thread.name for thread in threading.enumerate()} - before
            if not leftover:
                break
            time.sleep(0.05)
        self.assertEqual(leftover, set(), "Threads übrig: %r" % leftover)
        # the three pipes and the duplicated error channel of each session must be closed again
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), open_fds + 2,
                             "Dateideskriptoren geleckt: %d -> %d" % (open_fds, len(os.listdir("/proc/self/fd"))))
        self.assertEqual(self.failures, [])
        self.assertEqual(streamer._failed_starts, 0, "erfolgreiche Sessions dürfen nicht als Fehlstart zählen")

    def test_an_encoder_that_never_delivers_is_killed_after_the_first_frame_timeout(self):
        """A silently hung encoder blocks the pump inside read(2), so nothing in the loop could time it
        out - the watchdog thread has to kill the process. Without it the session would hang forever and
        the PS3 would sit on a black screen with no fallback and no fuse."""
        streamer = self._make([self.best], create_capture=SilentCapture)
        started = time.monotonic()
        streamer.start(self.target)
        self._wait_until_idle(live_streamer_module.FIRST_FRAME_TIMEOUT_S + 10.0)
        took = time.monotonic() - started
        self.assertGreater(took, live_streamer_module.FIRST_FRAME_TIMEOUT_S - 1.0,
                           "zu früh abgebrochen (%.1f s)" % took)
        self.assertLess(took, live_streamer_module.FIRST_FRAME_TIMEOUT_S + 6.0,
                        "der Abbruch dauerte %.1f s" % took)
        self.assertIn("live: Encoder lieferte keine Frames", log.get_recent())
        self.assertEqual(streamer._failed_starts, 1)
        self.assertEqual(childproc.children(), [], "ffmpeg blieb hängen")
        self.assertEqual(self.failures, [])
        streamer.reset_failures()

    def test_a_killed_encoder_ends_the_session_without_a_failed_start(self):
        """ffmpeg dies under us (OOM killer, a crash): os.read gives EOF or an OSError, and the pump must
        wind the session down, reap both children, and not count a session that had frames as a fault."""
        streamer = self._make([self.best])
        streamer.start(self.target)
        frames = Reassembler()
        self.assertIsNotNone(self._collect(10.0, frames, [], stop_at_first_vf=True), log.get_recent())
        self._collect(0.5, frames, [])
        session = streamer._session
        ffmpeg_pid, gst_pid = session.process.pid, session.capture.gst_pid
        os.kill(ffmpeg_pid, 9)
        self._wait_until_idle(10.0)
        streamer.stop()
        self.assertEqual(childproc.children(), [])
        self.assertIsNone(_process_state(ffmpeg_pid))
        self.assertIsNone(_process_state(gst_pid), "gst-launch überlebte den Tod von ffmpeg")
        self.assertEqual(streamer._failed_starts, 0)
        self.assertEqual(self.failures, [])
        self.assertRegex(log.get_recent(), r"live: \d+ Frames gesendet")


class AnnouncedLevel(unittest.TestCase):
    """SINFO's level field has to cover the picture size it sits next to. H.264 level 4.2 stops at 8704
    macroblocks; 1920x1088 needs 8160 and fits, everything above it does not."""

    def test_everything_up_to_full_hd_stays_at_the_proven_4_2(self):
        for width, height in ((1280, 720), (1408, 800), (1536, 864), (1792, 1008), (1920, 1088)):
            self.assertEqual(42, protocol.sinfo_level_for(width, height, 60), "%dx%d" % (width, height))

    def test_above_full_hd_the_level_grows_with_the_picture(self):
        self.assertEqual(50, protocol.sinfo_level_for(2048, 1152, 60))
        self.assertEqual(51, protocol.sinfo_level_for(2560, 1440, 60))
        self.assertEqual(52, protocol.sinfo_level_for(3840, 2160, 60))

    def test_the_macroblock_rate_counts_too_not_only_the_picture(self):
        # 2560x1440 is 14400 macroblocks - inside level 5.0's picture limit of 22080, but 60 of them a
        # second is 864000, past 5.0's rate limit of 589824. At 24 fps the same picture fits 5.0.
        self.assertEqual(51, protocol.sinfo_level_for(2560, 1440, 60))
        self.assertEqual(50, protocol.sinfo_level_for(2560, 1440, 24))

    def test_every_offered_size_gets_a_level_that_covers_it(self):
        for width, height in protocol.STREAM_SIZES:
            level = protocol.sinfo_level_for(width, height, protocol.FPS)
            macroblocks = (width // 16) * (height // 16)
            limit = dict((entry[0], entry[1]) for entry in protocol._H264_LEVELS)[level]
            self.assertLessEqual(macroblocks, limit, "%dx%d ueber Level %d hinaus" % (width, height, level))

    def test_sinfo_carries_the_level_that_belongs_to_its_own_size(self):
        streamer = LiveStreamer.__new__(LiveStreamer)
        streamer._sinfo_level, streamer._fps = protocol.SINFO_LEVEL, 60
        self.assertEqual(42, streamer._level_for(1920, 1088))
        self.assertEqual(51, streamer._level_for(2560, 1440))


if __name__ == "__main__":
    unittest.main()
