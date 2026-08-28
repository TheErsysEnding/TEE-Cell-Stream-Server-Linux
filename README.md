# TEE Cell Stream Server Linux

Stream your Linux desktop to a PlayStation 3 and play PC games with the PS3 controller — Remote Play the
other way round. Linux port of the Windows tool `cell-stream-server` from
[ps3-dev](https://github.com/mohasi/ps3-dev) (Apache-2.0). The PS3 side, the homebrew app **cell-stream**,
is unchanged: this server speaks its wire protocol byte for byte.

*Deutsche Anleitung: [README.de.md](README.de.md)*

![The window](docs/fenster-server.png)

**Measured against a real console** (PS3 over Ethernet, Ryzen 9 5900X + RTX 4070 Ti SUPER, Ubuntu 26.04,
GNOME 50 on Wayland):

| | |
|---|---|
| Frame rate | 60 fps, longest gap 22 ms |
| End-to-end latency | 25–30 ms |
| Decode time on the PS3 | 19–20 ms per picture |
| Dropped pictures | 0 of 893 |

## What you need

- **A PS3 with HEN or CFW**, running `cell-stream.pkg` from the
  [ps3-dev release](https://github.com/mohasi/ps3-dev/releases/tag/174-a5dd795). Without it there is
  nothing to stream to — this package is only the PC half.
- **A Linux desktop with a screen-sharing portal**: GNOME on Wayland is what this was built and measured
  on; X11 works through a fallback. Everything else comes from your distribution's own packages.
- **A GPU that encodes H.264** (NVIDIA NVENC or Intel/AMD VA-API), or a CPU fast enough for x264.

## Install

```
sudo apt install ./tee-cell-stream-server_1.6.0_all.deb
```

Get the `.deb` from [Releases](../../releases). Then **log out and back in once** — GNOME only reads newly
installed shell extensions when a session starts, and the bundled one is what keeps the capture alive
while a game runs fullscreen. After that the server switches it on by itself.

## Use it

Start **TEE Cell Stream Server**, confirm the screen-share prompt once, then start **Cell Stream** on the
PS3. There is nothing else to press: the console finds the server by itself (discovery beacon), connects
and streams. Either side can be started first, and each reconnects on its own.

While streaming, every button goes to the PC, so the PS3 app uses SELECT as its own modifier:

| Combination | What it does |
|---|---|
| SELECT + Cross | input mode: mouse+keyboard ↔ gamepad |
| SELECT + Square | presentation mode: vsync off → vsync → vsync + one-frame buffer |
| SELECT + R3 | show/hide the stats panel |
| SELECT + Triangle/Circle/L1/R1 | custom commands 1–4 |

## Three things Linux needs that Windows does not

All three were measured on the console, not guessed. They are the reason a straight port of the Windows
server runs at about 25 fps here while this one runs at 60.

**CAVLC instead of CABAC.** The PS3 decodes H.264 on its SPUs, where CABAC's serial arithmetic decoding is
the expensive part. With CABAC the console needed 38–40 ms per picture — far past the 16.7 ms a 60 fps
frame gets — so it dropped every other one. CAVLC at 6 Mbit/s brought that to 19–20 ms.

**The desktop's refresh rate decides the frame rate.** GNOME's screen cast hands out only about two thirds
of the refresh rate: 40.1 of 60 fps with the desktop at 1280×720@60, but 60.1 fps at 2560×1440@320 — even
though the second case also has to scale 1440p down to 720p. Since 1280×720 tops out at 60 Hz on most
monitors, the Windows original's trick of matching the desktop to the stream size costs a third of the
frames here. This server instead picks the smallest mode with at least 1.5× the frame rate.

**Fullscreen games freeze the capture.** When Mutter hands a fullscreen window straight to the monitor
(direct scanout) it stops compositing it, so the screen cast has nothing left to copy: the picture freezes
while sound and input carry on ([mutter#3074](https://gitlab.gnome.org/GNOME/mutter/-/work_items/3074),
[#3903](https://gitlab.gnome.org/GNOME/mutter/-/work_items/3903)). The bundled GNOME extension turns direct
scanout off while the server runs and restores it afterwards.

## What it does

Screen capture through xdg-desktop-portal/PipeWire (X11 fallback via `x11grab`), H.264 through NVENC,
VA-API or x264 with the original's low-latency encoder settings (no B-frames, intra refresh, one slice),
uncompressed desktop audio, and the PS3 controller replayed as either a virtual Xbox 360 gamepad
(`/dev/uinput`) or as mouse and keyboard with correct keyboard-layout handling via libxkbcommon.

The window is GTK4/libadwaita with a tray icon, autostart and four user-defined commands the console can
trigger. **Its interface is in German**, as is `README.de.md`; the code and its comments are in English.

![Custom commands](docs/fenster-befehle.png)

## When the PS3 shows fewer than 60 fps

`README.de.md` has the full troubleshooting chapter. The short version: read the console's stats panel
(SELECT + R3) and look at `decode`. Above 16.7 ms the PS3 cannot sustain 60 fps and starts dropping. The
two settings that move it are **entropy coding** (CAVLC is ~43 % cheaper to decode) and **bitrate**.

To measure what actually leaves for the console rather than guessing:

```
sudo tools/wire-fps.py <PS3-IP> -d 30
```

It counts the video frames on the wire and tells you from the gap distribution whether the source is
producing fewer or whether something is dropping them.

## Development

```
PYTHONPATH=src python3 -m teecellstream            # the app
PYTHONPATH=src python3 -m teecellstream --headless # server only, no window
PYTHONPATH=src python3 -m unittest discover -s tests
bash tests/run_integration.sh                      # a fake PS3 against the real server
bash packaging/build-deb.sh                        # → dist/*.deb
```

390 unit tests plus an integration test that impersonates a PS3 client and checks the stream against what
the console expects: fragment layout, clock sync, frame pacing, audio packet rate and the controller
channel. `SPEC.md` documents every module's contract and, where behaviour deviates from the Windows
original, the measurement that justified it.

## Credits and licence

Apache-2.0, like the original. The PS3 application, the Windows server this was ported from and the
application icon are the work of [mohasi](https://github.com/mohasi/ps3-dev); `upstream/` keeps unmodified
reference copies of the sources this was checked against, and `NOTICE` records who did what.
