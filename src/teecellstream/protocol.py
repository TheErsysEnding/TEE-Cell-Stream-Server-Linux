"""Wire protocol constants - must match apps/cell-stream (stream.c, net-common.h) on the PS3 byte for byte.

Everything the PS3 relies on lives here so no module can drift from another. Do not change values
without changing the PS3 app.
"""

# UDP ports: the server listens on SERVER_PORT, the PS3 binds CLIENT_PORT and receives the beacon there
SERVER_PORT = 38310
BEACON_PORT = 38311
CLIENT_PORT = BEACON_PORT

BEACON_MESSAGE = b"CELLSTREAM 1"
BEACON_INTERVAL_S = 1.0
BEACON_REFRESH_TARGETS_S = 30

# stream settings - deliberately baked in, this is an appliance (same values as the Windows server)
WIDTH = 1280
HEIGHT = 720
FPS = 60
KBPS = 10000
SEND_RATE_KBPS = KBPS * 3        # packets may leave faster than the video's own rate

# The PS3's decoder is the wall, and these two knobs are what move it. Measured on the target PC with a
# deliberately weak decoder (ffmpeg -threads 1 -cpuflags 0, no SIMD - the closest stand-in for cellVdec's
# SPU decoder) over 6 s of high-motion 720p60: 10 Mbit/s CABAC (the Windows original's settings) cost
# 2.8 ms per frame, CAVLC at the same rate 1.6 ms (-43%), 6 Mbit/s CAVLC 1.5 ms (-46%). On the real PS3
# the same stream measured 38-40 ms decode at 11-13 Mbit/s CABAC - far past the 16.7 ms a 60 fps frame
# gets, which is why the console dropped every other frame ("behind" climbing) and looked like 25 fps.
BITRATE_CHOICES_KBPS = (4000, 6000, 8000, 10000, 12000)
ENTROPY_CODERS = ("cavlc", "cabac")

SINFO_LEVEL = 42                 # H.264 level 4.2 covers everything we send; over-reserving on the PS3 is harmless
SINFO_REFS = 1

# video fragments (VF): 20-byte big-endian header, <= 1300 bytes payload
FRAGMENT_PAYLOAD_BYTES = 1300
FRAGMENT_HEADER_BYTES = 20
PROTOCOL_VERSION = 2

# audio (AF): 16-byte big-endian header, then frameCount x (left, right) int16 big-endian
AUDIO_HEADER_BYTES = 16
AUDIO_CHUNK_MS = 5
AUDIO_MAX_FRAMES_PER_PACKET = 512     # the PS3 (AUDIO_MAX_FRAMES) drops any packet larger than this
AUDIO_PREBUFFER_MS = 20
AUDIO_PREBUFFER_TIMEOUT_MS = 500
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2

# controller (CP): 20 bytes, PS3 -> server, 60/s
PAD_PACKET_BYTES = 20

# liveness: the pad packet doubles as proof the PS3 is still there
CLIENT_TIMEOUT_MS = 3000
STREAM_STARTUP_GRACE_MS = 10000
WATCHDOG_TICK_MS = 500

# encoder tuning (see upstream/server/LiveStreamer.cs for the measurements behind these)
REFRESH_SWEEP_SECONDS = 1
ANCHOR_KEYFRAME_SECONDS = 3600
REFRESH_MAX_RATE_PERCENT = 140
REFRESH_BUFFER_MS = 250

# sticks
STICK_MIN = -128
STICK_MAX = 127


class PadBits:
    """Bit positions of the PS3 pad in the CP packet's `buttons` field (matches pad.h on the PS3)."""

    UP = 0
    DOWN = 1
    LEFT = 2
    RIGHT = 3
    CROSS = 4
    CIRCLE = 5
    SQUARE = 6
    TRIANGLE = 7
    L1 = 8
    R1 = 9
    L2 = 10
    R2 = 11
    START = 12
    SELECT = 13
    L3 = 14
    R3 = 15


BUTTON_NAMES = (
    "up", "down", "left", "right", "cross", "circle", "square", "triangle",
    "L1", "R1", "L2", "R2", "start", "select", "L3", "R3",
)


def describe_buttons(mask: int) -> str:
    """'cross+L1' style listing of the set bits, for the log."""
    return "+".join(name for bit, name in enumerate(BUTTON_NAMES) if mask & (1 << bit))
