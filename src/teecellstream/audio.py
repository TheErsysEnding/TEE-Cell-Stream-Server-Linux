"""Desktop sound for the PS3: capture what the PC plays and send it as small uncompressed packets.

Port of AudioCapture.cs + AudioStreamer.cs. Windows captured the speakers via WASAPI loopback; here
ffmpeg reads the default sink's monitor through the pulse API (PipeWire's pulse server) and pipes raw
s16be stereo to us. Uncompressed on purpose: at 48kHz stereo it costs ~1.5Mbps (a rounding error next
to the video) and skips an encoder here and a decoder on the PS3 - exactly the latency we are trying
not to spend. One packet is 5ms of sound, so a lost packet is a 5ms gap nobody hears.

packet layout (16-byte header, big-endian, must match stream.c on the PS3):
  [0]='A' [1]='F' [2..5]=packetId [6..7]=frameCount [8..15]=capture time (server microseconds)
  payload: frameCount x (left, right) 16-bit signed samples, big-endian
"""

import os
import re
import struct
import subprocess
import threading
import time

from . import log, protocol
from .clock import now_us

try:
    from .childproc import popen as _child_popen   # PR_SET_PDEATHSIG: ffmpeg dies with us, never orphaned
except ImportError:                               # childproc not present (tests of this module alone)
    _child_popen = None

SAMPLE_RATE = protocol.AUDIO_SAMPLE_RATE
CHANNELS = protocol.AUDIO_CHANNELS
BYTES_PER_FRAME = CHANNELS * 2                    # s16 stereo

# capture
RING_SECONDS = 1                                  # like the original: one second of stereo
FRAGMENT_BYTES = 1920                             # 10ms per pulse fragment: small, frequent deliveries
# measured: with no pulse server ffmpeg exits at once ("No such process"), but a source that exists and
# never delivers (a monitor without a sink, a stalled server) keeps it waiting for ever - so "started"
# means bytes arrived, and this is how long we give them.
# Only the FIRST source is worth that long a wait. server.py calls us on the receive thread while holding
# the stream lock, so every second here is a second in which the PS3's pad, its TIME probes and its STOP
# are not read - they queue up and are then replayed in a burst. START_TIMEOUT_S per source would have
# multiplied that stall by the length of the list (up to 15s, past the PS3's 10s startup grace); a healthy
# monitor delivers its first fragment in ~10-50ms, so the ones behind it need a fraction of the time.
START_TIMEOUT_S = 3.0
FALLBACK_TIMEOUT_S = 0.5
MAX_SOURCES_TO_TRY = 5            # worst case 3.0 + 4 x 0.5 = 5s of blocked receive thread
STOP_TIMEOUT_S = 0.5
SOURCE_LIST_TIMEOUT_S = 2.0                       # "ffmpeg -sources pulse" takes ~40ms; this is the wedged case
MONITOR_SUFFIX = ".monitor"
# ONLY monitor sources belong in this list. Measured on this machine (ffmpeg 8.0.1, PipeWire's pulse
# server, checked with pw-dump which node the ffmpeg stream links to): a source name pulse cannot resolve
# is NOT an error - it silently substitutes the default CAPTURE device. The old fallback "default" did
# exactly that and recorded the line-in of a capture card; on most machines it is the microphone. The PS3
# would have played the room while the log said "nehme die Lautsprecher auf". Both names below were
# verified to link to the default sink (i.e. its monitor), on an empty and on a busy sink.
CAPTURE_SOURCES = ("@DEFAULT_MONITOR@", "@DEFAULT_SINK@.monitor")
# latency guard: PipeWire delivers sound continuously even while the PC is silent (WASAPI sent nothing),
# so anything the sender does not drain piles up in the ring and every frame of it is audio delay.
GUARD_HIGH_MS = 60
GUARD_LOW_MS = 20
STDERR_TAIL_CHARS = 2000

