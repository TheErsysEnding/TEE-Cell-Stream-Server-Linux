"""The cell-stream server: captures the desktop, sends it to the PS3, and replays the PS3's pad here.

Port of Server.cs. One UDP socket on :38310:
 - broadcasts a discovery beacon to :38311 every second so the PS3 finds us
 - answers TIME (clock sync), PLAY/STOP, PADMODE, KEY, CUSTOM, and the 60/s CP pad packets

The window (app.py/ui.py) is only a view onto this object: closing the window leaves all of this running.
"""

import atexit
import os
import shutil
import signal
import socket
import sys
import threading
import time

from . import capture, custom_commands, encoders, log, netinfo, protocol, shell_extension
from .audio import AudioStreamer
from .childproc import kill_all
from .clock import now_us
from .desktop_input import DesktopInput
from . import display_mode
from .display_mode import DisplayMode
from .live_streamer import LiveStreamer
from .pad_receiver import PadReceiver
from .power import keep_display_awake
from .settings import settings
from .virtual_gamepad import VirtualGamepad

BIND_ATTEMPTS = 25
BIND_RETRY_S = 0.2
FAILED_STARTS_BEFORE_GIVING_UP = 3


class Server:
    """One instance per process; create it, call start(), and read its properties from the window."""

    def __init__(self):
        self.sock: socket.socket | None = None
        self.live_streamer: LiveStreamer | None = None
        self.audio_streamer: AudioStreamer | None = None
        self.pad_receiver: PadReceiver | None = None
        self.display_mode = DisplayMode()
        self.stream_lock = threading.RLock()          # serialises start against stop across threads

        self.is_armed = False
        self.trip_reason: str | None = None
        self.connected_ps3: str | None = None
        self.available_encoders: list[encoders.VideoEncoder] = []
        self._chosen_encoder: encoders.VideoEncoder | None = None
        self.ffmpeg_path = "ffmpeg"

        self._stream_confirmed = False               # a pad packet has arrived, so the PS3 really is streaming
        self._last_client_packet = time.monotonic()
        self._running = False
        self._threads: list[threading.Thread] = []
        self.extension_state = shell_extension.UNAVAILABLE   # set by the shell-extension thread at start-up

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Brings the server up on its own threads. False = another copy holds the port."""
        if not self._bind_socket():
            return False

        # the fragment pacer sleeps to ~150us before each packet is due; with the default 5 ms switch
        # interval a thread waking from sleep can wait that long for the GIL, which was measured to
        # stretch a 27 ms keyframe to over 500 ms once another Python thread was busy. 0.5 ms keeps it tight.
        sys.setswitchinterval(0.0005)

        self.ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        self.live_streamer = LiveStreamer(
            self.sock, self.ffmpeg_path, protocol.FPS, protocol.KBPS, protocol.WIDTH, protocol.HEIGHT,
            protocol.SEND_RATE_KBPS, capture.create_capture, lambda: self.encoders_to_try,
            lambda: self.loss_recovery, self._on_all_encoders_failed,
            lambda: self.video_kbps, lambda: self.entropy_coder, lambda: self.stream_size,
            lambda: self.rate_control, lambda: self.slice_count)
        # the same resolved binary the video uses (and the one the "bereit:" line names): audio must not
        # fall back to a bare "ffmpeg" off PATH while video runs an absolute path
        self.audio_streamer = AudioStreamer(self.sock, self.ffmpeg_path)   # desktop sound goes with the desktop picture
        self.pad_receiver = PadReceiver(DesktopInput(), VirtualGamepad(), lambda: self.swap_mouse_sticks)
        self.is_armed = True                                     # the server runs by default; only a fault or the user stops it

        self.available_encoders = encoders.detect_available(self.ffmpeg_path)
        self._chosen_encoder = encoders.load_choice(self.available_encoders, settings)
        if self._chosen_encoder is None:
            self.trip_fuse("auf diesem PC funktioniert kein Video-Encoder (ffmpeg fehlt oder kann kein H.264)")
        log.write("bereit: " + self.settings_summary + ", ffmpeg = " + self.ffmpeg_path)

        self._running = True
        for name, target in (("beacon", self._run_beacon_loop), ("watchdog", self._run_client_watchdog),
                             ("receive", self._run_receive_loop), ("capture-warmup", self._warm_up_capture),
                             ("shell-extension", self._enable_shell_extension)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def install_exit_hooks(self) -> None:
        """Desktop resolution and child processes must be put back whatever way we die. Call from the main thread."""
        atexit.register(self.shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                pass   # not the main thread (the GTK app installs its own handlers instead)

    def _on_signal(self, signum, frame):
        self.shutdown()
        raise SystemExit(0)

    def shutdown(self) -> None:
        """The tray's Quit: put the desktop back before we go, or it is left at the streaming resolution."""
        if not self._running:
            return
        self._running = False
        self.stop_streaming("der Server wird beendet")
        if self.pad_receiver is not None:
            self.pad_receiver.close()
        kill_all()
        # unblocks the receive thread's recvfrom and gives the port back at once (a second copy waiting
        # in _bind_socket, or the same process starting a new Server, must not wait for our exit)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def _bind_socket(self) -> bool:
        """A new build replacing us finds the old copy still letting go of the port: wait rather than fall over."""
        for _attempt in range(BIND_ATTEMPTS):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                sock.bind(("0.0.0.0", protocol.SERVER_PORT))
                self.sock = sock
                log.write("lausche auf udp :%d, Beacon an :%d" % (protocol.SERVER_PORT, protocol.BEACON_PORT))
                return True
            except OSError:
                sock.close()
                time.sleep(BIND_RETRY_S)
        log.write("konnte udp :%d nicht belegen - eine andere Kopie des Servers hält den Port noch. Gebe auf." % protocol.SERVER_PORT)
        return False

    def _warm_up_capture(self) -> None:
        """First run: show the screen-share dialog now, while the user is at the PC, not when the PS3 connects."""
        try:
            capture.warm_up()
        except Exception as error:   # noqa: BLE001 - never let a portal hiccup kill the server
            log.write("capture: Vorbereitung fehlgeschlagen: %s" % error)

    def _enable_shell_extension(self) -> None:
        """Switch on the bundled GNOME extension (see shell_extension.py) - the package cannot, we can."""
        try:
            self.extension_state = shell_extension.ensure_enabled()
        except Exception as error:   # noqa: BLE001 - never let this take the server down
            log.write("extension: unerwarteter Fehler (%s)" % error)
            self.extension_state = shell_extension.FAILED

    # ------------------------------------------------------------------ what the window shows

    @property
    def is_ps3_connected(self) -> bool:
        return self.live_streamer is not None and self.live_streamer.is_streaming

    @property
    def settings_summary(self) -> str:
        recovery = "Intra-Refresh" if self.loss_recovery == "intra" else "Keyframes"
        return "%dx%d mit %d fps, %d Mbit/s, %s, %s" % (self.stream_size + (protocol.FPS,
                                                        self.video_kbps // 1000, self.entropy_coder.upper(), recovery))

    # the fuse. armed, the server answers the PS3; tripped, it ignores it and leaves the desktop alone.
    # it trips itself when the encoder will not start, because retrying that forever flapped the desktop
    # resolution on and off and made the PC unusable. only the user re-arms it.
    def arm(self) -> None:
        if self.is_armed:
            return
        self.is_armed = True
        self.trip_reason = None
        if self.live_streamer is not None:
            self.live_streamer.reset_failures()
        log.write("gestartet: warte auf die PS3")

    def disarm(self, why: str) -> None:
        if not self.is_armed:
            return
        self.is_armed = False
        self.trip_reason = why
        self.stop_streaming(why)
        log.write("gestoppt: " + why)

    def trip_fuse(self, fault: str) -> None:
        self.disarm(fault + ". Nach der Behebung auf Start drücken.")

    def _on_all_encoders_failed(self, reason: str) -> None:
        self.trip_fuse(reason)

    # the encoders this PC can actually run, best first, and the one to use
    @property
    def chosen_encoder(self) -> encoders.VideoEncoder | None:
        return self._chosen_encoder

    @chosen_encoder.setter
    def chosen_encoder(self, value: encoders.VideoEncoder | None) -> None:
        if value is None or value is self._chosen_encoder:
            return
        self._chosen_encoder = value
        encoders.save_choice(value, settings)
        log.write("encoders: ab jetzt " + value.name)

    @property
    def encoders_to_try(self) -> list[encoders.VideoEncoder]:
        """The chosen one first, then the rest as fallbacks."""
        order = []
        if self._chosen_encoder is not None:
            order.append(self._chosen_encoder)
        order.extend(encoder for encoder in self.available_encoders if encoder is not self._chosen_encoder)
        return order

    @property
    def loss_recovery(self) -> str:
        value = settings.get("loss_recovery", "intra")
        return value if value in ("intra", "keyframe") else "intra"

    @loss_recovery.setter
    def loss_recovery(self, value: str) -> None:
        if value not in ("intra", "keyframe") or value == self.loss_recovery:
            return
        settings.set("loss_recovery", value)
        log.write("video: Fehlerkorrektur ab dem nächsten Stream: " + ("Intra-Refresh" if value == "intra" else "Keyframes"))

    @property
    def video_kbps(self) -> int:
        """Video bitrate. The PS3's decoder - not the link - is what this limits: measured 38-40 ms decode per
        frame at 11-13 Mbit/s, which is past the 16.7 ms a 60 fps frame gets, so the console dropped every
        other one. Lower it until the picture keeps up; raise it for a sharper picture."""
        value = settings.get("video_kbps", protocol.KBPS)
        return value if value in protocol.BITRATE_CHOICES_KBPS else protocol.KBPS

    @video_kbps.setter
    def video_kbps(self, value: int) -> None:
        if value not in protocol.BITRATE_CHOICES_KBPS or value == self.video_kbps:
            return
        settings.set("video_kbps", int(value))
        log.write("video: Bitrate ab dem nächsten Stream: %d Mbit/s" % (value // 1000))

    @property
    def stream_size(self) -> tuple[int, int]:
        """What the PS3 is sent. Bigger is sharper and costs the console's SPU decoder in proportion."""
        value = settings.get("stream_size", "")
        # the misaligned sizes 1.7.0 offered map to their aligned neighbours rather than silently
        # dropping back to 720p on someone who had picked a large one
        value = {"1600x900": "1536x864", "1920x1080": "1920x1088"}.get(value, value)
        for size in protocol.STREAM_SIZES:
            if value == "%dx%d" % size:
                return size
        return protocol.STREAM_SIZES[0]

    @stream_size.setter
    def stream_size(self, value) -> None:
        size = tuple(value) if isinstance(value, (tuple, list)) else ()
        if size not in protocol.STREAM_SIZES or size == self.stream_size:
            return
        settings.set("stream_size", "%dx%d" % size)
        log.write("video: Auflösung ab dem nächsten Stream: %dx%d" % size)

    @property
    def entropy_coder(self) -> str:
        """CAVLC or CABAC. CAVLC costs the PS3 far less to decode (measured -43%) for a little less quality."""
        value = settings.get("entropy_coder", "cavlc")
        return value if value in protocol.ENTROPY_CODERS else "cavlc"

    @entropy_coder.setter
    def entropy_coder(self, value: str) -> None:
        if value not in protocol.ENTROPY_CODERS or value == self.entropy_coder:
            return
        settings.set("entropy_coder", value)
        log.write("video: Entropie-Codierung ab dem nächsten Stream: " + value.upper())

    @property
    def rate_control(self) -> str:
        """How the encoder spends the bitrate. Measured on the real console at 1920x1088, 35 Mbit/s, x264,
        CAVLC, intra refresh: VBR up to 32 ms latency, CBR up to 31, and "quality" best at up to 29 - and
        the difference was noticeable, not just on the counter. Only the x264 rung honours all three."""
        value = settings.get("rate_control", "quality")
        return value if value in protocol.RATE_CONTROLS else "quality"

    @rate_control.setter
    def rate_control(self, value: str) -> None:
        if value not in protocol.RATE_CONTROLS or value == self.rate_control:
            return
        settings.set("rate_control", value)
        log.write("video: Ratensteuerung ab dem nächsten Stream: " + value.upper())

    @property
    def slice_count(self) -> int:
        """Slices per picture, x264 only - a TEST setting, see protocol.SLICE_COUNTS. 1 is what every
        measurement so far was made with, and anything unreadable falls back to it."""
        try:
            value = int(settings.get("slice_count", 1))
        except (TypeError, ValueError):
            return 1
        return value if value in protocol.SLICE_COUNTS else 1

    @slice_count.setter
    def slice_count(self, value: int) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        if value not in protocol.SLICE_COUNTS or value == self.slice_count:
            return
        settings.set("slice_count", value)
        log.write("video: %d Slice(s) je Bild ab dem nächsten Stream (nur x264)" % value)

    @property
    def swap_mouse_sticks(self) -> bool:
        return bool(settings.get("swap_mouse_sticks", False))

    @swap_mouse_sticks.setter
    def swap_mouse_sticks(self, on: bool) -> None:
        settings.set("swap_mouse_sticks", bool(on))

    @property
    def switch_display_mode(self) -> bool:
        """Kept as a boolean because everything outside this class only ever asks "does it switch at all"."""
        return self.display_strategy != "off"

    @switch_display_mode.setter
    def switch_display_mode(self, on: bool) -> None:
        self.display_strategy = "capture" if on else "off"

    @property
    def display_strategy(self) -> str:
        """"off", "capture" (the measured default: most refresh the compositor can use) or "sixty" (desktop
        at 60 Hz, so game, compositor, grid and console all share one clock). See display_mode."""
        stored = settings.get("display_strategy")
        if stored in display_mode.DISPLAY_STRATEGIES:
            return stored
        # migrate the old boolean: it only ever meant off or the capture-optimised mode
        return "capture" if bool(settings.get("switch_display_mode", True)) else "off"

    @display_strategy.setter
    def display_strategy(self, value: str) -> None:
        if value not in display_mode.DISPLAY_STRATEGIES or value == self.display_strategy:
            return
        settings.set("display_strategy", value)
        settings.set("switch_display_mode", value != "off")   # keep the old key truthful for older builds
        log.write("display: Umschaltung ab dem nächsten Stream: " + value)

    # ------------------------------------------------------------------ the threads

    def _run_beacon_loop(self) -> None:
        targets = netinfo.get_beacon_targets()
        log.write("Beacon an: " + " ".join(ip for ip, _port in targets))
        seconds_since_refresh = 0
        while self._running:
            for target in targets:
                try:
                    self.sock.sendto(protocol.BEACON_MESSAGE, target)
                except OSError as error:
                    log.write("Beacon an %s fehlgeschlagen: %s" % (target[0], error))
            time.sleep(protocol.BEACON_INTERVAL_S)
            seconds_since_refresh += 1
            if seconds_since_refresh >= protocol.BEACON_REFRESH_TARGETS_S:   # pick up NIC changes
                targets = netinfo.get_beacon_targets()
                seconds_since_refresh = 0

    def _run_receive_loop(self) -> None:
        while self._running:
            try:
                packet, sender = self.sock.recvfrom(2048)
            except OSError:
                continue   # ICMP port-unreachable from a previous send surfaces here
            if not packet:
                continue
            self._last_client_packet = time.monotonic()   # proof the PS3 is still there (see watchdog)

            # the pad arrives 60 times a second, so match it before anything else and never log it
            if len(packet) >= protocol.PAD_PACKET_BYTES and packet[0] == 0x43 and packet[1] == 0x50:   # 'C' 'P'
                self._stream_confirmed = True
                self.pad_receiver.handle(packet, sender)
                continue

            try:
                self._handle_command(packet, sender)
            except Exception as error:   # noqa: BLE001 - one bad packet must not end the receive loop
                log.write("Fehler bei Paket von %s: %s" % (sender[0], error))

    def _handle_command(self, packet: bytes, sender) -> None:
        text = packet.decode("ascii", "replace")
        if text.startswith("TIME"):
            # clock sync: the PS3 pairs our clock with its own so it can measure per-stage latency
            self.sock.sendto(("TIME %d" % now_us()).encode("ascii"), sender)
        elif text.startswith("PLAY"):
            if not self.is_armed:
                return   # stopped, or the encoder is broken: do not touch the desktop
            with self.stream_lock:   # don't let a watchdog stop interleave with bringing a stream up
                self.connected_ps3 = sender[0]
                # the desktop must be at the streaming size BEFORE the capture starts
                keep_display_awake(True)
                strategy = self.display_strategy
                if strategy != "off" and not os.environ.get("TEE_CST_NO_DISPLAY_SWITCH"):
                    self.display_mode.match_for_capture(*self.stream_size, protocol.FPS, strategy)
                self.live_streamer.start(sender)     # repeat PLAYs are ignored inside
                self.audio_streamer.start(sender)
        elif text.startswith("PADMODE "):
            self.pad_receiver.set_gamepad_mode(text[8:].startswith("gamepad"))
        elif text.startswith("KEY ") and len(packet) >= 5:
            self.pad_receiver.type_key(chr(packet[4]))   # the raw byte after "KEY " is the character
        elif text.startswith("CUSTOM "):
            try:
                slot = int(text[7:].strip())
            except ValueError:
                return
            custom_commands.run(slot)
        elif text.startswith("STOP"):
            self.stop_streaming("die PS3 hat uns gebeten aufzuhören")
        else:
            log.write("unbekanntes Paket von %s: %r" % (sender[0], text[:40]))

    def stop_streaming(self, why: str) -> None:
        """Everything a stream turns on gets turned off here, whoever asked - a STOP, or the PS3 vanishing."""
        with self.stream_lock:
            was_streaming = self.is_ps3_connected
            if self.live_streamer is not None:
                self.live_streamer.stop()
            if self.audio_streamer is not None:
                self.audio_streamer.stop()
            self.connected_ps3 = None
            self._stream_confirmed = False
            if self.pad_receiver is not None:
                self.pad_receiver.release()
            self.display_mode.restore()
            keep_display_awake(False)   # idle again: let the screen sleep
            if was_streaming:
                log.write("Stream beendet: " + why + ". Warte wieder auf die PS3.")

    def _run_client_watchdog(self) -> None:
        """The PS3 sends its pad 60x a second for as long as it is streaming, so silence means it is gone."""
        while self._running:
            time.sleep(protocol.WATCHDOG_TICK_MS / 1000)
            if not self.is_ps3_connected:
                # the pump can stop on its own (every encoder failed, or ffmpeg died) with nothing to put the
                # desktop back. if it left the resolution switched, restore it here.
                if self.display_mode.is_changed:
                    self.stop_streaming("der Encoder hat von selbst aufgehört")
                continue
            timeout_ms = protocol.CLIENT_TIMEOUT_MS if self._stream_confirmed else protocol.STREAM_STARTUP_GRACE_MS
            if (time.monotonic() - self._last_client_packet) * 1000 < timeout_ms:
                continue
            self.stop_streaming("seit %dms nichts von der PS3" % timeout_ms)
