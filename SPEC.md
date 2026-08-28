# TEE Cell Stream Server Linux — Spezifikation / Modulverträge

Linux-Port des Windows-Tools `cell-stream-server` (ps3-dev, Apache-2.0, Release 174-a5dd795).
Die Original-Quellen liegen unter `upstream/server/*.cs` (Server) und `upstream/ps3-app/*` (PS3-Seite,
Protokoll-Referenz). **Die PS3-App bleibt unverändert; dieser Server spricht ihr Protokoll byte-genau.**

Ziel: Funktionen **originalgetreu** portieren. Nur dort abweichen, wo Windows-Mechanismen auf Linux
kein Gegenstück haben — dann den Linux-nativen Weg nehmen (Portal/PipeWire statt ddagrab, uinput statt
ViGEmBus/SendInput, Mutter-DBus statt ChangeDisplaySettings, JSON statt Registry, XDG-Autostart statt
Run-Key, StatusNotifierItem statt WinForms-Tray).

## Laufzeit-Umgebung (Referenzsystem, auf dem gemessen wurde — Code generisch halten)

- Ubuntu 26.04, GNOME 50 **Wayland** (X11 optional als Fallback), Python 3.14, PyGObject 3.56, GTK 4.22, libadwaita 1.9
- NVIDIA RTX 4070 Ti SUPER, Treiber 595.x; `ffmpeg` 8.0.1 (Ubuntu-Paket) mit `h264_nvenc`, `h264_vaapi`, `libx264`, `x11grab`, `pulse`
- GStreamer 1.28 mit `pipewiresrc`, `videoconvertscale`, `gst-launch-1.0`
- xdg-desktop-portal 1.21 (ScreenCast v5), PipeWire/WirePlumber, `/dev/uinput` mit uaccess-ACL
- `python3-evdev` 1.9.3 (Ubuntu-Paket), `libxkbcommon.so.0`
- `ip` (iproute2), `xdg-open`, `xrandr` (nur X11-Fallback)
- Referenzmonitor: 2560×1440@320 Hz, hat einen Modus `1280x720@60.000` (die hohe Bildwiederholrate ist für die Aufnahme entscheidend, siehe display_mode.py)

## Paket-Layout

```
src/teecellstream/
  __init__.py          Version, APP_ID = "de.tee.CellStreamServer", APP_NAME = "TEE Cell Stream Server"
  __main__.py          python3 -m teecellstream [--minimized]  → app.main(argv)
  protocol.py          ALLE Konstanten + PadBits (fertig, nicht ändern)
  clock.py             now_us() (fertig)
  log.py               write(), get_recent(), LOG_PATH (fertig)
  settings.py          Settings-Singleton, JSON (fertig)
  server.py            Server-Kern, Port von Server.cs (fertig — Verträge unten sind daraus abgeleitet)
  stream_sender.py     send_access_unit(), AnnexBSplitter
  encoders.py          Encoder-Leiter, Probe, ffmpeg-Argumente
  capture.py           Bildschirmaufnahme-Backends (Portal/PipeWire, X11, Testquelle) + Frame-Takt
  portal.py            xdg-desktop-portal ScreenCast-Session (Gio.DBus)
  live_streamer.py     LiveStreamer (Capture + ffmpeg + Splitter + Sender)
  audio.py             AudioCapture (ffmpeg -f pulse) + AudioStreamer
  pad_receiver.py      PadReceiver
  virtual_gamepad.py   VirtualGamepad (evdev UInput, Xbox 360)
  desktop_input.py     DesktopInput (uinput Maus+Tastatur, Layout via libxkbcommon)
  display_mode.py      DisplayMode (Mutter DisplayConfig DBus; xrandr-Fallback)
  power.py             keep_display_awake(bool) (SessionManager/ScreenSaver-Inhibit)
  custom_commands.py   4 Slots, xdg-open / sh -c
  netinfo.py           get_beacon_targets()
  childproc.py         popen() mit PR_SET_PDEATHSIG + Registry aller Kinder, kill_all()
  app.py               Adw.Application (Single-Instance), --minimized, Notifications
  ui.py                Hauptfenster (GTK4/libadwaita)
  tray.py              StatusNotifierItem (DBus) — best effort
  autostart.py         ~/.config/autostart/tee-cell-stream-server.desktop
data/
  tee-cell-stream-server.desktop, icons/ (App-Icon + Tray-Icons idle/live), 70-tee-cell-stream-uinput.rules
tests/
  test_*.py (unittest, ohne pytest-Abhängigkeit), fake_ps3.py (Integrations-Client)
packaging/
  build-deb.sh, control, postinst, prerm, postrm → tee-cell-stream-server_<ver>_all.deb
```

Alle Module: **nur Standardbibliothek + gi (GLib/Gio/Gtk/Adw) + evdev**. Kein pip. Kein pytest.
Logging **ausschließlich** über `log.write(...)` (deutsch, kurz, Präfix wie Original: `live:`, `audio:`,
`pad:`, `display:`, `encoders:`, `custom N:`, `beacon`, `portal:`, `capture:`).
Threads: `threading.Thread(daemon=True, name=...)`. Alles muss auch ohne GUI (headless, nur `Server`) laufen.

## Protokoll (aus protocol.py — verbindlich)

