"""capture.py / portal.py: the test source end to end, the frame clock, source death, X11 args, portal plumbing.

Safe on a live desktop: no portal dialog unless TEE_CST_PORTAL_TEST=1 (the portal is only asked, read-only,
for its version), no display-mode switch, no input. Settings and log go to a throwaway directory.
"""

import atexit
import gc
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="teecst-capture-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_TMP, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_TMP, "server.log"))
_RUNTIME = "/run/user/%d" % os.getuid()
if os.path.isdir(_RUNTIME):
    os.environ.setdefault("XDG_RUNTIME_DIR", _RUNTIME)
    if os.path.exists(_RUNTIME + "/bus"):
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + _RUNTIME + "/bus")

import re           # noqa: E402
import signal       # noqa: E402
import struct       # noqa: E402
import subprocess   # noqa: E402
import sys          # noqa: E402
import threading    # noqa: E402
import time         # noqa: E402
import unittest     # noqa: E402

from teecellstream import capture, log, portal   # noqa: E402
from teecellstream.settings import settings  # noqa: E402

W, H, FPS = 1280, 720, 60
FRAME = W * H * 3 // 2   # 1382400


def _gst_has(element: str) -> bool:
    if shutil.which("gst-launch-1.0") is None or shutil.which("gst-inspect-1.0") is None:
        return False
    return subprocess.run(["gst-inspect-1.0", element], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


HAVE_GST = all(_gst_has(e) for e in ("videotestsrc", "videoconvertscale", "fdsink", "queue"))
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_BUS = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


class FrameSink:
    """The ffmpeg stand-in: reads whole frames from a pipe on a thread and notes when each one arrived."""

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self.times: list[float] = []
        self.first: bytes | None = None
        self.last: bytes | None = None
        self.partial = 0
        self.total_bytes = 0
        self._thread = threading.Thread(target=self._run, name="test-sink", daemon=True)
        self._thread.start()

    def _run(self):
        buffer = bytearray(FRAME)
        view = memoryview(buffer)
        while True:
            filled = 0
            while filled < FRAME:
                got = os.readv(self.read_fd, [view[filled:]])
                if got <= 0:
                    break
                filled += got
                self.total_bytes += got
            if filled < FRAME:
                self.partial = filled
                break
            self.times.append(time.monotonic())
            if self.first is None:
                self.first = bytes(buffer)
            self.last = bytes(buffer)
        os.close(self.read_fd)

    def count(self) -> int:
        return len(self.times)

    def wait_for_frames(self, n: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count() >= n:
                return True
            time.sleep(0.01)
        return self.count() >= n

    def close(self):
        try:
            os.close(self.write_fd)
        except OSError:
            pass
        self._thread.join(3.0)

    @staticmethod
    def rate(times: list[float]) -> float:
        if len(times) < 2:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])


def _feed_thread(cap, target) -> threading.Thread:
    thread = threading.Thread(target=cap.feed, args=(target,), name="test-feed", daemon=True)
    thread.start()
    return thread


