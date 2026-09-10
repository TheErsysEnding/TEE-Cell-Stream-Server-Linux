"""Wire protocol constants - must match apps/cell-stream (stream.c, net-common.h) on the PS3 byte for byte.

Everything the PS3 relies on lives here so no module can drift from another. Do not change values
without changing the PS3 app.
"""

from fractions import Fraction

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
# 1920x1080, not 1088: H.264 codes whole 16x16 blocks, so the encoder pads to 1088 rows by itself
# and marks the extra eight as not-for-display. Asking the capture for 1088 instead meant resampling
# every row of a 1080 desktop to fit - a small stretch, but softer text on every single frame. The
# PS3 app reads the SPS cropping since v1.0.8 and shows the 1080 the encoder meant.
STREAM_SIZES = ((960, 544), (1280, 720), (1408, 800), (1536, 864), (1792, 1008), (1920, 1080))

# The default is named rather than taken as STREAM_SIZES[0]: 960x544 was added below 720p as a
# high-frame-rate probe, and letting it become everyone's default by position would have been a
# silent downgrade for anyone who never picked a size.
DEFAULT_SIZE = (1280, 720)
WIDTH, HEIGHT = DEFAULT_SIZE
FPS = 60

# Selectable frame rates. What the PS3 shows is 59.94 Hz (the SDK has no 60 Hz mode for 1080p or
# 720p, and an application may not choose it), so none of these divides the display rate exactly and
# a slow drift is unavoidable. What DOES differ is how evenly the pictures land on refreshes:
#
#   60 -> ~1 refresh per picture, even
#   55 -> mostly 1, but every ~11th picture stays up for two: a slight, regular hitch
#   50 -> every 5th picture stays up for two: the same hitch, five times a second
#   30 -> ~2 refreshes per picture, even again
#
# So 30 and 60 are the smooth ones and 50/55 trade smoothness for headroom. They are offered anyway
# because the headroom is real - at 30 fps the console gets 33.3 ms per picture instead of 16.7 -
# and because on a marginal setup a regular hitch can still beat a dropped frame.
#
# 59.94 is the television's own rate and therefore the one that lands exactly. 59.95 sits a hair above
# it and is there to be TRIED, not assumed: the nominal 59.94 is what the SDK promises, and a
# particular console and television need not run at exactly that. If they run a touch fast, 59.95 is
# the closer match; if they do not, it beats against 59.94 about once every hundred seconds. It is a
# measurement, and the session log on the console is what settles it.
FPS_CHOICES = (30, 50, 55, 59.94, 59.95, 60, 90, 120, 145, 240)


def fps_fraction(fps: float) -> tuple[int, int]:
    """The frame rate as the exact fraction ffmpeg and GStreamer want.

    59.94 is not 59.94: the television rate is 60000/1001 = 59.940059940..., and writing "59.94"
    instead would be 5994/100, off by 1e-4 - a frame every ten hours. Free to get right, so it is
    got right.

    Everything else becomes its own exact fraction rather than being rounded to a whole number. This
    used to round, with a 0.01 band around the television rate to catch 59.94 - and 59.95 falls inside
    that band, by 0.0001. Asking for 59.95 would have silently sent 59.94, which is exactly the
    difference the rate exists to measure. A rate now only counts as the television's when it is one."""
    if abs(fps - 60000.0 / 1001.0) < 0.001:
        return (60000, 1001)
    exact = Fraction(fps).limit_denominator(1000)
    return (exact.numerator, exact.denominator)


