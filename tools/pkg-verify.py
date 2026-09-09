#!/usr/bin/env python3
"""Packt ein PS3-.pkg aus und prueft die enthaltenen Dateien gegen Vorlagen.

Der Kopf allein sagt nur, dass die Groessenangaben zueinander passen. Er sagt nicht, ob wirklich
UNSERE Grafik und UNSERE PARAM.SFO drinstecken - die koennten vom Bauwerkzeug aus einem alten
Zwischenstand kopiert worden sein, und das faellt erst auf der Konsole auf. Deshalb hier der
komplette Weg: entschluesseln, Inhaltsverzeichnis lesen, Pruefsummen vergleichen.

Der Nutzdatenbereich ist verschluesselt, und zwar auf ZWEI Arten, je nach Typ im Kopf bei 0x04:
Retail-Pakete (0x80000001) mit AES-128-CTR und dem oeffentlich bekannten Paketschluessel,
Debug-Pakete (0x00000001) dagegen mit einem SHA-1-Schluesselstrom. Selbst gebaute Homebrew-Pakete
sind Debug-Pakete - genau daran ist mein erster Versuch mit AES gescheitert.

    pkg-verify.py PAKET.pkg [--erwarte NAME=DATEI ...] [--auspacken VERZEICHNIS]
"""

import hashlib
import pathlib
import struct
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PKG_AES_KEY = bytes.fromhex("2e7b71d7c9c9a14ea3221f188828b8f8")


def debug_stream(header, data):
    """Der Schluesselstrom der Debug-Pakete.

    Kein Blockchiffre: ein 64-Byte-Puffer wird wiederholt durch SHA-1 geschickt, die ersten 16
    Bytes des Ergebnisses sind der Strom fuer den naechsten Block, und danach wird der Zaehler in
    den letzten 8 Bytes des Puffers erhoeht. Der Puffer wird aus dem Kopf bei 0x60 aufgebaut,
    wobei beide Haelften doppelt eingetragen werden - das sieht nach einem Fehler aus, ist aber so
    gewollt und muss genau so nachgebaut werden.
    """
    key = bytearray(0x40)
    key[0x00:0x08] = header[0x60:0x68]
    key[0x08:0x10] = header[0x60:0x68]
    key[0x10:0x18] = header[0x68:0x70]
    key[0x18:0x20] = header[0x68:0x70]
    out = bytearray()
    for i in range(0, len(data), 16):
        h = hashlib.sha1(bytes(key)).digest()
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, h))
        key[0x38:0x40] = (int.from_bytes(key[0x38:0x40], "big") + 1).to_bytes(8, "big")
    return bytes(out)


def unpack(path):
    d = path.read_bytes()
    magic, = struct.unpack_from(">I", d, 0)
    if magic != 0x7F504B47:
        raise ValueError(f"kein PS3-Paket (Magic {magic:#010x})")
    item_count, = struct.unpack_from(">I", d, 0x14)
    total, data_off, data_size = struct.unpack_from(">QQQ", d, 0x18)
    cid = d[0x30:0x60].rstrip(b"\0").decode()
    iv = d[0x70:0x80]

    if total != len(d):
        raise ValueError(f"Kopf sagt {total} Bytes, Datei hat {len(d)}")

    pkg_type, = struct.unpack_from(">I", d, 0x04)
    enc = d[data_off:data_off + data_size]
    if pkg_type == 0x80000001:
        dec = Cipher(algorithms.AES(PKG_AES_KEY), modes.CTR(iv)).decryptor()
        body = dec.update(enc) + dec.finalize()
        mode = "retail / AES-128-CTR"
    else:
        body = debug_stream(d, enc)
        mode = "debug / SHA-1-Strom"

    items = []
    for i in range(item_count):
        no, ns, off, size, flags, _ = struct.unpack_from(">IIQQII", body, i * 0x20)
        name = body[no:no + ns].decode("utf-8", "replace")
        items.append({"name": name, "off": off, "size": size,
                      "dir": (flags & 0xFF) == 4,
                      "data": None if (flags & 0xFF) == 4 else body[off:off + size]})
    return {"content_id": cid, "total": total, "items": items, "mode": mode}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    args = argv[2:]
    expect = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("--"))
    outdir = None
    if "--auspacken" in args:
        outdir = pathlib.Path(args[args.index("--auspacken") + 1])
        outdir.mkdir(parents=True, exist_ok=True)

    pkg = unpack(pathlib.Path(argv[1]))
    print(f"  Content-ID  {pkg['content_id']}")
    print(f"  Groesse     {pkg['total']:,} Bytes")
    print(f"  Verfahren   {pkg['mode']}")
    print(f"  Inhalt:")
    ok = True
    for it in pkg["items"]:
        if it["dir"]:
            print(f"    {'<VERZ>':>12}  {it['name']}/")
            continue
        h = hashlib.sha256(it["data"]).hexdigest()
        line = f"    {it['size']:>12,}  {it['name']:<16} {h[:16]}"
        if it["name"] in expect:
            want = hashlib.sha256(pathlib.Path(expect[it["name"]]).read_bytes()).hexdigest()
            same = want == h
            ok &= same
            line += "   " + ("identisch mit Vorlage" if same else f"ABWEICHUNG (erwartet {want[:16]})")
        print(line)
        if outdir and it["name"]:
            dest = outdir / it["name"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(it["data"])
    if expect:
        print(f"  -> {'alle geprueften Dateien stimmen' if ok else 'MINDESTENS EINE WEICHT AB'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