# streamer
CHUNK_MS = protocol.AUDIO_CHUNK_MS
MAX_FRAMES_PER_PACKET = protocol.AUDIO_MAX_FRAMES_PER_PACKET   # the PS3 (AUDIO_MAX_FRAMES) drops any packet larger than this
# ... but AUDIO_MAX_FRAMES alone is not the whole limit: the PS3 receives into a 1500-byte buffer
# (net-common.h PACKET_MAX) and handleAudioPacket then insists on packetBytes >= 16 + frames*4. A bigger
# datagram is truncated by recv and therefore dropped WHOLE, not clipped - so 512 frames (2064 bytes)
# would lose all audio rather than cap it. This is what actually fits. (At 48kHz a chunk is 240 frames,
# so neither limit bites today; they matter only if the requested rate ever changes.)
PS3_PACKET_MAX_BYTES = 1500
PREBUFFER_MS = protocol.AUDIO_PREBUFFER_MS                     # ride out the 10ms delivery fragments without gapping
PREBUFFER_TIMEOUT_MS = protocol.AUDIO_PREBUFFER_TIMEOUT_MS     # ... but never wait longer than this for it
HEADER_BYTES = protocol.AUDIO_HEADER_BYTES
SLEEP_MARGIN_US = 2000            # sleep until this close to the due time ...
SPIN_MARGIN_US = 150              # ... then sleep again to here and spin the rest. Python cannot spin long: the GIL starves the video pump
_HEADER = struct.Struct(">2sIHQ")


def _spawn(args, **kw) -> subprocess.Popen:
    kw.setdefault("stdin", subprocess.DEVNULL)
    if _child_popen is not None:
        return _child_popen(args, **kw)
    return subprocess.Popen(args, **kw)