def max_fps_for_level42(width: int, height: int) -> int:
    """How many pictures a second H.264 level 4.2 allows at this size: 522240 macroblocks per second
    (Annex A), so 64 fps at 1920x1088 and 145 at 1280x720.

    This is the console's real ceiling, not a guideline. cellVdec will not open a stream above level
    4.2 - the same limit that makes 1920x1088 the largest size this project offers - so asking for a
    higher rate at a large size would produce a stream the PS3 refuses outright. The rate is capped
    instead of the level raised.

    Returns 0 when the PICTURE itself is already past 4.2 (more than 8704 macroblocks). No frame rate
    makes such a size decodable, so there is nothing to cap and the caller should leave the
    announcement alone."""
    macroblocks = ((width + 15) // 16) * ((height + 15) // 16)
    if macroblocks > 8704:
        return 0
    return max(1, 522240 // macroblocks)

# 12 Mbit/s rather than the 6 this shipped with: with CAVLC, the deblocking filter off and the HRD timing
# parameters in place, the console no longer cares much about the rate - measured 38-42 ms decode at
# 12 Mbit/s against 40-45 at 35 at 1792x1008 - so the old caution bought nothing and cost sharpness.
KBPS = 12000
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
#
# All three write HRD timing parameters into the bitstream (x264's nal-hrd), and that turned out to matter
# more than the rate control itself. Measured on the real console at 1920x1088, changing nothing else:
#     plain VBR, no HRD                 42-55 ms latency
#     pinned rate, no HRD               44-52 ms      <- same, so uniform frame sizes are NOT the reason
#     VBR + HRD                         29-33 ms
#     CBR (pinned rate + HRD + filler)  <=32 ms
# Without the timing parameters the PS3 evidently buffers a picture before showing it; with them it hands
# it straight on. It costs a handful of bytes in the SPS and nothing else, so every mode carries it.
#   vbr      - the target rate on average, with headroom for the refresh strip. What the console was proven with.
#   quality  - a constant quality instead of a constant rate, capped so the network still cannot be flooded.
#              A still desktop then costs almost nothing AND stays sharp, which is the case VBR handles worst.
#   cbr      - a genuinely constant rate. x264 pads with filler NAL units to hold it, which is bandwidth spent
#              on nothing; the splitter drops them (stream_sender: NAL type 12) so they cost the console only
#              the bytes off the wire.
RATE_CONTROLS = ("vbr", "quality", "cbr")
QUALITY_CRF = 20                 # x264's -crf for the "quality" mode: visually clean without being wasteful

# Slices per picture - a TEST setting, x264 only, and 1 stays the default because it MEASURED best.
#
# The idea was: cellVdec is handed several SPUs (4, in the console app's decode-h264.c), and a decoder that
# splits its work by slice can only use them if the picture HAS several. It had been tried once before, in
# 1.12.1, and dismissed - but that run was worthless three times over: the slices came from x264's
# sliced-threads on a 24-core machine (~24 of them, not 4), it predates the HRD timing that later halved the
# latency, and the splitter it ran through mis-framed multi-slice streams entirely (479 access units for 120
# pictures - see stream_sender.AnnexBSplitter). So it was re-run properly.
#
# Measured on the real console, 1920x1088, x264, 35 Mbit/s, CAVLC, intra refresh, everything else equal:
#     1 slice   ~29-33 ms latency, 22-30 ms decode   <- best, and the default
#     2 slices  minimally worse
#     4 slices  ~40 ms latency, ~35 ms decode        <- clearly worse
# No change in smoothness at any setting, which was never what slices were about.
#
# So decode cost RISES with the slice count while the bit cost does not (+0.13% at 4 slices, measured): each
# slice costs the decoder a fixed setup and returns nothing. cellVdec does not divide its work by slice.
#
# It rules out SLICE-level parallelism but not, on its own, all of it: a decoder can also split a single slice
# by macroblock row (a wavefront), which would use extra SPUs without needing slices and would be invisible
# here. That was tested separately and the answer is no. The console app was rebuilt with VDEC_SPU_COUNT at 5
# and at 6 (6 is everything it reserves, sys_spu_initialize(6, 0)), each under its own title id so all three
# could be compared back to back on the same console: decode time did not move, and neither did the residual
# unsteadiness. cellVdec therefore does not divide its work at all - the four SPUs are pipeline STAGES, not
# workers, and there is no fifth stage. Sony's sample saying "AVC uses 4 SPUs" is a statement of fact.
#
# So 22-30 ms of decode at 1920x1088 is simply what this decoder costs, and no setting on either side moves
# it. The switch below stays because it is the control group if that ever turns out to be wrong.
SLICE_COUNTS = (1, 2, 4)

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

# A second, compressed copy of the same sound, sent only so the PS3 can put it in a recording.
#
# The console cannot make this itself: its SDK ships decoders for AAC, ATRAC and MP3 but the only
# ENCODERS in it are CELP, JPEG and PNG. And the XMB's video player will not take raw PCM in an MP4 -
# measured on the console, a PCM track gives "incompatible file" while AAC and MP3 play. So the
# compression has to happen here, where ffmpeg is already running anyway.
#
# It costs 128 kbit/s next to 12-40 Mbit/s of video, and the PS3 does nothing with these packets but
# copy them into the file: no decode, no re-encode. Playback still uses the uncompressed AF packets,
# which stay exactly as they were - this is additional, not a replacement.
AUDIO_AAC_BITRATE_KBPS = 128
AUDIO_AAC_SAMPLES_PER_FRAME = 1024    # AAC-LC is 1024 samples per frame, always
AUDIO_AAC_MAX_FRAME_BYTES = 1024      # 128 kbit/s at 48 kHz averages ~340 B; this is generous headroom
AUDIO_ADTS_HEADER_BYTES = 7           # stripped before sending: the muxer wants raw AAC

# controller (CP): 20 bytes, PS3 -> server, 60/s
PAD_PACKET_BYTES = 20

# ------------------------------------------------------------------ real USB keyboard and mouse
# "HID " + one report, PS3 -> server, sent while the console is in USB-input mode. It carries the whole
# input state rather than events, exactly as a USB report does: which modifiers are down, which keys are
# down, and how far the mouse moved since the last one. State, not events, so a lost packet costs one
# frame of movement and never leaves a key stuck down.
#
# The key codes are RAW HID usages - positions on the keyboard, not characters. See hid_keys.py for why
# that is the only way the PC's own keyboard layout can decide what gets typed.
HID_MAGIC = b"HID "
HID_MAX_KEYS = 6              # what a boot-protocol keyboard reports; more than anyone presses at once
HID_HEADER_BYTES = 12         # magic(4) modifiers(1) keys(1) buttons(1) wheel(1) dx(2) dy(2)
HID_PACKET_MAX = HID_HEADER_BYTES + 2 * HID_MAX_KEYS

HID_BTN_LEFT, HID_BTN_RIGHT, HID_BTN_MIDDLE = 1 << 0, 1 << 1, 1 << 2


def parse_hid_packet(packet: bytes):
    """(modifiers, keys, buttons, dx, dy, wheel) or None when this is not a usable HID report.

    Everything is bounds-checked: this arrives over UDP from outside, and a short or oversized packet
    must cost nothing more than being ignored.
    """
    if not packet.startswith(HID_MAGIC) or len(packet) < HID_HEADER_BYTES:
        return None
    modifiers = packet[4]
    count = packet[5]
    buttons = packet[6]
    wheel = packet[7] - 256 if packet[7] > 127 else packet[7]        # int8
    dx = int.from_bytes(packet[8:10], "big", signed=True)            # the PS3 is big-endian
    dy = int.from_bytes(packet[10:12], "big", signed=True)
    if count > HID_MAX_KEYS or len(packet) < HID_HEADER_BYTES + 2 * count:
        return None
    keys = tuple(int.from_bytes(packet[HID_HEADER_BYTES + 2 * i:HID_HEADER_BYTES + 2 * i + 2], "big")
                 for i in range(count))
    return modifiers, keys, buttons, dx, dy, wheel


def build_hid_packet(modifiers: int, keys, buttons: int, dx: int, dy: int, wheel: int) -> bytes:
    """The same report the other way round - the tests send it, and it documents the layout."""
    keys = tuple(keys)[:HID_MAX_KEYS]
    return (HID_MAGIC
            + bytes([modifiers & 0xFF, len(keys), buttons & 0xFF, wheel & 0xFF])
            + int(dx).to_bytes(2, "big", signed=True)
            + int(dy).to_bytes(2, "big", signed=True)
            + b"".join(int(code).to_bytes(2, "big") for code in keys))

# liveness: the pad packet doubles as proof the PS3 is still there
CLIENT_TIMEOUT_MS = 3000
STREAM_STARTUP_GRACE_MS = 10000
WATCHDOG_TICK_MS = 500

# giving up on a stream that will not hold. A PS3 that cannot cope with the chosen bitrate drops the
# connection and its app immediately tries again - and every retry switched the desktop mode and back,
# which is what turns a bad setting into a screen that keeps going black. So: a stream that dies by
# itself this often in a row stops the server instead, and the mode is kept across a quick reconnect.
FAULTY_SESSIONS_BEFORE_GIVING_UP = 3
SHORT_SESSION_SECONDS = 20.0        # anything shorter than this never really got going
RECONNECT_KEEP_MODE_SECONDS = 6.0   # a PLAY within this window reuses the mode instead of switching again
# Short sessions only add up while they come in a burst. Measured from a real storm: PLAY, STOP about
# half a second later, the next PLAY two seconds after that, over and over. Somebody who simply starts
# and quits the app on the console a few times in an evening must never trip the limit, so a gap this
# long between two teardowns starts the count over.
STORM_WINDOW_SECONDS = 60.0

# encoder tuning (see upstream/server/LiveStreamer.cs for the measurements behind these)
REFRESH_SWEEP_SECONDS = 1

# How often a KEYFRAME-mode stream sends an IDR. Not the same concept as the sweep above, though the
# two shared one constant until now.
#
# It matters because of what the PS3 does after a loss: stream.c sets waitingForKeyframe and then
# discards every picture until the next IDR, so this interval IS the worst-case freeze. Measured in a
# 27-second console recording at the old 1 second: five gaps of 117/399/447/466/499 ms, each ending
# exactly on an IDR.
#
# Measured cost of halving it (1920x1088, 60 fps, 41 Mbit/s, real game content, x264 ultrafast):
#   1.0 s -> 41085 kbps, largest access unit 390 KB
#   0.5 s -> 41422 kbps (+0.8 %), largest access unit 389 KB
#   0.25 s -> 42473 kbps (+3.4 %), largest access unit 389 KB
# The burst does NOT grow - rate control caps the IDR - so a shorter interval does not cause more of
# the queue overruns it is meant to recover from. 0.5 s is the conservative step: it halves the freeze
# for under one percent, and only doubles the IDR rate rather than quadrupling what the send pacing
# has to carry.
KEYFRAME_INTERVAL_SECONDS = 0.5
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
