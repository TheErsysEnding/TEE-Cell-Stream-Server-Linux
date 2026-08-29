# TEE Cell Stream Server Linux — Anleitung

*English documentation: [README.md](README.md)*

Streamt den Linux-Desktop live auf eine PS3 und schickt den PS3-Controller zurück an den PC — Remote Play
in Gegenrichtung, wie Steam Remote Play/Moonlight, nur mit der PS3 als Client. Linux-Port des Windows-Tools
`cell-stream-server` aus [ps3-dev](https://github.com/mohasi/ps3-dev) (Apache-2.0). Die PS3-App
**cell-stream** (`cell-stream.pkg`, Release 174-a5dd795) bleibt unverändert: dieser Server spricht ihr
Protokoll byte-genau.

**Was heute geht:** 60 fps von 1280×720 bis **1920×1088 (Full HD)**, Ton, und der Controller steuert den
PC — als Maus und Tastatur oder als echtes Xbox-360-Gamepad, das Spiele als eingestecktes Pad sehen.
An einer echten PS3 gemessen, PC und Konsole direkt per Netzwerkkabel verbunden:

| Streamgröße | Pixel ggü. 720p | Latenz gesamt | Decode auf der PS3 | verworfen |
|---|---|---|---|---|
| 1280 × 720 | 1,00× | 25–30 ms | 19–22 ms | 0 von 893 |
| 1792 × 1008 | 1,96× | 40–47 ms | 38–42 ms | 0 |
| **1920 × 1088** | **2,27×** | **40–47 ms** | **38–44 ms** | **0** über eine ganze Runde |

Full HD bei 60 fps braucht **x264** statt NVENC: `--preset ultrafast` schaltet den Deblocking-Filter ab,
und der ist der teuerste Teil der H.264-Decodierung. Mit NVENC kostet dasselbe Bild 147 ms statt 38–44 ms.
Details im englischen [README.md](README.md).

![Das Fenster](docs/fenster-server.png)

## Installation (1 Klick)

```
sudo apt install ./tee-cell-stream-server_1.13.0_all.deb
```

Alles Nötige (ffmpeg mit NVENC, GStreamer/PipeWire, GTK4/libadwaita, evdev, Portal) kommt aus den
Ubuntu-Paketquellen. Das Paket richtet außerdem ein:

- `/dev/uinput`-Zugriff für den angemeldeten Benutzer (udev-Regel, wie bei Steam) → virtuelles Gamepad
- bei aktiver `ufw`-Firewall die Freigabe von **UDP 38310** (die PS3 spricht den Server darauf an)

Auf der PS3 (HEN/CFW): `cell-stream.pkg` aus dem Release installieren.

## Benutzung

1. **TEE Cell Stream Server** aus dem App-Menü starten. Beim **ersten Start** fragt GNOME einmal, welcher
   Bildschirm geteilt werden darf — bestätigen; die Entscheidung wird gespeichert (Restore-Token), danach
   kommt der Dialog nicht wieder.
2. Auf der PS3 die App **Cell Stream** starten. **Es gibt nichts zu drücken:** die PS3 findet den Server
   selbst (Beacon), verbindet und streamt. Beide Seiten können in beliebiger Reihenfolge starten; fällt eine
   weg, wartet die andere und verbindet neu.
3. Fenster schließen lässt den Server im Hintergrund weiterlaufen (Tray-Symbol: grau = wartet,
   grün = PS3 streamt). Beenden nur über Tray oder Menü → *Beenden*.

Während des Streams gehen **alle Tasten an den PC**; die App nutzt SELECT als Modifikator:

| Kombination | Wirkung |
|---|---|
| SELECT + Kreuz | Eingabemodus: Maus+Tastatur ↔ Gamepad |
| SELECT + Quadrat | Stream-Modus: vsync aus → vsync → vsync + 1 Frame Puffer |
| SELECT + R3 | Statistik-Panel ein/aus |
| SELECT + Dreieck/Kreis/L1/R1 | Custom-Befehl 1–4 (Reiter *Befehle*, Standard 1 = Steam Big Picture) |
| START (nicht streamend) | App beenden |

Maus-Modus: linker Stick = Zeiger, rechter Stick = Scrollen, Kreuz/Kreis/Quadrat = links/rechts/mitte,
D-Pad = Pfeiltasten, START = Super-Taste. Dreieck öffnet die Bildschirmtastatur der PS3; die Zeichen werden
am PC layoutkorrekt getippt (deutsches Layout wird berücksichtigt).

## Einstellungen im Fenster

- **Encoder**: NVIDIA (NVENC) → Intel/AMD (VA-API) → CPU (x264). Der erste funktionierende ist Standard;
  die Wahl wird gemerkt. Während eine PS3 streamt, ist die Auswahl gesperrt.
- **Auflösung**: 1280×720, 1408×800, 1536×864, 1792×1008 oder 1920×1088. Alle sind Vielfache von 16,
  weil H.264 in 16×16-Blöcken codiert und die PS3-App die *codierte* Größe zeichnet — 1920×1080 würde
  dort als 1920×1088 leicht verzerrt ankommen. Ab 1536×864 gehört **x264** als Encoder dazu.
- **Bitrate** (Standard 6 Mbit/s, bis 40 Mbit/s) und **Entropie-Codierung** (Standard CAVLC): siehe
  „Wenn die PS3 weniger als 60 fps zeigt". Für die Decodelast zählen vor allem die Pixel, nicht die Bits:
  bei 1792×1008 kosteten 35 statt 12 Mbit/s nur 2 ms Decode und 5 ms Latenz.
- **Slices pro Bild** (Standard 1): 2 oder 4 machen es auf der Konsole messbar schlechter und zeigen
  Nähte an den Grenzen — cellVdec verteilt ein Bild offenbar nicht scheibenweise auf die SPUs.
- **Fehlerkorrektur**: *Intra-Refresh* (Standard, wie Original) oder *Keyframes*. Der Original-Autor
  beobachtete, dass NVENC-Intra-Refresh-Streams bei manchen Sessions die Decodezeit der PS3 hochkriechen
  lassen — dann auf *Keyframes* wechseln.
- **Desktop während des Streams umschalten**: der Server wählt den kleinsten Monitormodus, der der
  Bildschirmaufnahme genug Bildwiederholrate lässt (siehe „Wenn die PS3 weniger als 60 fps zeigt") —
  dabei nur Standardgrößen wie 1280×720 oder 1920×1080, nie 1088. Weil ein Modus, den der Monitor nicht
  kann, einen schwarzen Bildschirm hinterlässt, fragt ein Dialog nach, ob das Bild noch da ist; ohne
  Bestätigung stellt der Server nach 15 Sekunden von selbst zurück. Abschaltbar; dann wird vom nativen Modus heruntergerechnet — mehr Daten je Bild
  und damit etwas mehr Eingabeverzögerung.
- **Sticks im Maus-Modus tauschen**, **Beim Anmelden starten** (minimiert in den Tray).

## Was auf Linux anders gelöst ist als im Windows-Original

| Windows | Linux |
|---|---|
| `ddagrab` (DirectX-Capture) | xdg-desktop-portal ScreenCast → PipeWire → GStreamer; X11: `x11grab` |
| gebündeltes ffmpeg | Ubuntu-ffmpeg (`h264_nvenc`, identische Encoder-Argumente) |
| WASAPI-Loopback | PipeWire-Sink-Monitor (`@DEFAULT_MONITOR@`) |
| ViGEmBus-Treiber | `/dev/uinput` (Kernel) — kein Treiber |
| `SendInput` | uinput-Maus/-Tastatur, Zeichen über libxkbcommon layoutkorrekt |
| `ChangeDisplaySettings` | Mutter DisplayConfig (DBus), X11: `xrandr` |
| Registry / Run-Key | `~/.config/tee-cell-stream-server/settings.json` / XDG-Autostart |
| WinForms-Tray | StatusNotifierItem (Ubuntu-AppIndicator) |

## Wenn die PS3 weniger als 60 fps zeigt

Der Engpass ist fast immer **der Decoder der PS3**, nicht das Netzwerk. Die Konsole braucht pro Bild
~20 ms zum Decodieren – mehr als die 16,7 ms, die bei 60 fps zwischen zwei Bildern liegen. Solange ihr
Decoder sauber „pipelined", reicht es trotzdem für 60 fps; kippt das, halbiert sich die Rate
(60-Hz-Ausgabe → 30 fps, **50-Hz-Ausgabe → 25 fps**).

**1. Zahlen ablesen: SELECT + R3** blendet auf der PS3 die Statistik ein.

| Wert | Gut | Bedeutung, wenn zu hoch |
|---|---|---|
| `decode` | < 16 ms | **Der Hauptverdächtige.** Über 16,7 ms schafft die PS3 keine 60 fps mehr und verwirft Bilder. |
| `network` | 3–9 ms | Leitung/WLAN. Bei Kabel praktisch immer niedrig. |
| `present` | ~0,6 ms | Übergabe an die Grafikeinheit. |
| `display` | 0 ms | ~8–10 ms heißt: vsync ist an (kostet eine Bildwiederholung). |
| `behind` | 0 | Zählt hoch = der Decoder kommt nicht mit und Bilder werden verworfen. |
| `incomplete` | 0 | Zählt hoch = Paketverlust auf dem Weg zur PS3. |
| `bitrate` | ≈ eingestellter Wert | Deutlich darüber heißt: der Encoder nutzt seinen Spielraum aus – Bitrate senken. |

**2. Am PC nachstellen** (Reiter *Server*, wirkt beim nächsten Verbindungsaufbau — die Regler sind
gesperrt, solange eine PS3 streamt):

- **Entropie-Codierung → CAVLC** (Standard). Gemessen mit einem bewusst schwachen Decoder als Ersatzmaß
  für die SPU-Decodierung der PS3: **−43 % Decodezeit** gegenüber CABAC bei gleicher Bitrate. Das ist der
  wirksamste einzelne Hebel. CABAC ist etwas schärfer pro Bit, aber für die PS3 spürbar teurer.
- **Bitrate senken** (Standard 6 Mbit/s statt der 10 Mbit/s des Windows-Originals). Der Original-Autor maß:
  10 → 16 Mbit/s trieb die Decodezeit der PS3 von 20 auf 45 ms. Auf diesem PC gemessen: 10 Mbit/s CABAC
  kostete 2,8 ms, 6 Mbit/s CAVLC nur 1,5 ms.
- **Fehlerkorrektur → Keyframes**, falls das Bild über eine längere Sitzung schlechter wird. Der
  Original-Autor beobachtete, dass NVENC-Intra-Refresh-Streams die Decodezeit der PS3 über eine Sitzung
  hochkriechen lassen und Artefakte hinterlassen, die erst ein Neustart beseitigt.

**2a. Der wichtigste Punkt: die Bildwiederholrate des Desktops.** GNOMEs Bildschirmaufnahme gibt nur rund
**zwei Drittel** der Bildwiederholrate heraus. An der echten PS3 gemessen, gleiche Software und gleicher
Inhalt: **40,1 fps** mit dem Desktop auf 1280×720@60, aber **60,1 fps** mit ihm auf 2560×1440@320 — obwohl
der zweite Fall zusätzlich 1440p auf 720p herunterrechnen muss. Seit 1.4.0 wählt der Server deshalb nicht
mehr die Streamgröße, sondern den **kleinsten Modus mit mindestens 1,5-facher Bildrate** (auf einem
240-Hz-Monitor typischerweise 1920×1080@240). Steht im Protokoll als „schalte den Desktop dafür auf …".
Auf einem reinen 60-Hz-Monitor ist nichts zu gewinnen; dort bleibt es bei 1280×720@60.

**2b. Nachmessen, statt zu raten.** `tools/wire-fps.py` zählt die Bilder, die wirklich zur PS3 gehen,
und sagt anhand der Abstände, ob die Quelle langsamer liefert oder ob Bilder verworfen werden:

```
sudo tools/wire-fps.py <IP-der-PS3> -d 30
```

Achtung: Auf der Leitung liegt seit `feed()` den Takt selbst hält eine **gleichmäßige** Kadenz nahe 60
Bilder/s — ein unverändertes Bild wird einfach noch einmal geschickt (die PS3 will eine gleichmäßige
Kadenz; eine schwankende kommt dort als Ruckeln an). „Nahe" heißt: liefert die Quelle selbst zwischen 58
und 62 Bildern/s, rastet das Raster genau auf ihre Rate ein und die Leitung führt eben diese Rate — auf
diesem PC misst GNOMEs ScreenCast 61,9/s, es liegen dort also 62 Bilder/s an (nachgemessen: Quelle 59/s →
59,00/s auf der Leitung, 62/s → 62,00/s, alles außerhalb 58–62 → 60,00/s). Das ist der Preis dafür, dass
ein neues Bild nicht auf sein Raster warten muss (Alter 1,9 ms statt 7,9 ms im Median).
`wire-fps.py` zeigt also nicht mehr, wie viele Bilder die *Quelle* liefert —
das steht am Ende jedes Streams im Protokoll: „… an ffmpeg: N neue, M Wiederholungen, K von einem neueren
überholt". Viele Wiederholungen = der Compositor liefert wenig; „überholt" = er liefert mehr als 60/s.

Liegen die Abstände auf dem 60-Hz-Raster (17 ms / 33 ms) und fehlt trotzdem ein Teil, verwirft etwas
vor dem Encoder Bilder — typischerweise die Bildschirmaufnahme des Compositors bei einer
**Vollbild**-Anwendung. Dann hilft, die Anwendung auf **randloses Fenster** umzustellen; das ist ein
bekanntes Verhalten von GNOME/Mutter (mutter#3074, #3903, #4214), bei dem Fensteraufnahme funktioniert
und Bildschirmaufnahme Bilder verliert.

**2c. Vollbild-Spiele: das Bild bleibt stehen.** Reicht GNOME ein Vollbildfenster direkt an den Monitor
durch (*direct scanout*), stellt es das Zusammensetzen ein — die Aufnahme friert auf dem letzten Bild ein,
während Ton und Controller weiterlaufen ([mutter#3074](https://gitlab.gnome.org/GNOME/mutter/-/work_items/3074),
[#3903](https://gitlab.gnome.org/GNOME/mutter/-/work_items/3903)). Im Protokoll steht dann „die
Bildschirmaufnahme liefert seit 3s kein neues Bild". Zwei Auswege:

- **Sofort:** das Spiel auf **randloses Fenster** stellen — dann gibt es kein Durchreichen.
- **Dauerhaft:** die mitgelieferte GNOME-Erweiterung. Sie schaltet die Direktdurchreichung ab, solange der
  Server läuft, und nimmt das danach zurück. **Der Server schaltet sie beim Start selbst ein** — nur einlesen
  muss GNOME sie einmal, und das passiert ausschließlich beim Anmelden. Nach der Installation also einmal
  ab- und anmelden, danach ist nichts mehr zu tun. Solange sie noch nicht eingelesen ist, sagt das Protokoll
  das ausdrücklich („noch nicht eingelesen — einmal ab- und anmelden").

**3. An der PS3 probieren: SELECT + Quadrat** schaltet die Darstellung um (vsync aus → vsync →
vsync + 1 Bild Puffer). *vsync aus* zeigt jedes Bild sofort, ohne auf die nächste Bildwiederholung zu
warten — geringste Latenz, dafür minimales Tearing. *vsync + 1 Bild Puffer* ist am gleichmäßigsten,
kostet aber spürbar Eingabeverzögerung.


## Log & Fehlersuche

Log: `~/.local/state/tee-cell-stream-server/server.log` (Menü → *Log öffnen*).

- **PS3 findet den Server nicht**: Sind PC und PS3 im selben Netz? Der Beacon geht an jede
  Broadcast-Adresse der aktiven Netzwerkkarten (im Log: „Beacon an: …"). Firewall: `sudo ufw status` muss
  `38310/udp ALLOW` zeigen.
- **Schwarzes Bild / „Bildschirmfreigabe abgelehnt"**: Portal-Dialog erneut bestätigen — den gespeicherten
  Token löschen (`screencast_restore_token` in `settings.json` auf `null`) und den Server neu starten.
- **Kein Gamepad, nur Maus**: `ls -l /dev/uinput` muss dem Benutzer Schreibrecht zeigen (ACL); nach der
  Installation einmal ab- und anmelden.
- **Kein Ton**: `ffmpeg -f pulse -i @DEFAULT_MONITOR@ -t 1 -f null -` muss laufen; sonst ist kein
  Standard-Ausgabegerät gesetzt.
- **Desktop bleibt auf 720p**: Menü → *Beenden* stellt zurück; im Notfall `Einstellungen → Anzeige`.

## Entwicklung

```
PYTHONPATH=src python3 -m teecellstream            # GUI
PYTHONPATH=src python3 -m teecellstream --headless # nur Server
PYTHONPATH=src python3 -m unittest discover -s tests -v
bash tests/run_integration.sh                      # Fake-PS3 gegen den Server (ohne Portal, ohne Umschaltung)
bash packaging/build-deb.sh                        # → dist/*.deb
sudo tools/wire-fps.py <IP-der-PS3> -d 30          # Bildrate auf der Leitung messen
```

Quellen des Originals zur Referenz: `upstream/` (Server-C#, PS3-App-Protokoll, READMEs mit den
gemessenen Erkenntnissen des Autors).