| Konstante | Wert |
|---|---|
| SERVER_PORT / BEACON_PORT | 38310 / 38311 (UDP; ein Socket, gebunden an 0.0.0.0:38310, SO_BROADCAST, SO_SNDBUF 1 MiB) |
| Beacon | ASCII `CELLSTREAM 1` jede Sekunde an 255.255.255.255:38311 **und** jede Interface-Broadcast-Adresse; Ziele alle 30 s neu ermitteln |
| WIDTH×HEIGHT, FPS, KBPS, SEND_RATE_KBPS | 1280×720, 60, 10000, 30000 (KBPS/SEND_RATE_KBPS sind nur noch der Rückfallwert; geliefert wird `video_kbps` = 6000 aus den Einstellungen, die Paketrate behält das Verhältnis 3:1) |
| BITRATE_CHOICES_KBPS / ENTROPY_CODERS | (4000, 6000, 8000, 10000, 12000) / ("cavlc", "cabac") — die zwei Regler, die die Decodelast der PS3 bestimmen (gemessen: CABAC 38–40 ms/Bild auf der Konsole, CAVLC @6 Mbit/s 19–20 ms) |
| SINFO | `SINFO 1280 720 42 1 60 <intraRefresh 0/1>` — 3× gesendet, als Antwort auf jedes PLAY |
| VF-Fragment | 20-Byte-Header big-endian: `[0]='V' [1]='F' [2..5]=frameId u32 [6..7]=fragIndex u16 [8..9]=fragCount u16 [10]=flags(bit0 keyframe) [11]=version(2) [12..19]=encoderExitUs u64`, Payload ≤ 1300 B; jedes Fragment außer dem letzten exakt 1300 B |
| Pacing | Fragmente mit SEND_RATE_KBPS ausgeben: `per_fragment_us = (20+1300)*8*1000 / send_rate_kbps` (=352 µs bei 30000) |
| AINFO / AF | `AINFO <rate> 2` (beim Start und 1×/s); AF 16-Byte-Header: `[0]='A' [1]='F' [2..5]=packetId u32 [6..7]=frameCount u16 [8..15]=captureUs u64`, Payload frameCount × (L,R) **s16 big-endian**; 5 ms Chunks, max 512 Frames/Paket |
| CP (PS3→PC) | 20 B: `[0]='C' [1]='P' [2..5]=packetId [6..7]=buttons u16 [8]=leftX [9]=leftY [10]=rightX [11]=rightY (int8, y positiv = unten) [12..19]=sendUs (bereits in Server-Uhr)` — 60×/s, gilt als Lebenszeichen |
| TIME | PS3 sendet `TIME`, Server antwortet `TIME <now_us>` (µs seit 2020-01-01 UTC) |
| PLAY / STOP | PLAY wird wiederholt bis SINFO ankommt → Wiederholungen ignorieren, nicht neu starten |
| PADMODE | `PADMODE gamepad` / `PADMODE mouse` (1×/s) |
| KEY | `KEY <byte>` — genau 1 Zeichen nach `KEY ` (`\b` Backspace, `\t` Tab, `\n` Return, sonst ASCII) |
| CUSTOM | `CUSTOM <1-4>` |
| Timeouts | CLIENT_TIMEOUT_MS 3000 (nach erstem CP), STREAM_STARTUP_GRACE_MS 10000 (vor erstem CP), WATCHDOG_TICK_MS 500 |

PadBits (Bit-Positionen in `buttons`): UP=0 DOWN=1 LEFT=2 RIGHT=3 CROSS=4 CIRCLE=5 SQUARE=6 TRIANGLE=7 L1=8 R1=9 L2=10 R2=11 START=12 SELECT=13 L3=14 R3=15.

## Modulverträge

### clock.py (fertig)
`now_us() -> int` — Mikrosekunden seit 2020-01-01 UTC, verankert beim Import an der Wanduhr, tickt mit `time.monotonic_ns()`.

### log.py (fertig)
`write(msg)`, `get_recent() -> str` (letzte 200 Zeilen), `LOG_PATH` (`~/.local/state/tee-cell-stream-server/server.log`, 2 MiB-Rotation).

### settings.py (fertig)
`settings.get(key, default)`, `settings.set(key, value)` (sofort gespeichert, thread-safe). Schlüssel:
`encoder` (str kind), `loss_recovery` ("intra"|"keyframe", default "intra"), `video_kbps` (int aus `protocol.BITRATE_CHOICES_KBPS`, default 6000),
`entropy_coder` ("cavlc"|"cabac", default "cavlc"), `switch_display_mode` (bool, default True),
`swap_mouse_sticks` (bool), `custom_commands` (Liste von 4 `{"kind": "none"|"run", "value": str, "label": str}`),
`screencast_restore_token` (str|None), `hide_notice_shown` (bool).
Datei: `~/.config/tee-cell-stream-server/settings.json`.

### stream_sender.py
```python
FRAGMENT_PAYLOAD_BYTES = 1300; FRAGMENT_HEADER_BYTES = 20; PROTOCOL_VERSION = 2   # aus protocol importieren
def send_access_unit(sock, target: tuple[str,int], frame_id: int, data: bytes|memoryview, keyframe: bool,
                     capture_us: int, send_rate_kbps: int) -> None
```
- Header exakt wie oben; Pacing: Fragment i ist fällig bei `start + i*per_fragment_us`; warten mit
  `time.sleep()` bis ~150 µs vor der Fälligkeit, dann kurz spinnen (Python-GIL: nie länger spinnen).
- `frame_id` als u32 maskieren.

```python
class AnnexBSplitter:
    def push(self, data: bytes) -> None
    def take_access_unit(self) -> tuple[bytes, bool] | None   # (AU-Bytes, keyframe)
```
- Port von `LiveAnnexBSplitter` (upstream/server/LiveStreamer.cs): Startcodes `00 00 01` (mit optional
  vorangestelltem `00`), NAL-Typ = `byte & 0x1F`; Bild-NALs 1 und 5; SPS/PPS/SEI hängen am **folgenden**
  Bild; eine AU endet, wo die erste NAL der nächsten beginnt; `keyframe = irgendeine NAL vom Typ 5`.
- **Performance:** Startcodes mit `bytes.find(b"\x00\x00\x01", pos)` suchen (C-Geschwindigkeit),
  niemals byteweise in Python iterieren. Puffer als `bytearray` mit Verbrauch per `del buf[:n]`.

