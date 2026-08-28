"""Receives the PS3's controller and replays it on the PC (port of PadReceiver.cs).

Either as a virtual Xbox gamepad (for games) or as the mouse and keyboard (for the desktop); the PS3
picks which with a PADMODE message. The gamepad needs a writable /dev/uinput (the package's udev rule
grants it), and we fall back to the mouse without it.

pad packet layout (20 bytes, big-endian, must match stream.c on the PS3):
  [0]='C' [1]='P' [2..5]=packetId [6..7]=buttons [8]=leftX [9]=leftY [10]=rightX [11]=rightY
  [12..19]=send time, already converted to OUR clock by the PS3

handle() runs on the server's receive thread outside any try - so nothing in here may raise, whatever
the packet looks like.
"""

import struct
import threading
import time

from . import log
from .clock import now_us
from .protocol import PAD_PACKET_BYTES, describe_buttons

REPORT_INTERVAL_MS = 2000
_CP_FIELDS = struct.Struct(">IHbbbbQ")   # after the 'CP' tag: packetId, buttons, leftX, leftY, rightX, rightY, sentUs


class PadReceiver:
    def __init__(self, desktop_input, gamepad, swap_sticks):
        self._desktop = desktop_input
        self._gamepad = gamepad
        self._swap_sticks = swap_sticks          # callable: the "swap sticks in mouse mode" preference, read per packet
        self._gate = threading.RLock()            # packets, mode switches and release() arrive on different threads
        self.report_interval_ms = REPORT_INTERVAL_MS
        self._report_started = time.monotonic()
        self.packets_received = 0
        self.packets_lost = 0
        self.last_trip_ms = 0                     # PS3 -> here, averaged over the last report window
        self._interval_trip_us = 0                # trip time is averaged over the last report window, not all time
        self._interval_packets = 0
        self._last_packet_id = -1
        self._last_buttons = 0
        self.gamepad_mode = False
        self._gamepad_unavailable = False
        self._last_reported_state = ""
        self._closed = False
        self._fault_logged = False

    # which PC device the pad drives. asking for the gamepad plugs a virtual one in and leaves it
    # there for the session; it comes back false when uinput is not available.
    def set_gamepad_mode(self, wanted: bool) -> None:
        with self._gate:
            if self._closed or wanted == self.gamepad_mode or (wanted and self._gamepad_unavailable):
                return
            if wanted and not self._gamepad.try_open():
                self._gamepad_unavailable = True   # the PS3 asks once a second; do not keep trying
                log.write("pad: kein virtuelles Gamepad verfügbar. Bleibe bei Maus und Tastatur.")
                return
            self._release_locked()   # let go of whatever the device we are leaving was holding down
            self.gamepad_mode = wanted
            log.write("pad: steuert jetzt " + ("ein virtuelles Xbox-Gamepad" if wanted else "Maus und Tastatur"))

    # a key typed on the PS3's on-screen keyboard, replayed on the PC keyboard
    def type_key(self, character: str) -> None:
        with self._gate:
            if self._closed:
                return
            try:
                self._desktop.type_character(character)
            except Exception as error:   # noqa: BLE001
                self._log_fault(error)

    # the stream ended - let go of anything the PS3 was holding down, or it stays stuck on the PC
    def release(self) -> None:
        with self._gate:
            self._release_locked()

    def _release_locked(self) -> None:
        try:
            self._desktop.release_all()
        except Exception as error:   # noqa: BLE001
            self._log_fault(error)
        try:
            self._gamepad.send(0, 0, 0, 0, 0)
        except Exception as error:   # noqa: BLE001
            self._log_fault(error)
        self._last_buttons = 0
        self._last_packet_id = -1

    def handle(self, packet: bytes, sender) -> None:
        try:
            with self._gate:
                self._handle_locked(packet)
        except Exception as error:   # noqa: BLE001 - a malformed packet must never end the receive loop
            self._log_fault(error)

    def _handle_locked(self, packet: bytes) -> None:
        if self._closed or len(packet) < PAD_PACKET_BYTES:
            return
        packet_id, buttons, left_x, left_y, right_x, right_y, sent_us = _CP_FIELDS.unpack_from(packet, 2)

        if self._last_packet_id >= 0 and packet_id > self._last_packet_id + 1:
            self.packets_lost += packet_id - self._last_packet_id - 1
        self._last_packet_id = packet_id
        self.packets_received += 1
        self._interval_trip_us += max(0, now_us() - sent_us)
        self._interval_packets += 1

        if self.gamepad_mode:
            self._gamepad.send(buttons, left_x, left_y, right_x, right_y)
        elif self._swap_sticks():
            self._desktop.apply(buttons, right_x, right_y, left_x, left_y)
        else:
            self._desktop.apply(buttons, left_x, left_y, right_x, right_y)

        # log every press and release as it happens - that is what proves the channel end to end
        if buttons != self._last_buttons:
            pressed = describe_buttons(buttons & ~self._last_buttons)
            released = describe_buttons(self._last_buttons & ~buttons)
            if pressed:
                log.write("pad: gedrückt " + pressed)
            if released:
                log.write("pad: losgelassen " + released)
            self._last_buttons = buttons

        if (time.monotonic() - self._report_started) * 1000 >= self.report_interval_ms:
            self._report_started = time.monotonic()
            state = "Sticks L(%d,%d) R(%d,%d)" % (left_x, left_y, right_x, right_y)
            trip_ms = self._interval_trip_us // self._interval_packets // 1000 if self._interval_packets > 0 else 0
            self.last_trip_ms = trip_ms
            self._interval_trip_us = 0
            self._interval_packets = 0
            if state != self._last_reported_state or self.packets_lost > 0:
                self._last_reported_state = state
                log.write("pad: %s, %d Pakete, %d verloren, %d ms PS3 -> hier"
                          % (state, self.packets_received, self.packets_lost, trip_ms))

    def close(self) -> None:
        """Shutdown: release everything and destroy the virtual devices. Later packets are ignored."""
        with self._gate:
            if self._closed:
                return
            self._release_locked()
            self._closed = True
            for device in (self._desktop, self._gamepad):
                try:
                    device.close()
                except Exception as error:   # noqa: BLE001
                    self._log_fault(error)

    def _log_fault(self, error: BaseException) -> None:
        if not self._fault_logged:
            self._fault_logged = True    # once; at 60 packets a second a repeat would flood the log
            log.write("pad: Fehler bei der Eingabe-Weitergabe: %r" % (error,))
