"""The live sender: captures the desktop, encodes it with a spawned ffmpeg, reads the raw Annex-B stream
from its output pipe, splits it into access units incrementally, and sends each one to the PS3 as it
comes out of the encoder (port of LiveStreamer.cs).

Windows fed ffmpeg from ddagrab inside the same command line. Here the capture is an object of its own
(capture.py: Portal/PipeWire, x11grab, or a test pattern): it is started before ffmpeg, and the raw-pipe
backends write their frames into ffmpeg's stdin from a feed thread while this module reads ffmpeg's stdout.

Tries each encoder the PC has in turn (the chosen one first) and falls back down the ladder; all of them
failing three times running trips the server's fuse instead of retrying forever - retrying flapped the
desktop resolution on and off and made the PC unusable.
"""

import os
import subprocess
import threading
import time

from . import childproc, encoders, log, protocol
from .clock import now_us
from .stream_sender import AnnexBSplitter, send_access_unit

FIRST_FRAME_TIMEOUT_S = 5.0
ENCODER_EXIT_WAIT_S = 3.0             # terminate, then this long, then kill
FAILED_STARTS_BEFORE_GIVING_UP = 3
READ_CHUNK_BYTES = 64 * 1024
ERROR_TAIL_CHARS = 2000
PUMP_JOIN_S = 2.0
FEED_JOIN_S = 2.0
# a raw 720p60 frame is 1.3 MiB; through the default 64 KiB pipe the feeder hands it over in 21 blocking
# steps paced by ffmpeg's reads. 1 MiB (the unprivileged maximum) takes most of a frame in one go, so the
# feed thread's 60 Hz tick stays crisp. the output pipe gets the same: a keyframe must not stall ffmpeg's
# muxer while we are still pacing the previous frame onto the wire.
PIPE_BYTES = 1 << 20


class _Session:
    """One PLAY's worth of state. A fresh object per start() so a pump that is still winding down can never
    read the flags of the session that replaced it."""

    __slots__ = ("target", "active", "intra", "width", "height", "process", "capture", "encoder")

    def __init__(self, target, intra: bool, width: int, height: int):
        self.target = target
        self.active = True
        self.intra = intra              # what SINFO promised the PS3: an intra-refresh stream (True) or keyframes
        # pinned for the session: SINFO goes out before the encoder starts, and a size changed in the window
        # in between would leave the two disagreeing
        self.width = width
        self.height = height
        self.process: subprocess.Popen | None = None
        self.capture = None
        self.encoder: encoders.VideoEncoder | None = None