def _wait_gone(pid: int, timeout: float) -> bool:
    """Reaped, not a zombie: the /proc entry disappears only once the parent has waited on it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists("/proc/%d" % pid):
            return True
        time.sleep(0.02)
    return not os.path.exists("/proc/%d" % pid)


class FrameMathTests(unittest.TestCase):
    def test_i420_size(self):
        self.assertEqual(capture.frame_bytes(W, H), 1382400)
        self.assertEqual(capture.frame_bytes(2, 2), 6)


class InputArgsTests(unittest.TestCase):
    def test_raw_pipe_args(self):
        cap = capture.TestCapture()
        cap.width, cap.height, cap.fps = W, H, FPS
        self.assertEqual(cap.ffmpeg_input_args(), [
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", "1280x720", "-framerate", "60", "-i", "pipe:0"])
        self.assertFalse(cap.needs_scale)
        self.assertEqual(cap.name, "test")
        self.assertEqual(capture.PortalCapture().name, "portal")
        self.assertFalse(capture.PortalCapture.needs_scale)

    def test_x11grab(self):
        cap = capture.X11Capture()
        self.assertTrue(cap.needs_scale)
        self.assertEqual(cap.name, "x11grab")
        saved = os.environ.get("DISPLAY")
        try:
            os.environ["DISPLAY"] = ":7"
            self.assertTrue(cap.start(W, H, FPS))
            args = cap.ffmpeg_input_args()
            self.assertEqual(args[:2], ["-f", "x11grab"])
            self.assertEqual(args, ["-f", "x11grab", "-framerate", "60", "-draw_mouse", "1", "-i", ":7"])
            self.assertEqual(cap.captured_fps, FPS)
            started = time.monotonic()
            cap.feed(None)   # must return at once: ffmpeg reads the screen itself
            self.assertLess(time.monotonic() - started, 0.1)
            cap.stop()
            self.assertEqual(cap.captured_fps, 0)

            del os.environ["DISPLAY"]
            self.assertFalse(capture.X11Capture().start(W, H, FPS))
        finally:
            if saved is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = saved


class SelectionTests(unittest.TestCase):
    def test_test_source_env(self):
        saved = os.environ.get("TEE_CST_TEST_SOURCE")
        os.environ["TEE_CST_TEST_SOURCE"] = "1"
        try:
            self.assertIsInstance(capture.create_capture(), capture.TestCapture)
            settings.set("screencast_restore_token", None)
            capture.warm_up()   # with the test source this must not go anywhere near the portal
            self.assertIsNone(settings.get("screencast_restore_token"))
        finally:
            if saved is None:
                del os.environ["TEE_CST_TEST_SOURCE"]
            else:
                os.environ["TEE_CST_TEST_SOURCE"] = saved

    def test_stop_before_start_is_harmless(self):
        cap = capture.TestCapture()
        cap.stop()
        cap.stop()
        started = time.monotonic()
        cap.feed(1)   # never started: nothing to feed, returns at once
        self.assertLess(time.monotonic() - started, 0.1)


class PortalPlumbingTests(unittest.TestCase):
    def test_request_path_from_unique_name(self):
        self.assertEqual(portal.sender_to_path_segment(":1.584"), "1_584")
        self.assertEqual(portal.request_path(":1.584", "teecst1_42"),
                         "/org/freedesktop/portal/desktop/request/1_584/teecst1_42")

    @unittest.skipUnless(HAVE_BUS, "no session bus")
    def test_screencast_version_readable(self):
        # read-only: a property Get never shows a dialog
        version = portal.screencast_version()
        self.assertGreaterEqual(version, portal.MIN_SCREENCAST_VERSION, "ScreenCast portal missing or too old")
        self.assertTrue(portal.is_available())

    def test_response_codes(self):
        self.assertEqual((portal.RESPONSE_SUCCESS, portal.RESPONSE_CANCELLED, portal.RESPONSE_OTHER), (0, 1, 2))
        self.assertTrue(issubclass(portal.PortalCancelled, portal.PortalError))


@unittest.skipUnless(HAVE_GST, "gst-launch-1.0 with videotestsrc/videoconvertscale not installed")
class TestCaptureEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.cap = capture.TestCapture()
        self.sink = FrameSink()
        self.feeder = None

    def tearDown(self):
        self.cap.stop()
        if self.feeder is not None:
            self.feeder.join(3.0)
        self.sink.close()

    def test_frames_at_60hz_into_pipe(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        pid = self.cap._process.pid
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        self.assertTrue(self.sink.wait_for_frames(1, 5.0), "no first frame within 5 s")
        first_at = time.monotonic()
        time.sleep(1.6)
        fps_sample = self.cap.captured_fps      # the reader's own count of what videotestsrc delivered
        time.sleep(0.4)
        self.cap.stop()
        self.feeder.join(2.0)
        self.assertFalse(self.feeder.is_alive(), "feed() did not end after stop()")
        self.assertEqual(self.cap.captured_fps, 0)
        self.assertTrue(_wait_gone(pid, 3.0), "gst-launch left behind (zombie or still running)")
        self.sink.close()

        times = [t for t in self.sink.times if t >= first_at]
        self.assertEqual(self.sink.partial, 0, "a torn frame reached the sink")
        # every frame exactly 1382400 bytes: the byte total is a whole number of frames
        self.assertEqual(self.sink.total_bytes, self.sink.count() * FRAME)
        self.assertGreaterEqual(len(times), 100, "expected ~120 frames in 2 s, got %d" % len(times))
        rate = FrameSink.rate(times)
        self.assertTrue(55 <= rate <= 65, "frame clock off: %.1f fps" % rate)
        # per-second buckets, too: the clock must not burst and starve. every COMPLETE second is checked
        buckets = {}
        for t in times:
            buckets[int(t - times[0])] = buckets.get(int(t - times[0]), 0) + 1
        complete_seconds = int(times[-1] - times[0])
        self.assertGreaterEqual(complete_seconds, 1)
        for second in range(complete_seconds):
            self.assertTrue(55 <= buckets.get(second, 0) <= 65, "second %d had %d frames" % (second, buckets.get(second, 0)))
        self.assertTrue(50 <= fps_sample <= 70, "captured_fps = %d" % fps_sample)

        # the picture is real I420 with a bright ball on a black ground: luma varies, black sits at the
        # limited-range floor (16, proving the bt709/tv conversion happened), chroma is neutral grey
        frame = self.sink.last
        self.assertEqual(len(frame), FRAME)
        luma = frame[:W * H]
        self.assertGreater(len(set(luma)), 1, "luma plane is flat")
        self.assertGreater(max(luma), 150, "no bright ball in the luma plane")
        self.assertTrue(16 <= min(luma) < 40, "black is not at the limited-range floor: %d" % min(luma))
        chroma = frame[W * H:]
        mean_chroma = sum(chroma) / len(chroma)
        self.assertTrue(100 <= mean_chroma <= 156, "chroma planes not near neutral: %.1f" % mean_chroma)

    def test_source_death_keeps_last_frame_flowing(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        self.assertTrue(self.sink.wait_for_frames(20, 5.0))

        gst = self.cap._process
        gst.kill()
        killed_at = time.monotonic()
        time.sleep(1.5)
        self.assertTrue(self.feeder.is_alive(), "feed() must survive the source dying")
        self.assertEqual(self.cap.captured_fps, 0, "captured_fps must drop once the source is gone")
        self.assertIsNotNone(gst.poll(), "dead gst-launch was not reaped")
        # A dead source changes nothing about what leaves here: the last picture goes out again in every
        # slot, at the stream rate. That is what the PS3 was built for (it repeats its own last picture when
        # it is starved, and calls 2 s without video "server gone"), and the intra-refresh sweep is counted
        # in pictures, so a full self-repair still takes 1 s and not the 6 s a 10/s idle repeat cost.
        after = [t for t in self.sink.times if t > killed_at + 0.2]
        self.assertGreaterEqual(len(after), 60, "only %d frames re-sent after the source died" % len(after))
        self.assertTrue(FPS * 0.93 <= FrameSink.rate(after) <= FPS * 1.07,
                        "idle re-send rate off: %.1f fps (expected ~%d)" % (FrameSink.rate(after), FPS))
        self.assertEqual(self.sink.partial, 0)

        stop_at = time.monotonic()
        self.cap.stop()
        self.feeder.join(1.0)
        self.assertFalse(self.feeder.is_alive(), "feed() did not end promptly after stop()")
        self.assertLess(time.monotonic() - stop_at, 0.5)

    def test_stop_ends_feed_promptly(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        self.assertTrue(self.sink.wait_for_frames(5, 5.0))
        stop_at = time.monotonic()
        self.cap.stop()
        self.feeder.join(1.0)
        self.assertFalse(self.feeder.is_alive())
        self.assertLess(time.monotonic() - stop_at, 0.5)
        self.cap.stop()   # idempotent

    def test_stop_before_first_frame(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        pid = self.cap._process.pid
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        self.cap.stop()
        self.feeder.join(1.0)
        self.assertFalse(self.feeder.is_alive())
        self.assertTrue(_wait_gone(pid, 3.0))

    def test_restart_reuses_instance(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        first_pid = self.cap._process.pid
        self.assertTrue(self.cap.start(W, H, FPS))   # start() while running: the old pipeline goes first
        self.assertTrue(_wait_gone(first_pid, 3.0))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        self.assertTrue(self.sink.wait_for_frames(5, 5.0))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
    def test_feeds_real_ffmpeg_and_ends_on_broken_pipe(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", *self.cap.ffmpeg_input_args(),
                "-frames:v", "30", "-f", "null", "-"]
        ffmpeg = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            self.feeder = _feed_thread(self.cap, ffmpeg.stdin)
            try:
                _out, err = ffmpeg.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                ffmpeg.kill()
                self.fail("ffmpeg did not finish 30 rawvideo frames from feed()")
            self.assertEqual(ffmpeg.returncode, 0, err.decode("utf-8", "replace"))
            # ffmpeg closed its stdin after 30 frames: feed() must notice the broken pipe and return
            self.feeder.join(3.0)
            self.assertFalse(self.feeder.is_alive(), "feed() kept running after ffmpeg went away")
        finally:
            if ffmpeg.poll() is None:
                ffmpeg.kill()
                ffmpeg.wait()

    def test_gst_pipeline_is_the_specified_one(self):
        self.assertTrue(self.cap.start(W, H, FPS))
        self.assertEqual(list(self.cap._process.args), [
            "gst-launch-1.0", "-q",
            "videotestsrc", "is-live=true", "pattern=ball",
            "!", "video/x-raw,framerate=60/1,width=1280,height=720",
            *_GST_TAIL])
        self.assertIsNotNone(self.cap._process.stdout)   # frames come out of stdout (fdsink fd=1)


# everything after the source, exactly as SPEC.md spells it (shared by the portal and the test source)
_GST_TAIL = ["!", "queue", "max-size-buffers=8", "max-size-time=0", "max-size-bytes=0", "leaky=downstream",
             "!", "videoconvertscale", "n-threads=4", "add-borders=false",
             "!", "video/x-raw,format=I420,width=1280,height=720,colorimetry=bt709,pixel-aspect-ratio=1/1",
             "!", "fdsink", "fd=1", "sync=false"]


class FakeSession:
    """Stand-in for portal.ScreenCastSession: no bus, no dialog; hands out a pipe fd instead of PipeWire's."""

    instances: list = []
    fail_with: type | None = None   # set to a PortalError subclass to make open() fail

    def __init__(self):
        self.opened_with = "never"
        self.closed = False
        self.node_id = 77
        self.token_out = "tok-new"
        self.read_fd, self.write_fd = os.pipe()
        self.handed_out = -1
        FakeSession.instances.append(self)

    def open(self, restore_token, timeout_s=120.0):
        self.opened_with = restore_token
        if FakeSession.fail_with is not None:
            raise FakeSession.fail_with("simulated")
        return self.node_id, self.token_out

    def open_pipewire_remote(self) -> int:
        self.handed_out = os.dup(self.read_fd)   # like FDList.get(): a dup the caller owns
        return self.handed_out

    def close(self):
        self.closed = True
        for fd in (self.read_fd, self.write_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.read_fd = self.write_fd = -1   # never close a number twice: it may belong to someone else by then


class _FakeSessionBase(unittest.TestCase):
    """Swaps the real portal session for FakeSession and gst-launch for a sleeping child."""

    def setUp(self):
        self._saved = (portal.ScreenCastSession, portal.is_available, capture._popen,
                       os.environ.get("TEE_CST_TEST_SOURCE"), os.environ.get("DISPLAY"))
        os.environ.pop("TEE_CST_TEST_SOURCE", None)
        FakeSession.instances = []
        FakeSession.fail_with = None
        portal.ScreenCastSession = FakeSession
        portal.is_available = lambda: True
        self.spawned: list[tuple[list, dict]] = []

        def fake_popen(args, **kw):
            self.spawned.append((list(args), dict(kw)))
            if self.popen_error is not None:
                raise self.popen_error
            return subprocess.Popen(["sleep", "30"], **kw)   # stands in for gst-launch: lives until stop()
        self.popen_error = None
        capture._popen = fake_popen
        settings.set("screencast_restore_token", None)

    def tearDown(self):
        portal.ScreenCastSession, portal.is_available, capture._popen, test_source, display = self._saved
        for key, value in (("TEE_CST_TEST_SOURCE", test_source), ("DISPLAY", display)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for session in FakeSession.instances:
            session.close()
        settings.set("screencast_restore_token", None)



class PortalCaptureWithFakeSessionTests(_FakeSessionBase):
    """PortalCapture / warm_up / create_capture against a fake session: the real portal is never asked."""

    def test_pipeline_args_fd_passing_and_token_persistence(self):
        settings.set("screencast_restore_token", "tok-old")
        cap = capture.PortalCapture()
        self.assertTrue(cap.start(W, H, FPS))
        try:
            session = FakeSession.instances[-1]
            self.assertEqual(session.opened_with, "tok-old")               # the saved token goes into SelectSources
            self.assertEqual(settings.get("screencast_restore_token"), "tok-new")   # ... and the fresh one is saved
            self.assertEqual(len(self.spawned), 1)
            args, kw = self.spawned[0]
            fd = session.handed_out
            self.assertEqual(args, [
                "gst-launch-1.0", "-q",
                "pipewiresrc", "fd=%d" % fd, "path=77", "do-timestamp=true", "always-copy=true", "keepalive-time=100",
                *_GST_TAIL])
            self.assertEqual(kw["pass_fds"], (fd,))
            self.assertIs(kw["stdout"], subprocess.PIPE)
            self.assertIs(kw["stderr"], subprocess.PIPE)
            self.assertEqual(cap._fd, -1, "our copy of the PipeWire fd must be closed once gst-launch holds its own")
            self.assertFalse(session.closed, "the session must stay open while the stream runs")
            pid = cap._process.pid
        finally:
            cap.stop()
        self.assertTrue(session.closed, "stop() must close the portal session")
        self.assertTrue(_wait_gone(pid, 3.0))

    def test_unchanged_token_is_not_rewritten(self):
        settings.set("screencast_restore_token", "tok-same")
        cap = capture.PortalCapture()
        # a session that hands back the same token: nothing to save (no log line, no file write)
        original_init = FakeSession.__init__

        def init(session):
            original_init(session)
            session.token_out = "tok-same"
        FakeSession.__init__ = init
        try:
            self.assertTrue(cap.start(W, H, FPS))
            self.assertEqual(settings.get("screencast_restore_token"), "tok-same")
        finally:
            FakeSession.__init__ = original_init
            cap.stop()

    def test_user_cancels_dialog(self):
        FakeSession.fail_with = portal.PortalCancelled
        cap = capture.PortalCapture()
        self.assertFalse(cap.start(W, H, FPS))
        self.assertEqual(self.spawned, [], "no gst-launch without a stream")
        self.assertTrue(FakeSession.instances[-1].closed, "a failed session must still be closed")
        self.assertIsNone(settings.get("screencast_restore_token"))
        started = time.monotonic()
        cap.feed(1)   # nothing running: returns at once
        self.assertLess(time.monotonic() - started, 0.1)
        cap.stop()    # harmless

    def test_gst_launch_missing_closes_session_and_fd(self):
        self.popen_error = FileNotFoundError("gst-launch-1.0")
        cap = capture.PortalCapture()
        self.assertFalse(cap.start(W, H, FPS))
        session = FakeSession.instances[-1]
        self.assertTrue(session.closed)
        self.assertEqual(cap._fd, -1)
        self.assertEqual(settings.get("screencast_restore_token"), "tok-new")   # the token is still worth keeping

    def test_warm_up_asks_once_and_keeps_the_token(self):
        capture.warm_up()   # no token saved: one session, dialog, token saved, session closed
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertIsNone(FakeSession.instances[0].opened_with)
        self.assertTrue(FakeSession.instances[0].closed)
        self.assertEqual(FakeSession.instances[0].handed_out, -1, "warm_up must not open a PipeWire stream")
        self.assertEqual(settings.get("screencast_restore_token"), "tok-new")

        capture.warm_up()   # token present: nothing happens
        self.assertEqual(len(FakeSession.instances), 1)

        FakeSession.fail_with = portal.PortalCancelled
        settings.set("screencast_restore_token", None)
        capture.warm_up()   # cancelled: swallowed, session closed, nothing saved
        self.assertEqual(len(FakeSession.instances), 2)
        self.assertTrue(FakeSession.instances[1].closed)
        self.assertIsNone(settings.get("screencast_restore_token"))

    def test_warm_up_is_a_no_op_without_portal(self):
        portal.is_available = lambda: False
        capture.warm_up()
        self.assertEqual(FakeSession.instances, [])

    def test_create_capture_selection(self):
        self.assertIsInstance(capture.create_capture(), capture.PortalCapture)
        portal.is_available = lambda: False
        os.environ["DISPLAY"] = ":0"
        self.assertIsInstance(capture.create_capture(), capture.X11Capture)
        del os.environ["DISPLAY"]
        self.assertIsNone(capture.create_capture())
        os.environ["TEE_CST_TEST_SOURCE"] = "1"
        self.assertIsInstance(capture.create_capture(), capture.TestCapture)   # wins over everything
        self.assertEqual(FakeSession.instances, [], "selection must not open a session")



class PortalCaptureRaceAndLeakTests(_FakeSessionBase):
    """The corners the first pass left open: the warm-up race in the other direction, a spawn failure that
    is not an OSError, a queue of PLAYs behind one dialog, and five start/stop cycles in a row."""

    def test_warm_up_after_a_play_won_the_race_must_not_ask_again(self):
        # server start: warm_up() and a PLAY's PortalCapture.start() run at the same time. when the PLAY wins
        # the gate and saves a token, warm_up must notice and NOT show the dialog a second time.
        cap = capture.PortalCapture()
        real_is_available = portal.is_available

        def is_available_then_play():
            portal.is_available = real_is_available
            self.assertTrue(cap.start(W, H, FPS))   # the PLAY slips in between warm_up's checks and its gate
            return True
        portal.is_available = is_available_then_play
        try:
            capture.warm_up()
        finally:
            cap.stop()
        self.assertEqual(len(FakeSession.instances), 1, "warm_up opened a second session (second dialog)")
        self.assertEqual(settings.get("screencast_restore_token"), "tok-new")

    def test_spawn_failure_of_any_kind_closes_the_session(self):
        self.popen_error = RuntimeError("Spawner kaputt")   # not an OSError: must still not leak the session
        cap = capture.PortalCapture()
        self.assertFalse(cap.start(W, H, FPS))
        session = FakeSession.instances[-1]
        self.assertTrue(session.closed, "portal session leaked after a failed spawn")
        self.assertEqual(cap._fd, -1, "PipeWire fd leaked after a failed spawn")
        cap.stop()

    def test_queued_play_waits_for_the_dialog_then_uses_its_token(self):
        # first dialog open (warm_up holding the gate); a PLAY arriving meanwhile waits and then goes
        # through with the token the dialog produced, never a second dialog
        dialog_open = threading.Event()
        let_dialog_finish = threading.Event()
        original_open = FakeSession.open

        def slow_open(session, restore_token, timeout_s=120.0):
            if restore_token is None:
                dialog_open.set()
                let_dialog_finish.wait(5.0)
            return original_open(session, restore_token, timeout_s)
        FakeSession.open = slow_open
        cap = capture.PortalCapture()
        try:
            warm = threading.Thread(target=capture.warm_up, name="test-warm", daemon=True)
            warm.start()
            self.assertTrue(dialog_open.wait(3.0))
            outcome = {}
            play = threading.Thread(target=lambda: outcome.setdefault("ok", cap.start(W, H, FPS)), name="test-play", daemon=True)
            play.start()
            time.sleep(0.3)
            self.assertTrue(play.is_alive(), "PLAY must wait for the open dialog, not race it")
            self.assertEqual(len(FakeSession.instances), 1)
            let_dialog_finish.set()
            play.join(5.0)
            warm.join(5.0)
            self.assertTrue(outcome.get("ok"))
            self.assertEqual(FakeSession.instances[-1].opened_with, "tok-new", "the queued PLAY must use the fresh token")
            self.assertEqual(len(FakeSession.instances), 2)
        finally:
            FakeSession.open = original_open
            let_dialog_finish.set()
            cap.stop()

    def test_queued_play_gives_up_when_the_dialog_never_closes(self):
        # nobody answers the first dialog: a PLAY queued behind it must not wait forever (each PS3 retry
        # would pile another waiting pump thread onto the gate) - it fails after the dialog timeout
        saved_timeout = capture.PORTAL_GATE_WAIT_S
        capture.PORTAL_GATE_WAIT_S = 0.3
        capture._portal_gate.acquire()
        try:
            cap = capture.PortalCapture()
            started = time.monotonic()
            self.assertFalse(cap.start(W, H, FPS))
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(FakeSession.instances, [], "no session may be opened while the gate is held")
        finally:
            capture._portal_gate.release()
            capture.PORTAL_GATE_WAIT_S = saved_timeout

    def test_five_cycles_leak_nothing(self):
        settings.set("screencast_restore_token", "tok-old")
        fds_before = len(os.listdir("/proc/self/fd"))
        threads_before = threading.active_count()
        cap = capture.PortalCapture()
        pids = []
        for _ in range(5):
            self.assertTrue(cap.start(W, H, FPS))
            pids.append(cap._process.pid)
            self.assertEqual(cap._fd, -1)
            cap.stop()
            self.assertTrue(FakeSession.instances[-1].closed)
        for pid in pids:
            self.assertTrue(_wait_gone(pid, 3.0), "child %d left behind" % pid)
        for session in FakeSession.instances:
            session.close()
        time.sleep(0.1)
        self.assertEqual(len(os.listdir("/proc/self/fd")), fds_before, "fd leak over 5 start/stop cycles")
        self.assertLessEqual(threading.active_count(), threads_before + 1, "thread leak over 5 start/stop cycles")   # +1: childproc's spawner


class _FakeBus:
    """Just enough of Gio.DBusConnection for ScreenCastSession._request: the Response arrives through the
    thread-default main context, like the real one, so the loop/timeout code runs for real."""

    def __init__(self, respond=None, request_path_override=None):
        self.respond = respond               # (code, results) or None for "never answers"
        self.override = request_path_override
        self.subscribed: list[str] = []
        self.calls: list[tuple] = []
        self._callbacks: dict[int, tuple] = {}
        self._next = 1

    def get_unique_name(self):
        return ":1.999"

    def signal_subscribe(self, sender, interface, member, path, arg0, flags, callback, *user):
        ident = self._next
        self._next += 1
        self.subscribed.append(path)
        self._callbacks[ident] = (path, callback)
        return ident

    def signal_unsubscribe(self, ident):
        self._callbacks.pop(ident, None)

    def call_sync(self, bus_name, path, interface, method, parameters, reply_type, flags, timeout, cancellable):
        from gi.repository import GLib
        self.calls.append((path, interface, method))
        if interface == portal.SCREENCAST_INTERFACE:
            token = parameters.unpack()[-1]["handle_token"]
            request = self.override or portal.request_path(self.get_unique_name(), token)
            if self.respond is not None:
                code, results = self.respond

                def deliver(*_args):
                    for path_, callback in list(self._callbacks.values()):
                        if path_ == request:
                            callback(self, ":1.7", request, portal.REQUEST_INTERFACE, "Response",
                                     GLib.Variant("(ua{sv})", (code, results)))
                    return False
                source = GLib.idle_source_new()
                source.set_callback(deliver)
                source.attach(GLib.MainContext.get_thread_default())
            return GLib.Variant("(o)", (request,))
        return None


@unittest.skipUnless(portal.Gio is not None, "PyGObject not installed")
class PortalRequestPlumbingTests(unittest.TestCase):
    """_request against a fake bus: subscription before the call, results, codes 1/2, timeout + Request.Close."""

    def _session(self, bus):
        session = portal.ScreenCastSession()
        session._bus = bus
        return session

    def test_subscribes_before_calling_and_returns_results(self):
        from gi.repository import GLib
        bus = _FakeBus(respond=(0, {"session_handle": GLib.Variant("s", "/org/freedesktop/portal/desktop/session/1_999/x")}))
        results = self._session(bus)._request("CreateSession", [], {}, 2.0)
        self.assertEqual(results["session_handle"], "/org/freedesktop/portal/desktop/session/1_999/x")
        self.assertEqual(len(bus.subscribed), 1)
        self.assertTrue(bus.subscribed[0].startswith("/org/freedesktop/portal/desktop/request/1_999/"))
        self.assertEqual([c[2] for c in bus.calls], ["CreateSession"])

    def test_listens_on_the_portal_named_path_too(self):
        bus = _FakeBus(respond=(0, {}), request_path_override="/org/freedesktop/portal/desktop/request/1_999/other")
        self._session(bus)._request("SelectSources", [], {}, 2.0)
        self.assertEqual(len(bus.subscribed), 2)
        self.assertEqual(bus.subscribed[1], "/org/freedesktop/portal/desktop/request/1_999/other")

    def test_user_cancel_is_code_1(self):
        with self.assertRaises(portal.PortalCancelled):
            self._session(_FakeBus(respond=(1, {})))._request("Start", [], {}, 2.0)

    def test_other_error_is_code_2(self):
        with self.assertRaises(portal.PortalError) as caught:
            self._session(_FakeBus(respond=(2, {})))._request("Start", [], {}, 2.0)
        self.assertNotIsInstance(caught.exception, portal.PortalCancelled)

    def test_no_answer_times_out_and_closes_the_request(self):
        bus = _FakeBus(respond=None)
        started = time.monotonic()
        with self.assertRaises(portal.PortalError) as caught:
            self._session(bus)._request("Start", [], {}, 0.3)
        elapsed = time.monotonic() - started
        self.assertTrue(0.25 <= elapsed < 2.0, "timeout took %.2f s" % elapsed)
        self.assertIn("no answer", str(caught.exception))
        closes = [c for c in bus.calls if c[2] == "Close" and c[1] == portal.REQUEST_INTERFACE]
        self.assertEqual(len(closes), 1, "Request.Close must take the dialog down after the timeout")
        self.assertEqual(closes[0][0], bus.subscribed[0])

    def test_open_without_a_stream_fails_cleanly(self):
        from gi.repository import GLib
        # CreateSession answers, SelectSources answers, Start answers with no streams: PortalError, session closable
        bus = _FakeBus(respond=(0, {"session_handle": GLib.Variant("s", "/org/freedesktop/portal/desktop/session/1_999/s")}))
        session = self._session(bus)
        saved = portal._session_bus
        portal._session_bus = lambda: bus
        try:
            with self.assertRaises(portal.PortalError):
                session.open("tok")
        finally:
            portal._session_bus = saved
        self.assertEqual([c[2] for c in bus.calls], ["CreateSession", "SelectSources", "Start"])
        session.close()
        self.assertEqual(bus.calls[-1], ("/org/freedesktop/portal/desktop/session/1_999/s", portal.SESSION_INTERFACE, "Close"))
        session.close()   # twice is fine
        self.assertEqual(len(bus.calls), 4)


@unittest.skipUnless(HAVE_BUS and portal.is_available(), "no ScreenCast portal on this bus")
class PortalRealSessionNoDialogTests(unittest.TestCase):
    """CreateSession + Session.Close against the real portal: no dialog, no permission, nothing to answer -
    proves the request-path derivation and the Response plumbing on a worker thread."""

    def test_create_session_and_close_from_a_worker_thread(self):
        outcome = {}

        def run():
            session = portal.ScreenCastSession()
            try:
                from gi.repository import GLib
                session._bus = portal._session_bus()
                results = session._request("CreateSession", [],
                                           {"session_handle_token": GLib.Variant("s", portal._new_token())}, 10.0)
                outcome["handle"] = results.get("session_handle")
                session.session_handle = outcome["handle"]
            except Exception as error:   # noqa: BLE001
                outcome["error"] = error
            finally:
                session.close()
                outcome["closed"] = session._closed
        thread = threading.Thread(target=run, name="test-portal", daemon=True)
        thread.start()
        thread.join(15.0)
        self.assertFalse(thread.is_alive(), "portal round trip hung")
        self.assertNotIn("error", outcome, "portal error: %s" % outcome.get("error"))
        self.assertTrue(str(outcome.get("handle", "")).startswith("/org/freedesktop/portal/desktop/session/"))
        self.assertTrue(outcome["closed"])

    def test_repeated_requests_give_their_main_context_back(self):
        # every _request needs a private GMainContext to catch the Response on, and a GMainContext owns an
        # eventfd. Let a callback close over something GLib holds - a MainLoop - and GDBus keeps the whole
        # chain alive past signal_unsubscribe, so the eventfd is never given back: measured against the real
        # portal as exactly three leaked fds per screen share, climbing with every PLAY the PS3 sends.
        from gi.repository import GLib

        def create_and_close():
            session = portal.ScreenCastSession()
            session._bus = portal._session_bus()
            results = session._request("CreateSession", [],
                                       {"session_handle_token": GLib.Variant("s", portal._new_token())}, 10.0)
            session.session_handle = results.get("session_handle")
            session.close()

        create_and_close()   # the first one opens what is only ever opened once (the bus connection)
        gc.collect()
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            create_and_close()
        gc.collect()
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), before,
                             "%d fds leaked over 5 portal requests" % (len(os.listdir("/proc/self/fd")) - before))


