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
# The stream size. 1280x720 is the original's, and for a long time the only one anyone had measured on a
# console. The larger ones exist because CAVLC turned out to leave headroom: on the real PS3, CAVLC at
# 17 Mbit/s decoded in 22 ms per picture where CABAC at the same rate needed 36-40. More pixels cost the
# SPU decoder roughly in proportion, so these are an experiment and not a recommendation - upstream measured
# 1080p at 80-120 ms and 27 fps, though with CABAC (upstream/ps3-app/README.md). The PS3 sizes its decoder
# from the stream's own SPS and letterboxes whatever it gets, so nothing on that side has to be told.
# Every one of these is a multiple of 16 in both directions. H.264 codes in 16x16
# macroblocks, so a height like 900 or 1080 is rounded UP (to 912 / 1088) and the encoder adds a cropping
# note for the display size. The PS3 ignores that note and takes the coded size - the console showed
# "1600x912" and "1920x1088" - so a misaligned size costs decode time for rows nobody wants to see and
# stretches the picture (1600x912 is 1.754, not 1.778). These sizes have neither problem.
# Three of them are exactly 16:9; 1408x800 is 1.760, because an exact 16:9 that is also macroblock-aligned
# needs a height divisible by 144 and there is none between 720 and 864. 0.9% off, and the PS3 letterboxes
# to the TV anyway.
# 1920x1088 rather than 1920x1080: 1080 is not a multiple of 16, so H.264 must code 1088 rows and mark
# the extra 8 as cropped - and the PS3 app never reads the cropping fields (h264.c derives its size from
# pic_width_in_mbs / pic_height_in_map_units alone, deliberately, because cellVdec must be given the CODED
# size). A 1080 stream therefore appears as 1088 on the console anyway, with 8 rows of encoder padding on
# screen and the picture squashed by that much. Sending 1088 real rows costs exactly the same to decode
# and wastes none of them.
# 1920x1088 is the ceiling, and it is the decoder's rather than ours. Tried on the real console: 2048x1152,
# 2560x1440 and 3840x2160 never connected at all - no picture, no error, the PS3 simply refused. That is
# exactly where H.264 level 4.2 stops (8704 macroblocks per picture); 1920x1088 needs 8160 and fits, and
# 2048x1152 needs 9216 and does not. cellVdec evidently will not go past 4.2, so the sizes above are gone.
STREAM_SIZES = ((1280, 720), (1408, 800), (1536, 864), (1792, 1008), (1920, 1088))
WIDTH, HEIGHT = STREAM_SIZES[0]
FPS = 60
KBPS = 10000
SEND_RATE_KBPS = KBPS * 3        # packets may leave faster than the video's own rate

# The PS3's decoder is the wall, and these two knobs are what move it. Measured on the target PC with a
# deliberately weak decoder (ffmpeg -threads 1 -cpuflags 0, no SIMD - the closest stand-in for cellVdec's
# SPU decoder) over 6 s of high-motion 720p60: 10 Mbit/s CABAC (the Windows original's settings) cost
# 2.8 ms per frame, CAVLC at the same rate 1.6 ms (-43%), 6 Mbit/s CAVLC 1.5 ms (-46%). On the real PS3
# the same stream measured 38-40 ms decode at 11-13 Mbit/s CABAC - far past the 16.7 ms a 60 fps frame
# gets, which is why the console dropped every other frame ("behind" climbing) and looked like 25 fps.
# 40 Mbit/s is the top because 45 and 50 could not get a stream started at all on the real console.
# The mechanism is in the PS3 app: an intra-refresh stream carries exactly ONE keyframe, the anchor IDR
# at the very start (ffmpeg's nvenc wrapper makes the IDR period infinite once intra refresh is on), and
# stream.c will not feed the decoder anything before it - the first access unit must be that keyframe,
# because it carries the SPS the decoder is built from. At those rates the anchor is several hundred
# kilobytes going out in one burst; lose a single fragment of it and there is never a second chance.
BITRATE_CHOICES_KBPS = (4000, 6000, 8000, 10000, 12000, 16000, 20000, 24000, 30000, 35000, 40000)
ENTROPY_CODERS = ("cavlc", "cabac")

# How the encoder spends the bitrate. Only the x264 rung honours all three; NVENC gets "cbr" as its own
# -rc cbr and treats "quality" as VBR, because its ull preset has no quality-targeted mode.
#   vbr      - the target rate on average, with headroom for the refresh strip. What the console was proven with.
#   quality  - a constant quality instead of a constant rate, capped so the network still cannot be flooded.
#              A still desktop then costs almost nothing AND stays sharp, which is the case VBR handles worst.
#   cbr      - a genuinely constant rate. x264 pads with filler NAL units to hold it, which is bandwidth spent
#              on nothing; the splitter drops them (stream_sender: NAL type 12) so they cost the console only
#              the bytes off the wire.
RATE_CONTROLS = ("vbr", "quality", "cbr")
QUALITY_CRF = 20                 # x264's -crf for the "quality" mode: visually clean without being wasteful

SINFO_LEVEL = 42                 # the floor: H.264 level 4.2 covers everything up to and including 1920x1088

# (level, max macroblocks per picture, max macroblocks per second) - Annex A of the H.264 spec. 4.2 stops at
# 8704 macroblocks, and 1920x1088 needs 8160, so every size above Full HD needs a higher level announced.
# The PS3 app builds its decoder from the stream's own SPS rather than from SINFO (see live_streamer), so
# this is an honest announcement rather than a load-bearing one - but announcing 4.2 for a 4K stream would
# simply be false.
_H264_LEVELS = ((42, 8704, 522240), (50, 22080, 589824), (51, 36864, 983040), (52, 36864, 2073600))


def sinfo_level_for(width: int, height: int, fps: int = 60) -> int:
    """The lowest H.264 level whose picture-size and macroblock-rate limits cover this stream, floored at 4.2."""
    macroblocks = ((width + 15) // 16) * ((height + 15) // 16)
    for level, max_frame, max_rate in _H264_LEVELS:
        if macroblocks <= max_frame and macroblocks * fps <= max_rate:
            return level
    return _H264_LEVELS[-1][0]
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

# The PS3 reassembles one access unit into a fixed 1 MB slot and drops anything larger without a word
# (FRAME_MAX_BYTES in stream.c, and the length check in its fragment handler). The VBV buffer is what
# decides how big a single picture may get, so above ~32 Mbit/s the 250 ms window alone would let a frame
# cross that line. Cap it with headroom; below 26 Mbit/s this changes nothing, so every measurement made
# so far still describes the stream it described.
PS3_MAX_AU_BYTES = 1024 * 1024
MAX_VBV_KBIT = PS3_MAX_AU_BYTES * 8 * 80 // 100 // 1000    # 80% of the slot

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