### encoders.py
```python
@dataclass
class VideoEncoder: kind: str; name: str; probe_args: list[str]; supports_intra_refresh: bool
LADDER: list[VideoEncoder]   # Reihenfolge: nvenc, vaapi, x264
def detect_available(ffmpeg_path: str) -> list[VideoEncoder]     # 1 schwarzes Frame kodieren, Timeout 15 s, Fehlgrund loggen
def load_choice(available, settings) -> VideoEncoder | None
def save_choice(encoder, settings) -> None
def intra_refresh_enabled(encoder, loss_recovery: str) -> bool   # nur wenn encoder.supports_intra_refresh and loss_recovery == "intra"
def build_ffmpeg_args(ffmpeg_path, encoder, capture_input_args: list[str], width, height, fps, kbps,
                      loss_recovery: str, capture_needs_scale: bool, entropy_coder: str = "auto") -> list[str]
```
Kinds/Namen: `nvenc` „NVIDIA GPU (NVENC)", `vaapi` „Intel/AMD GPU (VA-API)", `x264` „CPU (x264 – weniger fps möglich)".
Rate-Argumente (alle): `-b:v {kbps}k -maxrate {kbps*140//100}k -bufsize {kbps*250//1000}k -bf 0 -refs 1`.
`entropy_coder`: `"cavlc"`/`"cabac"` → `-coder <wert>` **auf jedem Rung** (nvenc, vaapi, x264), `"auto"` → gar kein `-coder`.
Der Wert muss überall hin: h264_vaapi hat CABAC als Default, ein stiller Fallback von nvenc auf VA-API würde die Wahl
sonst rückgängig machen, während Fenster und Log weiter CAVLC behaupten. Sonst ändert `-coder` nichts an der Kommandozeile.
`-refs 1` wird übergeben, aber NVENC schreibt trotzdem `max_num_ref_frames = 2` ins SPS (gemessen); x264 hält sich daran.
- **nvenc** (verifiziert auf dem Zielsystem): `-c:v h264_nvenc -preset p1 -tune ull -rc vbr -pix_fmt yuv420p -delay 0`
  + Rate + `-g {fps}` (intra **und** keyframe) + bei intra `-intra-refresh 1 -single-slice-intra-refresh 1`
  + `-color_range tv -colorspace bt709 -forced-idr 1 -f h264 -flush_packets 1 pipe:1`.
  **Abweichungen vom Windows-Original, beide gemessen:** `-delay 0` senkt die Encoder-Latenz von 34,8 ms auf 2,3 ms bei
  byte-identischer Ausgabe. Und `-g` ist bei ffmpegs NVENC-Wrapper mit Intra-Refresh die **Sweep-Länge** (GOP wird intern
  unendlich, keine periodischen IDRs): mit dem Windows-Wert `-g 216000` zeichnete die GPU auf statischem Bild gar keinen
  Refresh-Streifen (alle Frames 51 B), mit `-g 60` sichtbar (~100 B) — das erklärt die vom Original-Autor beobachteten
  NVENC-Artefakte. 1 IDR pro 300 Frames in beiden Fällen (verifiziert per ffprobe).
- **vaapi**: `-vaapi_device /dev/dri/renderD128`, Filter `format=nv12,hwupload`, `-c:v h264_vaapi -bf 0 -refs 1`
  + Rate + `-g {fps}`; kein Intra-Refresh (supports_intra_refresh=False → SINFO 0). Best effort, ungetestet.
- **x264**: `-c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -x264-params sliced-threads=0:slices=1:intra-refresh=1`
  + Rate + `-g {fps}` (intra) — im keyframe-Modus `intra-refresh=0` und `-g {fps}`.
- Eingabe: `capture_input_args` (vom Capture-Backend, siehe unten) kommt direkt nach `-hide_banner -loglevel warning`.
  Wenn `capture_needs_scale` (x11grab): `-vf scale={w}:{h}:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=yuv420p`.
  Roh-Pipe-Eingabe ist bereits I420/bt709/limited → keine Filter außer `-pix_fmt yuv420p`.
- Probe: `-hide_banner -loglevel error -f lavfi -i color=c=black:s=320x240:d=0.1 -frames:v 1 -c:v <enc> -f null -`
  (vaapi-Probe mit `-vaapi_device` + `-vf format=nv12,hwupload`).

### capture.py + portal.py
```python
class ScreenCapture:                       # Basisklasse
    name: str                              # "portal" | "x11grab" | "test"
    def start(self, width, height, fps) -> bool
    def ffmpeg_input_args(self) -> list[str]
    needs_scale: bool                      # True nur bei x11grab
    def feed(self, ffmpeg_stdin) -> None   # blockiert bis stop(); Roh-Pipe-Backends schreiben hier Frames
    def stop(self) -> None
    captured_fps: int                      # Statistik (Frames vom Quell-Backend in der letzten Sekunde)
def create_capture() -> ScreenCapture      # Auswahl: TEE_CST_TEST_SOURCE=1 → TestCapture; Portal verfügbar → PortalCapture; DISPLAY gesetzt → X11Capture; sonst None
def warm_up() -> None                      # Serverstart: wenn Portal-Backend und kein restore_token gespeichert → Dialog jetzt zeigen, Token sichern, Session schließen. Sonst no-op.
```
Umgebungsvariablen für Tests: `TEE_CST_TEST_SOURCE=1` (Testquelle statt Portal), `TEE_CST_NO_DISPLAY_SWITCH=1` (server.py schaltet den Desktop nicht um),
`TEE_CST_SETTINGS_PATH`, `TEE_CST_LOG_PATH` (eigene Dateien statt der echten). Kein Test darf ohne explizite Opt-in-Variable
(`TEE_CST_PORTAL_TEST=1`, `TEE_CST_DISPLAY_TEST=1`) einen Portal-Dialog auslösen oder die Auflösung umschalten.
- **PortalCapture** (Wayland und GNOME-X11): `portal.ScreenCastSession` (Gio.DBus, Session-Bus,
  `org.freedesktop.portal.Desktop` / `/org/freedesktop/portal/desktop`):
  `CreateSession` → `SelectSources(types=1 MONITOR, multiple=False, cursor_mode=2 EMBEDDED, persist_mode=2, restore_token=<gespeichert>)`
  → `Start(parent_window="")` → Ergebnis `streams a(ua{sv})` (node_id) + `restore_token` (speichern in settings
  `screencast_restore_token`) → `OpenPipeWireRemote` (fd via `call_with_unix_fd_list_sync`).
  Request-Pfade: `/org/freedesktop/portal/desktop/request/<sender ohne ':' und '.'→'_'>/<token>`; auf
  `org.freedesktop.portal.Request.Response` **vor** dem Aufruf abonnieren; Response-Code ≠ 0 → Fehler (1 = Nutzer hat abgebrochen).
  Session am Ende via `org.freedesktop.portal.Session.Close` schließen. Timeout für den Dialog: 120 s.
  Die Session wird pro Stream (bei PLAY) erzeugt und bei Stop geschlossen. `warm_up()`-Funktion: einmalige
  Session ohne Stream, nur um den Dialog beim Serverstart zu zeigen und den Token zu sichern, wenn noch keiner existiert.