@unittest.skipUnless(os.environ.get("TEE_CST_PORTAL_TEST") == "1", "opt-in: TEE_CST_PORTAL_TEST=1 shows the share dialog")
class PortalCaptureOptInTests(unittest.TestCase):
    """Shows the real share dialog (once, unless a token is already saved in the test settings file)."""

    def test_portal_capture_end_to_end(self):
        self.assertTrue(portal.is_available())
        cap = capture.PortalCapture()
        sink = FrameSink()
        feeder = None
        try:
            self.assertTrue(cap.start(W, H, FPS), "portal capture did not start (see log)")
            feeder = _feed_thread(cap, sink.write_fd)
            self.assertTrue(sink.wait_for_frames(30, 10.0), "no frames from the portal stream")
            time.sleep(1.0)
            self.assertGreater(cap._source_frames, 0)
            self.assertIsNotNone(settings.get("screencast_restore_token"), "restore token was not saved")
        finally:
            cap.stop()
            if feeder is not None:
                feeder.join(3.0)
            sink.close()
        self.assertEqual(sink.partial, 0)
        self.assertTrue(55 <= FrameSink.rate(sink.times) <= 65)

    def test_warm_up_saves_token(self):
        settings.set("screencast_restore_token", None)
        capture.warm_up()
        self.assertIsNotNone(settings.get("screencast_restore_token"))


