"""Which H.264 encoders this PC can run, and the ffmpeg command line for each.

Port of VideoEncoders.cs plus the Build*Arguments half of LiveStreamer.cs. The Windows ladder was
Quick Sync, nvenc, amf, x264 with ddagrab capture baked into every command line; on Linux the capture
is a separate concern (capture.py hands us its input arguments) and the hardware rungs are nvenc and
VA-API (Intel and AMD alike), then x264 on the CPU.

Which of them this PC can actually run is found out once, at start-up, by asking each to encode a single
frame - a machine with no NVIDIA card should never be offered nvenc, let alone waste a second failing it.
"""

import subprocess
from dataclasses import dataclass, field

from . import childproc, log, protocol

PROBE_TIMEOUT_S = 15
VAAPI_DEVICE = "/dev/dri/renderD128"

_PROBE_HEAD = ["-hide_banner", "-loglevel", "error"]
_PROBE_SOURCE = ["-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1", "-frames:v", "1"]
_OUTPUT = ["-f", "h264", "-flush_packets", "1", "pipe:1"]

# NVENC's `-delay` is how many frames ffmpeg's wrapper holds back before it hands one out (the default is
# the wrapper's own pipelining depth - two frames here - which buys nothing for a live screen). Measured on
# the target PC (RTX 4070 Ti SUPER, driver 595.84, ffmpeg 8.0.1; 720p60 raw yuv420p over a stdin pipe,
# black frames with a white marker once a second, 10 markers, two runs each): the marker's AU begins to
# leave ffmpeg 34.8 ms after the marker frame was written without it, 2.3 ms with `-delay 0` (the whole
# AU handed on by the splitter 51.4 -> 19.0 ms; the ~16.7 ms left is the splitter waiting for the next
# frame's start code). Output identical either way: 690/690 frames, the same byte count, one IDR, decodes
# clean. Two frames of latency for nothing, so it is on.
# Re-measured after the entropy coder and bitrate became settings (-coder cavlc, 6000 kbit/s, same rig,
# 10 markers, two runs each): 34.6/34.7 ms without, 2.0/2.2 ms with; whole AU 51.3 -> 18.6/18.8 ms.
NVENC_DELAY_ARGS = ["-delay", "0"]


@dataclass
class VideoEncoder:
    kind: str                       # "nvenc" | "vaapi" | "x264" - what settings.json remembers
    name: str                       # what the window shows
    probe_args: list[str] = field(default_factory=list)   # encodes one black frame: if that works, this PC has the hardware
    supports_intra_refresh: bool = True

    def __str__(self) -> str:
        return self.name


