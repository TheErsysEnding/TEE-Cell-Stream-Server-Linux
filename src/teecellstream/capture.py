"""Screen capture backends: where the desktop picture comes from before ffmpeg encodes it.

Windows had ddagrab inside ffmpeg. Linux has no single equivalent: on Wayland the picture arrives through
xdg-desktop-portal as a PipeWire stream, which the packaged ffmpeg cannot read - so a gst-launch-1.0
subprocess turns it into raw I420 frames on a pipe that ffmpeg reads as rawvideo. On plain X11 ffmpeg's
x11grab reads the screen itself. A videotestsrc backend stands in for the desktop in tests.

The pipe backends hand ffmpeg exactly `fps` pictures a second, on a grid that follows the source (see the
pacing note in feed()): the portal only delivers when something moves, so the source's own rate swings
between 0 and 240 a second, and the console wants one cadence. A picture that is there goes out at once;
a slot with nothing new re-sends the last one, which also keeps the PS3 (2 s without video is "the server
is gone" to it) and the intra-refresh sweep going.
"""

import fcntl
import os
import subprocess
import threading
import time

from . import log, portal
from .settings import settings

GST_LAUNCH = "gst-launch-1.0"
GST_EXIT_WAIT_S = 2.0
STDERR_TAIL_CHARS = 2000
FIRST_FRAME_POLL_S = 0.005
# How far BEFORE its grid point a picture that is already waiting may go out, as a fraction of the frame
# interval (4.17 ms at 60 fps). It is the room a source running at about `fps` needs so a picture arriving a
# hair early is not pushed into the next slot, and it is the whole of the jitter feed() adds: writes land in
# [due - WRITE_WINDOW, due], gaps in [T - WRITE_WINDOW, T + WRITE_WINDOW]. It trades the two criteria that
# pull against each other, measured against a source at 42/s (what GNOME's ScreenCast delivers while a game
# runs), 10 s each: 0.15 -> gap stddev 1.87 ms but a new picture ages 5.98 ms; 0.35 -> age 2.45 ms but stddev
# 4.18 ms, a full quarter of the interval. 0.25 is the knee (stddev 3.07 ms, age 4.34 ms), and evenness is
# the one the console reacts to - 4 ms of age sits on a link measured at 25-27 ms end to end.
WRITE_WINDOW_FRACTION = 0.25
# The grid follows the source's phase, or a source running at exactly `fps` in an unlucky phase has EVERY
# picture wait most of a slot - and it stays unlucky for hours, because the two clocks agree to a few ppm
# (measured against a 60 Hz source, 6 s: median age 12.57 ms with the servo off, 0.04 ms with it on).
# Correction = gain x (published - the EARLY EDGE of the window), slew-limited per write; see the servo
# paragraph in feed() for why the early edge and not its middle.
SERVO_GAIN = 0.3
# The most one slot may be moved, as a fraction of the interval. It is three things at once: the widest the
# grid's own rate may stray from `fps`, the fastest source rate the grid can still FOLLOW, and how long it
# takes to walk out of a cycle slip (a whole interval of phase, so 1/this many writes).
# It is set to the acceptance bar itself, +-2 pictures a second at 60 fps: anything the grid can follow
# inside that band is a source we may as well follow exactly, and anything outside it we must not.
# This is not a free choice - it is what the REAL portal needs. Measured on this PC's GNOME 50 ScreenCast
# with next to nothing moving on a 1920x1080@240 desktop, 25 s: the source delivers 61.9 pictures a second
# (arrival gaps mean 16.13 ms, sd 1.14), i.e. just OVER our 60, and at 1% the grid tops out at 60.6 and can
# never catch it - the arrival phase then walks through the whole slot every 0.4 s and EVERY picture waits:
#     slew 1%    60.00/s out, gaps sd 0.15 ms, a new picture ages 7.92 ms median / 14.58 p90 / 16.65 max
#     slew 3.3%  62.00/s out, gaps sd 0.03 ms, a new picture ages 1.90 ms median / 2.35 p90 / 2.7 max
# and 496 of 496 pictures went out in the second case against 481 of 496 in the first. Synthetic sources at
# exactly 62/s reproduce both rows. The old 1% was chosen against the rate it caused at a 42/s source
# (60.66/s at 2%); that bias is gone for a different reason - see SERVO_GATE_WRITES - so the slew is free.
# Downwards it stops working: at 0.25% a source running at 60.2/s is no longer tracked (age 3.33 ms
# instead of 0.05), and at 1% one running at 62/s is not (the table above).
# KNOW WHAT THIS BUYS AND WHAT IT COSTS. Following a source's phase inside the band means following its
# RATE: once the grid has locked, the source hands us exactly one picture per slot for ever, so nothing ever
# closes SERVO_GATE_WRITES again and the grid simply runs at the source's rate. Measured, 8 s per source at
# 1280x720: 57/s in -> 60.01/s out, 58 -> 59.85, 59 -> 59.00, 60.1 -> 60.10, 62 -> 62.00, 62.5 -> 60.14,
# 63 and above -> 60.00. So `fps` on the wire is exact only OUTSIDE this band; inside it the wire carries
# the source. On this PC that is 62/s, and the console decodes those two extra pictures a second and then
# throws them away (stream.c nextDrawIndexLocked clamps to publishSeq - 1), and the encoder spends 3.3% more
# than the bitrate it was given per second, because ffmpeg was told -framerate `fps`. It is worth it at
# 61.9/s - 1.9 ms of picture age against 7.9 - but it is a trade, not a free win, and the number to change
# if the console ever needs `fps` kept honest is this one.
SERVO_SLEW_FRACTION = 0.0333
# The servo only has a phase to lock while the source hands us EXACTLY one picture per slot. A repeat says
# the source is slower than we are, a superseded picture that it is faster than the grid can follow; neither
# has a phase, and chasing one only drags the rate along. So any write that was not one-for-one closes the
# gate, and this is how many one-for-one writes must pass before the servo is trusted again (0.5 s at 60 fps).
# Both halves are measured, 8 s per source, with the slew at 3.3%:
#   - closing on a repeat is what pays for the wider slew: a 42/s source came out at 60.80/s with only the
#     over-rate half of the gate and at 60.04/s with both (a 5/s source: 60.00/s, gaps sd 1.66 -> 0.91 ms).
#   - closing on a superseded picture is what keeps a fast source at `fps`: 63/s in gave 62.00/s without it
#     and 60.00/s with it, 90/s gave 61.07/s without and 60.00/s with.
# It does not shut the servo out of the case it exists for: a source at 60/s (or 62, or 59.94) delivers one
# picture per slot by definition, so the gate stands open there and the grid locks (age 0.02-0.09 ms).
SERVO_GATE_WRITES = 30
# A live capture that has produced nothing new for this long gets the frozen-capture hint, once per stream.
# Timed, not counted in repeats: repeats now leave here at `fps`, so counting them would measure nothing.
FROZEN_HINT_S = 3.0
# Per-second trace of what the source delivers, into the normal log. Off unless TEE_CST_TRACE is set, because
# it writes a line a second for as long as a stream runs - useful for a diagnosis session, noise otherwise.
# It is the only way to watch the source rate WITHOUT the server window in front, and the window being in
# front is itself one of the things that raises the rate (every redraw of it is another picture).
TRACE_SOURCE_RATE = bool(os.environ.get("TEE_CST_TRACE"))

