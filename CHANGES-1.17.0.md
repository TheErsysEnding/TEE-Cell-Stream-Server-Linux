# What changed between 1.12.1 and 1.17.0

Everything below was measured on a real PS3 Slim over a direct Ethernet cable, reading the console's own
stats panel while playing. Unless stated otherwise: **x264, CAVLC, intra refresh, 1920 × 1088.**

## The short version

Full HD latency went from **42–55 ms to 29 ms**, and the reason turned out to be eleven bytes.

| | latency at 1920 × 1088 |
|---|---|
| 1.12.1 (VBR, no HRD) | 42–55 ms |
| 1.17.0 (constant quality, HRD) | **29 ms** |

For context, where this started: the same picture cost **147 ms of decode and 224 ms of latency** with
NVENC before the encoder question was settled.

---

## 1.13.0 — sizes above Full HD, and why they are gone again

2048 × 1152, 2560 × 1440 and 3840 × 2160 were added to find the console's ceiling, and they found it:
**none of them connected at all** — no picture, no error, the PS3 simply refused.

That is exactly where H.264 level 4.2 stops, at 8704 macroblocks per picture. 1920 × 1088 needs 8160 and
fits; 2048 × 1152 needs 9216 and does not. `cellVdec` will not go past 4.2, so the sizes were removed in
1.14.0 and the finding written into `protocol.py` so nobody tries again.

Also settled on the PC side: at 3840 × 2160, x264 on the single thread that low latency requires manages
only **49 fps** — below the 60 the stream needs. 4K would have been a PC problem before it was ever a
console problem.

The H.264 level announced in SINFO now grows with the picture size instead of being pinned at 4.2. With
Full HD as the ceiling it always resolves to 4.2 again, but it can no longer silently under-announce.

## 1.14.0 — the rate control became a choice

Three ways to spend the bitrate, x264 only:

- **variable** — the target rate on average, with 40 % headroom for the intra-refresh strip
- **constant quality** — a constant quality (CRF 20) instead of a constant rate, capped by the same ceiling
- **constant bitrate** — holds the rate exactly, padding with filler NAL units when there is nothing to send

The sender drops the filler again (`stream_sender`, NAL type 12), so CBR's padding never reaches the
console — which is why the PS3's bitrate readout still varies under CBR. That is correct behaviour, not a
bug: the console receives only real picture data.

## 1.15.0 — two experiments that answered their question and were removed

Constant bitrate had halved the latency, and there were two candidate explanations. Two temporary modes
separated them:

| mode | what it isolated | measured |
|---|---|---|
| pinned rate, **no** HRD | are uniform frame sizes the reason? | 44–52 ms — no |
| VBR **with** HRD | are the timing parameters the reason? | 29–33 ms — yes |

The middle row is the whole proof. Pinning the rate without the timing information changed nothing, so it
is not the shape of the frames the console likes.

## 1.16.0 — HRD timing parameters everywhere

H.264 can carry HRD parameters: timing information telling a decoder when it may hand a picture on.
Without them the PS3 evidently buffers one first; with them it passes it straight through.

| stream | latency |
|---|---|
| plain VBR, no HRD | 42–55 ms |
| pinned constant rate, still no HRD | 44–52 ms |
| **VBR with HRD** | **29–33 ms** |
| CBR (pinned rate, HRD, filler) | ≤ 32 ms |

`x264`'s `nal-hrd` writes them. The cost: the SPS grows from **26 to 37 bytes**, once per stream. All
three rate controls now carry them.

## 1.17.0 — the defaults follow the measurements

| setting | was | is | why |
|---|---|---|---|
| rate control | variable | **constant quality** | measured the lowest latency of the three |
| bitrate | 6 Mbit/s | **12 Mbit/s** | the console stopped caring about the rate |
| error correction | intra refresh | unchanged | every measurement above was taken with it on |

The three rate controls at 1920 × 1088 and 35 Mbit/s, all else equal:

| rate control | latency |
|---|---|
| variable | up to 32 ms |
| constant bitrate | up to 31 ms |
| **constant quality** | **up to 29 ms** — and noticeably so, not only on the counter |

The bitrate default moved because the old caution no longer buys anything. With CAVLC, the deblocking
filter off and the timing parameters in place, 12 Mbit/s and 35 Mbit/s differ by 2 ms of decode. Six was
protecting against a problem that three other fixes had already removed.

---

## What is still open

**Four SPUs of six.** `decode-h264.c` initialises 6 and hands `cellVdec` 4, as a compile-time constant
taken from a Sony SDK sample. Disabling the 8th SPE on a test console changed nothing, which confirms it:
the number is fixed in the application, so no amount of hardware changes it. Only a rebuilt `.pkg` would.

**Why x264 loses to NVENC at 720p.** At 1920 × 1088 x264 is dramatically cheaper to decode (147 ms → 38–44).
At 1280 × 720 it measured 38–42 ms against NVENC's 19–22 — the opposite, at a *lower* bitrate. The
practical rule holds (NVENC at 720p, x264 above it), but the explanation does not, and the deblocking
argument alone clearly does not cover it.