- GStreamer als Subprozess (kein gi.Gst): `gst-launch-1.0 -q pipewiresrc fd=<fd> path=<node> do-timestamp=true always-copy=true keepalive-time=100
  ! queue max-size-buffers=8 max-size-time=0 max-size-bytes=0 leaky=downstream ! videoconvertscale n-threads=4 add-borders=false
  ! video/x-raw,format=I420,width=<w>,height=<h>,colorimetry=bt709,pixel-aspect-ratio=1/1 ! fdsink fd=1 sync=false`
  mit `pass_fds=(fd,)`; stderr in eine Log-Tail-Datei/Thread. Frames von stdout lesen (Größe w*h*3//2).
- **Frame-Takt** (`feed`): **feste Kadenz auf einem Raster, das der Quelle folgt.** Der Reader-Thread hält das
  neueste Frame und zählt eine Generation hoch; `feed` schreibt genau `fps` Bilder/s — im Takt das neueste, das
  es in diesem Moment gibt, und das letzte noch einmal, wenn die Quelle nichts Neues hatte. Grund: GNOME
  liefert schadensgetrieben (Tippen ~20/s, stehender Desktop 10/s über `keepalive-time`, Spiel 42/s,
  Mausbewegung 60–240/s), und die PS3 nimmt pro Bildwiederholung genau ein dekodiertes Bild aus einer
  Ein-Bild-Reserve und wiederholt sonst ihr letztes (`stream.c`, `takeFrameForDisplay`) — eine schwankende
  Rate kommt dort als Ruckeln an (gemeldet: „~20 fps auf der PS3, Statistik voller roter Balken"), während
  ddagrab unter Windows immer auf konstante 60 dupliziert hat.
  **Das Raster ist absolut**: `due += 1/fps` vom vorigen Rasterpunkt, **nie** vom Ende des Schreibvorgangs —
  ein Termin „letzter Schreibvorgang + 1/fps" wandert um dessen eigene Dauer nach hinten (ein 720p-Bild sind
  1,38 MB) und hielt damit *jedes* Bild auf (gemessen: 56,9 Bilder/s bei Alter Median 25,9 ms).
  **Fenster** (`WRITE_WINDOW_FRACTION` = 0,25): ein wartendes Bild darf ab `due - Fenster` raus, ein im
  Fenster ankommendes sofort — Schreibvorgänge liegen also in `[due - Fenster, due]`, Abstände in
  `[T - Fenster, T + Fenster]`.
  **Phasen-Servo** (`SERVO_GAIN` 0,3, `SERVO_SLEW_FRACTION` 3,33 %, `SERVO_GATE_WRITES` 30): jedes einzeln
  angekommene Bild zieht den nächsten Rasterpunkt begrenzt (höchstens `SERVO_SLEW_FRACTION` eines Takts) auf
  sich zu, sonst wartet eine Quelle mit genau `fps` in ungünstiger Phase bei *jedem* Bild fast einen ganzen
  Takt (gemessen 12,57 ms Median ohne, 0,04 ms mit Servo) — und das bleibt stundenlang so, weil beide Uhren
  auf wenige ppm übereinstimmen. Der Servo schweigt, solange die Quelle schneller ist als wir (dort gibt es
  keine Phase, nur das neueste Bild) und nach jedem Schreibvorgang, der nicht genau ein Bild verbraucht hat,
  für `SERVO_GATE_WRITES` weitere.
  **Die Slew-Grenze ist zugleich das Rate-Band**: solange das Raster der Quelle folgen *kann*, folgt es ihr
  auch in der Rate. Nachgemessen (1280×720, je 8 s, Quelle exakt gesetzt): 57/s → 60,01/s, 58/s → 59,85/s,
  **59/s → 59,00/s, 60,1/s → 60,10/s, 62/s → 62,00/s**, 62,5/s → 60,14/s, 63/s und darüber → 60,00/s. Auf
  der Leitung liegen also genau `fps` Bilder/s **nur außerhalb von `fps` ± 3,33 %**; innerhalb des Bandes
  liegt dort die Quellrate. Das ist gewollt (GNOMEs ScreenCast misst auf diesem PC 61,9/s; mitlaufen kostet
  1,9 ms Bildalter statt 7,9 ms), muss aber mitgedacht werden: ffmpeg bekommt `-framerate <fps>`, und die
  PS3 nimmt pro Bildwiederholung genau ein Bild — was darüber ankommt, dekodiert sie und verwirft es
  (`nextDrawIndexLocked`, `target = publishSeq - 1`).
  **Kein Quellbild darf verloren gehen**: `seen` wird ausschließlich in demselben Atemzug gesetzt, in dem auch
  geschrieben wird. Ein Bild wird nur überholt, wenn ein **neueres** seinen Platz einnimmt, nie von einer
  Wiederholung. Der alte Code setzte `seen` *vor* der Ratenentscheidung, verbrauchte damit Generationen
  ungeschrieben und wartete danach auf die nächste — das war die Lücke hinter „44522 Bilder von der Quelle,
  42548 an ffmpeg" (gemessen an einer Salve aus 5 Bildern im 4-ms-Abstand: 20,3 Schreibvorgänge/s,
  Bildalter p99 110 ms; jetzt 60,0/s und 11 ms). Die Stopp-Zeile schlüsselt daher nach neu / Wiederholungen /
  überholt auf.
  **Wiederholungen laufen dauerhaft mit voller `fps`**, kein Zurückfahren nach ruhigen Sekunden: mit
  Intra-Refresh ist ein unverändertes Bild fast leer, der Sweep zählt aber in *Bildern* (`-g` = fps), d. h.
  bei 10 Wiederholungen/s dauert die Selbstreparatur nach Paketverlust 6 s statt 1 s — und ein Ratenwechsel
  ist genau das, was die Konsole nicht mag.
  Der Fehler wird **nicht** auf ±einen halben Takt gefaltet: ein gefalteter Detektor bleibt in einem
  Zyklusschlupf hängen (gemessen: 60 Hz-Quelle, Alter Median 14,69 ms über 8 s, während Rate, Abstände und
  alle Zähler perfekt aussehen); ungefaltet läuft das Raster den ganzen Takt heraus und rastet wieder ein.
  Gemessen (**echte 1280×720-Bilder**, je 8 s, Quelle 0/5/20/42/60/90/240 Bilder/s): Ausgabe 60,00–60,03/s,
  Abstand Standardabweichung 0,08–3,06 ms, längster Abstand 21,3 ms; Alter eines neuen Bildes Median 1,95 ms
  bei 60/s und 5,98 ms bei 42/s (bei 42/s p90 13,1 ms). Diese Alterswerte enthalten den Weg des 1,38-MB-
  Bildes durch drei Pipes und sind das, was auf der Leitung ankommt; die früher hier genannten 0,04–0,05 ms
  stammen aus dem 64×48-Testaufbau (dort nachgestellt: 0,05 ms bei 20/s und 60/s, 4,60 ms bei 42/s).
  Kein Quellbild verloren: 42/s → 336 rein / 337 raus, 60/s → 480/480, 90/s → 720 rein, 480 geschrieben +
  240 überholt, „flat out" 6275 rein → 480 geschrieben, nichts verschluckt; gegen eine Quelle, die so
  schnell liefert, wie die Pipe es zulässt, 0,14 s eigene Thread-CPU in 8 s; 10 Start/Stopp-Zyklen bei
  720p: keine fds, keine Threads, kein Kindprozess übrig, kein zerrissenes Bild.
  **Überlauf**: ein Takt gilt erst als verloren, wenn sein *Rasterpunkt* vorbei ist (`now > due`) — nicht
  schon, wenn das Fenster des nächsten Takts offen ist. Der frühere Test auf `due - Fenster` warf Takte weg,
  die noch ein Viertel Intervall Zeit hatten, und zwar bei jedem Durchlauf gleich: ein Schreibvorgang von
  12,5 ms (er passt in die 16,67 ms seines Takts) halbierte die Ausgabe auf 30,00/s, ein 20/s-Quelle-Stream
  fiel von 60,03/s auf 40,03/s. Jetzt: 11/12,5/13/14/16 ms → jeweils 60,00/s ohne einen ausgelassenen Takt,
  erst über 16,67 ms wird ausgelassen (18 ms → 30,00/s, Phase erhalten, nie eine Salve).
  Erst schreiben, wenn ein erstes Frame da ist. Schreibfehler (BrokenPipe) → Ende.
  `captured_fps` meldet 0, sobald `STALE_FPS_S` (2 s) lang kein Bild kam (eine stehende Quelle darf nicht
  ihre letzte Zahl weitermelden); der Wert zeigt weiter die **Quelle**, nicht die Ausgaberate.
  `ffmpeg_input_args = ["-f","rawvideo","-pix_fmt","yuv420p","-video_size",f"{w}x{h}","-framerate",str(fps),"-i","pipe:0"]`
  (dazu `-probesize 32 -analyzeduration 0` vor `-f rawvideo`).