# ---------------------------------------------------------------------------------------------------
# feed()'s pacing, hammered with a source whose rate we set exactly and whose pictures say what they are.
# gst-launch cannot do either, so _popen is swapped for a small writer of our own; everything above it -
# the reader thread, the three buffers, the generation, the cap, the idle repeat - is the real code.

# every picture is one repeated byte, so a picture assembled out of two source pictures is visible as
# more than one distinct byte in it. The first 8 bytes carry the moment it was written (CLOCK_MONOTONIC,
# comparable across processes) so the sink can say how OLD what feed() handed on is.
_WRITER = r"""
import struct, sys, time
size, period = %d, %s
out = sys.stdout.buffer
n = 0
nxt = time.monotonic()
while True:
    n = (n %% 251) + 1
    if period:
        nxt += period
        delay = nxt - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    out.write(struct.pack("<d", time.monotonic()) + bytes([n]) * (size - 8))
    out.flush()
"""

# The damage pattern of a window redraw on an otherwise still desktop, which is what GNOME's ScreenCast
# actually hands over: a salvo of five pictures a few ms apart, then a fifth of a second of nothing. The last
# picture of each salvo carries the marker byte, so the sink can say whether the NEWEST one got out and when.
_SALVO_WRITER = r"""
import struct, sys, time
size = %d
out = sys.stdout.buffer
n = 0
nxt = time.monotonic()
while True:
    for k in range(5):
        n = (n %% 200) + 1
        out.write(struct.pack("<d", time.monotonic()) + bytes([250 if k == 4 else n]) * (size - 8))
        out.flush()
        time.sleep(0.004)
    nxt += 0.2
    delay = nxt - time.monotonic()
    if delay > 0:
        time.sleep(delay)
"""

