#!/usr/bin/env python3
"""Builds the icons in this directory from the original cell-stream.ico.

  hicolor/<size>/apps/tee-cell-stream-server.png        the app icon (the ico's 256px entry, downscaled)
  hicolor/<size>/apps/tee-cell-stream-server-idle.png   grey dot   - the tray while waiting for a PS3
  hicolor/<size>/apps/tee-cell-stream-server-live.png   green dot  - the tray while a PS3 streams
  tee-cell-stream-server*.png                           unthemed copies, for icon-theme search paths that
                                                        do not read a hicolor tree (St.IconTheme in gnome-shell)

The Windows server drew its tray dot at run time (a 12px circle on a 16px canvas); a StatusNotifierItem
refers to icons by name, so they are pre-drawn here in the same colours. Run: python3 data/icons/make-icons.py
"""

import math
import os
import struct
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

try:
    import cairo
except ImportError:   # python3-gi normally brings it; fall back to plain pixel maths below
    cairo = None

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(HERE, "..", "..", "upstream", "server", "cell-stream.ico")
APP_ICON = "tee-cell-stream-server"
APP_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)
DOT_SIZES = (22, 24, 32, 48)
DOT_COLOURS = {"idle": (0x8A, 0x8A, 0x8A), "live": (0x3D, 0xD5, 0x6D)}   # same as MainWindow.SetTrayIcon
UNTHEMED_DOT_SIZE = 48
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def largest_png_in_ico(path: str) -> tuple[int, bytes]:
    """The ico is a directory of PNG-compressed entries; slice the biggest one out untouched."""
    with open(path, "rb") as handle:
        data = handle.read()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError("not an .ico file: " + path)
    best = None
    for index in range(count):
        width, height, _colours, _reserved, _planes, _bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        width = width or 256   # 0 means 256 in the directory's single byte
        height = height or 256
        if best is None or width * height > best[0] * best[1]:
            best = (width, height, offset, size)
    width, _height, offset, size = best
    png = data[offset:offset + size]
    if not png.startswith(PNG_MAGIC):
        raise ValueError("the ico entry is a BMP, not PNG - extend make-icons.py")
    return width, png


def write_app_icons(png: bytes, source_size: int) -> None:
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    source = loader.get_pixbuf()
    for size in APP_SIZES:
        target = os.path.join(HERE, "hicolor", "%dx%d" % (size, size), "apps", APP_ICON + ".png")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if size == source_size:
            with open(target, "wb") as handle:
                handle.write(png)   # lossless: the original bytes
        else:
            scaled = source.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
            scaled.savev(target, "png", [], [])
    with open(os.path.join(HERE, APP_ICON + ".png"), "wb") as handle:
        handle.write(png)


def draw_dot_cairo(size: int, rgb: tuple[int, int, int], target: str) -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    context = cairo.Context(surface)
    margin = size * 2 / 16   # the original: FillEllipse(2, 2, 12, 12) on 16px
    context.arc(size / 2, size / 2, size / 2 - margin, 0, 2 * math.pi)
    context.set_source_rgb(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    context.fill()
    surface.write_to_png(target)


def draw_dot_pixbuf(size: int, rgb: tuple[int, int, int], target: str) -> None:
    """No cairo: rasterise the circle by hand with 4x4 supersampling for a smooth edge."""
    radius = size / 2 - size * 2 / 16
    centre = size / 2
    samples = 4
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            inside = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = x + (sx + 0.5) / samples - centre
                    py = y + (sy + 0.5) / samples - centre
                    if px * px + py * py <= radius * radius:
                        inside += 1
            alpha = round(255 * inside / (samples * samples))
            pixels += bytes((rgb[0], rgb[1], rgb[2], alpha))
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(pixels)), GdkPixbuf.Colorspace.RGB, True, 8, size, size, size * 4)
    pixbuf.savev(target, "png", [], [])


def write_dot_icons() -> None:
    draw = draw_dot_cairo if cairo is not None else draw_dot_pixbuf
    for state, rgb in DOT_COLOURS.items():
        name = "%s-%s.png" % (APP_ICON, state)
        for size in DOT_SIZES:
            target = os.path.join(HERE, "hicolor", "%dx%d" % (size, size), "apps", name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            draw(size, rgb, target)
        draw(UNTHEMED_DOT_SIZE, rgb, os.path.join(HERE, name))


def main() -> int:
    source_size, png = largest_png_in_ico(ICO_PATH)
    write_app_icons(png, source_size)
    write_dot_icons()
    print("icons written to " + HERE + " (app %dpx source, dots via %s)" % (source_size, "cairo" if cairo else "pixbuf"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
