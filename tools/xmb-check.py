#!/usr/bin/env python3
"""Prueft eine PIC1.PNG gegen die Bereiche, die das XMB selbst ueberschreibt.

Die Zonen sind nicht geschaetzt, sondern aus einem Bildschirmfoto der laufenden App gemessen:
gesucht wurden echte neutralweisse Pixel (Saettigungsfenster 12), damit die weisse Schrift des
XMB von unserer cremefarbenen Wortmarke getrennt bleibt - die ist auch hell, aber 28 Stufen
vom Neutral entfernt und wuerde eine naivere Suche verfaelschen.

    xmb-check.py PIC1.PNG [ausgabe.png]

Meldet, wie viel auffaellige Bildinformation in jeder verbotenen Zone liegt, und legt daneben
ein Kontrollbild ab, in dem die Zonen eingezeichnet und die Titelzeile nachgestellt sind.
"""

import sys
import pathlib
from PIL import Image, ImageDraw, ImageFont

# (Name, x0, y0, x1, y1, ob ein Treffer wirklich schlimm ist)
ZONES = [
    ("Titelzeile + Datum",  762,  491, 1534,  539, True),
    ("Uhrleiste",          1628,   89, 1804,  118, True),
    ("Netz-Overlay",       1467,  928, 1777, 1005, False),
    ("Boost-Modus",          58,  960,  260, 1005, False),
    ("Spielespalte",          0,  400,  760, 1080, False),
]
SAFE = (780, 150, 1900, 450)

MED = "/usr/share/fonts/opentype/montserrat/Montserrat-SemiBold.otf"
TITLE = "TEE Remote Play - PC Games on PS3 V0.1 Beta"


def busy(img, box):
    """Anteil der Pixel in `box`, die sich deutlich vom dunklen Grund abheben.

    Ein Verlauf oder eine blasse Struktur darf dort liegen - das ueberschreibt das XMB einfach.
    Gemeint sind Elemente, die gelesen werden SOLLEN, und die sind hell. Schwelle 96 liegt
    bewusst ueber dem, was ein dunkler Grund je erreicht, und unter der schwaechsten Farbe,
    die auf einem Fernseher noch sichtbar ist.
    """
    crop = img.crop(box).convert("L")
    hist = crop.histogram()
    return sum(hist[97:]) / max(1, sum(hist))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    src = pathlib.Path(argv[1])
    out = pathlib.Path(argv[2]) if len(argv) > 2 else src.with_name(src.stem + "-check.png")

    img = Image.open(src).convert("RGB")
    if img.size != (1920, 1080):
        print(f"  WARNUNG: {img.size} statt (1920, 1080)")

    print(f"  {src.name}")
    worst = 0.0
    for name, x0, y0, x1, y1, hard in ZONES:
        frac = busy(img, (x0, y0, x1, y1))
        if hard:
            worst = max(worst, frac)
        flag = "  <-- Kollision" if (hard and frac > 0.02) else ""
        print(f"    {name:<22} {frac*100:5.1f} % hell{'  (hart)' if hard else ''}{flag}")

    vis = img.copy()
    d = ImageDraw.Draw(vis, "RGBA")
    d.rectangle(SAFE, outline=(60, 220, 90, 255), width=3)
    d.text((SAFE[0] + 8, SAFE[1] + 6), "sicherer Bereich", fill=(60, 220, 90, 255),
           font=ImageFont.truetype(MED, 20))
    for name, x0, y0, x1, y1, hard in ZONES:
        d.rectangle((x0, y0, x1, y1), fill=(255, 40, 40, 46) if hard else (255, 170, 40, 30),
                    outline=(255, 60, 60, 200) if hard else (255, 170, 40, 150), width=2)
    # Die Titelzeile so nachstellen, wie das XMB sie zeichnet - Groesse aus dem Foto abgeleitet.
    d.text((762, 494), TITLE, font=ImageFont.truetype(MED, 34), fill=(238, 238, 240, 255))
    vis.save(out, optimize=True)
    print(f"    Kontrollbild: {out}")
    return 0 if worst <= 0.02 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
