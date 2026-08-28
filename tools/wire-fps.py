#!/usr/bin/env python3
"""Count the video frames actually leaving for the PS3, straight off the wire.

The console's own stats panel reports `receivedFps` — how many frames ARRIVE — which stays at 60 even
when the picture judders, because a repeated frame counts just as much as a new one. This tool measures
what really goes out, and its gap histogram separates the two causes that look identical on screen:

  gaps spread smoothly around some value  -> the SOURCE produces that rate (the game, or the compositor)
  gaps quantised onto the 1/fps grid      -> frames are being DROPPED somewhere before us

Needs root for the capture (tcpdump). Usage:

    sudo tools/wire-fps.py 10.42.0.237            # 10 s
    sudo tools/wire-fps.py 10.42.0.237 -d 60      # a minute, with per-second rates
"""

import argparse
import collections
import os
import statistics
import struct
import subprocess
import sys
import tempfile

BEACON_PORT = 38311
VF_HEADER = 20          # [0]='V' [1]='F' [2..5]=frameId [6..7]=fragIndex [8..9]=fragCount ...
FRAME_INTERVAL_MS = 1000.0 / 60


def capture(host: str, seconds: int, interface: str | None) -> bytes:
    """tcpdump the first fragment of every video frame (fragIndex == 0) for `seconds`."""
    # udp[8],[9] are the payload's 'V','F'; udp[14:2] is the payload's fragIndex - one packet per frame
    rule = ("udp and dst host %s and dst port %d and udp[8]==0x56 and udp[9]==0x46 and udp[14:2]==0"
            % (host, BEACON_PORT))
    with tempfile.NamedTemporaryFile(suffix=".pcap", delete=False) as handle:
        path = handle.name
    command = ["tcpdump", "-n", "-s", "96", "-w", path]
    if interface:
        command += ["-i", interface]
    command += [rule]
    try:
        subprocess.run(["timeout", str(seconds + 2)] + command, capture_output=True, timeout=seconds + 20)
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def frame_times(pcap: bytes) -> list[float]:
    """Arrival timestamps of every frame's first fragment, in seconds."""
    if len(pcap) < 24:
        return []
    nanoseconds = struct.unpack("<I", pcap[:4])[0] == 0xA1B23C4D
    times, offset = [], 24
    while offset + 16 <= len(pcap):
        seconds, fraction, included, _original = struct.unpack("<IIII", pcap[offset:offset + 16])
        offset += 16
        packet = pcap[offset:offset + included]
        offset += included
        if len(packet) < 14 + 20 + 8 + 10:
            continue
        header_length = (packet[14] & 0x0F) * 4
        payload = packet[14 + header_length + 8:]
        if len(payload) < 10 or payload[0:2] != b"VF":
            continue
        times.append(seconds + fraction / (1e9 if nanoseconds else 1e6))
    return times


def report(times: list[float], per_second: bool) -> int:
    if len(times) < 3:
        print("Keine Videobilder gesehen. Streamt die PS3 gerade, und stimmt die Adresse?")
        return 1

    duration = times[-1] - times[0]
    print("Bilder:   %d in %.1f s = %.1f fps" % (len(times), duration, len(times) / duration))

    if per_second:
        start = times[0]
        counts = collections.Counter(int(t - start) for t in times)
        row = [counts.get(i, 0) for i in range(int(duration) + 1)]
        print("fps je Sekunde:")
        for i in range(0, len(row), 20):
            print("  s%02d: %s" % (i, " ".join("%2d" % v for v in row[i:i + 20])))

    gaps = [(times[i + 1] - times[i]) * 1000 for i in range(len(times) - 1)]
    print("Abstände: Median %.1f ms, Mittel %.1f ms, längster %.1f ms"
          % (statistics.median(gaps), statistics.mean(gaps), max(gaps)))

    # a gap within +-15 % of a whole number of frame intervals is "on the grid"
    on_grid = 0
    for gap in gaps:
        multiple = round(gap / FRAME_INTERVAL_MS)
        if multiple >= 1 and abs(gap - multiple * FRAME_INTERVAL_MS) <= 0.15 * FRAME_INTERVAL_MS:
            on_grid += 1
    share = on_grid / len(gaps)

    histogram = collections.Counter(round(g) for g in gaps if g < 5 * FRAME_INTERVAL_MS)
    print("Histogramm (ms):", ", ".join("%d:%dx" % (k, v) for k, v in sorted(histogram.items())[:12]))

    print()
    if share >= 0.8:
        missing = 1 - (len(times) / duration) / 60
        print("Befund:   Die Abstände liegen zu %.0f %% auf dem 60-Hz-Raster." % (share * 100))
        if missing > 0.05:
            print("          Es fallen also Bilder aus (~%.0f %% fehlen) - die Quelle rendert nicht langsamer." % (missing * 100))
            print("          Verdächtig: Bildschirmaufnahme des Compositors bei Vollbild-Anwendungen.")
            print("          Test: die Anwendung auf randloses Fenster stellen und erneut messen.")
        else:
            print("          Die Bildrate ist sauber - hier fehlt nichts.")
    else:
        print("Befund:   Die Abstände sind gleichmäßig verteilt (nur %.0f %% auf dem Raster)." % (share * 100))
        print("          Die Quelle liefert wirklich diese Rate; es wird nichts verworfen.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="Adresse der PS3, z. B. 10.42.0.237")
    parser.add_argument("-d", "--duration", type=int, default=10, help="Messdauer in Sekunden (Vorgabe 10)")
    parser.add_argument("-i", "--interface", help="Netzwerkkarte (Vorgabe: die von tcpdump gewählte)")
    arguments = parser.parse_args()

    if os.geteuid() != 0:
        print("Braucht Root für die Aufzeichnung:  sudo %s %s" % (sys.argv[0], arguments.host))
        return 1

    print("Messe %d s Richtung %s ..." % (arguments.duration, arguments.host))
    return report(frame_times(capture(arguments.host, arguments.duration, arguments.interface)),
                  arguments.duration >= 20)


if __name__ == "__main__":
    sys.exit(main())
