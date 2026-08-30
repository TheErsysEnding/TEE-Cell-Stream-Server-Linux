# 1.20.0 — several slices per picture become testable, and the old measurement is void

## Why this exists

Version 1.12.1 recorded that multiple slices per picture "measured clearly WORSE on the console" and
pinned the encoder to `slices=1`. That result does not survive inspection. Three separate reasons:

1. **It was never four slices.** The test ran through x264's `sliced-threads=1`, which ties the slice
   count to the thread count. On a 24-core machine that is roughly 24 slices per picture, not 4.
2. **It predates the HRD timing parameters.** 1.12.1 came before 1.16.0, when every configuration sat
   at 40–55 ms and differences of a few milliseconds disappeared in the noise.
3. **The splitter could not carry a multi-slice stream at all.** This is the real one, see below.

## The splitter bug

`AnnexBSplitter` closed an access unit at the first NAL that followed a picture NAL. That rule is exactly
right while a picture is one slice, and wrong as soon as it is not: every slice after the first looked
like the beginning of the next picture.

Measured against a real x264 stream, 1920x1088, 120 pictures, `slices=4`:

```
old rule:  479 access units for 120 pictures
```

Four times too many. Each slice went on the wire as its own frame with its own frame id, so the console
was handed quarter-pictures as if they were whole ones. Whatever 1.12.1 measured, it was not the console's
opinion of slices.

## The fix

H.264 numbers every slice by the macroblock it begins at, in `first_mb_in_slice` — the very first syntax
element of the slice header, exp-Golomb coded. Exp-Golomb writes 0 as the single bit `1`, so "does this
slice open a new picture" is **one bit test** on the byte behind the NAL header. No parsing, no state.

The splitter now reads that bit. Single-slice streams behave exactly as before (the 16.2 MB real-NVENC
regression stream still yields its 600 units, at 844 MB/s), and multi-slice streams come out as one unit
per picture.

The literal port of the Windows original that the property test compares against carries the same rule,
with a comment marking it as the one deliberate divergence — the original is only defined for one slice
per picture, so comparing on multi-slice data compares against something with no correct answer.

## New: "Slices je Bild (Versuch)"

A test setting, 1 (default) / 2 / 4, x264 only. It does **not** use `sliced-threads` — that would tie the
slice count to the thread count and bring back the frame-buffering latency `-threads 1` exists to avoid.
`slices=N` with one thread divides the picture and leaves the threading alone.

Nothing else changed: same encoder, same rate control, same entropy coder, same defaults.

## New: what actually went out

Every stream now ends with a line reporting the wire truth, because "asked for 4 slices" and "emitted 4
slices" are different statements:

```
sender: 25182 Bilder gesendet, 4 Slices (25180x), 1 Slices (2x), Ø 61.3 KB je Bild = 48.1 Fragmente
```

## Measured cost of slices

1920x1088, x264 ultrafast, CAVLC, intra refresh, 120 pictures:

| slices | constant quality (CRF 20) | vs. 1 slice | constant bitrate |
|---|---|---|---|
| 1 | 7 489 399 bytes | — | 8 959 310 |
| 2 | 7 493 958 bytes | +0.06 % | 8 957 595 |
| 4 | 7 498 956 bytes | **+0.13 %** | 8 958 346 |

Slices cost bits in principle — nothing may be predicted across a slice boundary and each one carries its
own header — but at this picture size the penalty is 0.13 %. Under constant bitrate it is zero by
definition, the rate being pinned.

## What the experiment is actually for

`cellVdec` is handed a number of SPUs by the application (4 in the console app's `decode-h264.c`, taken
from Sony's `pamf_dmux` sample). Whether it can use them depends on how Sony's precompiled SPU module
partitions the work — which nobody outside Sony can read, because neither the console app nor any other
open PS3 H.264 client contains a line of SPU code.

But it can be measured. If the decoder splits by slice, four slices should shorten decode time on a
four-SPU decoder. If nothing changes, it does not split by slice — and then handing it a fifth SPU would
be pointless too. Either outcome answers a question that would otherwise need a firmware disassembly.

## Tests

434 tests green. New: multi-slice framing across three chunkings (whole, split, byte-by-byte), the slice
report, and single-slice pictures still reporting as one.

---

# Result (measured on the console, same day)

1920x1088, x264, 35 Mbit/s, CAVLC, intra refresh, everything else identical between runs:

| slices | latency | decode | smoothness |
|---|---|---|---|
| **1** | **~29–33 ms** | **22–30 ms** | unchanged |
| 2 | minimally worse | | unchanged |
| 4 | ~40 ms | ~35 ms | unchanged |

**More slices are worse, monotonically.** The default stays 1.

The bit cost cannot explain it — 4 slices cost 0.13 % more bits at constant quality and nothing at all at
constant bitrate, while decode time rose by roughly a quarter. So the extra cost is per-slice setup inside
the decoder, paid four times over and returning nothing: **cellVdec does not divide its work by slice.**

## What that settles, and what it does not

It settles the slice question properly, for the first time — the earlier verdict happened to point the same
way, but it was measured through a splitter that was quartering the pictures, so it was not evidence of
anything.

It does **not** settle the SPU question. A decoder can also split a single slice by macroblock row (a
wavefront), and that form of parallelism would use extra SPUs without ever needing more than one slice —
completely invisible to this test. Whether `VDEC_SPU_COUNT 4` → 5 helps therefore still requires building
the console app with that number changed.

The switch stays in place for exactly that comparison: if the SPU count is ever raised, the slice runs are
the control group.