class LiveStreamer:
    # send_rate_kbps caps how fast packets leave, independent of the video bitrate. it must stay under what
    # the link can actually carry (WiFi to the PS3 tops out ~22Mbps - its radio is 802.11g), or a keyframe's
    # burst overruns it and the picture freezes until the next one.
    def __init__(self, sock, ffmpeg_path, fps, kbps, width, height, send_rate_kbps, create_capture,
                 encoders_to_try, loss_recovery, on_all_encoders_failed, video_kbps=None, entropy_coder=None,
                 stream_size=None):
        self._sock = sock
        self._ffmpeg_path = ffmpeg_path
        self._fps = fps
        self._kbps = kbps
        self._width = width
        self._height = height
        self._send_rate_kbps = send_rate_kbps
        self._create_capture = create_capture
        self._encoders_to_try = encoders_to_try
        self._loss_recovery = loss_recovery
        self._on_all_encoders_failed = on_all_encoders_failed
        # bitrate and entropy coder are read again at every stream start, so a change in the window takes
        # effect on the next connect without restarting the server. None keeps the constructor's value.
        self._video_kbps = video_kbps
        self._entropy_coder = entropy_coder
        self._stream_size = stream_size

        # SINFO's level field. The Windows original had to get it right because its PS3 build sized the
        # decoder from it; this PS3 app does not - it reads coded size, level and ref frames out of the
        # stream's own SPS when the first keyframe lands, and only uses SINFO's fps and intra-refresh flag
        # (upstream/ps3-app/stream.c: openDecoderForStream, and the comment above requestPlay). Measured
        # here: both rungs write level 3.2 into the SPS at every bitrate the window offers (4 to 12 Mbit/s),
        # so 4.2 over-announces - harmless, and kept because that is what the console was proven with.
        # Above Full HD 4.2 would be an under-announcement rather than an over-one (its picture-size limit
        # is 8704 macroblocks and 2560x1440 needs 14400), so the level follows the size from there on.
        self._sinfo_level = protocol.SINFO_LEVEL

        self._gate = threading.RLock()          # start() and stop() against each other
        self._session: _Session | None = None
        self._pump_thread: threading.Thread | None = None
        self._failed_starts = 0

    def _current_kbps(self) -> int:
        """The user's bitrate, read fresh for each stream (the window can change it between sessions).
        A plain number instead of a callable is honoured too; None keeps the constructor's rate."""
        chosen = self._video_kbps() if callable(self._video_kbps) else self._video_kbps
        return int(self._kbps if chosen is None else chosen)

    def _current_send_rate_kbps(self) -> int:
        """Packets may leave faster than the video's own rate, as in the original (3x: SEND_RATE_KBPS =
        KBPS * 3). Keep that ratio when the user picks another bitrate - at a fixed 30000 the pacer would
        burst a keyframe far past what the PS3's 802.11g radio carries, and at a fixed 30000 with 4 Mbit/s
        video the cap would stop meaning anything at all."""
        return max(1, self._current_kbps() * self._send_rate_kbps // max(1, self._kbps))

    def _current_entropy_coder(self) -> str:
        """"cavlc", "cabac", or "auto" (the encoder's own default) - anything else counts as "auto"."""
        chosen = self._entropy_coder() if callable(self._entropy_coder) else self._entropy_coder
        return chosen if chosen in protocol.ENTROPY_CODERS else "auto"

    def _level_for(self, width: int, height: int) -> int:
        """The H.264 level to announce for this picture size, never below the 4.2 the console was proven
        with. Takes the size explicitly so it can never disagree with the size in the same SINFO."""
        return max(self._sinfo_level, protocol.sinfo_level_for(width, height, self._fps))

    def _current_size(self) -> tuple[int, int]:
        """The stream size for the next session; anything unknown falls back to the constructor's."""
        chosen = self._stream_size() if callable(self._stream_size) else self._stream_size
        if isinstance(chosen, (tuple, list)) and tuple(chosen) in protocol.STREAM_SIZES:
            return int(chosen[0]), int(chosen[1])
        return self._width, self._height

    # the PS3 repeats PLAY until it hears back, so a repeat is not a new session: answer it and carry on.
    # restarting on every repeat meant the encoder hunt began again each time and never finished.
    @property
    def is_streaming(self) -> bool:
        session = self._session
        return session is not None and session.active

    def _is_streaming_to(self, target) -> bool:
        session = self._session
        return session is not None and session.active and session.target == target

    def reset_failures(self) -> None:
        self._failed_starts = 0

    def start(self, target) -> None:
        with self._gate:
            if self._is_streaming_to(target):
                self.send_stream_info(target)
                return
            self._stop_locked()
            width, height = self._current_size()
            session = _Session(target, self._announced_intra(), width, height)
            self._session = session
            self.send_stream_info(target)   # answer the PS3 straight away: bringing an encoder up can take seconds
            self._pump_thread = threading.Thread(target=self._run_pump, args=(session,), name="live-pump", daemon=True)
            self._pump_thread.start()

    # SINFO announces the source's frame rate, and whether this is an intra-refresh stream - which decides
    # how the PS3 handles a loss (hold the picture for the next keyframe, or decode on through the damage
    # and let the sweep clean it up). the PS3 configures its decoder from the stream's own SPS.
    # the flag describes the encoder the pump will try first, and the PS3 reads SINFO only while waiting for
    # the first frame - so the session pins the flag and every rung of the ladder is run to match it: a
    # fallback from VA-API (announced: keyframes) to nvenc is run in keyframe mode, or the PS3 would wait for
    # a keyframe that an intra-refresh stream never sends and stay frozen after the first lost packet.
    def _announced_intra(self) -> bool:
        candidates = self._encoders_to_try()
        return encoders.intra_refresh_enabled(candidates[0] if candidates else None, self._loss_recovery())

    def send_stream_info(self, target) -> None:
        session = self._session
        if session is not None and session.active and session.target == target:
            intra, width, height = session.intra, session.width, session.height
        else:
            intra = self._announced_intra()
            width, height = self._current_size()
        info = ("SINFO %d %d %d %d %d %d" % (width, height, self._level_for(width, height), protocol.SINFO_REFS,
                                            self._fps, 1 if intra else 0)).encode("ascii")
        try:
            for _ in range(3):
                self._sock.sendto(info, target)
        except OSError as error:
            log.write("live: SINFO an %s fehlgeschlagen: %s" % (target[0], error))

    # ending ffmpeg is what unblocks the pump's read; the pump then does the orderly cleanup itself (capture,
    # pipes, reaping). waiting for it matters: the next stream's capture must not find the old one still busy.
    # the wait is a BOUND, not a guarantee - PUMP_JOIN_S is 2 s while the pump's own cleanup can in the worst
    # case take longer (capture.stop's terminate grace, its two thread joins, a blocking portal Session.Close,
    # then ENCODER_EXIT_WAIT_S). a longer join would block the receive thread instead, which is worse. what a
    # late finisher can still cost is two screen shares overlapping for a moment: it cannot corrupt the next
    # session, because every start() builds a fresh _Session and a fresh capture object (see _Session).
    def stop(self) -> None:
        with self._gate:
            self._stop_locked()

    def _stop_locked(self) -> None:
        session = self._session
        if session is not None:
            session.active = False
            process = session.process
            if process is not None:
                _end_process(process)
        thread = self._pump_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(PUMP_JOIN_S)
        self._pump_thread = None

    # start on the encoder the user chose and, if it will not run, walk down the rest of the ones this PC
    # has. all of them failing is a fault worth stopping for, not worth retrying forever.
    #
    # what counts as a failed start (deviation from the original, which counted every session that ended
    # without a frame - a STOP included): only an attempt that genuinely came to nothing while nobody had
    # asked us to stop. on Linux the portal's share dialog can outlast the watchdog's 10 s grace, and three
    # such STOPs would have tripped the fuse with the misleading "kein Encoder startet". but a rung that
    # produced no frames before the STOP landed is still counted, or a ladder that takes longer to fail than
    # the grace (5 s first-frame timeout per rung) would never trip it and the desktop would flap forever.
    def _run_pump(self, session: _Session) -> None:
        try:
            attempts = list(self._encoders_to_try())
            any_encoder_worked = False
            capture_failed = False
            encoder_failed = not attempts   # nothing to try is a failure too
            for encoder in attempts:
                if not session.active:
                    break
                log.write("live: versuche Encoder " + encoder.name)
                outcome = self._pump_encoder(session, encoder)
                if outcome is None:
                    capture_failed = session.active   # no picture source at all: the other encoders would fail the same way
                    break
                if outcome:
                    any_encoder_worked = True
                    break
                if session.active:
                    encoder_failed = True

            session.active = False
            if any_encoder_worked:
                self._failed_starts = 0
                log.write("live: Stream an %s:%d beendet" % session.target)
                return
            if not (encoder_failed or capture_failed):
                return   # stopped before anything could fail (the share dialog was still up)

            self._failed_starts += 1
            if self._failed_starts >= FAILED_STARTS_BEFORE_GIVING_UP:
                what = "die Bildschirmaufnahme startet nicht" if capture_failed else "kein Encoder startet"
                self._on_all_encoders_failed("%s, %d-mal in Folge" % (what, self._failed_starts))
        except Exception as error:   # noqa: BLE001 - a pump that dies must still leave the flags right
            session.active = False
            log.write("live: Pumpe abgebrochen: %r" % (error,))
            # nobody else will: an ffmpeg or a capture left behind here would hold the screen share (and
            # gst-launch) until the server exits
            process, session.process = session.process, None
            if process is not None:
                _end_process(process)
                _close_pipes(process)
            capture, session.capture = session.capture, None
            if capture is not None:
                _stop_capture(capture)

    # spawns one ffmpeg fed by one capture and pumps its output to the client. returns False if the encoder
    # produced nothing (caller falls back to the next encoder), True otherwise, None if the capture itself
    # would not start (pointless to try another encoder on it).
    def _pump_encoder(self, session: _Session, encoder: encoders.VideoEncoder):
        try:
            capture = self._create_capture()
        except Exception as error:   # noqa: BLE001
            log.write("live: Bildschirmaufnahme nicht möglich: %s" % error)
            return None
        if capture is None:
            log.write("live: keine Bildschirmaufnahme möglich (kein Portal, kein DISPLAY)")
            return None
        session.capture = capture
        try:
            started = capture.start(session.width, session.height, self._fps)
        except Exception as error:   # noqa: BLE001
            log.write("live: Bildschirmaufnahme (%s) abgebrochen: %s" % (capture.name, error))
            started = False
        if not started:
            log.write("live: Bildschirmaufnahme (%s) startet nicht" % capture.name)
            _stop_capture(capture)
            session.capture = None
            return None
        if not session.active:   # stopped while the portal dialog was up
            _stop_capture(capture)
            session.capture = None
            return False

        input_args = list(capture.ffmpeg_input_args())
        raw_pipe = "pipe:0" in input_args
        loss_recovery = "intra" if session.intra else "keyframe"   # what SINFO promised, whatever rung this is
        try:
            args = encoders.build_ffmpeg_args(self._ffmpeg_path, encoder, input_args, session.width, session.height,
                                              self._fps, self._current_kbps(), loss_recovery, capture.needs_scale,
                                              self._current_entropy_coder())
            process = _spawn_ffmpeg(args, raw_pipe)
        except (OSError, ValueError) as error:
            log.write("live: ffmpeg startet nicht: %s" % error)
            _stop_capture(capture)
            session.capture = None
            return False
        session.process = process
        session.encoder = encoder

        # ffmpeg blocks if its error channel fills up, so drain it; keep the tail for diagnosis
        error_tail = {"text": ""}

        # its own duplicate of the pipe, closed by the thread itself: a pump that dies unexpectedly closes
        # process.stderr on its way out, and a raw read(2) still blocked on that number would come back
        # holding whatever file the next open() puts there.
        error_fd = os.dup(process.stderr.fileno())

        def drain_errors():
            try:
                while True:
                    try:
                        data = os.read(error_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    error_tail["text"] = (error_tail["text"] + data.decode("utf-8", "replace"))[-ERROR_TAIL_CHARS:]
            finally:
                os.close(error_fd)

        error_thread = threading.Thread(target=drain_errors, name="live-ffmpeg-stderr", daemon=True)
        error_thread.start()

        # the raw-pipe backends push their frames into ffmpeg from here; x11grab's feed returns at once.
        # closing stdin when the feed ends tells ffmpeg the picture is over, which ends the pump too.
        def feed_frames():
            try:
                capture.feed(process.stdin)
            except Exception as error:   # noqa: BLE001
                if session.active:
                    log.write("live: Bildquelle abgebrochen: %s" % error)
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass

        feed_thread = threading.Thread(target=feed_frames, name="live-feed", daemon=True)
        feed_thread.start()

        # the read blocks, so the first-frame timeout can't just be a check inside the loop - a silently hung
        # encoder would never reach it. instead kill the process after the timeout if no frame has come out
        # yet; that unblocks the read and the loop falls through to the next encoder.
        first_frame_seen = threading.Event()

        def first_frame_watchdog():
            if not first_frame_seen.wait(FIRST_FRAME_TIMEOUT_S) and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

        timeout_thread = threading.Thread(target=first_frame_watchdog, name="live-first-frame", daemon=True)
        timeout_thread.start()

        splitter = AnnexBSplitter()
        frame_id = 0
        send_rate_kbps = self._current_send_rate_kbps()   # fixed for this session, like the encoder settings
        send_error_logged = False
        first_frame_timer = time.monotonic()
        output_fd = process.stdout.fileno()

        while session.active:
            try:
                chunk = os.read(output_fd, READ_CHUNK_BYTES)   # one read(2): returns as soon as anything is there
            except OSError:
                break
            if not chunk:
                break
            splitter.push(chunk)
            while True:
                unit = splitter.take_access_unit()
                if unit is None:
                    break
                data, keyframe = unit
                # stamp the moment the frame left the encoder; the PS3 measures every stage from here
                try:
                    send_access_unit(self._sock, session.target, frame_id, data, keyframe, now_us(), send_rate_kbps)
                except OSError as error:
                    if not send_error_logged:
                        send_error_logged = True
                        log.write("live: Senden an %s fehlgeschlagen: %s" % (session.target[0], error))
                frame_id += 1
                if frame_id == 1:
                    first_frame_seen.set()
                    log.write("live: erstes Frame %d ms nach Encoder-Start gesendet"
                              % int((time.monotonic() - first_frame_timer) * 1000))

        first_frame_seen.set()   # release the timeout thread if we're leaving for any other reason
        timeout_thread.join()
        died_on_its_own = process.poll() is not None   # note it before we terminate it ourselves

        # ffmpeg first (SIGTERM only, no wait), then the capture: a feeder blocked on a write to a stalled
        # encoder is freed by the pipe breaking, so stopping the capture can never wait on it. then reap.
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        _stop_capture(capture)
        _end_process(process)
        feed_thread.join(FEED_JOIN_S)
        error_thread.join(1.0)
        _close_pipes(process)
        session.process = None
        session.capture = None

        if frame_id == 0:
            log.write("live: Encoder lieferte keine Frames. ffmpeg sagte:\n" + error_tail["text"].strip())
            return False
        # An encoder that died mid-stream used to leave nothing behind but the feeder's "broken pipe":
        # the reason was drained into error_tail and then thrown away, because only the zero-frame case
        # printed it. A session that ends because ffmpeg quit is exactly when that text is wanted.
        # session.active is still true here when the pump is leaving on its own rather than being stopped.
        if session.active and died_on_its_own and error_tail["text"].strip():
            log.write("live: ffmpeg hat sich nach %d Frames beendet. Es sagte:\n%s"
                      % (frame_id, error_tail["text"].strip()))
        log.write("live: %d Frames gesendet" % frame_id)
        return True


def _spawn_ffmpeg(args: list[str], raw_pipe: bool) -> subprocess.Popen:
    kw = dict(stdin=subprocess.PIPE if raw_pipe else subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        return childproc.popen(args, pipesize=PIPE_BYTES, **kw)
    except OSError:
        # a kernel with a smaller pipe-max-size refuses the size; the default pipe still works, just chattier
        return childproc.popen(args, **kw)


def _end_process(process: subprocess.Popen) -> None:
    """terminate, give it ENCODER_EXIT_WAIT_S, then kill - and always reap, so nothing is left a zombie."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(ENCODER_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(1.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _close_pipes(process: subprocess.Popen) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _stop_capture(capture) -> None:
    try:
        capture.stop()
    except Exception as error:   # noqa: BLE001 - a capture that will not stop must not stop the cleanup
        log.write("live: Bildschirmaufnahme (%s) ließ sich nicht beenden: %s" % (getattr(capture, "name", "?"), error))