# A source that has been quiet this long counts as 0 fps. captured_fps is only ever written when a picture
# arrives, so a stalled source would otherwise report its last number for the rest of the session.
STALE_FPS_S = 2.0
# the unprivileged maximum (/proc/sys/fs/pipe-max-size). a 720p frame (1.35 MB) still does not fit whole,
# but 16x the default 64 KiB means far fewer wake-ups per frame on both sides of each pipe
PIPE_SIZE_BYTES = 1024 * 1024


def frame_bytes(width: int, height: int) -> int:
    """I420: full-size luma plus two quarter-size chroma planes."""
    return width * height * 3 // 2


def _popen(args, **kw) -> subprocess.Popen:
    """childproc.popen (children die with us - PDEATHSIG) when it is there, plain Popen otherwise."""
    try:
        from .childproc import popen
    except ImportError:
        popen = subprocess.Popen
    return popen(args, **kw)


def _grow_pipe(fd: int) -> None:
    try:
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_SIZE_BYTES)
    except OSError:
        pass   # over the limit or not a pipe: the default size works, just with more wake-ups


class ScreenCapture:
    """What every backend offers LiveStreamer. The base itself captures nothing."""

    name = "none"
    needs_scale = False        # True when ffmpeg must scale the input itself (x11grab delivers the full desktop)

    def __init__(self):
        self.captured_fps = 0  # frames the source delivered in the last second (statistics only)

    def start(self, width: int, height: int, fps: int) -> bool:
        return False

    def ffmpeg_input_args(self) -> list[str]:
        return []

    def feed(self, ffmpeg_stdin) -> None:
        """Blocks until stop(); raw-pipe backends write frames to ffmpeg_stdin here."""

    def stop(self) -> None:
        pass


# BGRA -> I420, either on the CPU or on the GPU.
#
# The compositor hands its pictures over as DMA-BUFs living in graphics memory. Converting them on the CPU
# means reading 3.5 MB of that memory per picture across the bus, uncached - the slowest kind of read there
# is on an NVIDIA card - and if the consumer cannot keep the compositor's pace, Mutter simply skips the
# pictures it cannot hand over. Measured against the real console: a steady 40 of 60 frames, with the gaps
# landing exactly on the 60 Hz grid (the signature of a producer skipping, not of a slow source).
# The GPU path uploads the DMA-BUF as a texture (no copy), converts there, and downloads I420 - which is
# also 2.5x less data to move (1.38 MB against 3.5 MB).
def _convert_stage(width: int, height: int) -> list[str]:
    target = "video/x-raw,format=I420,width=%d,height=%d,colorimetry=bt709,pixel-aspect-ratio=1/1" % (width, height)
    if not os.environ.get("TEE_CST_GPU_CONVERT"):
        return ["!", "videoconvertscale", "n-threads=4", "add-borders=false", "!", target]
    # Opt-in: scheitert auf diesem PC an glupload (kein GL-Kontext im Unterprozess, DMA-BUF-Import)
    return ["!", "glupload",
            "!", "glcolorscale",
            "!", "glcolorconvert",
            "!", "video/x-raw(memory:GLMemory),format=I420,width=%d,height=%d" % (width, height),
            "!", "gldownload",
            "!", target]



