#!/usr/bin/env python3
"""Lesen und Schreiben von PARAM.SFO - der Datei, aus der die PS3 den Namen im XMB nimmt.

Das Format ist schlicht: ein Kopf, eine Indextabelle mit fester Satzlaenge, dann eine
Tabelle der Schluesselnamen und eine der Werte. Der Haken steckt im Feld `max_len`: jeder
Wert hat eine feste Huelle, die beim Schreiben mit Nullbytes aufgefuellt wird. Wer den
Titel aendert, darf diese Huelle NICHT verkleinern - sonst verschieben sich alle
nachfolgenden Datenversaetze und Sonys Lader liest Unsinn.

Benutzung:
    sfo.py show   PARAM.SFO
    sfo.py set    PARAM.SFO NEU.SFO TITLE="TEE Remote Play" TITLE_ID=TEERMPLY1
"""

import struct
import sys

FMT_UTF8_SPECIAL = 0x0004   # ohne abschliessende Null (kommt in PARAM.SFO praktisch nicht vor)
FMT_UTF8 = 0x0204           # nullterminierter Text
FMT_UINT32 = 0x0404


def read(path):
    """Liefert eine geordnete Abbildung Schluessel -> (Wert, Format, max_len)."""
    d = open(path, "rb").read()
    magic, version, key_off, data_off, count = struct.unpack_from("<IIIII", d, 0)
    if magic != 0x46535000:  # "\0PSF"
        raise ValueError(f"{path}: keine PARAM.SFO (Magic {magic:#x})")
    out = {}
    for i in range(count):
        ko, fmt, ln, mx, do = struct.unpack_from("<HHIII", d, 0x14 + i * 0x10)
        start = key_off + ko
        key = d[start:d.index(b"\0", start)].decode("ascii")
        raw = d[data_off + do: data_off + do + ln]
        if fmt == FMT_UINT32:
            val = struct.unpack("<I", raw[:4])[0]
        else:
            val = raw.rstrip(b"\0").decode("utf-8")
        out[key] = (val, fmt, mx)
    return out


def write(path, entries):
    """Baut die Datei vollstaendig neu auf - einfacher und sicherer als Bytes zu flicken."""
    keys = sorted(entries)  # der Lader erwartet die Indextabelle alphabetisch sortiert
    key_blob, data_blob, index = bytearray(), bytearray(), bytearray()

    for key in keys:
        val, fmt, mx = entries[key]
        if fmt == FMT_UINT32:
            raw, ln = struct.pack("<I", int(val)), 4
        else:
            enc = str(val).encode("utf-8") + b"\0"
            if len(enc) > mx:
                raise ValueError(f"{key}: {len(enc)} Bytes passen nicht in die Huelle von {mx}")
            raw, ln = enc, len(enc)
        index += struct.pack("<HHIII", len(key_blob), fmt, ln, mx, len(data_blob))
        key_blob += key.encode("ascii") + b"\0"
        data_blob += raw + b"\0" * (mx - len(raw))

    # Die Schluesseltabelle wird auf 4 Byte ausgerichtet, bevor die Daten beginnen.
    key_blob += b"\0" * (-len(key_blob) % 4)
    key_off = 0x14 + len(index)
    header = struct.pack("<IIIII", 0x46535000, 0x00000101, key_off, key_off + len(key_blob), len(keys))
    open(path, "wb").write(header + bytes(index) + bytes(key_blob) + bytes(data_blob))


def main(argv):
    if len(argv) >= 3 and argv[1] == "show":
        for k, (v, fmt, mx) in read(argv[2]).items():
            print(f"  {k:<16} {str(v):<40} (Huelle {mx} Bytes)")
        return 0
    if len(argv) >= 4 and argv[1] == "set":
        entries = read(argv[2])
        for pair in argv[4:]:
            k, _, v = pair.partition("=")
            if k not in entries:
                raise SystemExit(f"Schluessel {k} steht nicht in der Vorlage")
            _, fmt, mx = entries[k]
            entries[k] = (int(v) if fmt == FMT_UINT32 else v, fmt, mx)
        write(argv[3], entries)
        print(f"  geschrieben: {argv[3]}")
        for k, (v, fmt, mx) in read(argv[3]).items():
            print(f"    {k:<16} {v}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