def list_pulse_sources(ffmpeg_path: str) -> list[str]:
    """The pulse sources ffmpeg can see, in its own order. Empty when it cannot ask (no server, old build).

    "ffmpeg -sources pulse" prints one indented "<name> [<description>] (none)" line per source on stdout
    (measured: ~40ms) and exits 0 even when it failed, so the list itself is the only signal.
    """
    try:
        process = _spawn([ffmpeg_path, "-hide_banner", "-loglevel", "error", "-sources", "pulse"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return []
    try:
        out, _ = process.communicate(timeout=SOURCE_LIST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return []
    names = []
    for line in out.decode("utf-8", "replace").splitlines():
        if not line.startswith((" ", "*")):
            continue                      # the "Auto-detected sources for pulse:" heading and error lines
        name = line.lstrip("* ").split(" [", 1)[0].strip()
        if name:
            names.append(name)
    return names


def build_af_packet(packet_id: int, frame_count: int, capture_us: int, samples: bytes) -> bytes:
    """One AF datagram; samples are already s16be interleaved stereo (frame_count x 4 bytes)."""
    return _HEADER.pack(b"AF", packet_id & 0xFFFFFFFF, frame_count & 0xFFFF, capture_us & 0xFFFFFFFFFFFFFFFF) + samples


def build_ainfo(sample_rate: int) -> bytes:
    return ("AINFO %d %d" % (sample_rate, CHANNELS)).encode("ascii")


class AudioCapture:
    """Captures the default sink's monitor via ffmpeg and keeps the most recent sound in a ring for the sender."""

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self.sample_rate = SAMPLE_RATE      # we ask ffmpeg for 48kHz stereo whatever the sink runs at
        self.dropped_frames = 0             # ring overran or the guard trimmed it: the sender wasn't draining fast enough
        self.source: str | None = None      # which pulse source is feeding us
        self._ring_gate = threading.Lock()
        self._ring = bytearray()
        self._ring_capacity_bytes = RING_SECONDS * self.sample_rate * BYTES_PER_FRAME
        self._guard_high_bytes = self.sample_rate * GUARD_HIGH_MS // 1000 * BYTES_PER_FRAME
        self._guard_low_bytes = self.sample_rate * GUARD_LOW_MS // 1000 * BYTES_PER_FRAME
        self._guard_logged = False
        self._capturing = False
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail = ""
        self._first_data = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Starts capture; False (with the reason logged) if no pulse source will feed us. Video streams anyway."""
        self.stop()
        self.dropped_frames = 0
        self._guard_logged = False
        self.source = None
        with self._ring_gate:
            self._ring = bytearray()

        sources = self._sources_to_try()   # never raises: an ffmpeg it cannot run simply lists nothing
        if not sources:
            return False   # reason already logged: there is no monitor to record

        reasons = []
        for index, source in enumerate(sources):
            try:
                reason = self._try_source(source, START_TIMEOUT_S if index == 0 else FALLBACK_TIMEOUT_S)
            except OSError as error:
                # no ffmpeg at all (or not executable): no source will do any better, so don't try them
                log.write("audio: ffmpeg lässt sich nicht starten, streame nur Video (%s)" % error)
                return False
            if reason is None:
                self.source = source
                log.write("audio: nehme die Lautsprecher auf: %dHz, %d Kanäle, 16-bit (pulse %s)" % (self.sample_rate, CHANNELS, source))
                return True
            reasons.append("%s: %s" % (source, reason))
            if index + 1 < len(sources):
                log.write("audio: %s startet nicht (%s), versuche %s" % (source, reason, sources[index + 1]))
        log.write("audio: konnte die Lautsprecher nicht öffnen, streame nur Video (%s)" % "; ".join(reasons))
        return False

    def _sources_to_try(self) -> list[str]:
        """The monitor sources to probe, best first. Empty (with a reason logged) when recording would be wrong.

        The list ffmpeg gives us is the only way to tell "there is no playback device" from "the magic name
        is fine": pulse answers an unresolvable name with the default CAPTURE device instead of an error, so
        without this check a machine whose sinks are all gone would stream its microphone to the PS3.
        """
        listed = list_pulse_sources(self.ffmpeg_path)
        monitors = [name for name in listed if name.endswith(MONITOR_SUFFIX)]
        if listed and not monitors:
            log.write("audio: keine Monitor-Quelle vorhanden (kein Wiedergabegerät), streame nur Video "
                      "(pulse bietet nur %s)" % ", ".join(listed[:4]))
            return []
        # the magic names first (they follow the default sink); then every monitor by name, in case the
        # default sink is the one that is broken
        return (list(CAPTURE_SOURCES) + [name for name in monitors if name not in CAPTURE_SOURCES])[:MAX_SOURCES_TO_TRY]

    def stop(self) -> None:
        self._capturing = False
        process, self._process = self._process, None
        if process is not None:
            self._end_process(process)
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(STOP_TIMEOUT_S)

    def _capture_args(self, source: str) -> list[str]:
        return [self.ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-f", "pulse", "-fragment_size", str(FRAGMENT_BYTES), "-i", source,
                "-ac", str(CHANNELS), "-ar", str(self.sample_rate), "-f", "s16be", "pipe:1"]

    def _try_source(self, source: str, timeout: float) -> str | None:
        """Spawns ffmpeg on one source and waits for its first bytes. Returns None on success, else why not.

        Raises OSError when ffmpeg itself cannot be spawned (missing binary) - that is not the source's fault.
        """
        self._first_data.clear()
        self._stderr_tail = ""
        process = _spawn(self._capture_args(source), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self._process = process
        self._capturing = True
        self._reader = threading.Thread(target=self._run_reader, args=(process,), name="audio-capture", daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(process,), name="audio-capture-stderr", daemon=True)
        self._stderr_thread.start()

        # started = the first bytes are here (see START_TIMEOUT_S). an early exit is a failure right away.
        deadline = time.monotonic() + timeout
        exited_early = False
        while time.monotonic() < deadline:
            if self._first_data.wait(0.05):
                return None
            if process.poll() is not None:
                exited_early = True
                break
        self._capturing = False
        self._process = None
        self._end_process(process)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(STOP_TIMEOUT_S)
        drain, self._stderr_thread = self._stderr_thread, None
        if drain is not None:
            drain.join(STOP_TIMEOUT_S)   # the process is gone, so its last words are in the pipe: collect them for the log
        if exited_early:
            return "ffmpeg beendet mit %s%s" % (process.returncode, self._stderr_reason())
        return "keine Daten innerhalb %.1fs%s" % (timeout, self._stderr_reason())

    def _stderr_reason(self) -> str:
        # ffmpeg prefixes every line with "[in#0 @ 0x55...] " - noise in the window's log
        tail = re.sub(r"\[[^\]]*@ 0x[0-9a-f]+\] ", "", self._stderr_tail).strip().replace("\n", " | ")
        return ", " + tail[-300:] if tail else ""

    @staticmethod
    def _end_process(process: subprocess.Popen) -> None:
        """terminate, briefly wait, kill: ffmpeg treats SIGTERM like 'q' and leaves at once, but never trust it."""
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(STOP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            pass   # a stop must never raise into the server's stop path; a stuck child is the kernel's problem now
        # the pipes are closed by the threads reading them (they hit EOF now that the writer is dead):
        # closing here could hand the fd number to the next ffmpeg while the old reader is about to read it

    # ------------------------------------------------------------------ threads

    def _run_reader(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        fd = stream.fileno()
        try:
            while self._capturing:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    # a capture that had begun and died mid-stream is news; one that never delivered is
                    # reported by start() as the reason it moved on to the next source
                    if self._capturing and self._process is process and self._first_data.is_set():
                        log.write("audio: Aufnahme abgebrochen (ffmpeg weg%s)" % self._stderr_reason())
                    break
                if self._process is not process:
                    break   # a probe that was given up on, or a stop: these bytes belong to a dead session
                            # and must not land in the next one's ring (nor pass it off as "it delivered")
                self._write(chunk)
                self._first_data.set()
        except Exception as error:   # noqa: BLE001 - a thread must never die silently
            log.write("audio: Aufnahme-Thread gestorben: %s" % error)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        """ffmpeg's stderr must be read or it blocks on a full pipe; keep only a tail for the log."""
        stream = process.stderr
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", "replace"))[-STDERR_TAIL_CHARS:]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ the ring

    def _write(self, data: bytes) -> None:
        # every trim here drops WHOLE frames only. a pipe read can end mid-frame (ffmpeg writes 1920-byte
        # fragments today, but nothing guarantees that), and the ring's tail then holds a partial frame:
        # dropping an odd number of bytes from the head would shift the L/R and high/low byte boundaries
        # for every sample that follows - loud noise for the rest of the session, with no way back.
        say = False
        with self._ring_gate:
            ring = self._ring
            overflow = len(ring) + len(data) - self._ring_capacity_bytes
            if overflow > 0:
                overflow -= overflow % BYTES_PER_FRAME
                self.dropped_frames += overflow // BYTES_PER_FRAME
                del ring[:overflow]
            ring += data
            # the guard: more than GUARD_HIGH_MS waiting means the sender fell behind (or the PC delivers a
            # hair faster than our clock ticks); every frame of it is delay the PS3 hears. drop back to a
            # small cushion in one step - an audible blip once in a while beats latency creeping for ever.
            if len(ring) > self._guard_high_bytes:
                excess = len(ring) - self._guard_low_bytes
                excess -= excess % BYTES_PER_FRAME
                self.dropped_frames += excess // BYTES_PER_FRAME
                del ring[:excess]
                say = not self._guard_logged
                self._guard_logged = True
        if say:
            # outside the lock on purpose: log.write goes to a file and may rotate it, and the sender waits
            # on this lock every 5ms
            log.write("audio: Puffer über %dms, verwerfe Ältestes bis %dms (Latenzschutz)" % (GUARD_HIGH_MS, GUARD_LOW_MS))

    def read_frames(self, frames: int) -> tuple[bytes, int]:
        """Takes up to `frames` stereo frames out of the ring, padded with silence; also how many were real."""
        wanted = frames * BYTES_PER_FRAME
        with self._ring_gate:
            ring = self._ring
            available = min(wanted, len(ring)) // BYTES_PER_FRAME * BYTES_PER_FRAME
            data = bytes(ring[:available])
            del ring[:available]
        if available < wanted:
            data += bytes(wanted - available)
        return data, available // BYTES_PER_FRAME

    def read(self, frames: int) -> bytes:
        """s16be interleaved stereo, always frames x 4 bytes; a short ring is topped up with silence."""
        return self.read_frames(frames)[0]

    @property
    def buffered_frames(self) -> int:
        with self._ring_gate:
            return len(self._ring) // BYTES_PER_FRAME

    @property
    def is_capturing(self) -> bool:
        return self._capturing and self._process is not None and self._process.poll() is None


class AudioStreamer:
    """Sends the captured speaker audio to the PS3, paced at exactly one chunk per CHUNK_MS."""

    def __init__(self, sock, ffmpeg_path: str = "ffmpeg"):
        self.sock = sock
        self.capture = AudioCapture(ffmpeg_path)
        self._streaming = False
        self._send_thread: threading.Thread | None = None
        self._target = None

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def start(self, target) -> None:
        target = tuple(target)
        if self._streaming and target == self._target:
            return   # a repeated PLAY, not a new session
        self.stop()
        try:
            if not self.capture.start():
                return   # no speakers to capture (reason is logged): video still streams
        except Exception as error:   # noqa: BLE001 - audio is optional, never take the video down with it
            log.write("audio: Aufnahme-Start fehlgeschlagen, streame nur Video (%s)" % error)
            return
        self._streaming = True
        self._target = target
        thread = threading.Thread(target=self._run_send_loop, args=(target,), name="audio-send", daemon=True)
        self._send_thread = thread
        try:
            thread.start()
        except RuntimeError as error:   # out of threads: without this the ffmpeg would run on with no sender
            log.write("audio: Sende-Thread startet nicht, streame nur Video (%s)" % error)
            self._streaming = False
            self._send_thread = None
            self._target = None
            self.capture.stop()

    def stop(self) -> None:
        self._streaming = False
        thread, self._send_thread = self._send_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_TIMEOUT_S)
        self._target = None
        self.capture.stop()

    def _run_send_loop(self, target) -> None:
        try:
            self._send_loop(target)
        except Exception as error:   # noqa: BLE001
            log.write("audio: Sende-Thread gestorben: %s" % error)
        finally:
            # ended on our own (send error): say so, or the PS3's next PLAY from the same port would be taken
            # for a repeat of this session and audio would stay dead for as long as it keeps streaming video.
            # only while this is still the current session's thread - stop() has already moved on otherwise.
            if self._send_thread is threading.current_thread():
                self._streaming = False
                self.capture.stop()   # nobody is draining the ring any more; leaving ffmpeg running would
                                      # hold a pulse stream and burn CPU until the watchdog's stop, or for ever

    def _send_loop(self, target) -> None:
        capture = self.capture
        sample_rate = capture.sample_rate
        # a high-rate device (176.4/192kHz) would put more frames in a 5ms chunk than the PS3 accepts,
        # and it drops such a packet whole - losing all audio. cap the chunk at what it takes (both its
        # AUDIO_MAX_FRAMES and its 1500-byte receive buffer, see PS3_PACKET_MAX_BYTES); that just means
        # shorter, more frequent packets. pace by the chunk's real duration so the clock stays realtime
        # whatever the cap does.
        chunk_frames = min(sample_rate * CHUNK_MS // 1000, MAX_FRAMES_PER_PACKET,
                           (PS3_PACKET_MAX_BYTES - HEADER_BYTES) // BYTES_PER_FRAME)
        chunk_us = chunk_frames * 1_000_000 // sample_rate

        # tells the PS3 the rate so it can open its speaker feed. repeated once a second for the whole
        # stream: the PS3 may still be finishing the video handshake when the first ones arrive, and a
        # missed announcement would otherwise mean no sound for the entire session.
        info = build_ainfo(sample_rate)
        packets_per_second = max(1, 1_000_000 // chunk_us)
        try:
            self.sock.sendto(info, target)
        except OSError as error:
            log.write("audio: Senden abgebrochen: %s" % error)
            return
        log.write("audio: streame %dHz Stereo an %s:%d (%dkbps)" % (sample_rate, target[0], target[1], sample_rate * 32 // 1000))

        # build a small cushion before playing so the 10ms delivery fragments don't gap the sound. but give
        # up waiting quickly: should the source ever go quiet (WASAPI did, during silence) a session would
        # otherwise sit here for ever and never send a single packet.
        # measured on PipeWire: fragments land every 10ms with ~2ms jitter and we start right after the one
        # that fills the cushion, so the ring swings between ~5 and 20ms and a late fragment costs one silent
        # 5ms packet a few times a minute at first; each one lifts the swing by 5ms, after which it is quiet.
        prebuffer_frames = sample_rate * PREBUFFER_MS // 1000
        prebuffer_deadline = time.monotonic() + PREBUFFER_TIMEOUT_MS / 1000
        while self._streaming and time.monotonic() < prebuffer_deadline:
            if capture.buffered_frames >= prebuffer_frames:
                break
            time.sleep(0.001)

        start_us = now_us()
        packet_id = 0
        silent_packets = 0
        try:
            while self._streaming:
                # packet N carries the sound due N chunks after we started
                due_us = start_us + packet_id * chunk_us
                wait_us = due_us - now_us()
                if wait_us > SLEEP_MARGIN_US:
                    time.sleep((wait_us - SLEEP_MARGIN_US) / 1_000_000)
                wait_us = due_us - now_us()
                if wait_us > SPIN_MARGIN_US:
                    time.sleep((wait_us - SPIN_MARGIN_US) / 1_000_000)
                while now_us() < due_us:
                    pass   # only the last hair; Linux' sleep is accurate to ~0.1ms

                samples, real_frames = capture.read_frames(chunk_frames)
                if real_frames == 0:
                    silent_packets += 1   # nothing captured: send silence, keep the clock going

                self.sock.sendto(build_af_packet(packet_id, chunk_frames, now_us(), samples), target)
                if packet_id % packets_per_second == 0:
                    self.sock.sendto(info, target)
                packet_id += 1
        except OSError as error:
            log.write("audio: Senden abgebrochen: %s" % error)
        log.write("audio: %d Pakete gesendet (%d ohne Ton-Daten, %d Frames verworfen)" % (packet_id, silent_packets, capture.dropped_frames))