- **X11Capture**: `["-f","x11grab","-framerate",str(fps),"-draw_mouse","1","-i",os.environ["DISPLAY"]]`, `needs_scale=True`, `feed()` kehrt sofort zurück.
- **TestCapture**: wie Portal, aber Quelle `videotestsrc is-live=true pattern=ball ! video/x-raw,framerate=60/1,width=<w>,height=<h>` (für Integrationstests ohne Portal).

### live_streamer.py
```python
class LiveStreamer:
    def __init__(self, sock, ffmpeg_path, fps, kbps, width, height, send_rate_kbps, create_capture,
                 encoders_to_try, loss_recovery, on_all_encoders_failed, video_kbps=None, entropy_coder=None)
    # encoders_to_try / loss_recovery / video_kbps / entropy_coder sind Callables (oder feste Werte, oder None
    # = beim Konstruktorwert bleiben); sie werden bei JEDEM start() neu gelesen, damit eine Änderung im Fenster
    # ab dem nächsten Stream greift. Die Paketrate behält dabei das Verhältnis des Konstruktors (kbps*3).
    is_streaming: bool
    def start(self, target) -> None      # wiederholtes PLAY vom selben Ziel: nur SINFO erneut senden
    def stop(self) -> None
    def reset_failures(self) -> None
    def send_stream_info(self, target) -> None
```
Port von `LiveStreamer.RunPump/PumpEncoder`: pro Encoder ffmpeg starten (childproc.popen, stdin=PIPE wenn Roh-Pipe,
stdout=PIPE, stderr → Drain-Thread mit Tail 2000 Zeichen), Capture `start()` **vor** ffmpeg, `feed()` in eigenem Thread,
stdout in 64 KiB-Blöcken lesen → Splitter → `send_access_unit(..., capture_us=now_us())`. Erstes-Frame-Timeout 5 s
(Kill-Thread), 0 Frames → nächster Encoder; alle scheitern → `on_all_encoders_failed(reason)` nach 3 Fehlstarts in Folge.
Nach Ende `capture.stop()` und ffmpeg beenden (terminate, 3 s, kill). `SINFO`-Intra-Flag aus `encoders.intra_refresh_enabled`.

### audio.py
```python
class AudioCapture:
    sample_rate: int; dropped_frames: int; buffered_frames: int
    def start(self) -> bool; def stop(self); def read(self, frames: int) -> bytes   # s16be interleaved, kurz → mit Stille aufgefüllt
class AudioStreamer:
    def __init__(self, sock, ffmpeg_path="ffmpeg"); def start(self, target); def stop(self)
```
- Capture: `ffmpeg -hide_banner -loglevel error -f pulse -fragment_size 1920 -i @DEFAULT_MONITOR@ -ac 2 -ar 48000 -f s16be pipe:1`.
  **Nur Monitor-Quellen, niemals `-i default`.** Gemessen (pw-dump, während ffmpeg lief): einen Quellnamen, den
  PipeWires Pulse-Server nicht auflösen kann, ersetzt er stillschweigend durch das Standard-**Aufnahme**gerät —
  `-i default` nahm damit den Line-In einer Capture-Karte auf (auf den meisten PCs das Mikrofon), während das Log
  „nehme die Lautsprecher auf" schrieb; scheitern konnte dieser Fallback nie. Reihenfolge daher:
  `@DEFAULT_MONITOR@`, `@DEFAULT_SINK@.monitor` (beide verifiziert auf den Standard-Sink verlinkt), danach jede
  namentliche `*.monitor`-Quelle aus `ffmpeg -sources pulse`. Listet die Aufzählung Quellen, aber keinen Monitor
  (kein Wiedergabegerät), wird gar nicht aufgenommen (nur Video, mit Log-Zeile). Höchstens `MAX_SOURCES_TO_TRY`
  (5) Quellen; nur die erste bekommt `START_TIMEOUT_S` (3 s), jede weitere `FALLBACK_TIMEOUT_S` (0,5 s) —
  `AudioCapture.start()` läuft auf dem Empfangsthread unter `stream_lock`, jede Sekunde dort ist eine Sekunde,
  in der CP/TIME/STOP der PS3 nicht gelesen werden. Ring 1 s. Wenn der Ring > 60 ms hält, ältestes verwerfen bis 20 ms (zählt als dropped, einmal loggen) — bei
  PipeWire fließt Ton **auch bei Stille** kontinuierlich, anders als WASAPI.