SALVO_MARKER = 250

PACE_W, PACE_H = 64, 48
PACE_FRAME = PACE_W * PACE_H * 3 // 2   # 4608


class _WrittenSource(capture._PipeCapture):
    """A _PipeCapture whose 'gst-launch' is the writer above, at a rate the test picks."""

    name = "written"
    source_period = None   # seconds between pictures; None = as fast as the pipe takes them
    source_script = None   # a writer of the test's own (the salvo above) instead of the steady one

    def _open_source(self):
        return ["written"]


class _StampedSink:
    """Reads whole pictures off a pipe and keeps, for each: when it arrived, how old it was, whether it
    was assembled out of more than one source picture."""

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        self.times: list[float] = []
        self.ages: list[float] = []
        self.stamps: list[float] = []   # when the source wrote this picture: a repeat carries it again
        self.marks: list[int] = []      # the picture's content byte (SALVO_MARKER for a salvo's newest)
        self.torn = 0
        self.partial = 0
        self._thread = threading.Thread(target=self._run, name="test-stamped-sink", daemon=True)
        self._thread.start()

    def _run(self):
        buffer = bytearray(PACE_FRAME)
        view = memoryview(buffer)
        while True:
            filled = 0
            while filled < PACE_FRAME:
                got = os.readv(self.read_fd, [view[filled:]])
                if got <= 0:
                    break
                filled += got
            if filled < PACE_FRAME:
                self.partial = filled
                break
            now = time.monotonic()
            stamp = struct.unpack_from("<d", buffer)[0]
            self.times.append(now)
            self.stamps.append(stamp)
            self.marks.append(buffer[8])
            self.ages.append((now - stamp) * 1000.0)
            if len(set(buffer[8:])) != 1:
                self.torn += 1
        os.close(self.read_fd)

    def close(self):
        try:
            os.close(self.write_fd)
        except OSError:
            pass
        self._thread.join(3.0)

    def gaps(self) -> list[float]:
        return [b - a for a, b in zip(self.times, self.times[1:])]

    def age(self, quantile: float) -> float:
        ordered = sorted(self.ages)
        return ordered[min(len(ordered) - 1, int(len(ordered) * quantile))] if ordered else 0.0

    def new_pictures(self) -> list[tuple[float, float, int]]:
        """(stamp, age in ms, marker byte) for every write that carried a picture not sent before -
        a repeat is the same source picture again, so it carries the stamp it already had."""
        out, previous = [], None
        for stamp, age, mark in zip(self.stamps, self.ages, self.marks):
            if stamp != previous:
                out.append((stamp, age, mark))
            previous = stamp
        return out

    def new_age(self, quantile: float) -> float:
        ordered = sorted(age for _stamp, age, _mark in self.new_pictures())
        return ordered[min(len(ordered) - 1, int(len(ordered) * quantile))] if ordered else 0.0

    @staticmethod
    def spread(gaps: list[float]) -> tuple[float, float]:
        """(standard deviation, longest) of the gaps between writes, in ms."""
        if len(gaps) < 2:
            return 0.0, 0.0
        mean = sum(gaps) / len(gaps)
        sd = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5
        return sd * 1000.0, max(gaps) * 1000.0


class _TimedWriter:
    """ffmpeg's stdin with a write that costs exactly `cost` seconds - a file object, so feed() takes the
    Popen.stdin path through _make_writer. It is how a 1.38 MB frame behaves when the encoder is slow to
    drain its pipe, without needing a slow encoder."""

    def __init__(self, cost: float):
        self.cost = cost
        self.times: list[float] = []

    def write(self, view) -> int:
        self.times.append(time.monotonic())
        if self.cost:
            time.sleep(self.cost)
        return len(view)


# one picture and then silence for ever: every write after the first is a repeat, so each one starts on
# its grid point (a pending picture may start a window early, which would hide the overrun branch)
_ONE_PICTURE = r"""
import struct, sys, time
size = %d
sys.stdout.buffer.write(struct.pack("<d", time.monotonic()) + bytes([1]) * (size - 8))
sys.stdout.buffer.flush()
time.sleep(3600)
"""


