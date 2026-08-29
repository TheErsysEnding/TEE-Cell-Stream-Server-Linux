"""encoders.py: the probe on this machine, and the exact ffmpeg argument set per encoder and loss recovery.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_encoders -v
Safe on a live desktop: only encodes a few synthetic frames, touches no display or portal.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_SCRATCH = tempfile.mkdtemp(prefix="tee-cst-test-encoders-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_SCRATCH, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_SCRATCH, "server.log"))
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from teecellstream import encoders, log, protocol   # noqa: E402
from teecellstream.settings import Settings   # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FF = FFMPEG or "ffmpeg"

NVENC, VAAPI, X264 = encoders.LADDER
RAW_INPUT = ["-probesize", "32", "-analyzeduration", "0", "-f", "rawvideo", "-pix_fmt", "yuv420p",
             "-video_size", "1280x720", "-framerate", "60", "-i", "pipe:0"]
X11_INPUT = ["-f", "x11grab", "-framerate", "60", "-draw_mouse", "1", "-i", ":0"]
HEAD = [FF, "-hide_banner", "-loglevel", "warning"]
RATE = ["-b:v", "10000k", "-maxrate", "14000k", "-bufsize", "2500k", "-bf", "0", "-refs", "1"]
OUT = ["-f", "h264", "-flush_packets", "1", "pipe:1"]
SCALE = "scale=1280:720:flags=lanczos:out_color_matrix=bt709:out_range=tv"
NVENC_CODEC = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-rc", "vbr", "-pix_fmt", "yuv420p", "-delay", "0"]
NVENC_TAIL = ["-color_range", "tv", "-colorspace", "bt709", "-forced-idr", "1"]
X264_CODEC = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
              "-threads", "1"]   # frame threading would buffer ~one frame per core: 433 ms measured


def _nal_types(stream):
    """NAL unit types in an Annex-B stream, in order (3- and 4-byte start codes alike)."""
    found = []
    position = 0
    while True:
        position = stream.find(b"\x00\x00\x01", position)
        if position < 0 or position + 3 >= len(stream):
            return found
        found.append(stream[position + 3] & 0x1F)
        position += 3


def _build(encoder, loss_recovery, capture_input=RAW_INPUT, needs_scale=False):
    return encoders.build_ffmpeg_args(FF, encoder, capture_input, 1280, 720, 60, 10000, loss_recovery, needs_scale)



# ---------------------------------------------------------------- reading the encoder's own bits back
# The PS3 configures cellVdec from the stream's SPS (upstream/ps3-app/stream.c, openDecoderForStream) and
# decodes with what the PPS says, so the flags we pass are only worth as much as what actually lands in the
# bitstream. These few functions read it: emulation-prevention bytes out, Exp-Golomb in.

def _rbsp(nal_body):
    data, out, zeros = nal_body[1:], bytearray(), 0
    for byte in data:
        if zeros == 2 and byte == 3:      # emulation prevention: 00 00 03 -> 00 00
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data):
        self.data, self.position = data, 0

    def u(self, count):
        value = 0
        for _ in range(count):
            byte = self.data[self.position >> 3]
            value = (value << 1) | ((byte >> (7 - (self.position & 7))) & 1)
            self.position += 1
        return value

    def ue(self):
        leading = 0
        while self.u(1) == 0:
            leading += 1
            if leading > 32:
                raise ValueError("kaputter Exp-Golomb-Code")
        return (1 << leading) - 1 + (self.u(leading) if leading else 0)

    def se(self):
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _parse_sps(nal_body):
    bits = _Bits(_rbsp(nal_body))
    sps = {"profile_idc": bits.u(8), "constraints": bits.u(8), "level_idc": bits.u(8)}
    bits.ue()                                                   # seq_parameter_set_id
    if sps["profile_idc"] in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = bits.ue()
        if chroma == 3:
            bits.u(1)
        bits.ue(); bits.ue(); bits.u(1)
        if bits.u(1):                                           # seq_scaling_matrix_present_flag
            for index in range(8 if chroma != 3 else 12):
                if bits.u(1):
                    last = nxt = 8
                    for _ in range(16 if index < 6 else 64):
                        if nxt:
                            nxt = (last + bits.se() + 256) % 256
                        last = nxt or last
    bits.ue()                                                   # log2_max_frame_num_minus4
    poc_type = bits.ue()
    if poc_type == 0:
        bits.ue()
    elif poc_type == 1:
        bits.u(1); bits.se(); bits.se()
        for _ in range(bits.ue()):
            bits.se()
    sps["max_num_ref_frames"] = bits.ue()
    bits.u(1)                                                   # gaps_in_frame_num_value_allowed_flag
    width_mbs, height_map_units = bits.ue() + 1, bits.ue() + 1
    sps["frame_mbs_only_flag"] = bits.u(1)
    if not sps["frame_mbs_only_flag"]:
        bits.u(1)
    sps["coded_width"] = width_mbs * 16
    sps["coded_height"] = height_map_units * 16 * (2 - sps["frame_mbs_only_flag"])
    bits.u(1)                                                   # direct_8x8_inference_flag
    sps["cropping"] = bool(bits.u(1))
    sps["crop"] = tuple(bits.ue() for _ in range(4)) if sps["cropping"] else (0, 0, 0, 0)
    return sps


def _parse_pps(nal_body):
    bits = _Bits(_rbsp(nal_body))
    bits.ue(); bits.ue()                                        # pic_parameter_set_id, seq_parameter_set_id
    return {"entropy_coding_mode_flag": bits.u(1),
            "bottom_field_pic_order_in_frame_present_flag": bits.u(1),
            "num_slice_groups_minus1": bits.ue()}


def _slice_type(nal_body):
    """0/5 = P, 1/6 = B, 2/7 = I (H.264 7.4.3). The PS3 gets P and I only - a B-frame would need
    reordering the whole pipeline is built to avoid."""
    bits = _Bits(_rbsp(nal_body))
    bits.ue()                                                   # first_mb_in_slice
    return bits.ue()


def _nal_bodies(stream):
    """(nal_type, body) per NAL, body starting at the header byte, trailing start-code zeros trimmed."""
    starts = []
    position = 0
    while True:
        position = stream.find(b"\x00\x00\x01", position)
        if position < 0:
            break
        starts.append(position + 3)
        position += 3
    bodies = []
    for index, start in enumerate(starts):
        end = starts[index + 1] - 3 if index + 1 < len(starts) else len(stream)
        while end > start and stream[end - 1] == 0:
            end -= 1
        bodies.append((stream[start] & 0x1F, stream[start:end]))
    return bodies


def _access_units(stream):
    """Split like LiveAnnexBSplitter does: parameter NALs belong to the picture that follows them."""
    units, current = [], []
    for entry in _nal_bodies(stream):
        if current and any(nal_type in (1, 5) for nal_type, _ in current):
            units.append(current)
            current = []
        current.append(entry)
    if current:
        units.append(current)
    return units

class LadderTests(unittest.TestCase):
    def test_ladder_order_kinds_and_names(self):
        self.assertEqual([e.kind for e in encoders.LADDER], ["nvenc", "vaapi", "x264"])
        self.assertEqual([e.name for e in encoders.LADDER],
                         ["NVIDIA GPU (NVENC)", "Intel/AMD GPU (VA-API)", "CPU (x264 – weniger fps möglich)"])
        self.assertEqual([e.supports_intra_refresh for e in encoders.LADDER], [True, False, True])
        self.assertEqual(str(X264), X264.name)   # what a dropdown shows

    def test_probe_arguments(self):
        source = ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1", "-frames:v", "1"]
        self.assertEqual(NVENC.probe_args, source + ["-c:v", "h264_nvenc", "-f", "null", "-"])
        self.assertEqual(X264.probe_args, source + ["-c:v", "libx264", "-f", "null", "-"])
        self.assertEqual(VAAPI.probe_args,
                         ["-hide_banner", "-loglevel", "error", "-vaapi_device", "/dev/dri/renderD128"] + source[3:]
                         + ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-f", "null", "-"])

    def test_intra_refresh_enabled(self):
        self.assertTrue(encoders.intra_refresh_enabled(NVENC, "intra"))
        self.assertTrue(encoders.intra_refresh_enabled(X264, "intra"))
        self.assertFalse(encoders.intra_refresh_enabled(VAAPI, "intra"))     # VA-API cannot, whatever the setting
        self.assertFalse(encoders.intra_refresh_enabled(NVENC, "keyframe"))
        self.assertFalse(encoders.intra_refresh_enabled(None, "intra"))

    def test_probe_failure_description_skips_generic_trailer(self):
        text = ("\n[h264_vaapi @ 0x1] Failed to initialise VAAPI connection: -1 (unknown libva error).\n"
                "[vost#0:0/h264_vaapi @ 0x2] Error while opening encoder\n"
                "Conversion failed!\n")
        self.assertEqual(encoders._describe_probe_failure(text),
                         ": [h264_vaapi @ 0x1] Failed to initialise VAAPI connection: -1 (unknown libva error).")
        self.assertEqual(encoders._describe_probe_failure("Conversion failed!\n[out#0/null @ 0x] Nothing was written\n"), "")
        self.assertEqual(encoders._describe_probe_failure(""), "")


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(os.path.join(_SCRATCH, "choice-%d.json" % id(self)))

    def test_nothing_remembered_means_best_available(self):
        self.assertIs(encoders.load_choice([NVENC, X264], self.settings), NVENC)
        self.assertIsNone(encoders.load_choice([], self.settings))

    def test_roundtrip_and_unavailable_choice_falls_back(self):
        encoders.save_choice(X264, self.settings)
        self.assertEqual(self.settings.get("encoder"), "x264")
        self.assertIs(encoders.load_choice([NVENC, X264], self.settings), X264)
        # remembered encoder no longer on this PC (card removed): the best one that is
        self.assertIs(encoders.load_choice([NVENC], self.settings), NVENC)
        # survives a reload from disk
        reloaded = Settings(self.settings._path)
        self.assertIs(encoders.load_choice([NVENC, X264], reloaded), X264)


class ArgumentTests(unittest.TestCase):
    """The exact argument lists - what the target PC was verified with."""

    def test_nvenc_intra_refresh(self):
        # -g is the SWEEP length once nvenc's intra refresh is on (see build_ffmpeg_args): one second, not the
        # Windows server's hour, which left a lost frame unrepaired until the next connect
        self.assertEqual(_build(NVENC, "intra"),
                         HEAD + RAW_INPUT + NVENC_CODEC + RATE
                         + ["-g", "60", "-intra-refresh", "1", "-single-slice-intra-refresh", "1"] + NVENC_TAIL + OUT)

    def test_nvenc_entropy_coder(self):
        """-coder lands right after the codec block and before -delay; "auto" adds nothing (the old default)."""
        for coder in ("cavlc", "cabac"):
            args = encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, 6000, "intra", False, coder)
            self.assertIn("-coder", args)
            self.assertEqual(args[args.index("-coder") + 1], coder)
            self.assertLess(args.index("-coder"), args.index("-delay"))
            self.assertGreater(args.index("-coder"), args.index("h264_nvenc"))
        for coder in ("auto", "", "unsinn"):
            self.assertNotIn("-coder", encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, 6000, "intra", False, coder))
        # the default keeps the pre-existing command line byte for byte
        self.assertEqual(encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, 6000, "intra", False),
                         encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, 6000, "intra", False, "auto"))

    def test_x264_entropy_coder(self):
        args = encoders.build_ffmpeg_args(FF, X264, RAW_INPUT, 1280, 720, 60, 6000, "intra", False, "cavlc")
        self.assertEqual(args[args.index("-coder") + 1], "cavlc")

    def test_vaapi_entropy_coder(self):
        """VA-API's wrapper defaults to CABAC ("ffmpeg -h encoder=h264_vaapi": "-coder ... (default cabac)"),
        which is exactly what the PS3 cannot decode in time - so a fallback to this rung must carry the
        user's choice, not silently undo it."""
        for coder in ("cavlc", "cabac"):
            args = encoders.build_ffmpeg_args(FF, VAAPI, RAW_INPUT, 1280, 720, 60, 6000, "intra", False, coder)
            self.assertEqual(args[args.index("-coder") + 1], coder)
            self.assertGreater(args.index("-coder"), args.index("h264_vaapi"))
            self.assertLess(args.index("-coder"), args.index("-b:v"))
        self.assertNotIn("-coder", encoders.build_ffmpeg_args(FF, VAAPI, RAW_INPUT, 1280, 720, 60, 6000, "intra", False))

    def test_the_coder_choice_changes_nothing_else(self):
        """Every rung, every loss mode: asking for an entropy coder inserts "-coder <name>" and touches
        nothing else, so the command line stays the one that was verified against the console."""
        for encoder in encoders.LADDER:
            for loss in ("intra", "keyframe"):
                base = encoders.build_ffmpeg_args(FF, encoder, RAW_INPUT, 1280, 720, 60, 6000, loss, False)
                for coder in ("cavlc", "cabac"):
                    args = encoders.build_ffmpeg_args(FF, encoder, RAW_INPUT, 1280, 720, 60, 6000, loss, False, coder)
                    position = args.index("-coder")
                    self.assertEqual(args[position + 1], coder)
                    self.assertEqual(args[:position] + args[position + 2:], base,
                                     "%s/%s/%s weicht mehr als um -coder ab" % (encoder.kind, loss, coder))

    def test_bitrate_flows_into_the_rate_arguments(self):
        """Every choice the window offers must produce a consistent target/ceiling/buffer trio - with the
        buffer capped, because it also bounds the size of one picture and the PS3 drops one over 1 MB."""
        for kbps in protocol.BITRATE_CHOICES_KBPS:
            args = encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, kbps, "intra", False, "cavlc")
            self.assertEqual(args[args.index("-b:v") + 1], "%dk" % kbps)
            self.assertEqual(args[args.index("-maxrate") + 1], "%dk" % (kbps * 140 // 100))
            self.assertEqual(args[args.index("-bufsize") + 1],
                             "%dk" % min(kbps * 250 // 1000, protocol.MAX_VBV_KBIT))

    def test_nvenc_keyframes(self):
        self.assertEqual(_build(NVENC, "keyframe"),
                         HEAD + RAW_INPUT + NVENC_CODEC + RATE + ["-g", "60"] + NVENC_TAIL + OUT)

    def test_nvenc_x11grab_scales_on_the_cpu(self):
        self.assertEqual(_build(NVENC, "intra", X11_INPUT, True),
                         HEAD + X11_INPUT + ["-vf", SCALE + ",format=yuv420p"] + NVENC_CODEC + RATE
                         + ["-g", "60", "-intra-refresh", "1", "-single-slice-intra-refresh", "1"] + NVENC_TAIL + OUT)

    def test_vaapi_always_keyframes(self):
        expected = (HEAD + ["-vaapi_device", "/dev/dri/renderD128"] + RAW_INPUT + ["-vf", "format=nv12,hwupload"]
                    + ["-c:v", "h264_vaapi"] + RATE + ["-g", "60"] + OUT)
        self.assertEqual(_build(VAAPI, "intra"), expected)
        self.assertEqual(_build(VAAPI, "keyframe"), expected)

    def test_vaapi_x11grab(self):
        self.assertEqual(_build(VAAPI, "intra", X11_INPUT, True),
                         HEAD + ["-vaapi_device", "/dev/dri/renderD128"] + X11_INPUT
                         + ["-vf", SCALE + ",format=nv12,hwupload"] + ["-c:v", "h264_vaapi"] + RATE + ["-g", "60"] + OUT)

    def test_x264_intra_refresh(self):
        self.assertEqual(_build(X264, "intra"),
                         HEAD + RAW_INPUT + X264_CODEC + ["-x264-params", "sliced-threads=0:slices=1:intra-refresh=1:nal-hrd=vbr"]
                         + RATE + ["-g", "60"] + OUT)

    def test_x264_keyframes(self):
        self.assertEqual(_build(X264, "keyframe"),
                         HEAD + RAW_INPUT + X264_CODEC + ["-x264-params", "sliced-threads=0:slices=1:intra-refresh=0:nal-hrd=vbr"]
                         + RATE + ["-g", "60"] + OUT)

    def test_x264_x11grab(self):
        self.assertEqual(_build(X264, "intra", X11_INPUT, True),
                         HEAD + X11_INPUT + ["-vf", SCALE + ",format=yuv420p"] + X264_CODEC
                         + ["-x264-params", "sliced-threads=0:slices=1:intra-refresh=1:nal-hrd=vbr"] + RATE + ["-g", "60"] + OUT)

    def test_rate_scales_with_kbps(self):
        args = encoders.build_ffmpeg_args(FF, X264, RAW_INPUT, 1280, 720, 60, 4000, "intra", False)
        self.assertEqual(args[args.index("-b:v") + 1], "4000k")
        self.assertEqual(args[args.index("-maxrate") + 1], "5600k")
        self.assertEqual(args[args.index("-bufsize") + 1], "1000k")

    def test_no_bitrate_can_make_a_picture_the_ps3_would_drop(self):
        """The console reassembles an access unit into a fixed 1 MB slot and drops a larger one silently
        (FRAME_MAX_BYTES, stream.c). The VBV window is what bounds a single picture, so it must stay under."""
        for kbps in protocol.BITRATE_CHOICES_KBPS:
            args = encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, kbps, "intra", False, "cavlc")
            bufsize_kbit = int(args[args.index("-bufsize") + 1].rstrip("k"))
            self.assertLessEqual(bufsize_kbit * 1000 // 8, protocol.PS3_MAX_AU_BYTES, "%d kbps" % kbps)

    def test_the_cap_leaves_every_measured_bitrate_alone(self):
        """Everything measured on the console was at 12000 or below; those command lines must not move."""
        for kbps in (4000, 6000, 8000, 10000, 12000, 16000, 20000, 24000):
            args = encoders.build_ffmpeg_args(FF, NVENC, RAW_INPUT, 1280, 720, 60, kbps, "intra", False, "cavlc")
            self.assertEqual("%dk" % (kbps * protocol.REFRESH_BUFFER_MS // 1000),
                             args[args.index("-bufsize") + 1], "%d kbps" % kbps)

    def test_x264_encodes_on_one_thread(self):
        """Without it x264 falls back to frame threading and buffers about one frame per core before it
        hands the first one out - measured 26 frames = 433 ms on this 24-core PC. sliced-threads=1 also
        fixes the delay but cuts the picture into one slice per thread, and multiple slices measured
        worse on the console. The option must sit AFTER the input or ffmpeg reads it as a decoder setting."""
        for loss_recovery in ("intra", "keyframe"):
            args = encoders.build_ffmpeg_args(FF, X264, RAW_INPUT, 1280, 720, 60, 10000, loss_recovery, False)
            self.assertEqual("1", args[args.index("-threads") + 1])
            self.assertGreater(args.index("-threads"), args.index("-i"), "sonst gilt es fürs Dekodieren")
            self.assertIn("sliced-threads=0", args[args.index("-x264-params") + 1])
            self.assertIn("slices=1", args[args.index("-x264-params") + 1])

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            _build(encoders.VideoEncoder("amf", "AMD (Windows only)", [], True), "intra")


@unittest.skipUnless(FFMPEG, "ffmpeg fehlt")
class ProbeOnThisMachineTests(unittest.TestCase):
    def test_detect_available(self):
        available = encoders.detect_available(FFMPEG)
        kinds = [encoder.kind for encoder in available]
        recent = log.get_recent()

        self.assertIn("x264", kinds, "libx264 fehlt in ffmpeg?! Log:\n" + recent)
        if os.path.exists("/dev/nvidiactl"):
            self.assertIn("nvenc", kinds, "NVIDIA-Karte da, aber nvenc-Probe scheitert. Log:\n" + recent)
        # the ladder order is kept, whatever subset works
        self.assertEqual(kinds, [encoder.kind for encoder in encoders.LADDER if encoder.kind in kinds])
        self.assertIn("encoders: " + ", ".join(encoder.name for encoder in available), recent)

        # the development PC: NVIDIA only, no VA-API driver -> exactly nvenc + x264, and VA-API's failure
        # is logged with ffmpeg's real reason, not a silent no
        va_drivers = glob.glob("/usr/lib/*/dri/*_drv_video.so") + glob.glob("/usr/lib/dri/*_drv_video.so")
        if os.path.exists("/dev/nvidiactl") and not va_drivers:
            self.assertEqual(kinds, ["nvenc", "x264"])
        if "vaapi" not in kinds:
            lines = [line for line in recent.splitlines() if VAAPI.name + " nicht verfügbar" in line]
            self.assertTrue(lines, "kein Log-Eintrag für die VA-API-Probe:\n" + recent)
            self.assertRegex(lines[-1], r"nicht verfügbar: \S+", "Fehlgrund fehlt: " + lines[-1])

    def test_missing_ffmpeg_is_logged_not_raised(self):
        self.assertEqual(encoders.detect_available("/nonexistent/tee-cst-ffmpeg"), [])
        self.assertIn("encoders: konnte NVIDIA GPU (NVENC) nicht testen", log.get_recent())
        self.assertIn("encoders: keiner funktioniert auf diesem PC", log.get_recent())

    def _run_five_frames(self, encoder):
        # a lavfi source stands in for the capture; -frames:v after the input is an output option, so
        # the argument set is exactly the streaming one, just cut short
        source = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60", "-frames:v", "5"]
        args = encoders.build_ffmpeg_args(FFMPEG, encoder, source, 1280, 720, 60, 10000, "intra", False)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertTrue(result.stdout.startswith(b"\x00\x00\x00\x01"), "kein Annex-B-Startcode")
        # x264 writes the IDR slice behind SPS/PPS/SEI with a 3-byte start code, nvenc with 4 - so count NAL types
        types = _nal_types(result.stdout)
        self.assertEqual(types[:2], [7, 8], "SPS und PPS müssen vorn stehen: %r" % types)
        self.assertEqual(types.count(5), 1, "genau ein IDR erwartet (Frame 1): %r" % types)
        self.assertEqual(types.count(1), 4, "vier P-Frames erwartet (kein B-Frame): %r" % types)
        return result.stdout

    def test_x264_argument_set_really_runs(self):
        self._run_five_frames(X264)

    @unittest.skipUnless(os.path.exists("/dev/nvidiactl"), "keine NVIDIA-Karte")
    def test_nvenc_argument_set_really_runs(self):
        self._run_five_frames(NVENC)

    def _access_unit_sizes(self, stream):
        """Bytes from each picture NAL to the next - a still source makes a plain P-frame ~50 B, a frame
        carrying an intra-refresh strip visibly more."""
        starts = []
        position = 0
        while True:
            position = stream.find(b"\x00\x00\x01", position)
            if position < 0 or position + 3 >= len(stream):
                break
            if stream[position + 3] & 0x1F in (1, 5):
                starts.append(position - 1 if position > 0 and stream[position - 1] == 0 else position)
            position += 3
        return [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]

    def _encode_still_frames(self, encoder, loss_recovery, frames):
        source = ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=60", "-frames:v", str(frames)]
        args = encoders.build_ffmpeg_args(FFMPEG, encoder, source, 1280, 720, 60, 10000, loss_recovery, False)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    @unittest.skipUnless(os.path.exists("/dev/nvidiactl"), "keine NVIDIA-Karte")
    def test_nvenc_intra_refresh_really_sweeps_without_keyframes(self):
        # the point of the intra-refresh rung: no keyframe after the first, and a strip redrawn in most frames
        # so a lost frame heals within the sweep. with the Windows value (-g 216000) this GPU redrew nothing at all.
        stream = self._encode_still_frames(NVENC, "intra", 150)
        self.assertEqual(_nal_types(stream).count(5), 1, "Intra-Refresh darf keine periodischen Keyframes senden")
        sizes = self._access_unit_sizes(stream)[1:]   # skip the IDR
        baseline = min(sizes)
        refreshed = sum(1 for size in sizes if size > baseline + 20)
        self.assertGreaterEqual(refreshed, len(sizes) // 4,
                                "kein Refresh-Streifen zu sehen (%d von %d Frames größer als %d B)" % (refreshed, len(sizes), baseline))

    @unittest.skipUnless(os.path.exists("/dev/nvidiactl"), "keine NVIDIA-Karte")
    def test_nvenc_keyframe_mode_really_sends_periodic_idr(self):
        stream = self._encode_still_frames(NVENC, "keyframe", 150)
        types = [t for t in _nal_types(stream) if t in (1, 5)]
        self.assertEqual([i for i, t in enumerate(types) if t == 5], [0, 60, 120], "IDR jede Sekunde erwartet")

    def test_x264_intra_refresh_really_sweeps_without_keyframes(self):
        stream = self._encode_still_frames(X264, "intra", 150)
        self.assertEqual(_nal_types(stream).count(5), 1)
        sizes = self._access_unit_sizes(stream)[1:]
        baseline = min(sizes)
        self.assertGreaterEqual(sum(1 for size in sizes if size > baseline + 20), len(sizes) // 4)


HAVE_NVENC = os.path.exists("/dev/nvidiactl")


@unittest.skipUnless(FFMPEG, "ffmpeg fehlt")
class BitstreamTests(unittest.TestCase):
    """Every encoder x loss recovery x entropy coder actually run, and the stream read back bit by bit.

    What the PS3 needs (upstream/ps3-app/stream.c): the coded size from the SPS must be 1280x720 - cellVdec
    is built from it and a wrong size decodes to black or locks the console; one slice per picture (the
    reassembler hands whole access units over, and a sweep only converges if each frame follows the last);
    no B-frames; SPS+PPS in the first access unit, because the decoder is created from that keyframe; and
    the entropy coder really being the one that was asked for - CABAC cost the console 38-40 ms a frame.
    """

    def _encode(self, encoder, loss_recovery, coder, frames=90, kbps=6000, source="1280x720", needs_scale=False):
        source_args = ["-f", "lavfi", "-i", "testsrc2=size=%s:rate=60" % source, "-frames:v", str(frames)]
        args = encoders.build_ffmpeg_args(FFMPEG, encoder, source_args, 1280, 720, 60, kbps,
                                          loss_recovery, needs_scale, coder)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(result.returncode, 0,
                         "%s/%s/%s: %s" % (encoder.kind, loss_recovery, coder, result.stderr.decode("utf-8", "replace")))
        return result.stdout

    def _check(self, stream, tag, frames, expect_intra, expect_entropy):
        units = _access_units(stream)
        self.assertEqual(len(units), frames, "%s: %d Access Units für %d Bilder" % (tag, len(units), frames))

        first = [nal_type for nal_type, _ in units[0]]
        self.assertEqual(first[:2], [7, 8], "%s: SPS und PPS müssen vor dem ersten Bild stehen: %r" % (tag, first))
        self.assertIn(5, first, "%s: das erste Access Unit muss das IDR enthalten: %r" % (tag, first))

        sps = _parse_sps([body for nal_type, body in units[0] if nal_type == 7][0])
        pps = _parse_pps([body for nal_type, body in units[0] if nal_type == 8][0])
        self.assertEqual((sps["coded_width"], sps["coded_height"]), (1280, 720),
                         "%s: die PS3 baut cellVdec aus dieser Größe" % tag)
        self.assertEqual(sps["crop"], (0, 0, 0, 0), "%s: kein Cropping erwartet" % tag)
        self.assertEqual(sps["frame_mbs_only_flag"], 1, "%s: nur Vollbilder, keine Halbbilder" % tag)
        # -refs 1 is passed on every rung, but NVENC's driver writes 2 into the SPS whatever we ask
        # (measured: -refs 1 alone gives 4, with -bf 0 it gives 2; x264 honours the 1). The PS3 reserves
        # what the SPS says, so 2 is harmless - more than that would be a real change.
        self.assertLessEqual(sps["max_num_ref_frames"], 2, "%s: zu viele Referenzbilder im SPS" % tag)
        self.assertEqual(pps["num_slice_groups_minus1"], 0, "%s: keine Slice-Gruppen" % tag)
        self.assertEqual(pps["entropy_coding_mode_flag"], expect_entropy,
                         "%s: PPS sagt %s, verlangt war %s" % (tag, "CABAC" if pps["entropy_coding_mode_flag"] else "CAVLC",
                                                               "CABAC" if expect_entropy else "CAVLC"))

        for index, unit in enumerate(units):
            pictures = [body for nal_type, body in unit if nal_type in (1, 5)]
            self.assertEqual(len(pictures), 1, "%s: Bild %d hat %d Slices, erwartet 1" % (tag, index, len(pictures)))
            self.assertNotIn(_slice_type(pictures[0]) % 5, (1,), "%s: Bild %d ist ein B-Frame" % (tag, index))

        keyframes = [index for index, unit in enumerate(units) if any(nal_type == 5 for nal_type, _ in unit)]
        if expect_intra:
            self.assertEqual(keyframes, [0], "%s: Intra-Refresh sendet nur das eine Anker-IDR: %r" % (tag, keyframes))
        else:
            self.assertEqual(keyframes, [0, 60], "%s: Keyframe-Modus: IDR jede Sekunde: %r" % (tag, keyframes))
        return sps, pps

    def _run_matrix(self, encoder, default_entropy):
        for loss_recovery in ("intra", "keyframe"):
            for coder, expected in (("auto", default_entropy), ("cavlc", 0), ("cabac", 1)):
                tag = "%s/%s/%s" % (encoder.kind, loss_recovery, coder)
                with self.subTest(tag):
                    expect_intra = encoders.intra_refresh_enabled(encoder, loss_recovery)
                    stream = self._encode(encoder, loss_recovery, coder)
                    self._check(stream, tag, 90, expect_intra, expected)

    @unittest.skipUnless(HAVE_NVENC, "keine NVIDIA-Karte")
    def test_nvenc_every_combination(self):
        self._run_matrix(NVENC, default_entropy=1)     # nvenc's own default is CABAC

    def test_x264_every_combination(self):
        self._run_matrix(X264, default_entropy=0)      # -preset ultrafast turns CABAC off by itself

    def test_the_scaling_path_still_codes_1280x720(self):
        """x11grab hands the desktop over at its own size; the scale filter must land exactly on the size
        the PS3 builds its decoder from."""
        for encoder in ([NVENC] if HAVE_NVENC else []) + [X264]:
            with self.subTest(encoder.kind):
                stream = self._encode(encoder, "intra", "cavlc", frames=12, source="2560x1440", needs_scale=True)
                sps = _parse_sps([body for nal_type, body in _nal_bodies(stream) if nal_type == 7][0])
                self.assertEqual((sps["coded_width"], sps["coded_height"]), (1280, 720))

    def test_every_bitrate_the_window_offers_encodes(self):
        for kbps in protocol.BITRATE_CHOICES_KBPS:
            with self.subTest(kbps=kbps):
                stream = self._encode(NVENC if HAVE_NVENC else X264, "intra", "cavlc", frames=60, kbps=kbps)
                self.assertGreater(len(stream), 1000)

    def test_ffmpegs_own_trace_agrees_about_the_entropy_coder(self):
        """Cross-check of the bit reader above against ffmpeg's trace_headers, so a bug in our parser
        cannot make a CABAC stream look like CAVLC."""
        encoder = NVENC if HAVE_NVENC else X264
        for coder, expected in (("cavlc", 0), ("cabac", 1)):
            with self.subTest(coder):
                path = os.path.join(_SCRATCH, "trace-%s-%s.h264" % (encoder.kind, coder))
                with open(path, "wb") as handle:
                    handle.write(self._encode(encoder, "intra", coder, frames=6))
                trace = subprocess.run([FFMPEG, "-hide_banner", "-v", "trace", "-i", path, "-c", "copy",
                                        "-bsf:v", "trace_headers", "-f", "null", "-"],
                                       capture_output=True, timeout=60).stderr.decode("utf-8", "replace")
                flags = [line.split("=")[-1].strip() for line in trace.splitlines() if "entropy_coding_mode_flag" in line]
                self.assertTrue(flags, "trace_headers sagte nichts über den Entropie-Coder")
                self.assertEqual(set(flags), {str(expected)}, "trace_headers: %r" % flags[:4])
                # and the bit reader read the same thing out of the same file
                with open(path, "rb") as handle:
                    stream = handle.read()
                pps = _parse_pps([body for nal_type, body in _nal_bodies(stream) if nal_type == 8][0])
                self.assertEqual(pps["entropy_coding_mode_flag"], expected)


class RateControl(unittest.TestCase):
    """Three ways to spend the bitrate. VBR is what the console was proven with and stays the default;
    the other two exist because a still desktop is exactly the case VBR handles worst."""

    def _x264(self, rate_control):
        return encoders.build_ffmpeg_args("ffmpeg", X264, ["-i", "in"], 1920, 1088, 60, 12000,
                                          "intra", False, "cavlc", rate_control)

    def test_vbr_is_the_default_and_unchanged(self):
        self.assertEqual(self._x264("vbr"), self._x264("nonsense"))
        args = self._x264("vbr")
        self.assertIn("-b:v", args)
        self.assertNotIn("-minrate", args)
        self.assertNotIn("-crf", args)

    def test_cbr_pins_all_three_rates_to_the_same_number(self):
        args = self._x264("cbr")
        for flag in ("-b:v", "-maxrate", "-minrate"):
            self.assertEqual("12000k", args[args.index(flag) + 1], flag)

    def test_cbr_asks_x264_to_actually_hold_the_rate(self):
        # without nal-hrd=cbr, x264 simply undershoots on easy frames instead of padding
        params = self._x264("cbr")[self._x264("cbr").index("-x264-params") + 1]
        self.assertIn("nal-hrd=cbr", params)

    def test_every_mode_writes_hrd_timing_because_the_console_needs_it(self):
        """The single biggest latency win after the encoder choice: 42-55 ms without these parameters on
        the real console, 29-33 ms with them. Proven by elimination - a pinned rate WITHOUT them stayed at
        44-52 ms, so it is the timing information and not the uniform frame sizes."""
        for mode in protocol.RATE_CONTROLS:
            args = self._x264(mode)
            self.assertIn("nal-hrd=", args[args.index("-x264-params") + 1], mode)
        for mode in ("vbr", "quality"):
            args = self._x264(mode)
            self.assertIn("nal-hrd=vbr", args[args.index("-x264-params") + 1], mode)

    def test_quality_targets_quality_and_keeps_the_ceiling(self):
        args = self._x264("quality")
        self.assertEqual(str(protocol.QUALITY_CRF), args[args.index("-crf") + 1])
        self.assertNotIn("-b:v", args)          # a target rate would fight the quality target
        self.assertEqual("16800k", args[args.index("-maxrate") + 1])

    def test_every_mode_keeps_the_vbv_under_the_consoles_per_picture_limit(self):
        for mode in protocol.RATE_CONTROLS:
            args = self._x264(mode)
            bufsize_kbit = int(args[args.index("-bufsize") + 1].rstrip("k"))
            self.assertLessEqual(bufsize_kbit * 1000 // 8, protocol.PS3_MAX_AU_BYTES, mode)

    def test_nvenc_follows_for_cbr_and_falls_back_for_quality(self):
        for mode, expected in (("vbr", "vbr"), ("cbr", "cbr"), ("quality", "vbr")):
            args = encoders.build_ffmpeg_args("ffmpeg", NVENC, ["-i", "in"], 1920, 1088, 60, 12000,
                                              "intra", False, "cavlc", mode)
            self.assertEqual(expected, args[args.index("-rc") + 1], mode)


if __name__ == "__main__":
    unittest.main()