class _PipeCapture(ScreenCapture):
    """GStreamer subprocess -> raw I420 frames -> feed() -> ffmpeg's stdin.

    A reader thread keeps the newest complete frame and counts a generation up; feed() writes exactly `fps`
    pictures a second on a grid that follows that generation - the newest one there is when the slot comes,
    the last one again when the source had nothing. Three buffers are what makes that safe without copying:
    one the reader is filling, one holding the newest, one feed() may still be writing out (_latest and
    _writing, both only ever changed under _gate - the reader picks neither).
    """

    def __init__(self):
        super().__init__()
        self.width = self.height = self.fps = 0
        self._frame_bytes = 0
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._gate = threading.Lock()
        self._buffers: list[bytearray] = []
        self._latest = -1           # index of the newest complete frame, -1 until the first arrives
        self._generation = 0        # bumped for every picture the source delivers; feed() sends on the change
        self._published_at = 0.0    # when the reader published _latest - feed()'s servo locks its grid to it
        self._fresh = threading.Condition(self._gate)
        self._writing = -1          # index feed() is currently pushing to ffmpeg
        self._source_frames = 0
        self._sent_frames = 0
        self._new_frames = 0        # of those, pictures the source had actually changed
        self._repeat_frames = 0     # ...the last one sent again because the slot found nothing new
        self._skipped_frames = 0    # source pictures a NEWER one took the place of (never a repeat)
        self._late_slots = 0        # slots lost because a write ran past its own grid point
        # Smoothness, which the frame COUNT cannot show: 60 pictures a second leave even when the content
        # in them is unevenly spaced in time, and that is what an eye reads as judder. These record the gap
        # between the publish times of consecutive DISTINCT pictures as they actually went out.
        self._content_gaps_ms: list[float] = []
        self._last_published_sent = 0.0
        self._reader: threading.Thread | None = None
        self._drain: threading.Thread | None = None
        self._stderr_tail = ""
        self._started_at = 0.0
        # start() and stop() against each other. Without it a stop() still inside its (blocking) portal
        # Session.Close tears down the session a start() has meanwhile installed, and the fresh gst-launch
        # is left reading a PipeWire node nobody shares any more - proven with a slowed-down close.
        self._lifecycle = threading.RLock()

    @property
    def captured_fps(self) -> int:
        """Pictures the source delivered in the last full second, 0 once it has gone quiet."""
        if self._captured_fps and time.monotonic() - self._fps_window_end > STALE_FPS_S:
            return 0
        return self._captured_fps

    @captured_fps.setter
    def captured_fps(self, value: int) -> None:
        self._captured_fps = int(value)
        self._fps_window_end = time.monotonic()

    # -- what a backend fills in
    def _open_source(self) -> list[str]:
        """The gst-launch elements that produce the picture (up to, not including, the queue)."""
        raise NotImplementedError

    def _source_fds(self) -> tuple:
        return ()

    def _after_spawn(self) -> None:
        pass

    def _close_source(self) -> None:
        pass

    # -- ScreenCapture
    def start(self, width: int, height: int, fps: int) -> bool:
        with self._lifecycle:
            return self._start_locked(width, height, fps)

    def _start_locked(self, width: int, height: int, fps: int) -> bool:
        self.stop()   # a pipeline left over from an earlier run must not double up
        self.width, self.height, self.fps = width, height, fps
        self._frame_bytes = frame_bytes(width, height)
        self._buffers = [bytearray(self._frame_bytes) for _ in range(3)]
        self._latest = self._writing = -1
        self._source_frames = self._sent_frames = 0
        self._new_frames = self._repeat_frames = self._skipped_frames = self._late_slots = 0
        self._content_gaps_ms = []
        self._last_published_sent = 0.0
        self._stderr_tail = ""
        self._stop = threading.Event()

        try:
            source = self._open_source()
        except portal.PortalError:
            return False   # the portal already said why
        except Exception as error:   # noqa: BLE001 - a broken source must not take the receive thread down
            log.write("capture: %s: Quelle lässt sich nicht öffnen: %s" % (self.name, error))
            return False

        args = [GST_LAUNCH, "-q", *source,
                # Slack between the compositor and our reader. Two buffers was too tight: the reader is a
                # Python thread sharing the GIL with the feeder, the encoder pump and the sender, so a stall
                # of one frame interval was enough for this queue to LEAK (drop the oldest) - measured against
                # the real PS3 as a rock-steady 41 fps out of a 60 fps source, with the frame gaps showing the
                # signature (median 17.6 ms, mean 24.1 ms: it flows at 60 and then skips). Eight buffers ride
                # out a 130 ms hiccup; leaky stays, because on a live stream a late picture is worthless.
                # Open: a later run with a fullscreen game on screen still saw ~42 of 60 frames arrive, and
                # going 2 -> 8 changed nothing there - so 8 is kept as cheap slack, not as a proven cure.
                "!", "queue", "max-size-buffers=8", "max-size-time=0", "max-size-bytes=0", "leaky=downstream",
                ] + _convert_stage(width, height) + [
                "!", "fdsink", "fd=1", "sync=false"]
        try:
            self._process = _popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=self._source_fds())
        except Exception as error:   # noqa: BLE001 - whatever the spawner throws, the portal session must not leak
            log.write("capture: %s startet nicht: %s" % (GST_LAUNCH, error))
            self._close_source()
            return False
        self._after_spawn()
        self._started_at = time.monotonic()
        _grow_pipe(self._process.stdout.fileno())

        self._drain = threading.Thread(target=self._run_drain, args=(self._process,), name="capture-stderr", daemon=True)
        self._drain.start()
        self._reader = threading.Thread(target=self._run_reader, args=(self._process, self._stop), name="capture-reader", daemon=True)
        self._reader.start()
        log.write("capture: %s gestartet (%dx%d, %d fps, pid %d)" % (self.name, width, height, fps, self._process.pid))
        return True

    def ffmpeg_input_args(self) -> list[str]:
        # probesize/analyzeduration: rawvideo needs no probing, and the defaults would hold the first
        # frames back while ffmpeg "analyses" a stream whose format it was told outright.
        #
        # -framerate is simply the truth now: feed() hands over exactly `fps` pictures a second whatever the
        # source does, so rawvideo's PTS = n/framerate is the wall clock and the encoder's rate control sees
        # the timeline it was configured for. It was NOT true while feed() forwarded the source's own rate,
        # and it did not hurt then either - measured with the real nvenc line (CAVLC, 6000 kbit/s, intra
        # refresh) over 20 s from a 60 Hz and a 42 Hz source: wire bitrate 5898 -> 4136 kbit/s (exactly
        # x42/60, i.e. never over budget), picture size median 12649 -> 11301 bytes, recovery_point SEI every
        # 60 pictures in both. That case cannot arise any more; what remains is why the fix is not
        # -use_wallclock_as_timestamps 1, in case anyone reaches for it: it would price the stream per second
        # instead of per picture, and the PS3's decoder - the bottleneck at 19-20 ms per picture with CAVLC -
        # is priced per picture. Timing never reaches the console anyway: the VF header carries frameId and
        # encoderExitUs, no PTS, and stream.c shows each access unit as it arrives.
        return ["-probesize", "32", "-analyzeduration", "0",
                "-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", "%dx%d" % (self.width, self.height),
                "-framerate", str(self.fps), "-i", "pipe:0"]

    def feed(self, ffmpeg_stdin) -> None:
        stop = self._stop
        reader = self._reader
        if self._process is None or reader is None or self.fps <= 0:
            return
        write = self._make_writer(ffmpeg_stdin)
        interval = 1.0 / self.fps

        # nothing to send until the source has delivered once; a source that dies before that never will
        while self._latest < 0:
            if stop.wait(FIRST_FRAME_POLL_S):
                return
            if not reader.is_alive() and self._latest < 0:
                log.write("capture: die Quelle lieferte kein einziges Bild")
                return
        log.write("capture: sende Bilder an ffmpeg (erstes %d ms nach dem Start)" % int((time.monotonic() - self._started_at) * 1000))

        buffers = self._buffers   # this run's buffers: a start() that re-used the instance must not be touched by us

        # PACING: exactly one picture per 1/fps leaves here, as early in its slot as there is one to send.
        #
        # `due` is a grid point, advanced by `due += interval` from the PREVIOUS grid point - never from the
        # end of the write. A deadline of "last write + 1/fps" moves later by the write's own duration every
        # round (a 720p picture is 1.38 MB), so against a source at or above the rate it delayed EVERY picture:
        # measured 56.9 pictures a second at a median age of 25.9 ms, on a link that is 25-27 ms end to end.
        #
        # Why a grid at all, when following the source one-for-one is what killed the beat of the first fixed
        # 60 Hz clock: because GNOME's ScreenCast is damage-driven and its rate is not ours to choose. Typing
        # in a terminal produces ~20 pictures a second, a still desktop 10 (pipewiresrc's keepalive), a game
        # 42, mouse movement 60-240; forwarding that verbatim showed on the console as ~20 fps and a stats
        # panel full of red bars, because the PS3 takes one decoded picture per refresh out of a one-frame
        # reserve and repeats the last one whenever it is starved (stream.c, takeFrameForDisplay). ddagrab on
        # Windows duplicated up to a constant 60 and the console app was written against that.
        #
        # What is NOT a repeat of the old fixed clock: the beat came from a free-running grid crossing the
        # source's phase, sending one picture twice and skipping the one between. Neither can happen here.
        #  - `seen` is assigned only in the same breath as the write, so a picture is only ever passed over
        #    because a NEWER one took its place; a repeat goes out only when nothing at all is pending.
        #  - the grid FOLLOWS the source: a picture that had to wait pulls the next grid point towards it (the
        #    servo below), so the phase is held inside the window instead of wandering across it.
        #
        # The window is the slack: a picture that is already there goes out at `due - window` at the earliest,
        # so an arrival a hair early is not pushed into the next slot, and one arriving inside the window goes
        # out the moment it lands. Writes therefore land in [due - window, due] and nowhere else.
        #
        # Measured on this PC with REAL 1280x720 pictures (1.38 MB per write, real reader thread and pipes,
        # source rate set exactly, 8 s per case, re-measured after the overrun fix below):
        #     source    out/s   gap mean/stddev/p99/max ms   age of a new picture med/p90   pictures in -> out
        #     dead      60.00   16.67 / 0.12 / 17.0 / 17.1   -                              0 -> 480 repeats
        #     5/s       60.03   16.66 / 1.52 / 20.6 / 20.7   2.07 /  2.31 ms                40 -> 40 + 440 repeats
        #     20/s      60.02   16.66 / 2.12 / 19.5 / 19.7   2.03 /  2.27 ms                160 -> 160 + 320 repeats
        #     42/s      60.03   16.66 / 3.06 / 21.1 / 21.3   5.98 / 13.09 ms                336 -> 336 + 144 repeats
        #     60/s      60.00   16.67 / 0.14 / 17.0 / 17.6   1.95 /  2.18 ms                480 -> 480 + 0 repeats
        #     90/s      60.00   16.67 / 0.10 / 16.9 / 17.0   8.75 /  8.98 ms                720 -> 480, 240 superseded
        #     240/s     60.00   16.67 / 0.11 / 17.0 / 17.2   4.50 /  4.64 ms                1920 -> 480, 1440 superseded
        #     flat out  60.00   16.67 / 0.08 / 16.9 / 17.0   2.22 /  2.79 ms                6275 -> 480
        #   The age column is the picture's whole way from the source process's own stamp to ffmpeg's side,
        #   so it carries three crossings of a 1.38 MB frame: about 1.9 ms of it is that traffic and is the
        #   floor here, not the loop. The 0.04-0.05 ms this table used to claim for 5/20/60 is the SAME loop
        #   measured with 64x48 pictures - the size PacingTests uses - and it still reproduces there exactly
        #   (0.05 ms at 20/s and 60/s, 4.60 ms at 42/s, gap stddev 0.02-3.07 ms). Both are true; only one of
        #   them is what the console gets.
        #   The RATE column holds only outside `fps` +- SERVO_SLEW_FRACTION. Inside that band the grid locks
        #   onto the source and carries its rate: measured 59/s in -> 59.00/s out, 60.1 -> 60.10, 62 -> 62.00,
        #   62.5 -> 60.14, 63 and above -> 60.00. See SERVO_SLEW_FRACTION for why, and README/SPEC for what it
        #   means on the wire.
        #   The shipped loop on the same runs: dead 9.9/s (101 ms gaps), 5 -> 10.0, 20 -> 20.0, 42 -> 42.0,
        #   60 -> 60.0, 90 -> 60.0 but with a gap stddev of 4.97 ms (9.5 ms next to 24.7 ms: it writes on
        #   arrival, so two pictures inside one slot go out 10 ms apart), 240 -> 60.0. That is problem (a):
        #   whatever the source does below 60 reaches the console unchanged. Over 30 s every source picture
        #   is accounted for - 42/s: 1303 from the source = 1303 written + 0 superseded; 60/s: 1861 = 1860 +
        #   0, the one left over being the picture published between the last write and stop().
        #   The 42/s row is where the two criteria meet: 4.4 ms of age is the price of gaps that vary by
        #   3.1 ms instead of the source's own 23.8 ms rhythm. WRITE_WINDOW_FRACTION is the knob.
        #
        # Over the rate the OLDEST picture is what is lost, never the newest and never a generation nobody
        # wrote: while the loop sleeps out the rest of a slot the reader keeps replacing `_latest`, and the
        # deadline takes whatever is newest then. The old loop set `seen` BEFORE deciding it was over the
        # rate, so that picture was consumed unwritten and the loop then waited for the NEXT one - measured
        # with a salvo source (5 pictures 4 ms apart, then quiet, i.e. what a window redraw looks like):
        #     shipped:   20.3 writes/s, gap stddev 42.5 ms, longest gap 115.6 ms, picture age p99 110 ms;
        #                527 pictures from the source, 427 to ffmpeg - 100 consumed without being written
        #     this loop: 60.0 writes/s, gap stddev 1.51 ms, longest gap 20.9 ms, age p99 10.6 ms;
        #                527 from the source = 317 written + 209 superseded by a newer one, none swallowed
        #
        # The repeats keep running at the full rate for as long as the stream lasts - no backing off after N
        # still seconds, although each one costs the console a decode. Three reasons, in order of weight:
        # the intra-refresh sweep is counted in PICTURES (-g = fps, and the recovery_point SEI was measured
        # landing every 60 of them whatever the input rate), so at 10 repeats a second the self-repair after
        # a lost packet takes 6 s instead of 1 - on a STILL desktop, which is exactly where nothing else will
        # ever overwrite the damage; a back-off is itself a rate change, and a changing rate is the whole
        # complaint this loop exists to answer, with the worst possible moment being the first mouse movement
        # after the idle; and with intra refresh an unchanged picture is a refresh strip plus skip macroblocks,
        # so it is cheap in the only currency the console counts, bits. ddagrab on Windows sent 60 always.
        #
        # What the loop itself costs: against a source producing 321000 pictures a second it wrote its 300 in
        # 5 s at a gap stddev of 0.05 ms and burned 0.028 s of its own thread CPU - it waits, it never spins,
        # because with a picture in hand it sleeps on the stop Event where the reader cannot reach it. Ten
        # start/stop cycles through the real gst pipeline: fds 5 -> 5, threads 2 -> 2, no child left behind,
        # 60.0 writes a second in every one of them.
        seen = 0                    # generation of the last picture WRITTEN
        interval = 1.0 / self.fps
        window = interval * WRITE_WINDOW_FRACTION
        slew = interval * SERVO_SLEW_FRACTION
        # Where in the window the servo parks the source's arrivals: on its EARLY EDGE, i.e. on the
        # earliest instant a picture may leave. Everything from there to `due` is then slip margin - an
        # arrival that jitters up to a whole window late still lands in its own slot and goes out on
        # arrival, instead of missing it and waiting most of the next one (the cycle slip below).
        # Measured against a 60 Hz source jittered by +-2 ms, which is what a compositor-paced source
        # really looks like, 3 runs of 10 s each: parked on the middle of the window, 25-32 % of the
        # pictures waited more than half a frame (median age 3.6 ms, p90 12.0); parked here, NONE did
        # (median 0.11 ms, p90 1.56, and the gaps came out no worse: stddev 0.92 against 1.01 ms).
        # It costs nothing where the source is clean (a dead-on 60 Hz source ages 0.10 ms instead of
        # 0.04) and nothing where it is too rough for any margin to help (+-6 ms of jitter: 34 % against
        # 26 %, inside the run-to-run spread). Parking it EARLIER still - 1.5 windows - buys margin
        # nobody needs by making every picture wait: 2.21 ms median at a clean 60 Hz.
        target = window
        due = time.monotonic()      # the grid starts at the first picture
        last_new = due
        clean_writes = SERVO_GATE_WRITES + 1   # writes since one found more than a picture waiting
        frozen_logged = False
        while not stop.is_set():
            index = -1
            hold = 0.0
            with self._fresh:
                if self._buffers is not buffers or stop.is_set():
                    break                            # a start() re-used this instance, or stop(): both are
                                                     # tested under the lock, so a stop() cannot slip between
                                                     # the test and the Condition wait below and be missed
                now = time.monotonic()
                pending = self._generation != seen
                ready = (due - window) if pending else due
                if now < ready:
                    # Two waits, and which is which matters. With a picture in hand nothing that arrives can
                    # change what happens at the deadline (we take the newest there is), so we sleep on the
                    # stop Event, deaf to the reader - a source producing hundreds of thousands of pictures a
                    # second must not wake this thread once for each. With nothing in hand the Condition is
                    # the right one: an arriving picture has to cut the wait short. stop() ends both, the
                    # Event by its flag and the Condition by the notify_all in _stop_locked.
                    if pending:
                        hold = ready - now
                    else:
                        self._fresh.wait(ready - now)
                        continue
                else:
                    new = self._generation - seen    # 0 = repeat, 1 = one for one, >1 = over the rate
                    if not seen:
                        new = 1                      # first write of this run: start() does not reset the
                                                     # generation, so the difference would count a previous
                                                     # stream's pictures as superseded by this one
                    index = self._latest             # always the newest there is, never an older one
                    if index < 0:
                        break                        # a start() re-used this instance between the check
                                                     # above and here and reset _latest; -1 would index the
                                                     # LAST buffer of the old list and send a stale picture.
                                                     # Nothing is consumed yet, so nothing is lost by leaving
                    published = self._published_at
                    seen = self._generation          # consumed ONLY together with the write below
                    self._writing = index
            if hold:
                if stop.wait(hold):
                    break
                continue

            # A source that is alive but silent for this long is almost always Mutter having handed a
            # fullscreen window straight to the monitor: it stops compositing, so the screen cast has
            # nothing left to copy and freezes on its last picture while sound and input carry on
            # (mutter#3074, #3903). Say so once - from the console it looks like the picture simply died.
            source = self._process   # read once: stop() sets it to None, and testing the attribute and then
                                     # calling .poll() on it raises AttributeError straight out of this thread
            if new:
                last_new = now
            elif (not frozen_logged and now - last_new >= FROZEN_HINT_S
                    and source is not None and source.poll() is None):
                frozen_logged = True
                log.write("capture: die Bildschirmaufnahme liefert seit %.0fs kein neues Bild, obwohl sie läuft - "
                          "das passiert, wenn ein Spiel im Vollbild läuft (GNOME reicht es dann direkt an den "
                          "Monitor durch). Randloses Fenster hilft sofort; dauerhaft die beiliegende "
                          "GNOME-Erweiterung aktivieren." % FROZEN_HINT_S)
            try:
                write(memoryview(buffers[index]))
            except (OSError, ValueError) as error:   # BrokenPipe: ffmpeg is gone; ValueError: its pipe was closed under us
                log.write("capture: ffmpeg nimmt keine Bilder mehr an (%s)" % error)
                break
            finally:
                with self._gate:
                    if self._buffers is buffers:
                        self._writing = -1
            self._sent_frames += 1
            if new:
                self._new_frames += 1
                self._skipped_frames += new - 1      # superseded by a NEWER picture, never by a repeat
                # how far apart in time the CONTENT of two consecutive sent pictures is. A perfectly smooth
                # 60 fps stream has every one of these at 16.7 ms; a run of 8 ms and 25 ms alternating sends
                # 60 pictures a second and still looks like half that.
                if self._last_published_sent:
                    self._content_gaps_ms.append((published - self._last_published_sent) * 1000.0)
                self._last_published_sent = published
            else:
                self._repeat_frames += 1

            # The servo, and its gate. Only a write that took exactly one new picture says anything about
            # phase, and only a RUN of them says the source is at our rate: with none there is no arrival to
            # measure, with more than one the source is faster than the grid may go, and either way chasing a
            # phase that is walking only drags the rate along with it (measured, 8 s each: 42/s in comes out
            # at 60.80/s if only the over-rate half closes the gate and 60.04/s if a repeat closes it too;
            # 63/s in comes out at 62.00/s with no gate at all and 60.00/s with it).
            # The error is the arrival's distance from `target` (the early edge of the
            # window, see above) and is NOT wrapped into +-half an interval, so it always pulls the grid
            # EARLIER: that errs towards a slot the source has nothing for (a repeat) rather than towards two
            # arrivals sharing one slot (a lost picture).
            # It also has to be unwrapped to get out of a CYCLE SLIP, and that one is measured, not modelled:
            # an arrival that misses its own slot - it lands after `due`, so the slot has already gone out as
            # a repeat - belongs to a slot that is spent and waits most of the next one. The rate, the gaps
            # and every counter stay perfect while each picture is a frame late. A wrapped detector reads
            # that state as "aligned" and parks there: a 60 Hz source sat at a median age of 14.69 ms for a
            # whole 8 s run. Unwrapped, the error reads -18.8 ms, the grid walks the whole interval out at
            # the slew limit and re-locks (1.7 s at 1%).
            # Parking the arrivals on the early edge instead of the middle of the window is what stops the
            # slip HAPPENING (measured above); the unwrapped error is what gets out of one when the jitter
            # is wider than the window anyway.
            correction = 0.0
            clean_writes = 0 if new != 1 else clean_writes + 1
            if new == 1 and clean_writes > SERVO_GATE_WRITES:
                correction = max(-slew, min(slew, SERVO_GAIN * (published - (due - target))))
            due += interval + correction
            now = time.monotonic()
            if now > due:
                # the write ran past its own grid point (ffmpeg back-pressure, a scheduling hiccup): skip the
                # grid points that have gone by and keep the phase - never write a burst to catch up, the
                # console would only throw it away.
                # The test is `now > due` - the slot's own grid point - and NOT `now > due - window`. A slot
                # is not spent until its grid point has gone by: the window is the room a picture has to go
                # out EARLY, not a deadline. Testing the early edge threw away slots that were still a
                # quarter of an interval from being due, and because a write's duration does not change from
                # one round to the next it did so on every single one - a stable halving, not a hiccup.
                # Measured with a writer whose duration is set exactly (a dead source, so every write is a
                # repeat and starts at its grid point; 1280x720, real reader thread, 5 s per case):
                #     write costs   11.0 ms -> 60.00/s, no slot skipped
                #     write costs   12.5 ms -> 30.00/s, 174 slots skipped in 5 s   <- interval - window
                #     write costs   16.0 ms -> 30.00/s, 174 slots skipped
                # and from a 20/s source (mostly repeats) 60.03/s at 11 ms against 40.03/s at 13 ms. Every
                # one of those writes fits inside its 16.67 ms slot. With the grid point as the test all of
                # them hold 60.00/s and nothing is skipped, and a slot is lost only when the write really
                # cannot fit in it. The console feels this one directly: a still desktop is all repeats, so
                # this halved the wire rate exactly where the intra-refresh sweep (counted in pictures) is
                # the only thing repairing packet loss.
                missed = int((now - due) // interval) + 1
                due += interval * missed
                self._late_slots += missed

    def smoothness_report(self) -> str:
        """What the frame count cannot say: how evenly spaced the pictures were that actually went out.

        The console's receivedFps and our own "N an ffmpeg" both stay at 60 while the picture judders,
        because a repeated picture counts as much as a new one and an evenly PACED send says nothing about
        evenly spaced CONTENT. This measures the distance between the publish times of consecutive distinct
        pictures: a smooth 60 fps stream keeps every one of them near 16.7 ms."""
        gaps = list(self._content_gaps_ms)
        if len(gaps) < 30:
            return "capture: zu wenige Bilder für eine Gleichmäßigkeits-Aussage (%d)" % len(gaps)
        gaps.sort()
        median = gaps[len(gaps) // 2]
        p90, p99 = gaps[int(len(gaps) * 0.90)], gaps[int(len(gaps) * 0.99)]
        ideal = 1000.0 / max(1, self.fps)
        # how many pictures land within a quarter of an interval of where a steady stream would put them
        window = ideal / 4
        on_time = sum(1 for gap in gaps if abs(gap - ideal) <= window)
        doubled = sum(1 for gap in gaps if gap >= ideal * 1.75)   # a gap this long is a visibly held picture
        return ("capture: Gleichmäßigkeit über %d Bilder - Abstand im Median %.1f ms (ideal %.1f), "
                "90%% unter %.1f ms, 99%% unter %.1f ms; %.0f%% im Takt, %d sichtbare Hänger"
                % (len(gaps), median, ideal, p90, p99, 100.0 * on_time / len(gaps), doubled))

    def stop(self) -> None:
        with self._lifecycle:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stop.set()
        try:
            with self._fresh:
                self._fresh.notify_all()   # a feed() waiting for the next picture must not sit out its timeout
        except RuntimeError:
            pass
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(GST_EXIT_WAIT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in (self._reader, self._drain):
            if thread is not None and thread is not threading.current_thread():
                thread.join(2.0)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self._close_source()
        self.captured_fps = 0
        # split by kind: "44522 von der Quelle, 42548 an ffmpeg" read like 1974 lost pictures and was in fact
        # the over-rate branch eating generations (see the pacing note in feed()). Now every source picture is
        # accounted for - it went out, or a NEWER one took its place - and it is visible in the log.
        log.write("capture: %s gestoppt (%d Bilder von der Quelle, %d an ffmpeg: %d neue, %d Wiederholungen, "
                  "%d von einem neueren überholt%s)"
                  % (self.name, self._source_frames, self._sent_frames, self._new_frames, self._repeat_frames,
                     self._skipped_frames,
                     ", %d Takte ausgelassen" % self._late_slots if self._late_slots else ""))
        log.write(self.smoothness_report())

    # -- threads
    def _run_reader(self, process: subprocess.Popen, stop: threading.Event) -> None:
        fd = process.stdout.fileno()
        size = self._frame_bytes
        window_start = time.monotonic()
        window_frames = 0
        last_written = last_skipped = 0   # counter values at the start of the current one-second window
        # this run's buffers, held locally exactly as feed() holds them. stop() joins this thread before a
        # start() swaps the list, but that join has a timeout: if it ever ran out, this reader would go on
        # publishing an index into the list the NEXT run's feed() reads from - an index into a buffer that
        # run's reader has never filled, i.e. a blank picture on the console. Bind the list instead of
        # following the attribute, and leave the moment it is not ours any more.
        buffers = self._buffers
        try:
            while not stop.is_set():
                if self._buffers is not buffers:
                    break
                index = self._free_buffer()
                view = memoryview(buffers[index])
                filled = 0
                while filled < size:
                    try:
                        got = os.readv(fd, [view[filled:]])
                    except InterruptedError:
                        continue
                    except OSError:
                        got = 0
                    if got <= 0:
                        break
                    filled += got
                if filled < size:
                    break   # EOF: gst-launch is gone (a partial frame is never published)
                published = time.monotonic()   # taken outside the lock: the fps window below reuses it
                with self._fresh:
                    if self._buffers is not buffers:
                        break                      # see above: not our list any more, so not our picture
                    self._latest = index
                    self._published_at = published   # feed() ages this picture, and phases its grid, from here
                    self._source_frames += 1
                    self._generation += 1
                    self._fresh.notify_all()   # feed() sends this picture at once - see the pacing note there
                if self._source_frames == 1:
                    last_written, last_skipped = self._new_frames, self._skipped_frames
                    log.write("capture: erstes Bild von der Quelle nach %d ms" % int((time.monotonic() - self._started_at) * 1000))
                window_frames += 1
                now = published
                if now - window_start >= 1.0:
                    self.captured_fps = window_frames
                    if TRACE_SOURCE_RATE:
                        # One line a second while a stream runs. It exists because the status line in the
                        # window cannot be used to find out what raises the source rate: having the window in
                        # front is itself one of the things being tested (every redraw of it is another
                        # picture GNOME hands us). A log line can be read while the window is minimised.
                        written, skipped = self._new_frames, self._skipped_frames
                        log.write("trace: Quelle %d/s, an ffmpeg %d neu, %d überholt"
                                  % (window_frames, written - last_written, skipped - last_skipped))
                        last_written, last_skipped = written, skipped
                    window_frames = 0
                    window_start = now
        except Exception as error:   # noqa: BLE001
            log.write("capture: Lesefehler an der Quelle: %s" % error)
        finally:
            self.captured_fps = 0
            if not stop.is_set():
                # the source died under a running stream. feed() keeps re-sending the last picture so the PS3
                # sees video (a frozen desktop beats "server gone"); the next PLAY starts a fresh pipeline.
                try:
                    code = process.wait(1.0)
                except subprocess.TimeoutExpired:
                    code = None
                tail = self._stderr_tail.strip().splitlines()
                log.write("capture: die Quelle liefert keine Bilder mehr (%s%s) - sende das letzte Bild weiter" % (
                    GST_LAUNCH + (" beendet mit Code %d" % code if code is not None else " hängt"),
                    ": " + tail[-1] if tail else ""))

    def _run_drain(self, process: subprocess.Popen) -> None:
        """gst-launch blocks if its error channel fills up, so drain it; keep the tail for diagnosis."""
        try:
            for raw in process.stderr:
                line = raw.decode("utf-8", "replace")
                self._stderr_tail = (self._stderr_tail + line)[-STDERR_TAIL_CHARS:]
        except (OSError, ValueError):
            pass

    def _free_buffer(self) -> int:
        with self._gate:
            for index in range(len(self._buffers)):
                if index != self._latest and index != self._writing:
                    return index
        raise RuntimeError("kein freier Bildpuffer")   # cannot happen with three buffers

    @staticmethod
    def _make_writer(target):
        """A write-everything function for either a raw fd or a file object (Popen.stdin)."""
        if isinstance(target, int):
            _grow_pipe(target)

            def write(view):
                offset = 0
                while offset < len(view):
                    offset += os.write(target, view[offset:])
            return write

        try:
            _grow_pipe(target.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        flush = getattr(target, "flush", None)

        def write(view):
            offset = 0
            while offset < len(view):
                written = target.write(view[offset:])
                if written is None:   # a file object that always takes everything
                    break
                offset += written
            if flush is not None:
                flush()
        return write


class PortalCapture(_PipeCapture):
    """Wayland (and GNOME on X11): the portal's PipeWire stream, read by gst pipewiresrc."""

    name = "portal"

    def __init__(self):
        super().__init__()
        self._session: portal.ScreenCastSession | None = None
        self._fd = -1

    def _open_source(self) -> list[str]:
        # a dialog nobody answers holds the gate for DIALOG_TIMEOUT_S, and the PS3 gives up and re-PLAYs every
        # 10 s meanwhile - each one a pump thread queued here. bound the wait, or the queue grows for as long
        # as the user is away (measured on the fake session; the same length as the dialog's own timeout)
        if not _portal_gate.acquire(timeout=PORTAL_GATE_WAIT_S):
            log.write("portal: ein Freigabe-Dialog ist seit %d s offen und unbeantwortet - dieser Versuch entfällt" % int(PORTAL_GATE_WAIT_S))
            raise portal.PortalError("Freigabe-Dialog noch offen")
        try:
            # read the token INSIDE the gate: warm_up() may be holding it while the user answers the first
            # dialog, and the token it saves must be the one we use - or a PLAY arriving meanwhile would show
            # the dialog a second time
            saved = settings.get("screencast_restore_token")
            self._session = portal.ScreenCastSession()
            try:
                node_id, token = self._session.open(saved)
                _remember_token(saved, token)
                self._fd = self._session.open_pipewire_remote()
            except Exception:
                self._close_source()
                raise
        finally:
            _portal_gate.release()
        # keepalive-time: GNOME only sends a frame when the screen changes; re-sending the last buffer every
        # 100 ms keeps the pipeline (and our fps statistic) alive on a still desktop
        return ["pipewiresrc", "fd=%d" % self._fd, "path=%d" % node_id, "do-timestamp=true", "always-copy=true",
                "keepalive-time=100"]

    def _source_fds(self) -> tuple:
        return (self._fd,) if self._fd >= 0 else ()

    def _after_spawn(self) -> None:
        # gst-launch holds its own copy of the PipeWire fd now; ours would only leak
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def _close_source(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        session, self._session = self._session, None
        if session is not None:
            session.close()


class TestCapture(_PipeCapture):
    """videotestsrc instead of the desktop: for tests and integration runs without a portal dialog."""

    name = "test"

    def _open_source(self) -> list[str]:
        return ["videotestsrc", "is-live=true", "pattern=ball",
                "!", "video/x-raw,framerate=%d/1,width=%d,height=%d" % (self.fps, self.width, self.height)]


class X11Capture(ScreenCapture):
    """Plain X11 without a portal: ffmpeg's x11grab reads the screen itself; nothing to feed."""

    name = "x11grab"
    needs_scale = True   # x11grab delivers the whole desktop at its own size; ffmpeg scales (see encoders)

    def __init__(self):
        super().__init__()
        self.fps = 0
        self._display = ""

    def start(self, width: int, height: int, fps: int) -> bool:
        self._display = os.environ.get("DISPLAY", "")
        if not self._display:
            log.write("capture: x11grab braucht DISPLAY")
            return False
        self.fps = fps
        self.captured_fps = fps   # nominal: ffmpeg pulls at exactly this rate, we never see the frames
        log.write("capture: x11grab auf %s (%d fps, skaliert auf %dx%d)" % (self._display, fps, width, height))
        return True

    def ffmpeg_input_args(self) -> list[str]:
        return ["-f", "x11grab", "-framerate", str(self.fps), "-draw_mouse", "1", "-i", self._display]

    def feed(self, ffmpeg_stdin) -> None:
        return   # ffmpeg reads the screen directly

    def stop(self) -> None:
        self.captured_fps = 0


# one share dialog at a time: warm_up() at server start and a PLAY arriving meanwhile must not race
_portal_gate = threading.Lock()
PORTAL_GATE_WAIT_S = portal.DIALOG_TIMEOUT_S   # how long a PLAY waits behind somebody else's open dialog


def _remember_token(saved: str | None, token: str | None) -> None:
    """Every Start hands out a fresh token and retires the old one, so the newest must always be saved."""
    if token and token != saved:
        settings.set("screencast_restore_token", token)
        log.write("portal: Freigabe-Token gesichert - der Dialog erscheint erst wieder nach einem Widerruf")


def create_capture() -> ScreenCapture | None:
    """The backend for this desktop: test source (opt-in) > portal > x11grab; None when there is nothing."""
    if os.environ.get("TEE_CST_TEST_SOURCE") == "1":
        return TestCapture()
    if portal.is_available():
        return PortalCapture()
    if os.environ.get("DISPLAY"):
        return X11Capture()
    log.write("capture: keine Bildschirmquelle (kein ScreenCast-Portal, kein DISPLAY)")
    return None


def warm_up() -> None:
    """Server start: show the share dialog now, while the user is at the PC, and keep the token. No-op otherwise."""
    if os.environ.get("TEE_CST_TEST_SOURCE") == "1":
        return
    if settings.get("screencast_restore_token"):
        return
    if not portal.is_available():
        return
    with _portal_gate:
        # check again INSIDE the gate: a PLAY that came in between the check above and here has shown the
        # dialog itself and saved the token - asking now would put the same dialog up a second time
        if settings.get("screencast_restore_token"):
            return
        log.write("portal: erste Einrichtung - der Freigabe-Dialog fragt einmalig, welcher Monitor zur PS3 gestreamt wird")
        session = portal.ScreenCastSession()
        try:
            _node_id, token = session.open(None)
            _remember_token(None, token)
            if not token:
                log.write("portal: das Portal gab keinen Token heraus - der Dialog erscheint bei jedem Stream")
        except portal.PortalError:
            pass   # already logged; the dialog comes back at the next PLAY
        finally:
            session.close()