class PacingTests(unittest.TestCase):
    """feed()'s cadence and the three-buffer hand-over - against a source whose rate we set exactly.

    One test per rule the console needs (the numbers each one guards were measured at 1280x720 and are in
    the pacing note in capture.py; here the pictures are 64x48 so the pipe adds almost nothing to the age).
    """

    INTERVAL_MS = 1000.0 / FPS

    def setUp(self):
        self._saved_popen = capture._popen
        self.children: list[subprocess.Popen] = []
        self.cap = _WrittenSource()
        self.sink = _StampedSink()
        self.feeder = None
        self.cpu = {}

        def fake_popen(args, **kw):
            kw.pop("pass_fds", None)
            if self.cap.source_script:
                code = self.cap.source_script % PACE_FRAME
            else:
                period = "None" if self.cap.source_period is None else repr(self.cap.source_period)
                code = _WRITER % (PACE_FRAME, period)
            child = subprocess.Popen([sys.executable, "-u", "-c", code], **kw)
            self.children.append(child)
            return child
        capture._popen = fake_popen

    def tearDown(self):
        capture._popen = self._saved_popen
        self.cap.stop()
        if self.feeder is not None:
            self.feeder.join(3.0)
        self.sink.close()
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()

    def _reset(self):
        """A second source in the same test: end this one completely before the next one starts."""
        self.tearDown()
        self.setUp()

    def _run(self, source_fps, fps=FPS, seconds=2.0, script=None):
        self.cap.source_period = None if source_fps is None else 1.0 / source_fps
        self.cap.source_script = script
        self.assertTrue(self.cap.start(PACE_W, PACE_H, fps))

        def feed():   # thread_time is this thread's own CPU: proves feed() waits rather than spins
            started = time.thread_time()
            self.cap.feed(self.sink.write_fd)
            self.cpu["seconds"] = time.thread_time() - started
        self.feeder = threading.Thread(target=feed, name="test-feed", daemon=True)
        self.feeder.start()
        deadline = time.monotonic() + 5.0
        while not self.sink.times and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.sink.times, "no picture reached ffmpeg's side at all")
        time.sleep(0.3)                # let the grid settle on the source before anything is counted
        del self.sink.times[:]         # measure the steady state, not the start-up
        del self.sink.ages[:]
        del self.sink.stamps[:]
        del self.sink.marks[:]
        self.source_before = self.cap._source_frames
        time.sleep(seconds)
        self.source_after = self.cap._source_frames
        self.cap.stop()
        self.feeder.join(3.0)
        self.assertFalse(self.feeder.is_alive(), "feed() did not end after stop()")
        return FrameSink.rate(self.sink.times)

    # --- the per-second trace line -----------------------------------------------------------------
    # It is what makes a diagnosis session possible at all: the status line in the window cannot answer
    # "what raises the source rate", because having the window in front is itself one of the things that
    # raises it (every redraw of it is another picture GNOME hands us). Off unless TEE_CST_TRACE is set.

    def _traced(self, source_fps, seconds=2.5):
        lines = []
        saved_write, saved_flag = log.write, capture.TRACE_SOURCE_RATE
        log.write, capture.TRACE_SOURCE_RATE = lambda text: lines.append(text), True
        try:
            self._run(source_fps, seconds=seconds)
        finally:
            log.write, capture.TRACE_SOURCE_RATE = saved_write, saved_flag
        return [line for line in lines if line.startswith("trace: ")]

    def test_trace_reports_a_source_over_the_grid_and_the_pictures_it_costs(self):
        traces = self._traced(90)
        self.assertGreaterEqual(len(traces), 2, traces)
        rates = [int(re.search(r"source (\d+)/s", line).group(1)) for line in traces]
        self.assertTrue(all(rate > 62 for rate in rates), traces)     # 90/s really is over the band
        superseded = [int(re.search(r"(\d+) superseded", line).group(1)) for line in traces]
        self.assertTrue(any(count > 0 for count in superseded),
                        "a source over the grid must show pictures being superseded: %r" % traces)

    def test_trace_reports_a_source_on_the_grid_as_costing_nothing(self):
        traces = self._traced(FPS)
        self.assertGreaterEqual(len(traces), 2, traces)
        superseded = [int(re.search(r"(\d+) superseded", line).group(1)) for line in traces]
        self.assertEqual([], [count for count in superseded if count > 2], traces)

    def test_nothing_is_traced_while_the_switch_is_off(self):
        lines = []
        saved_write, saved_flag = log.write, capture.TRACE_SOURCE_RATE
        log.write, capture.TRACE_SOURCE_RATE = lambda text: lines.append(text), False
        try:
            self._run(90, seconds=2.0)
        finally:
            log.write, capture.TRACE_SOURCE_RATE = saved_write, saved_flag
        self.assertEqual([], [line for line in lines if line.startswith("trace: ")])

    def test_source_at_120_is_capped_at_the_stream_rate(self):
        rate = self._run(120)
        self.assertTrue(54 <= rate <= 66, "120 fps source came out at %.1f fps (cap is %d)" % (rate, FPS))
        self.assertEqual(self.sink.torn, 0)

    def test_every_source_rate_comes_out_at_the_stream_rate(self):
        # CRITERION 1. GNOME's ScreenCast is damage-driven: typing gives ~20 pictures a second, a still
        # desktop 10, a game 42, mouse movement 240. The console gets one cadence out of all of them - it
        # takes one decoded picture per refresh and repeats the last one when it is starved, so a source
        # rate that reaches it unchanged shows as judder and a stats panel full of red bars (the shipped
        # loop measured 10.0/s, 20.0/s and 42.0/s for those three sources).
        for source in (5, 20, 42, 60, 240):
            with self.subTest(source=source):
                if source != 5:
                    self._reset()
                rate = self._run(source, seconds=2.5)
                sd, longest = _StampedSink.spread(self.sink.gaps())
                self.assertTrue(58 <= rate <= 62,
                                "%d/s in came out at %.1f/s" % (source, rate))
                self.assertLess(sd, self.INTERVAL_MS / 4,
                                "%d/s in: gaps vary by %.2f ms (a quarter of a frame is %.2f)"
                                % (source, sd, self.INTERVAL_MS / 4))
                self.assertLess(longest, 2 * self.INTERVAL_MS,
                                "%d/s in: longest gap %.1f ms" % (source, longest))

    def test_a_new_picture_is_not_held_back_by_the_cadence(self):
        # CRITERION 2. The cadence may fill gaps, never delay a picture that is there: it must go out inside
        # its own slot. The regression this guards is the deadline cap ("last write + 1/fps"), which moved
        # later by the write's own duration every round and aged every picture by 25.9 ms.
        # The two bars differ because the source rates do: at 60/s the grid locks onto the arrivals and the
        # age is the pipe (measured 0.05 ms at 1280x720), while a source at 42/s shares no phase with 60 and
        # must wait for the window to open - up to a whole interval minus the window, 4.4 ms in the middle.
        for source, limit in ((60, 4.0), (42, 6.0)):
            with self.subTest(source=source):
                if source != 60:
                    self._reset()
                rate = self._run(source, seconds=2.5)
                self.assertTrue(58 <= rate <= 62, "%d/s in came out at %.1f/s" % (source, rate))
                self.assertLess(self.sink.new_age(0.5), limit,
                                "%d/s in: a new picture waits %.2f ms (median)" % (source, self.sink.new_age(0.5)))
                self.assertLess(self.sink.new_age(0.9), self.INTERVAL_MS,
                                "%d/s in: p90 age %.2f ms" % (source, self.sink.new_age(0.9)))

    def test_no_source_picture_is_lost_below_the_stream_rate(self):
        # CRITERION 3. A picture may only ever be passed over because a NEWER one took its place - never
        # because the slot was spent on a repeat, and never because the loop consumed the generation and
        # then went back to waiting. Below the rate that means: everything the source produced goes out.
        rate = self._run(42, seconds=3.0)
        produced = self.source_after - self.source_before
        written = len(self.sink.new_pictures())
        self.assertTrue(58 <= rate <= 62, "42/s in came out at %.1f/s" % rate)
        self.assertGreaterEqual(written, produced - 2,
                                "%d of %d source pictures never reached ffmpeg" % (produced - written, produced))
        self.assertEqual(self.sink.torn, 0)

    def test_over_the_rate_the_newest_picture_wins_and_none_is_swallowed(self):
        # CRITERIA 3 and 4, and the bug the log line "44522 Bilder von der Quelle, 42548 an ffmpeg" was
        # hiding. The old loop marked a generation seen BEFORE deciding it was over the rate, so that
        # picture was consumed without being written and the loop then waited for the NEXT one - which on a
        # damage-driven desktop (a salvo of five pictures 4 ms apart, then a fifth of a second of nothing)
        # meant the salvo's newest picture only left with the next idle repeat, up to 100 ms later.
        # Measured at 1280x720: 20.3 writes/s and a picture age p99 of 110 ms then, 60.0/s and 11 ms now.
        rate = self._run(None, seconds=4.0, script=_SALVO_WRITER)
        newest = [(age, mark) for _stamp, age, mark in self.sink.new_pictures() if mark == SALVO_MARKER]
        self.assertTrue(58 <= rate <= 62, "a salvo source came out at %.1f/s" % rate)
        self.assertGreaterEqual(len(newest), 18, "only %d of ~20 salvos had their newest picture written" % len(newest))
        ages = sorted(age for age, _mark in newest)
        # a quantile, not the maximum: these are timed from the source process's own clock, and that process
        # can be descheduled for 100 ms on a desktop running OBS - measured, once in 31 salvos, while every
        # counter in feed() said the picture had gone out in its own slot
        self.assertLess(ages[len(ages) // 2], self.INTERVAL_MS,
                        "a salvo's newest picture waits %.1f ms (median) to go out" % ages[len(ages) // 2])
        self.assertLess(ages[int(len(ages) * 0.9)], 2 * self.INTERVAL_MS,
                        "p90 %.1f ms" % ages[int(len(ages) * 0.9)])
        sd, longest = _StampedSink.spread(self.sink.gaps())
        self.assertLess(longest, 2 * self.INTERVAL_MS, "longest gap %.1f ms" % longest)
        self.assertEqual(self.sink.torn, 0)

    def test_feed_fills_the_slots_without_spinning(self):
        # CRITERION 6. Both ends of the range: a source that delivers 5 pictures a second still owes the
        # console 55 repeats, and one that delivers hundreds of thousands must not wake this thread for each
        # (with a picture in hand feed() sleeps on the stop Event, where the reader's notify cannot reach
        # it). Measured at 64x48 against 321000 pictures a second: 0.028 s of thread CPU in 5 s.
        for source in (5, None):
            with self.subTest(source=source):
                if source is None:
                    self._reset()
                rate = self._run(source, seconds=3.0)
                self.assertTrue(58 <= rate <= 62, "%s in came out at %.1f/s" % (source, rate))
                self.assertLess(self.cpu["seconds"], 0.3,
                                "feed() burned %.2f s of CPU in 3 s" % self.cpu["seconds"])

    def test_the_cap_skips_pictures_instead_of_delaying_them(self):
        # REGRESSION. The cap used to be a deadline of "last write + 1/fps". That deadline moves later by
        # the write's own duration every time round, so against a source at or above the stream rate it
        # delayed every picture instead of dropping the odd one: measured at 1280x720 from a 60 Hz source,
        # 56.9 pictures a second at a median age of 25.9 ms - on a link measured at 25-27 ms end to end.
        # The source is 60/s here, not 66 as it was: at 66 no even cadence can keep the age near zero (the
        # newest picture at a grid point is up to a whole source period old), so the assertion below would
        # have been measuring the source's phase, not the bug. The 240/s case guards the same thing where
        # it can be guarded - over the rate the age must stay inside ONE source period, which is what
        # proves the picture taken at the slot is the newest there is.
        rate = self._run(60)
        self.assertTrue(58 <= rate <= 62, "60 fps source came out at %.1f fps" % rate)
        self.assertLess(self.sink.new_age(0.5), 4.0,
                        "the cap is ageing pictures again: median %.1f ms" % self.sink.new_age(0.5))
        self._reset()
        rate = self._run(240)
        self.assertTrue(58 <= rate <= 62, "240 fps source came out at %.1f fps" % rate)
        self.assertLess(self.sink.new_age(0.9), 1000.0 / 240 + 3.0,
                        "over the rate an older picture than the newest went out: p90 age %.1f ms"
                        % self.sink.new_age(0.9))

    def test_a_write_that_still_fits_its_slot_does_not_cost_the_slot(self):
        # REGRESSION, and the one the console feels on a STILL desktop. The overrun branch decided a slot
        # was spent by testing the NEXT slot's early edge (`due - window <= now`) instead of its grid point.
        # The window is the room a picture has to go out EARLY, not a deadline - so a write that ended
        # anywhere in the last quarter of an interval before the next window opened threw a whole slot away.
        # A write's duration does not change from one round to the next, so it did that on EVERY round: a
        # stable halving of the wire rate, not a hiccup. Measured at 1280x720 with a writer whose duration
        # is set exactly, 5 s per case, against a dead source (every write a repeat, so it starts at its
        # grid point): 11 ms -> 60.00/s with nothing skipped, 12.5 ms -> 30.00/s with 174 slots skipped,
        # 16 ms -> 30.00/s. From a 20/s source: 60.03/s at 11 ms against 40.03/s at 13 ms.
        # A still desktop is all repeats, and the intra-refresh sweep counts PICTURES - halving the rate
        # there doubles the time a lost packet stays on screen, which is exactly where nothing else repairs
        # it. Every one of those writes fits inside its 16.67 ms slot; a slot may only be lost when the
        # write really cannot fit in one.
        interval = 1.0 / FPS
        window = interval * capture.WRITE_WINDOW_FRACTION
        for cost, floor in ((interval - window, 57.0),        # exactly where the old test tripped
                            (interval - window + 0.001, 57.0),
                            (interval * 0.95, 57.0)):
            with self.subTest(cost_ms=round(cost * 1000, 2)):
                self._reset()
                writer = _TimedWriter(cost)
                self.cap.source_period = None          # nothing new ever: every write is a repeat
                self.cap.source_script = _ONE_PICTURE
                self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
                self.feeder = _feed_thread(self.cap, writer)
                deadline = time.monotonic() + 5.0
                while not writer.times and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(writer.times, "no picture reached ffmpeg's side at all")
                time.sleep(0.4)
                del writer.times[:]
                late_before = self.cap._late_slots
                time.sleep(2.0)
                late = self.cap._late_slots - late_before
                times = list(writer.times)
                self.cap.stop()
                self.feeder.join(3.0)
                rate = FrameSink.rate(times)
                self.assertGreaterEqual(rate, floor,
                                        "a %.1f ms write - which fits in a %.2f ms slot - dropped the "
                                        "cadence to %.1f/s (%d slots thrown away)"
                                        % (cost * 1000, interval * 1000, rate, late))
                # Not zero: the tightest case here leaves 0.84 ms of the slot free, which is inside the
                # scheduler's own noise on a busy machine, and this asserts a RATE and not a schedule. The
                # bug it guards against skipped every other slot - 174 in 5 s, ~35/s - so anything that
                # keeps the rate above `floor` with a handful of skips over 2 s (120 slots) is the fix
                # working, and 20 would already mean the halving is back.
                self.assertLess(late, 20, "%d slots skipped for a write that fits in one" % late)
                sd, longest = _StampedSink.spread([(b - a) for a, b in zip(times, times[1:])])
                self.assertLess(longest, 2 * self.INTERVAL_MS, "longest gap %.1f ms" % longest)

    def test_a_write_that_cannot_fit_its_slot_skips_instead_of_bursting(self):
        # The other side of the same branch: once the write really is longer than a slot, the grid points
        # that have gone by are skipped and the phase is kept - the console throws a burst away, so there
        # is nothing to be won by writing one. What must NOT happen is the loop falling behind for ever.
        interval = 1.0 / FPS
        writer = _TimedWriter(interval * 1.6)
        self.cap.source_period = None
        self.cap.source_script = _ONE_PICTURE
        self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
        self.feeder = _feed_thread(self.cap, writer)
        deadline = time.monotonic() + 5.0
        while not writer.times and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(writer.times)
        time.sleep(0.4)
        del writer.times[:]
        late_before = self.cap._late_slots
        time.sleep(2.0)
        late = self.cap._late_slots - late_before
        times = list(writer.times)
        self.cap.stop()
        self.feeder.join(3.0)
        rate = FrameSink.rate(times)
        self.assertGreater(late, 0, "a write of 1.6 slots skipped nothing")
        self.assertTrue(25 <= rate <= 32, "an over-long write should land on the half grid, got %.1f/s" % rate)
        sd, _longest = _StampedSink.spread([(b - a) for a, b in zip(times, times[1:])])
        self.assertLess(sd, self.INTERVAL_MS / 2, "the skipped grid is uneven: sd %.2f ms" % sd)

    def test_source_at_60_comes_through_one_for_one(self):
        rate = self._run(60)
        self.assertTrue(56 <= rate <= 64, "60 fps source came out at %.1f fps" % rate)
        self.assertEqual(self.sink.torn, 0)

    def test_source_at_30_is_padded_to_the_stream_rate(self):
        # was test_source_at_30_is_followed_not_padded: the contract is the other way round now. Half the
        # console's slots would be empty otherwise, and it repeats its last picture when it is starved -
        # which is what the user reported as "~20 fps on the PS3" while every counter said the stream was
        # fine. The pictures the source does produce must still not be aged by the padding.
        rate = self._run(30)
        self.assertTrue(58 <= rate <= 62, "30 fps source came out at %.1f fps" % rate)
        self.assertLess(self.sink.new_age(0.9), 10.0)

    def test_a_dead_source_keeps_the_full_cadence(self):
        # CRITERION 5. was test_dead_source_falls_back_to_the_idle_repeat, which expected 1/IDLE_REPEAT_S
        # (~10/s). The PS3 calls 2 s without video "server gone" (SERVER_TIMEOUT_MS in stream.c), so 10/s
        # was enough for that - but the intra-refresh sweep is counted in PICTURES (-g = fps), so at 10/s a
        # lost packet took 6 s to be repaired instead of 1, and the rate change itself is what the console's
        # render loop dislikes. A frozen desktop now looks exactly like a moving one from the wire.
        self.cap.source_period = None
        self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        deadline = time.monotonic() + 5.0
        while len(self.sink.times) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.children[-1].kill()          # the source is gone; feed() must keep the last picture flowing
        time.sleep(0.3)
        del self.sink.times[:]
        time.sleep(1.5)
        rate = FrameSink.rate(self.sink.times)
        self.assertTrue(58 <= rate <= 62, "a dead source repeats at %.1f/s, expected ~%d" % (rate, FPS))
        sd, longest = _StampedSink.spread(self.sink.gaps())
        self.assertLess(longest, 2 * self.INTERVAL_MS, "longest gap %.1f ms" % longest)
        self.assertLess(sd, self.INTERVAL_MS / 4, "gaps vary by %.2f ms with nothing to send" % sd)
        self.assertEqual(self.cap.captured_fps, 0, "a source that is gone must not still report fps")

    def test_an_over_producing_source_never_hands_over_half_a_picture(self):
        # the reader refills two buffers as fast as the pipe allows while feed() writes out the third: if
        # _latest/_writing did not fence them off, a picture built out of two source pictures would show up
        rate = self._run(None, seconds=3.0)
        self.assertTrue(54 <= rate <= 66, "uncapped source came out at %.1f fps" % rate)
        self.assertEqual(self.sink.partial, 0, "a short picture reached ffmpeg")
        self.assertEqual(self.sink.torn, 0, "%d of %d pictures were assembled out of two source pictures"
                         % (self.sink.torn, len(self.sink.times)))
        self.assertGreater(self.cap._source_frames, 20 * self.cap._sent_frames,
                           "the source did not actually outrun feed() (%d vs %d) - the test proved nothing"
                           % (self.cap._source_frames, self.cap._sent_frames))
        # and it must WAIT for those pictures, not spin through them: one wake-up per picture a runaway
        # source produces would burn a core and starve the encoder pump of the GIL
        self.assertLess(self.cpu["seconds"], 0.5, "feed() burned %.2f s of CPU in 3 s" % self.cpu["seconds"])

    def test_captured_fps_goes_quiet_with_the_source(self):
        self.cap.source_period = 1.0 / 60
        self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        time.sleep(1.4)
        self.assertTrue(40 <= self.cap.captured_fps <= 70, "captured_fps = %d" % self.cap.captured_fps)
        self.children[-1].send_signal(signal.SIGSTOP)   # alive, but not a picture any more
        try:
            time.sleep(capture.STALE_FPS_S + 0.6)
            self.assertEqual(self.cap.captured_fps, 0,
                             "a stalled source still reports its last number (%d)" % self.cap.captured_fps)
        finally:
            self.children[-1].send_signal(signal.SIGCONT)

    def test_a_reader_that_outlived_its_join_cannot_publish_into_the_next_run(self):
        # stop() joins the reader before a start() swaps the buffer list, but that join has a 2 s timeout.
        # A reader that ran past it used to follow self._buffers, so it would publish an index into the NEW
        # list - a buffer the new run's reader has never filled - and the next slot would hand that blank
        # picture to ffmpeg. The swap is done here directly, which is exactly what the reader would see.
        self.cap.source_period = 1.0 / 30
        self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
        deadline = time.monotonic() + 5.0
        while self.cap._generation == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(self.cap._generation, 0, "the source never delivered")
        fresh = [bytearray(PACE_FRAME) for _ in range(3)]
        self.cap._buffers = fresh                      # a start() that raced this reader
        generation = self.cap._generation
        time.sleep(0.4)                                # ~12 source pictures' worth
        self.assertEqual(self.cap._generation, generation,
                         "the old reader published %d more pictures into the new run's buffers"
                         % (self.cap._generation - generation))
        self.assertTrue(all(not any(buffer) for buffer in fresh),
                        "the old reader wrote into a buffer list that is not its own")

    def test_stop_wakes_a_feed_parked_on_the_condition(self):
        self.cap.source_period = 1.0 / 60
        self.assertTrue(self.cap.start(PACE_W, PACE_H, FPS))
        self.feeder = _feed_thread(self.cap, self.sink.write_fd)
        deadline = time.monotonic() + 5.0
        while len(self.sink.times) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.children[-1].send_signal(signal.SIGSTOP)   # no more pictures: feed() parks in Condition.wait
        time.sleep(0.25)
        # stop() runs on a thread of its own here because it also has to terminate the source, and a source
        # that is SIGSTOPped cannot answer SIGTERM - it costs the full GST_EXIT_WAIT_S before the kill. What
        # is measured is that feed() is let go FIRST (stop sets the event and notifies before any of that).
        started = time.monotonic()
        stopper = threading.Thread(target=self.cap.stop, name="test-stopper", daemon=True)
        stopper.start()
        self.feeder.join(1.0)
        elapsed = time.monotonic() - started
        self.children[-1].send_signal(signal.SIGCONT)
        stopper.join(5.0)
        self.assertFalse(self.feeder.is_alive(), "feed() slept through stop()")
        self.assertLess(elapsed, 0.5, "feed() needed %.2f s to come out of the Condition" % elapsed)


class LifecycleTests(unittest.TestCase):
    """Ten cycles and an overlapping start/stop: nothing left running, nothing left open."""

    def test_ten_cycles_hold_the_cadence_and_leak_nothing(self):
        # CRITERION 7, with the pacing in it: every cycle must reach the full stream rate from a source that
        # delivers a third of it, and ten of them must leave no thread, fd or child behind. feed() has two
        # wait sites now (the Condition while nothing is pending, the stop Event while a picture waits for
        # its slot) and stop() has to end both - a leak here would be a feed() that outlived its stream.
        saved = capture._popen
        children: list[subprocess.Popen] = []

        def fake_popen(args, **kw):
            kw.pop("pass_fds", None)
            child = subprocess.Popen([sys.executable, "-u", "-c", _WRITER % (PACE_FRAME, repr(1.0 / 20))], **kw)
            children.append(child)
            return child
        capture._popen = fake_popen
        cap = _WrittenSource()
        rates = []
        try:
            def cycle(index):
                self.assertTrue(cap.start(PACE_W, PACE_H, FPS), "start %d failed" % index)
                sink = _StampedSink()
                feeder = _feed_thread(cap, sink.write_fd)
                time.sleep(1.0)
                cap.stop()
                feeder.join(2.0)
                self.assertFalse(feeder.is_alive(), "feed() outlived stop() in cycle %d" % index)
                sink.close()
                rates.append(FrameSink.rate(sink.times[3:]))

            cycle(0)                     # one warm-up cycle, so the baseline is the steady state
            time.sleep(0.2)
            fds_before = sorted(os.listdir("/proc/self/fd"))
            threads_before = threading.active_count()
            for index in range(1, 11):
                cycle(index)
            for child in children:
                self.assertTrue(_wait_gone(child.pid, 3.0), "source %d left behind" % child.pid)
            time.sleep(0.3)
            leaked = set(os.listdir("/proc/self/fd")) - set(fds_before)
            self.assertEqual(leaked, set(), "fd leak over 10 start/stop cycles: %s" % sorted(leaked))
            self.assertLessEqual(threading.active_count(), threads_before,
                                 "thread leak over 10 cycles: %s" % sorted(t.name for t in threading.enumerate()))
            for index, rate in enumerate(rates[1:], 1):
                self.assertTrue(56 <= rate <= 64, "cycle %d ran at %.1f/s from a 20/s source" % (index, rate))
        finally:
            capture._popen = saved
            cap.stop()
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    @unittest.skipUnless(HAVE_GST, "gst-launch-1.0 with videotestsrc/videoconvertscale not installed")
    def test_ten_start_stop_cycles_leak_nothing(self):
        cap = capture.TestCapture()
        pids = []

        def cycle(index):
            self.assertTrue(cap.start(320, 240, FPS), "start %d failed" % index)
            pids.append(cap._process.pid)
            read_fd, write_fd = os.pipe()
            drain = threading.Thread(target=lambda: _swallow(read_fd), name="test-drain", daemon=True)
            drain.start()
            feeder = _feed_thread(cap, write_fd)
            time.sleep(0.4)
            cap.stop()
            feeder.join(2.0)
            self.assertFalse(feeder.is_alive(), "feed() outlived stop() in cycle %d" % index)
            os.close(write_fd)
            drain.join(2.0)

        cycle(0)                       # one warm-up cycle, so the baseline is the steady state
        time.sleep(0.2)
        fds_before = sorted(os.listdir("/proc/self/fd"))
        threads_before = threading.active_count()
        for index in range(1, 11):
            cycle(index)
        for pid in pids:
            self.assertTrue(_wait_gone(pid, 3.0), "gst-launch %d left behind" % pid)
        time.sleep(0.3)
        leaked = set(os.listdir("/proc/self/fd")) - set(fds_before)
        self.assertEqual(leaked, set(), "fd leak over 10 start/stop cycles: %s" % sorted(leaked))
        self.assertLessEqual(threading.active_count(), threads_before,
                             "thread leak over 10 cycles: %s" % sorted(t.name for t in threading.enumerate()))

    def test_a_stop_still_running_must_not_close_the_next_start_s_session(self):
        # a portal Session.Close is a blocking DBus round trip. While one was in flight, a start() from
        # another thread used to install a fresh session that the finishing stop() then closed - leaving a
        # gst-launch reading a PipeWire node nobody shares any more (black picture, whole encoder ladder
        # walked for nothing).
        class Slow(capture._PipeCapture):
            name = "slow"

            def __init__(self):
                super().__init__()
                self.opened, self.closed = [], []

            def _open_source(self):
                self._session = object()
                self.opened.append(self._session)
                return ["slow"]

            def _close_source(self):
                time.sleep(0.4)
                session = getattr(self, "_session", None)
                if session is not None:
                    self.closed.append(session)
                self._session = None

        saved = capture._popen
        capture._popen = lambda args, **kw: subprocess.Popen(["sleep", "30"], **{k: v for k, v in kw.items() if k != "pass_fds"})
        cap = Slow()
        try:
            self.assertTrue(cap.start(PACE_W, PACE_H, FPS))
            stopper = threading.Thread(target=cap.stop, name="test-stopper", daemon=True)
            stopper.start()
            time.sleep(0.05)          # stop() is inside the slow close now
            self.assertTrue(cap.start(PACE_W, PACE_H, FPS))
            second = cap.opened[-1]
            stopper.join(5.0)
            self.assertFalse(stopper.is_alive())
            self.assertNotIn(second, cap.closed, "the finishing stop() closed the session start() had just opened")
            self.assertIsNotNone(cap._process)
            self.assertIsNone(cap._process.poll(), "the new pipeline was left without its session")
        finally:
            cap.stop()
            capture._popen = saved


def _swallow(read_fd: int) -> None:
    while True:
        try:
            if not os.read(read_fd, 1 << 20):
                break
        except OSError:
            break
    os.close(read_fd)



class SmoothnessReport(unittest.TestCase):
    """60 pictures a second can still judder: the count says nothing about how evenly spaced their content
    is. These are the four shapes the report has to tell apart."""

    def _report(self, gaps, fps=60):
        instance = capture.PortalCapture.__new__(capture.PortalCapture)
        instance.fps = fps
        instance._content_gaps_ms = list(gaps)
        return instance.smoothness_report()

    def test_an_even_stream_reads_as_fully_in_time(self):
        self.assertIn("100% on the cadence", self._report([16.7] * 100))
        self.assertIn("0 visible hitches", self._report([16.7] * 100))

    def test_a_beat_between_source_and_grid_reads_as_out_of_time(self):
        # 60 pictures a second leave, but their content alternates 8 ms and 25 ms apart: the count is
        # perfect and the motion is not. Nothing but this measurement can see it.
        self.assertIn("0% on the cadence", self._report([8.0, 25.0] * 50))

    def test_a_genuinely_slower_source_shows_up_in_the_median(self):
        report = self._report([25.0] * 100)
        self.assertIn("median gap 25.0 ms", report)
        self.assertIn("0% on the cadence", report)

    def test_held_pictures_are_counted_separately(self):
        self.assertIn("10 visible hitches", self._report([16.7] * 90 + [33.4] * 10))

    def test_too_few_samples_say_so_instead_of_inventing_a_number(self):
        self.assertIn("too few", self._report([16.7] * 5))


if __name__ == "__main__":
    unittest.main()

