# translations_de - the German half of every string outside the window (log lines, notifications,
# fallback reasons). ui.py carries its own table right next to the widgets it labels; this file
# carries the rest, so a module does not need a catalogue block of its own on top of its code.
#
# English is the source language: these keys are the exact strings the code passes to _(). A key
# that no longer matches its call site silently falls back to English, which is why the test suite
# checks every key here against the sources.

GERMAN = {
    ' exited with code %d': ' beendet mit Code %d',
    ' is stuck': ' hängt',
    '%d slices (%dx)': '%d Slices (%dx)',
    '%d superseded by a newer one%s)': '%d von einem neueren überholt%s)',
    '%dx%d at %d fps, %d Mbit/s, %s, %s': '%dx%d mit %d fps, %d Mbit/s, %s, %s',
    '%s %dx%d (rate chosen by xrandr)': '%s %dx%d (Rate von xrandr gewählt)',
    '%s: no answer after %d s': '%s: keine Antwort nach %d s',
    '%s; without a rate: %s': '%s; ohne Rate: %s',
    '(is the uinput module loaded and /dev/uinput writable?)':
        '(uinput-Modul geladen und /dev/uinput beschreibbar?)',
    '(pulse offers only %s)': '(pulse bietet nur %s)',
    ', without a token': ', ohne Token',
    '. Press Start once it is fixed.': '. Nach der Behebung auf Start drücken.',
    '. Waiting for the PS3 again.': '. Warte wieder auf die PS3.',
    '90%% under %.1f ms, 99%% under %.1f ms; %.0f%% on the cadence, %d visible hitches':
        '90%% unter %.1f ms, 99%% unter %.1f ms; %.0f%% im Takt, %d sichtbare Hänger',
    'Already running': 'Läuft schon',
    'Another copy of the server is already running.': 'Eine andere Kopie des Servers läuft bereits.',
    'CPU (x264 – fewer fps possible)': 'CPU (x264 – weniger fps möglich)',
    'Comment=Stream the PC desktop to a PS3 (cell-stream)':
        'Comment=PC-Desktop zur PS3 streamen (cell-stream)',
    'CreateSession without a session_handle': 'CreateSession ohne session_handle',
    'Open the log': 'Log öffnen',
    'Start without streams': 'Start ohne streams',
    'Streaming stopped': 'Streaming gestoppt',
    'Streaming to a PS3': 'Streaming zur PS3',
    'TEE Cell Stream Server is running (headless). Log: ':
        'TEE Cell Stream Server läuft (headless). Log: ',
    "a second copy was started - showing the running one's window":
        'eine zweite Kopie wurde gestartet - zeige das Fenster der laufenden',
    'audio: %d packets sent (%d with no audio data, %d frames dropped)':
        'audio: %d Pakete gesendet (%d ohne Ton-Daten, %d Frames verworfen)',
    'audio: %s does not start (%s), trying %s': 'audio: %s startet nicht (%s), versuche %s',
    'audio: buffer over %dms, dropping the oldest down to %dms (latency guard)':
        'audio: Puffer über %dms, verwerfe Ältestes bis %dms (Latenzschutz)',
    'audio: capture failed to start, streaming video only (%s)':
        'audio: Aufnahme-Start fehlgeschlagen, streame nur Video (%s)',
    'audio: capturing the speakers: %dHz, %d channels, 16-bit (pulse %s)':
        'audio: nehme die Lautsprecher auf: %dHz, %d Kanäle, 16-bit (pulse %s)',
    'audio: could not open the speakers, streaming video only (%s)':
        'audio: konnte die Lautsprecher nicht öffnen, streame nur Video (%s)',
    'audio: ffmpeg will not start, streaming video only (%s)':
        'audio: ffmpeg lässt sich nicht starten, streame nur Video (%s)',
    'audio: no monitor source present (no playback device), streaming video only ':
        'audio: keine Monitor-Quelle vorhanden (kein Wiedergabegerät), streame nur Video ',
    'audio: the sending thread will not start, streaming video only (%s)':
        'audio: Sende-Thread startet nicht, streame nur Video (%s)',
    'autostart: could not change the setting: %s': 'Autostart: konnte die Einstellung nicht ändern: %s',
    'autostart: no longer starts at login': 'Autostart: startet nicht mehr beim Anmelden',
    'autostart: starts at login (minimised)': 'Autostart: startet beim Anmelden (minimiert)',
    'beacon: `ip addr` failed (%s), announcing only to %s':
        'beacon: `ip addr` fehlgeschlagen (%s), sende nur an %s',
    'capture: %s does not start: %s': 'capture: %s startet nicht: %s',
    'capture: %s started (%dx%d, %d fps, pid %d)': 'capture: %s gestartet (%dx%d, %d fps, pid %d)',
    'capture: %s stopped (%d pictures from the source, %d to ffmpeg: %d new, %d repeats, ':
        'capture: %s gestoppt (%d Bilder von der Quelle, %d an ffmpeg: %d neue, %d Wiederholungen, ',
    'capture: %s: cannot open the source: %s': 'capture: %s: Quelle lässt sich nicht öffnen: %s',
    'capture: ffmpeg accepts no more pictures (%s)': 'capture: ffmpeg nimmt keine Bilder mehr an (%s)',
    'capture: first picture from the source after %d ms':
        'capture: erstes Bild von der Quelle nach %d ms',
    'capture: no screen source (no ScreenCast portal, no DISPLAY)':
        'capture: keine Bildschirmquelle (kein ScreenCast-Portal, kein DISPLAY)',
    'capture: read error at the source: %s': 'capture: Lesefehler an der Quelle: %s',
    'capture: sending pictures to ffmpeg (first one %d ms after the start)':
        'capture: sende Bilder an ffmpeg (erstes %d ms nach dem Start)',
    'capture: smoothness over %d pictures - median gap %.1f ms (ideal %.1f), ':
        'capture: Gleichmäßigkeit über %d Bilder - Abstand im Median %.1f ms (ideal %.1f), ',
    'capture: the screen capture has produced no new picture for %.0fs although it is running - ':
        'capture: die Bildschirmaufnahme liefert seit %.0fs kein neues Bild, obwohl sie läuft - ',
    'capture: the source delivered no picture at all': 'capture: die Quelle lieferte kein einziges Bild',
    'capture: the source delivers no more pictures (%s%s) - sending the last one on':
        'capture: die Quelle liefert keine Bilder mehr (%s%s) - sende das letzte Bild weiter',
    'capture: too few pictures to say anything about smoothness (%d)':
        'capture: zu wenige Bilder für eine Gleichmäßigkeits-Aussage (%d)',
    'capture: x11grab on %s (%d fps, scaled to %dx%d)':
        'capture: x11grab auf %s (%d fps, skaliert auf %dx%d)',
    'childproc: the spawner does not answer, starting directly':
        'childproc: Spawner antwortet nicht, starte direkt',
    'could not take udp :%d - another copy of the server still holds the port. Giving up.':
        'konnte udp :%d nicht belegen - eine andere Kopie des Servers hält den Port noch. Gebe auf.',
    'crashed%s: %s: %s': 'abgestürzt%s: %s: %s',
    'custom %d: could not start %s - %s': 'custom %d: konnte %s nicht starten - %s',
    'custom %d: nothing is bound to this slot': 'custom %d: an diesem Slot hängt nichts',
    'custom %d: started: %s': 'custom %d: gestartet: %s',
    'custom: could not save - %s': 'custom: konnte nicht speichern - %s',
    'display: could not ask (%s), leaving the resolution as it is':
        'display: Rückfrage ging nicht auf (%s), lasse die Auflösung stehen',
    'display: could not read the current resolution (%s), streaming scaled instead':
        'display: konnte die aktuelle Auflösung nicht lesen (%s), streame stattdessen skaliert',
    'display: could not read the modes (%s)': 'display: konnte die Modi nicht lesen (%s)',
    'display: desktop back at %s': 'display: Desktop wieder auf %s',
    'display: desktop switched to %dx%d (was %dx%d; %s); it is restored after the ':
        'display: Desktop auf %dx%d umgeschaltet (war %dx%d; %s); wird nach dem Stream ',
    'display: error reading the resolution (%s), streaming scaled instead':
        'display: Fehler beim Lesen der Auflösung (%s), streame stattdessen skaliert',
    'display: neither Mutter (DBus) nor X11 found - the resolution will not be switched':
        'display: weder Mutter (DBus) noch X11 gefunden - die Auflösung wird nicht umgeschaltet',
    'display: no confirmation after %d s - switching back to the old resolution':
        'display: keine Bestätigung nach %d s - schalte auf die alte Auflösung zurück',
    'display: restoring %dx%d failed (%s)': 'display: Wiederherstellung von %dx%d fehlgeschlagen (%s)',
    'display: restoring %dx%d failed (%s) - please switch back in the display settings':
        'display: Wiederherstellung von %dx%d fehlgeschlagen (%s) - bitte in den Anzeige-Einstellungen zurückschalten',
    'display: streaming %dx%d, the desktop is already at %dx%d@%g - nothing to switch':
        'display: streame %dx%d, der Desktop steht schon auf %dx%d@%g - nichts umzuschalten',
    'display: streaming %dx%d, wanted the desktop at %dx%d@%g - could not, ':
        'display: streame %dx%d, wollte den Desktop auf %dx%d@%g bringen - ging nicht, ',
    'display: switching from the next stream on: ': 'display: Umschaltung ab dem nächsten Stream: ',
    'encoders: %s does not answer (timed out after %d s)':
        'encoders: %s antwortet nicht (Timeout nach %d s)',
    'encoders: %s not available%s': 'encoders: %s nicht verfügbar%s',
    'encoders: could not remember the choice: %s': 'encoders: konnte die Wahl nicht merken: %s',
    'encoders: could not test %s: %s': 'encoders: konnte %s nicht testen: %s',
    'encoders: none of them works on this PC': 'encoders: keiner funktioniert auf diesem PC',
    'error in a packet from %s: %s': 'Fehler bei Paket von %s: %s',
    'extension: GNOME Shell not reachable (%s)': 'extension: GNOME Shell nicht erreichbar (%s)',
    'extension: GNOME extension enabled - full-screen games no longer freeze':
        'extension: GNOME-Erweiterung eingeschaltet - Vollbild-Spiele frieren jetzt nicht mehr ein',
    'extension: GNOME refused to enable the extension':
        'extension: GNOME hat das Einschalten der Erweiterung abgelehnt',
    'extension: could not enable the GNOME extension (%s)':
        'extension: konnte die GNOME-Erweiterung nicht einschalten (%s)',
    'extension: no session bus (%s)': 'extension: kein Session-Bus (%s)',
    'extension: the bundled GNOME extension has not been read yet - log out and in ':
        'extension: die mitgelieferte GNOME-Erweiterung ist noch nicht eingelesen - einmal ab- und ',
    'extension: unexpected error (%s)': 'extension: unerwarteter Fehler (%s)',
    'ffmpeg exited with %s%s': 'ffmpeg beendet mit %s%s',
    'game runs full-screen (a borderless window helps in the meantime).':
        'Spiel im Vollbild läuft (randloses Fenster hilft solange).',
    'layout %r%s cannot be translated': 'Layout %r%s lässt sich nicht übersetzen',
    'listening on udp :%d, beacon to :%d': 'lausche auf udp :%d, Beacon an :%d',
    'live: ffmpeg does not start: %s': 'live: ffmpeg startet nicht: %s',
    'live: frame %d at %d bytes exceeds the PS3 limit of %d bytes - dropped':
        'live: Frame %d mit %d Bytes überschreitet das PS3-Limit von %d Bytes - verworfen',
    'live: no screen capture possible (no portal, no DISPLAY)':
        'live: keine Bildschirmaufnahme möglich (kein Portal, kein DISPLAY)',
    'live: screen capture (%s) does not start': 'live: Bildschirmaufnahme (%s) startet nicht',
    'live: screen capture (%s) would not stop: %s':
        'live: Bildschirmaufnahme (%s) ließ sich nicht beenden: %s',
    'live: screen capture not possible: %s': 'live: Bildschirmaufnahme nicht möglich: %s',
    'live: the encoder produced no frames. ffmpeg said:\n':
        'live: Encoder lieferte keine Frames. ffmpeg sagte:\n',
    'monitor). A borderless window helps at once; for good, the bundled ':
        'Monitor durch). Randloses Fenster hilft sofort; dauerhaft die beiliegende ',
    'mouse and keyboard': 'Maus und Tastatur',
    'neither Mutter (DBus) nor X11 reachable': 'weder Mutter (DBus) noch X11 erreichbar',
    'no %dx%d mode on %s': 'kein Modus %dx%d auf %s',
    'no active mode on the primary monitor': 'kein aktiver Modus auf dem primären Monitor',
    'no data within %.1fs%s': 'keine Daten innerhalb %.1fs%s',
    'no display control': 'keine Anzeige-Steuerung',
    'no encoder starts': 'kein Encoder startet',
    'no free picture buffer': 'kein freier Bildpuffer',
    'no logical monitor': 'kein logischer Monitor',
    'no remembered mode': 'kein gemerkter Modus',
    'no session': 'keine Sitzung',
    'no session bus': 'kein Session-Bus',
    'no video encoder works on this PC (ffmpeg is missing or cannot do H.264)':
        'auf diesem PC funktioniert kein Video-Encoder (ffmpeg fehlt oder kann kein H.264)',
    'nothing from the PS3 for %dms': 'seit %dms nichts von der PS3',
    'once, then it enables itself. Until then the picture freezes as soon as a ':
        'anmelden, dann schaltet sie sich von selbst ein. Bis dahin friert das Bild ein, sobald ein ',
    'pad: could not create the virtual gamepad: %s ':
        'pad: virtuelles Gamepad konnte nicht angelegt werden: %s ',
    'pad: error forwarding input: %r': 'pad: Fehler bei der Eingabe-Weitergabe: %r',
    'pad: keyboard layout %s cannot be loaded (%s) - using %s':
        'pad: Tastaturlayout %s nicht ladbar (%s) - nehme %s',
    'pad: mouse and keyboard (uinput) created': 'pad: Maus und Tastatur (uinput) angelegt',
    'pad: mouse and keyboard (uinput) removed': 'pad: Maus und Tastatur (uinput) entfernt',
    'pad: no key code for %r in layout %s - ignored':
        'pad: kein Tastencode für %r im Layout %s - ignoriert',
    'pad: no virtual gamepad available. Staying on mouse and keyboard.':
        'pad: kein virtuelles Gamepad verfügbar. Bleibe bei Maus und Tastatur.',
    'pad: pressed ': 'pad: gedrückt ',
    'pad: python3-evdev is missing - no virtual gamepad possible':
        'pad: python3-evdev fehlt - kein virtuelles Gamepad möglich',
    'pad: uinput not available (%s) - mouse/keyboard control off':
        'pad: uinput nicht verfügbar (%s) - Maus/Tastatur-Steuerung aus',
    'pad: virtual Xbox 360 gamepad created': 'pad: virtuelles Xbox-360-Gamepad angelegt',
    'pad: virtual Xbox 360 gamepad created (%s)': 'pad: virtuelles Xbox-360-Gamepad angelegt (%s)',
    'portal: CreateSession returned no session': 'portal: CreateSession lieferte keine Sitzung',
    'portal: a sharing dialog has been open and unanswered for %d s - skipping this attempt':
        'portal: ein Freigabe-Dialog ist seit %d s offen und unbeantwortet - dieser Versuch entfällt',
    'portal: first-time setup - the sharing dialog asks once which monitor is streamed to the PS3':
        'portal: erste Einrichtung - der Freigabe-Dialog fragt einmalig, welcher Monitor zur PS3 gestreamt wird',
    'portal: no answer to %s after %d s - dialog abandoned':
        'portal: keine Antwort auf %s nach %d s - Dialog abgebrochen',
    'portal: no session bus - screen sharing is not possible':
        'portal: kein Session-Bus - keine Bildschirmfreigabe möglich',
    'portal: requesting screen sharing with the saved token':
        'portal: frage die Bildschirmfreigabe mit dem gespeicherten Token an',
    'portal: screen sharing is running (PipeWire node %d%s)':
        'portal: Bildschirmfreigabe läuft (PipeWire-Node %d%s)',
    'portal: sharing started, but with no stream': 'portal: Freigabe gestartet, aber ohne Stream',
    'portal: sharing token saved - the dialog only returns after it is revoked':
        'portal: Freigabe-Token gesichert - der Dialog erscheint erst wieder nach einem Widerruf',
    'portal: the portal handed out no token - the dialog will appear on every stream':
        'portal: das Portal gab keinen Token heraus - der Dialog erscheint bei jedem Stream',
    'portal: the session would not close: %s': 'portal: Sitzung ließ sich nicht schließen: %s',
    'portal: the sharing dialog is up - please pick the monitor and allow it':
        'portal: Freigabe-Dialog wird angezeigt - bitte den Monitor auswählen und freigeben',
    'portal: the user cancelled screen sharing':
        'portal: der Nutzer hat die Bildschirmfreigabe abgebrochen',
    'power: cannot keep the screen awake (SessionManager: %s; ScreenSaver: %s)':
        'power: kann den Bildschirm nicht wach halten (SessionManager: %s; ScreenSaver: %s)',
    'primary monitor %s has no active mode': 'primärer Monitor %s hat keinen aktiven Modus',
    'quit: the server would not stop cleanly: %s':
        'Beenden: der Server ließ sich nicht sauber stoppen: %s',
    'scaling instead': 'es wird stattdessen skaliert',
    'sender: %d pictures sent, %s, %.1f KB per picture on average = %.1f fragments':
        'sender: %d Bilder gesendet, %s, Ø %.1f KB je Bild = %.1f Fragmente',
    'sender: no pictures sent': 'sender: keine Bilder gesendet',
    'sharing dialog still open': 'Freigabe-Dialog noch offen',
    'started: waiting for the PS3': 'gestartet: warte auf die PS3',
    'stopped: ': 'gestoppt: ',
    'the PS3 asked us to stop': 'die PS3 hat uns gebeten aufzuhören',
    'the encoder stopped on its own': 'der Encoder hat von selbst aufgehört',
    'the screen capture does not start': 'die Bildschirmaufnahme startet nicht',
    'the server is shutting down': 'der Server wird beendet',
    'this happens when a game runs full-screen (GNOME then hands it straight to the ':
        'das passiert, wenn ein Spiel im Vollbild läuft (GNOME reicht es dann direkt an den ',
    'trace: source %d/s, to ffmpeg %d new, %d superseded':
        'trace: Quelle %d/s, an ffmpeg %d neu, %d überholt',
    'tray: could not remove the icon: %s': 'tray: konnte das Symbol nicht entfernen: %s',
    'tray: did not get bus name %s': 'tray: Busname %s nicht bekommen',
    'tray: no StatusNotifierWatcher (AppIndicator extension off?) - no tray icon':
        'tray: kein StatusNotifierWatcher (AppIndicator-Erweiterung aus?) - ohne Tray-Symbol',
    'tray: no session bus (%s) - no tray icon': 'tray: kein Session-Bus (%s) - ohne Tray-Symbol',
    'unknown packet from %s: %r': 'unbekanntes Paket von %s: %r',
    'video: %d slice(s) per picture from the next stream on (x264 only)':
        'video: %d Slice(s) je Bild ab dem nächsten Stream (nur x264)',
    'video: bitrate from the next stream on: %d Mbit/s':
        'video: Bitrate ab dem nächsten Stream: %d Mbit/s',
    'video: entropy coder from the next stream on: ':
        'video: Entropie-Codierung ab dem nächsten Stream: ',
    'video: error correction from the next stream on: ':
        'video: Fehlerkorrektur ab dem nächsten Stream: ',
    'video: rate control from the next stream on: ': 'video: Ratensteuerung ab dem nächsten Stream: ',
    'video: resolution from the next stream on: %dx%d': 'video: Auflösung ab dem nächsten Stream: %dx%d',
    'window: could not close cleanly: %s': 'Fenster: konnte nicht sauber schließen: %s',
    'window: monitoring failed: %s': 'Fenster: Überwachung fehlgeschlagen: %s',
    'audio: capture aborted (ffmpeg gone%s)': 'audio: Aufnahme abgebrochen (ffmpeg weg%s)',
    'audio: sending aborted: %s': 'audio: Senden abgebrochen: %s',
    'audio: streaming %dHz stereo to %s:%d (%dkbps)': 'audio: streame %dHz Stereo an %s:%d (%dkbps)',
    'audio: the capture thread died: %s': 'audio: Aufnahme-Thread gestorben: %s',
    'audio: the sending thread died: %s': 'audio: Sende-Thread gestorben: %s',
    'beacon to %s failed: %s': 'Beacon an %s fehlgeschlagen: %s',
    'capture: preparation failed: %s': 'capture: Vorbereitung fehlgeschlagen: %s',
    'capture: x11grab needs DISPLAY': 'capture: x11grab braucht DISPLAY',
    'display: %dx%d was refused (%s), streaming scaled instead':
        'display: %dx%d wurde abgelehnt (%s), streame stattdessen skaliert',
    'live: %d frames sent': 'live: %d Frames gesendet',
    'live: SINFO to %s failed: %s': 'live: SINFO an %s fehlgeschlagen: %s',
    'live: ffmpeg exited after %d frames. It said:\n%s':
        'live: ffmpeg hat sich nach %d Frames beendet. Es sagte:\n%s',
    'live: first frame sent %d ms after the encoder started':
        'live: erstes Frame %d ms nach Encoder-Start gesendet',
    'live: screen capture (%s) aborted: %s': 'live: Bildschirmaufnahme (%s) abgebrochen: %s',
    'live: sending to %s failed: %s': 'live: Senden an %s fehlgeschlagen: %s',
    'live: stream to %s:%d ended': 'live: Stream an %s:%d beendet',
    'live: the picture source aborted: %s': 'live: Bildquelle abgebrochen: %s',
    'live: the pump aborted: %r': 'live: Pumpe abgebrochen: %r',
    'pad: %s, %d packets, %d lost, %d ms PS3 -> here':
        'pad: %s, %d Pakete, %d verloren, %d ms PS3 -> hier',
    'pad: gamepad report failed: %s': 'pad: Gamepad-Report fehlgeschlagen: %s',
    'pad: input to uinput failed: %s': 'pad: Eingabe an uinput fehlgeschlagen: %s',
    'pad: virtual gamepad removed': 'pad: virtuelles Gamepad entfernt',
    'portal: %s failed: %s': 'portal: %s fehlgeschlagen: %s',
    'portal: OpenPipeWireRemote failed: %s': 'portal: OpenPipeWireRemote fehlgeschlagen: %s',
    'portal: screen sharing failed (%s reports code %d)':
        'portal: Bildschirmfreigabe fehlgeschlagen (%s meldet Code %d)',
    'portal: session closed': 'portal: Sitzung geschlossen',
    'power: releasing the screen failed (%s)': 'power: Freigabe des Bildschirms fehlgeschlagen (%s)',
    'signal %d - shutting down': 'Signal %d - beende',
    'tray: action failed: %s': 'tray: Aktion fehlgeschlagen: %s',
    'tray: registration failed: %s': 'tray: Registrierung fehlgeschlagen: %s',
    'tray: signal %s failed: %s': 'tray: Signal %s fehlgeschlagen: %s',
    'window: refresh failed: %s': 'Fenster: Aktualisierung fehlgeschlagen: %s',
    'Show': 'Anzeigen',
    ' is streaming.': ' streamt.',
    '. Waiting for the PS3 again.': '. Warte wieder auf die PS3.',
    'Action': 'Aktion',
    'Can you see this window?': 'Siehst du dieses Fenster?',
    'Command %d': 'Befehl %d',
    'Command or URI': 'Befehl oder URI',
    'Name': 'Name',
    'PS3 connected': 'PS3 verbunden',
    'PS3 disconnected': 'PS3 getrennt',
    'Waiting for it to come back.': 'Warte, bis sie wiederkommt.',
    'a virtual Xbox gamepad': 'ein virtuelles Xbox-Gamepad',
    'pad: now driving ': 'pad: steuert jetzt ',
    'stream ended: ': 'Stream beendet: ',
    'pad: keyboard layout ': 'pad: Tastaturlayout ',
    'pad: released ': 'pad: losgelassen ',
    'ready: ': 'bereit: ',
    'beacon to: ': 'Beacon an: ',
}