- Streamer: Port von AudioStreamer.cs — chunk_frames = min(rate*5/1000, 512, (1500−16)//4 = 371),
  Prebuffer 20 ms (max 500 ms warten), Pacing mit now_us (sleep bis 2 ms vorher, dann kurz spinnen),
  AINFO beim Start + 1×/s, Log am Ende.
  Zur dritten Grenze: `AUDIO_MAX_FRAMES` (512) allein ist nicht die ganze Grenze — die PS3 empfängt in einen
  1500-Byte-Puffer (net-common.h `PACKET_MAX`) und `handleAudioPacket` verlangt danach `packetBytes >= 16 + frames*4`.
  Ein größeres Datagramm wird von `recv` abgeschnitten und deshalb GANZ verworfen, nicht gekappt: 512 Bilder
  (2064 B) hätten den Ton komplett verloren statt ihn zu begrenzen. Bei 48 kHz sind es ohnehin 240 Bilder
  (976 B), beide Grenzen greifen also erst bei einer anderen Abtastrate.

### pad_receiver.py / virtual_gamepad.py / desktop_input.py
```python
class PadReceiver:
    def __init__(self, desktop_input: DesktopInput, gamepad: VirtualGamepad, swap_sticks: Callable[[], bool])
    def set_gamepad_mode(self, wanted: bool); def type_key(self, ch: str); def release(self); def handle(self, packet: bytes, sender); def close(self)
class VirtualGamepad:
    is_open: bool
    def try_open(self) -> bool; def send(self, buttons, lx, ly, rx, ry); def close(self)
class DesktopInput:
    def apply(self, buttons, lx, ly, rx, ry); def release_all(self); def type_character(self, ch: str); def close(self)
```
- PadReceiver: Port von PadReceiver.cs (Paket-Parsing, Verlustzählung, Trip-Zeit, Button-Logs bei Änderung, 2-s-Report).
- VirtualGamepad: `evdev.UInput` mit name `"Microsoft X-Box 360 pad"`, vendor 0x045e, product 0x028e, version 0x0110, BUS_USB.
  Keys: BTN_A(Cross) BTN_B(Circle) BTN_X(Square) BTN_Y(Triangle) BTN_TL(L1) BTN_TR(R1) BTN_SELECT(Select) BTN_START(Start) BTN_MODE BTN_THUMBL(L3) BTN_THUMBR(R3).
  Abs: ABS_X/Y/RX/RY (-32768..32767, fuzz 16, flat 128), ABS_Z/ABS_RZ (0..255, L2/R2 digital → 255), ABS_HAT0X/HAT0Y (-1..1, D-Pad; up = -1).
  **Achsen: evdev y positiv = unten, wie die PS3 → NICHT invertieren** (Windows/XInput invertierte). Dead zone 12, full tilt 115 (wie Original `ToXboxAxis`).
  Nach jedem Report `syn()`. Fehler (kein /dev/uinput) → `try_open()` False mit Grund im Log.
- DesktopInput (uinput): eine Maus (REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES; BTN_LEFT/RIGHT/MIDDLE) und eine
  Tastatur (alle KEY_*). Mapping wie DesktopInput.cs: linker Stick → Zeiger (quadratische Kennlinie, 880 px/s bei Vollausschlag 112,
  Dead zone 16, Zeitbasis mit 50-ms-Kappung), rechter Stick Y → Scrollen (42 Rasten/s; Stick unten = Seite runter → REL_WHEEL **-1** je Raste),
  Cross/Circle/Square = links/rechts/mitte, D-Pad = Pfeiltasten, START = KEY_LEFTMETA (Super).
  `type_character`: `\b`→KEY_BACKSPACE, `\t`→KEY_TAB, `\n`→KEY_ENTER, sonst Zeichen → (keycode, modifiers) über
  **libxkbcommon (ctypes)**: Layout aus `gsettings get org.gnome.desktop.input-sources mru-sources` (das zuletzt per
  Super+Space gewählte Layout — bewusste Abweichung vom Original, die bei zwei Layouts das tut, was der Nutzer erwartet),
  sonst `sources` (erste `('xkb', 'de')`-Angabe), Fallback `$XKB_DEFAULT_LAYOUT`, dann "us".
  `gsettings` bekommt `GSETTINGS_TIMEOUT_S` = 1 s (2 Aufrufe → höchstens 2 s), denn die Tabelle wurde ursprünglich beim
  ersten `KEY`-Paket auf dem Empfangsthread gebaut und 2×5 s hätten den Client-Watchdog (CLIENT_TIMEOUT_MS 3000)
  ausgelöst und den Stream abgerissen; zusätzlich wärmt ein Daemon-Thread die Tabelle vor, sobald die uinput-Geräte
  angelegt sind. Kandidatenwahl: **gewöhnliche Taste vor Sondertaste, dann wenigste Modifier** — sonst gewinnt ein
  Zeichen auf einer exotischen Zusatztaste (gemessen auf `de`: `(` → KEY_KPLEFTPAREN, `$` → KEY_DOLLAR(434),
  `€` → KEY_EURO(435)) gegen Shift+8 / Shift+4 / AltGr+E — und Codes über 255 erreichen keinen X11/Xwayland-Client.
  Keymap via `xkb_keymap_new_from_names`, dann alle Keycodes/Level nach dem
  Keysym des Zeichens durchsuchen (`xkb_keymap_key_get_syms_by_level`, Level 1 = Shift, 2 = AltGr/ISO_Level3_Shift), evdev-Code = xkb-Keycode − 8.
  Fallback ohne libxkbcommon: US-Tabelle. Unbekanntes Zeichen → Log einmalig, ignorieren.

### display_mode.py — Moduswahl (gemessen, weicht bewusst vom Original ab)
`choose_capture_mode(modes, stream_w, stream_h, fps) -> (w, h, refresh)` und
`DisplayMode.match_for_capture(stream_w, stream_h, fps)`; server.py ruft **match_for_capture**, nicht match_to.
GNOMEs ScreenCast gibt nur ~2/3 der Bildwiederholrate heraus — an der echten PS3 gemessen: **40,1 fps** bei
Desktop 1280×720@60, **60,1 fps** bei 2560×1440@320 (obwohl dort zusätzlich 1440p→720p skaliert wird).
Da 1280×720 auf den meisten Monitoren bei 60 Hz endet, kostet die originalgetreue 1:1-Umschaltung ein Drittel
der Bilder. Regel: **kleinster Modus mit `width ≥ stream_w`, `height ≥ stream_h`, `refresh ≥ CAPTURE_REFRESH_FACTOR (1,5) × fps`,
keine `+vrr`-Variante**; bei Gleichstand die höhere Rate. Findet sich keiner (reiner 60-Hz-Monitor), Rückfall auf
`(stream_w, stream_h, fps)` = Verhalten des Originals. Klein zählt, weil jedes Pixel je Bild aus dem Grafikspeicher
gelesen wird (1920×1080 = 8,3 MB gegen 14,7 MB bei 2560×1440), und genau daraus besteht die Eingabeverzögerung.

### display_mode.py / power.py
```python
class DisplayMode:
    is_changed: bool
    def match_to(self, width, height, refresh_hz) -> bool; def restore(self) -> None
def keep_display_awake(streaming: bool) -> None
```
- Mutter: `org.gnome.Mutter.DisplayConfig` `/org/gnome/Mutter/DisplayConfig` — `GetCurrentState` → serial, monitors, logical_monitors, properties.
  Primären logischen Monitor nehmen; passenden Modus `"{w}x{h}@{hz}"` (Toleranz ±0.2 Hz; sonst irgendein `{w}x{h}@*` bevorzugt ≥ hz) suchen; wenn er
  bereits aktiv ist → True ohne Änderung. `ApplyMonitorsConfig(serial, 1 /*temporary*/, logical_monitors, {})` mit dem neuen mode_id
  und scale 1.0 für den primären Monitor; andere Monitore unverändert übernehmen, aber solche rechts vom primären um die Breitendifferenz nach
  links schieben (kein Überlappen/Lücken). Originalmodus + scale merken; `restore()` wendet ihn wieder an (bei Fehler `GetCurrentState`
  neu holen und erneut versuchen). Nur ein Restore pro Wechsel (Flag zuerst löschen).
- X11-Fallback (kein Mutter-Bus, aber `DISPLAY`): `xrandr --output <primary> --mode WxH --rate hz`, Restore mit dem gemerkten Modus.
- Signal-/atexit-Sicherung liegt in server.py (ruft `restore()`).
- power.py: `org.gnome.SessionManager.Inhibit("tee-cell-stream-server", 0, "Streaming zur PS3", 12)` → Cookie, `Uninhibit`;
  Fallback `org.freedesktop.ScreenSaver.Inhibit`. Fehler nur loggen.

### custom_commands.py
```python
SLOT_COUNT = 4
def get(slot) -> dict; def set(slot, command: dict) -> None; def run(slot) -> None
```
Erststart: Slot 1 = `{"kind":"run","value":"steam://open/bigpicture","label":"Big Picture"}`. `run`: Wert, der wie eine URI
aussieht (`^[a-zA-Z][a-zA-Z0-9+.-]*://`) → `xdg-open <uri>`, sonst `sh -c <value>`; Popen detached (start_new_session), Log.

### netinfo.py
`get_beacon_targets() -> list[tuple[str,int]]`: `("255.255.255.255", 38311)` + je IPv4-Adresse (nicht lo) die Broadcast-Adresse
(`ip -j -4 addr show up` parsen; Fallback: nichts außer global). Duplikate entfernen.

### childproc.py
`popen(args, **kw) -> subprocess.Popen` mit `preexec_fn` = `prctl(PR_SET_PDEATHSIG, SIGKILL)` (ctypes libc) und Registrierung;
`kill_all()`; `atexit`-Hook. Kinder erhalten `stdin=DEVNULL`, wenn nicht anders angegeben.

### app.py / ui.py / tray.py / autostart.py (GUI — modern, libadwaita)
- `Adw.Application(application_id=APP_ID, flags=HANDLES_COMMAND_LINE)`; zweite Instanz → vorhandenes Fenster präsentieren.
  `--minimized` startet ohne Fenster. Beim Start `Server.start()`; scheitert das (Port belegt) → Adw.MessageDialog „Läuft schon" + Ende.
- Fenster: `Adw.ApplicationWindow` 640×600, `Adw.ToolbarView` + `Adw.HeaderBar` mit `Adw.ViewSwitcher` (Seiten „Server", „Befehle")
  und Menü-Button (Menü: „Log öffnen", „Autostart" (Toggle), „Über TEE Cell Stream Server", „Beenden").
  Seite Server (in `Adw.Clamp`): Statuszeile (farbiger Punkt grau/grün via CSS-Klasse + Text „Warte auf eine PS3 …" / „PS3 verbunden: <ip>" / „Gestoppt"),
  Untertitel = `settings_summary` bzw. Trip-Grund; Button Start/Stop (`suggested-action` / `destructive-action`);
  `Adw.PreferencesGroup` „Video": `Adw.ComboRow` Encoder, `Adw.ComboRow` „Fehlerkorrektur"
  (Intra-Refresh (Standard) / Keyframes – falls NVENC Artefakte zeigt), `Adw.ComboRow` „Bitrate"
  (Modell aus `protocol.BITRATE_CHOICES_KBPS`, „6 Mbit/s (empfohlen)"), `Adw.ComboRow` „Entropie-Codierung"
  (Modell aus `protocol.ENTROPY_CODERS`, Standard CAVLC), `Adw.SwitchRow` „Desktop während des Streams auf 1280×720 schalten".
  **Alle vier ComboRows sind gesperrt, solange eine PS3 streamt** (sie wirken erst ab dem nächsten Stream, und eine
  Auswahl, die in den ≤500 ms bis zum nächsten Tick durchrutscht, wird mit deutscher Log-Zeile zurückgenommen);
  die Modelle kommen aus `protocol.py`, damit das Fenster nie einen Wert anbieten kann, den der Server ablehnt;
  Gruppe „Eingabe": SwitchRow „Sticks im Maus-Modus tauschen (rechter Stick bewegt den Zeiger)";
  Gruppe „System": SwitchRow „Beim Anmelden starten (minimiert)"; darunter Hinweistext „Schließen des Fensters lässt den Server im
  Hintergrund weiterlaufen. Beenden über das Tray-Symbol oder das Menü."; Protokoll-Ansicht (monospace TextView, read-only, auto-scroll, dunkler Hintergrund).
  Seite Befehle: Erklärung + 4 Zeilen (Nummer, ComboRow Aktion „Keine"/„Befehl oder URI ausführen", EntryRow „Befehl oder URI", EntryRow „Name"), speichert bei Änderung.
- Refresh alle 500 ms (`GLib.timeout_add`): Status, Punkt, Button, Encoder-Sperre, Log-Text nur bei Änderung. `Gio.Notification` bei
  Verbindung/Trennung und bei ausgelöster Sicherung. Fenster schließen = verstecken (Notification einmalig, `hide_notice_shown`).
- tray.py: `org.kde.StatusNotifierItem` auf dem Session-Bus (eigener Busname `org.kde.StatusNotifierItem-<pid>-1`), Registrierung bei
  `org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem`; Properties Category="ApplicationStatus", Id=APP_ID, Title, Status="Active",
  IconName (idle/live-Icons aus hicolor: `tee-cell-stream-server-idle`, `tee-cell-stream-server-live`; im Dev-Lauf `IconThemePath` = data/icons),
  ToolTip; Menü via `com.canonical.dbusmenu` (Anzeigen / Log öffnen / — / Beenden); `Activate` = Fenster zeigen; `NewIcon`-Signal bei Wechsel.
  Fehlt der Watcher → still ohne Tray.
- autostart.py: `~/.config/autostart/tee-cell-stream-server.desktop` (`Exec=tee-cell-stream-server --minimized`, `X-GNOME-Autostart-enabled=true`).
- CSS (Adw.StyleManager folgt dem System): `.status-dot-idle { background: #8a8a8a }`, `.status-dot-live { background: #3dd56d }`, Punkt 12 px rund; Log-View `.log-view` monospace 12 px.
- Sprache der GUI: Deutsch. Fenstertitel „TEE Cell Stream Server".

### server.py (fertig) — Zusammenspiel
Siehe Datei. Wichtig für alle Module: `Server` hält `stream_lock`, PLAY läuft im Empfangsthread:
`keep_display_awake(True)` → `display_mode.match_to(1280,720,60)` (nur wenn `switch_display_mode`) → `live_streamer.start(sender)` → `audio_streamer.start(sender)`.
Stop (STOP, Watchdog, Shutdown): `live_streamer.stop()`, `audio_streamer.stop()`, `pad_receiver.release()`, `display_mode.restore()`, `keep_display_awake(False)`.

## Tests (tests/)
- Unit-Tests je Modul (unittest): Fragment-Header-Layout, Splitter mit echtem nvenc-Output (ffmpeg lavfi → h264-Datei als Fixture erzeugen),
  Audio-Paketlayout, CP-Parsing + Gamepad-Mapping (Achsenwerte, D-Pad), Encoder-Argumente, netinfo-Parsing, Settings-Roundtrip.
- Jedes Testmodul belegt seine Scratch-Pfade mit `os.environ.setdefault("TEE_CST_SETTINGS_PATH"/"TEE_CST_LOG_PATH", ...)`
  und darf **keine** Fremdmodule in `sys.modules` ersetzen: `settings.py`/`log.py` lösen ihren Pfad einmalig beim Import
  auf, und ein im Prozess hinterlassener Stub wird von jedem später importierten Modul geerbt. Beides muss stimmen,
  damit `python3 -m unittest discover -s tests` (alle Module in EINEM Prozess) grün bleibt — einzeln grün genügt nicht.
- `tests/fake_ps3.py`: bindet :38311, wartet auf Beacon, TIME-Sync (10 Proben), PLAY→SINFO, sammelt VF-Fragmente (Reassembly wie stream.c),
  schreibt Annex-B nach Datei, empfängt AINFO/AF (zählt Pakete/s), sendet 60×/s CP (Knopf-Muster + Stick-Werte), PADMODE gamepad/mouse,
  KEY, CUSTOM, STOP. Prüft am Ende per ffmpeg-Decode: Auflösung 1280×720, erstes AU ist Keyframe mit SPS, Bild nicht schwarz — und
  gegen die heutigen Standardwerte: Bildrate im Mittel ≥ 55/s bei ≥ 50/s in jeder Sekunde, größte Lücke zwischen zwei Bildern
  ≤ 100 ms, 0 verlorene Bilder, Netzlatenz ≤ 25 ms im Mittel, Bitrate unter `-maxrate`, `entropy_coding_mode_flag` aus dem
  **PPS des empfangenen Stroms** (`--expect-entropy cavlc`, sonst wäre der wichtigste Regler nirgends geprüft: er steht weder
  in SINFO noch in ffprobes Ausgabe), SPS-Level ≤ SINFO-Level, Intra-Refresh-Strom ohne Keyframe pro Sekunde, AINFO 1×/s,
  Ruhe nach STOP, wiederholtes PLAY ohne Neustart des Streams.
  Läuft mit `TEE_CST_TEST_SOURCE=1` und `TEE_CST_NO_DISPLAY_SWITCH=1` gegen `python3 -m teecellstream --headless`.
- `tests/run_integration.sh`: startet den Server unter `setsid` (damit `pgrep -s` genau seinen Prozessbaum sieht und
  fremde ffmpeg-Läufe nicht als Leck zählen), fährt 2 Sessions gegen EINEN Serverprozess (Reconnect + Leckprüfung nach
  jeder), prüft die `bereit:`-Zeile gegen die gelieferten Standardwerte und verlangt, dass kein Kindprozess übrig bleibt.

## Packaging (packaging/)
Paket `tee-cell-stream-server`, Version aus `src/teecellstream/__init__.py` (`__version__`, derzeit 1.2.1 — `build-deb.sh` liest sie von dort, `packaging/control` hat nur `@VERSION@`), Architecture `all`, Maintainer `TEE <github@teebug.de>`, Section `net`.
Depends: `python3 (>= 3.12), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, python3-evdev, ffmpeg, gstreamer1.0-tools, gstreamer1.0-pipewire,
gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, xdg-desktop-portal, iproute2, xdg-utils, udev, libxkbcommon0`.
Recommends: `xdg-desktop-portal-gnome | xdg-desktop-portal-gtk, gnome-shell-extension-appindicator, x11-xserver-utils`.
Dateien: `/usr/lib/tee-cell-stream-server/teecellstream/`, `/usr/bin/tee-cell-stream-server`, `.desktop`, Icons (hicolor 16…256 + scalable falls SVG),
`/usr/lib/udev/rules.d/70-tee-cell-stream-uinput.rules` (`KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"`),
`/usr/lib/modules-load.d/tee-cell-stream-server.conf` (`uinput`), `/usr/share/doc/tee-cell-stream-server/{README.md,copyright}`.
postinst: udevadm reload+trigger, modprobe uinput, icon-cache, desktop-database, **ufw**: wenn `ufw status` „active" → `ufw allow 38310/udp comment 'TEE Cell Stream Server'`.
postrm (remove/purge): ufw-Regel entfernen. `build-deb.sh` baut mit `dpkg-deb --root-owner-group --build` und prüft mit `lintian` falls vorhanden (nur informativ).