# best first: the first one a PC can run is the default. any of them is still selectable.
LADDER: list[VideoEncoder] = [
    VideoEncoder("nvenc", "NVIDIA GPU (NVENC)",
                 _PROBE_HEAD + _PROBE_SOURCE + ["-c:v", "h264_nvenc", "-f", "null", "-"], True),
    # VA-API has no intra refresh in ffmpeg's wrapper, so this rung streams with periodic keyframes and
    # tells the PS3 so (SINFO flag 0). untested here - no Intel/AMD GPU on the development PC.
    VideoEncoder("vaapi", "Intel/AMD GPU (VA-API)",
                 _PROBE_HEAD + ["-vaapi_device", VAAPI_DEVICE] + _PROBE_SOURCE
                 + ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"], False),
    VideoEncoder("x264", "CPU (x264 – weniger fps möglich)",
                 _PROBE_HEAD + _PROBE_SOURCE + ["-c:v", "libx264", "-f", "null", "-"], True),
]


def detect_available(ffmpeg_path: str) -> list[VideoEncoder]:
    available = [encoder for encoder in LADDER if _can_run(ffmpeg_path, encoder)]
    log.write("encoders: keiner funktioniert auf diesem PC" if not available
              else "encoders: " + ", ".join(encoder.name for encoder in available))
    return available


def _can_run(ffmpeg_path: str, encoder: VideoEncoder) -> bool:
    try:
        probe = childproc.popen([ffmpeg_path] + encoder.probe_args,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        log.write("encoders: konnte %s nicht testen: %s" % (encoder.name, error))
        return False
    try:
        # read both pipes to the end: a probe that fills its pipe would otherwise hang
        _out, error_bytes = probe.communicate(timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        probe.kill()
        probe.communicate()
        log.write("encoders: %s antwortet nicht (Timeout nach %d s)" % (encoder.name, PROBE_TIMEOUT_S))
        return False
    if probe.returncode == 0:
        return True

    # hardware the PC lacks is expected to fail here - but so is a too-old driver, and that one a user
    # needs told. log the reason (first real line of ffmpeg's output) so it isn't a silent no.
    error_text = error_bytes.decode("utf-8", "replace")
    log.write("encoders: %s nicht verfügbar%s" % (encoder.name, _describe_probe_failure(error_text)))
    return False


# ffmpeg's boilerplate trailer ("nothing was written", "conversion failed") hides the real cause, which
# comes first (driver too old, no device, codec not built in). return the first meaningful line as
# ": <reason>", skipping the generic trailer; empty if it said nothing useful.
_GENERIC_TRAILERS = ("nothing was written", "conversion failed", "error opening output", "frame=", "[out#")


def _describe_probe_failure(error_text: str) -> str:
    if not error_text:
        return ""
    for raw in error_text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(trailer in lowered for trailer in _GENERIC_TRAILERS):
            continue
        return ": " + line
    return ""


# the chosen encoder is remembered, so the next run starts on the one that worked
def load_choice(available: list[VideoEncoder], settings) -> VideoEncoder | None:
    if not available:
        return None
    try:
        saved = settings.get("encoder", None)
        if saved is not None:
            for encoder in available:
                if encoder.kind == saved:
                    return encoder
    except Exception:   # noqa: BLE001 - a broken settings file must not cost us the encoder
        pass
    return available[0]   # nothing remembered: the best one this PC has


def save_choice(encoder: VideoEncoder, settings) -> None:
    try:
        settings.set("encoder", encoder.kind)
    except Exception as error:   # noqa: BLE001
        log.write("encoders: konnte die Wahl nicht merken: %s" % error)


def intra_refresh_enabled(encoder: VideoEncoder | None, loss_recovery: str) -> bool:
    """What SINFO's last field says: the PS3 decodes through a loss only if the stream repairs itself."""
    return encoder is not None and encoder.supports_intra_refresh and loss_recovery == "intra"


# ------------------------------------------------------------------ the command lines

# INTRA REFRESH: no keyframe bursts - every frame redraws a thin strip instead, sweeping across the
# picture. Measured 59ms -> 39ms end-to-end, and frozen frames 287 -> 0. The sweep length and the rate
# control are the whole story (see upstream/server/LiveStreamer.cs): a FAST sweep under tight CBR is a
# visible blur bar; a 1s sweep under VBR with headroom is ~10x below what the eye picks up, and 1s is
# also how long a lost frame stays visible. The constants live in protocol.py.

def _rate_args(kbps: int) -> list[str]:
    """VBR with headroom (maxrate) so the sweep strip can borrow bits, and a small VBV buffer that keeps
    any single frame - including the anchor IDR - well under the receiver's per-frame limit.

    -refs 1 is a request, not a promise: measured on this rig, x264 honours it but NVENC's driver writes
    max_num_ref_frames = 2 into the SPS anyway (4 without -bf 0). The PS3 reserves what the SPS says
    (stream.c, openDecoderForStream), so 2 costs it one more frame buffer and nothing else."""
    # the VBV window doubles as the biggest a single picture may get, and the PS3 drops one over 1 MB
    bufsize = min(kbps * protocol.REFRESH_BUFFER_MS // 1000, protocol.MAX_VBV_KBIT)
    return ["-b:v", "%dk" % kbps,
            "-maxrate", "%dk" % (kbps * protocol.REFRESH_MAX_RATE_PERCENT // 100),
            "-bufsize", "%dk" % bufsize,
            "-bf", "0", "-refs", "1"]


def _gop_args(interval_seconds: int, fps: int) -> list[str]:
    """-g is the interval between keyframes (periodic-keyframe modes) or the length of a full refresh sweep
    (x264 and nvenc with intra refresh on - see the nvenc note in build_ffmpeg_args)."""
    return ["-g", str(interval_seconds * fps)]


def _scale_filter(width: int, height: int, output_format: str) -> str:
    # out_color_matrix=bt709 matches the PS3's BT.709 colour shader (its default, BT.601, shifts colours);
    # out_range=tv because the shader expects limited range - full-range YUV lands every colour wrong.
    return "scale=%d:%d:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=%s" % (width, height, output_format)



def _coder_args(entropy_coder: str) -> list[str]:
    """CABAC or CAVLC. The PS3 decodes CAVLC far more cheaply - its cellVdec runs on the SPUs, where CABAC's
    serial bit-by-bit arithmetic decoding is the expensive part. Measured with a SIMD-less decoder as a
    stand-in (see protocol.py): -43% decode cost at the same bitrate, for a little less quality per bit.
    On the real console decode was 38-40 ms per frame with CABAC at 11-13 Mbit/s, past the 16.7 ms budget
    of a 60 fps frame, so the console dropped every other one. "auto" leaves the encoder's own default.
    """
    return [] if entropy_coder not in ("cabac", "cavlc") else ["-coder", entropy_coder]


def build_ffmpeg_args(ffmpeg_path: str, encoder: VideoEncoder, capture_input_args: list[str], width: int, height: int,
                      fps: int, kbps: int, loss_recovery: str, capture_needs_scale: bool,
                      entropy_coder: str = "auto") -> list[str]:
    """The whole ffmpeg command line: capture input in, raw Annex-B H.264 out on stdout.

    A raw-pipe capture (Portal/PipeWire, test source) already delivers I420/bt709/limited at the output
    size, so it needs no filter; x11grab delivers the desktop at its own size and colour, so that one is
    scaled and converted here (capture_needs_scale).
    """
    intra = intra_refresh_enabled(encoder, loss_recovery)
    args = [ffmpeg_path, "-hide_banner", "-loglevel", "warning"]

    if encoder.kind == "nvenc":
        args += capture_input_args
        if capture_needs_scale:
            args += ["-vf", _scale_filter(width, height, "yuv420p")]
        # p1/ull/vbr: the fastest preset, the low-latency tuning, and VBR for the sweep's headroom.
        # -pix_fmt yuv420p matches the known-good CPU path's colours.
        args += ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-rc", "vbr", "-pix_fmt", "yuv420p"]
        args += _coder_args(entropy_coder)
        args += NVENC_DELAY_ARGS
        args += _rate_args(kbps)
        if intra:
            # ffmpeg's nvenc wrapper turns -g into the SWEEP once intra refresh is on (nvenc.c: intraRefreshPeriod =
            # gopLength, intraRefreshCnt = gopLength - 1, and the IDR period becomes infinite) - so the Windows
            # server's hour-long -g (meant to push the anchor keyframe out of the session) made an hour-long
            # sweep there too, and a lost frame was never repaired: that is the "nvenc artifacts until a restart"
            # the PS3 README complains about. Measured on the target PC (200 black 720p60 frames): -g 216000 gave
            # one IDR and not a single refreshed strip, -g 60 gave one IDR and a strip in half the frames. There
            # is no periodic keyframe either way, so the sweep length goes here, as it does for x264.
            args += _gop_args(protocol.REFRESH_SWEEP_SECONDS, fps)
            args += ["-intra-refresh", "1", "-single-slice-intra-refresh", "1"]
        else:
            args += _gop_args(protocol.REFRESH_SWEEP_SECONDS, fps)
        args += ["-color_range", "tv", "-colorspace", "bt709", "-forced-idr", "1"]
        args += _OUTPUT
        return args

    if encoder.kind == "vaapi":
        # the frame is uploaded to the GPU (hwupload) as nv12 and encoded there. no intra refresh in
        # this wrapper, so a keyframe every second whatever loss_recovery says (SINFO tells the PS3).
        args += ["-vaapi_device", VAAPI_DEVICE]
        args += capture_input_args
        if capture_needs_scale:
            args += ["-vf", _scale_filter(width, height, "nv12") + ",hwupload"]
        else:
            args += ["-vf", "format=nv12,hwupload"]
        args += ["-c:v", "h264_vaapi"]
        # this wrapper defaults to CABAC, which is exactly what the PS3 cannot decode in time - so the
        # user's choice has to be passed here too, or a fallback from nvenc to VA-API would silently
        # undo it (verified: "ffmpeg -h encoder=h264_vaapi" -> "-coder ... (default cabac)").
        args += _coder_args(entropy_coder)
        args += _rate_args(kbps)
        args += _gop_args(protocol.REFRESH_SWEEP_SECONDS, fps)
        args += _OUTPUT
        return args

    if encoder.kind == "x264":
        # x264 spells intra refresh as an x264 param and uses -g as the sweep length. sliced-threads off
        # and one slice: a sweep only works if each frame strictly follows the last.
        #
        # -threads 1 is what makes this rung usable at all. With sliced threads off, x264 falls back to
        # FRAME threading, and on a many-core machine that buffers about one frame per thread before it
        # hands the first one out: measured on this 24-core PC, 26 frames held back = 433 ms of latency,
        # in a program whose whole point is 25 ms. sliced-threads=1 also fixes the delay but splits each
        # picture into one slice per thread, and multiple slices measured clearly WORSE on the console.
        # One thread costs nothing here: 294 fps at 1792x1008, 400 at 1536x864 - five times real time.
        args += capture_input_args
        if capture_needs_scale:
            args += ["-vf", _scale_filter(width, height, "yuv420p")]
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                 "-threads", "1",   # AFTER the input: before it, ffmpeg would read it as a decoder setting
                 "-x264-params", "sliced-threads=0:slices=1:intra-refresh=%d" % (1 if intra else 0)]
        args += _coder_args(entropy_coder)
        args += _rate_args(kbps)
        args += _gop_args(protocol.REFRESH_SWEEP_SECONDS, fps)
        args += _OUTPUT
        return args

    raise ValueError("unbekannter Encoder: %r" % encoder.kind)
