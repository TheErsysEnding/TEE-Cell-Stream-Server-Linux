#!/usr/bin/env python3
"""XMB artwork for "TEE Remote Play" - SHARP ANGULAR concept, blue.

Renders the two files the PS3 asks for:
  ICON0.PNG   320x176  - the tile in the XMB game column
  PIC1.PNG   1920x1080 - the full-screen background shown while that tile is selected

Both are looked at on a TELEVISION from two or three metres. That drives every decision here:
nothing thinner than roughly 5 px survives the console's scaler plus the set's sharpening, so the
mark is built from plates, not from lines.

Three things differ from tools/make-branding.py, and each of them is the point of this file:

1. BLUE, not orange. The author's other PS3 app ("TEE VANCED") already owns orange-on-black. The
   ground stays dark for the same reason it was dark before - the XMB draws its own white title
   over PIC1, and a bright background makes that title unreadable.

2. PIC1 keeps out of the XMB's own chrome. Measured off a photo of the real console, the XMB and
   the user's CFW paint over most of the frame; the previous design put the wordmark straight
   underneath the white app title. FORBIDDEN and PRIMARY below are those measurements, and the
   script asserts against them at render time rather than trusting the eye.

3. Sharp angular styling: 45-degree clipped corners instead of rounded ones, and ONE diagonal -
   a saturated blue blade that sweeps up behind the mark and stops, cut off square to its own
   axis, just short of the wordmark. Every edge in the piece is at 0, 45 or 90 degrees; the only
   curves left are the broadcast arcs, which mean something.

   Note what the blade CANNOT do: a 45-degree band and a tall left-aligned text block cannot be
   separated by a straight cut at any angle, because over the height of the block the band slides
   sideways by that same height and walks into the letters. The mark therefore sits ON the blade
   and the type sits clear of it, and the blade's reach is bounded by geometry - see blade().
"""

import pathlib
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

OUT = pathlib.Path(__file__).resolve().parent.parent / "branding"
ICON_PATH = OUT / "ICON0.PNG"
PIC_PATH = OUT / "PIC1.PNG"

# --------------------------------------------------------------- Palette ----
#
# Saturated blue against near-white, on a near-black navy. The two greys are cool on purpose: a
# neutral grey next to this blue reads as dirty yellow on a warm-set TV.
BLADE = (18, 86, 210)        # the diagonal itself - saturated, still dark enough to sit under white
EDGELINE = (122, 192, 255)   # the lit upper-left edge of the blade; the brightest blue in the piece
ACCENT = (74, 158, 255)      # the wordmark's blue - one step brighter than BLADE so type wins
ICE = (232, 240, 252)        # near-white; not 255 white, which blooms on a CRT/plasma
STEEL = (128, 154, 194)      # strapline
DIM = (100, 126, 166)        # version label - the quietest thing that is still meant to be READ
MARK_COLOUR = ICE           # das Zeichen selbst; ACCENT waere die blaue Variante
CHROME = (38, 66, 116)       # the tile's chamfered outline
NIGHT = (4, 6, 12)           # the cut-off tile corners

# Nothing legible is allowed below DIM. A television's contrast curve crushes the low end, so a
# "tasteful" 60/255 grey is simply gone from the sofa. Same reasoning holds the weakest broadcast
# arc at 110/255 further down instead of a prettier 60.

HEAVY = "/usr/share/fonts/opentype/montserrat/Montserrat-ExtraBold.otf"
MEDIUM = "/usr/share/fonts/opentype/montserrat/Montserrat-SemiBold.otf"

LINK = "linktr.ee/theersysending"
VERSION_LABEL = "V1.0"
# What the release is actually about, and the one thing worth claiming on a tile seen from a sofa.
# Short form on the 320 px tile, full form on the background where there is room for it.
CAPABILITY = "FULL HD 60 FPS"
CAPABILITY_LONG = "FULL HD 60 FPS SUPPORT"
PITCH = "STREAM PC GAMES ON PLAYSTATION 3"       # tile, all caps
PITCH_LONG = "Stream PC games on PlayStation 3"  # background, mixed case

# ------------------------------------------------------- XMB zone survey ----
#
# Measured from a photo of this app selected on the real console. The XMB (and this user's CFW)
# draws all of this ON TOP of PIC1. Decoration may run underneath; nothing legible may.
FORBIDDEN = {
    "icon column":    (0, 400, 760, 1080),
    "app title":      (760, 470, 1550, 560),   # white text - the collision this concept fixes
    "platform badge": (1180, 515, 1260, 555),
    "clock bar":      (1320, 70, 1910, 140),
    "network overlay": (1260, 915, 1800, 1020),
    "boost label":    (30, 960, 280, 1020),
}
PRIMARY = (780, 150, 1900, 450)     # the lockup lives here, centred on x=1340
SECONDARY = (800, 580, 1900, 900)   # quiet elements only
TILE_SAFE = (10, 10, 310, 166)      # 10 px inside every edge of the tile


def font(path, size):
    return ImageFont.truetype(path, size)


# ---------------------------------------------------------------- Ground ----

def vertical_gradient(size, top, bottom):
    """A dithered vertical ramp.

    A dark gradient only spans a handful of 8-bit values, so a straight ramp lands in wide flat
    bands with a visible step between them - obvious on a TV. One stage of ordered dithering
    scatters the rounding error and the steps disappear.
    """
    w, h = size
    BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        exact = [top[i] + (bottom[i] - top[i]) * t for i in range(3)]
        for x in range(w):
            threshold = (BAYER[y & 3][x & 3] + 0.5) / 16.0
            px[x, y] = tuple(int(exact[i]) + (1 if (exact[i] - int(exact[i])) > threshold else 0)
                             for i in range(3))
    return img


def glow(size, centre, radius, colour, strength):
    """A soft round light, as a blurred disc - that way it has no visible edge."""
    layer = Image.new("L", size, 0)
    ImageDraw.Draw(layer).ellipse(
        [centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius],
        fill=strength)
    layer = layer.filter(ImageFilter.GaussianBlur(radius * 0.55))
    return Image.new("RGB", size, colour), layer


def lay(base, mask, colour):
    """Paint `colour` through `mask` onto `base` (RGB in, RGB out)."""
    layer = Image.new("RGBA", base.size, colour + (255,))
    layer.putalpha(mask)
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


# ------------------------------------------------------------- Geometry -----

def aa(size, draw_fn, ss=4):
    """Draw hard-edged geometry oversampled, then shrink it once.

    Pillow does not antialias polygons. A 45-degree cut drawn at 1x staircases, and the console's
    scaler turns that staircase into crawling pixels. BOX, not LANCZOS: at an integer ratio BOX is
    an exact area average, while LANCZOS rings and leaves a dark halo around a flat plate.
    """
    layer = Image.new("RGBA", (size[0] * ss, size[1] * ss), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), ss)
    return layer.resize(size, Image.BOX)


def chamfer(rect, c):
    """A rectangle with all four corners clipped at 45 degrees - the concept's core shape."""
    x0, y0, x1, y1 = rect
    return [(x0 + c, y0), (x1 - c, y0), (x1, y0 + c), (x1, y1 - c),
            (x1 - c, y1), (x0 + c, y1), (x0, y1 - c), (x0, y0 + c)]


def band_mask(size, c_top, half_w, slope=-1.0, ss=3):
    """Mask of an infinite 45-degree band. `c_top` is where its centre line crosses y=0.

    slope=-1 leans the band forward ("/"): every row down moves it one pixel left.
    """
    w, h = size
    m = Image.new("L", (w * ss, h * ss), 0)
    y0, y1 = -2, h + 2
    pts = [(c_top + slope * y0 - half_w, y0), (c_top + slope * y0 + half_w, y0),
           (c_top + slope * y1 + half_w, y1), (c_top + slope * y1 - half_w, y1)]
    ImageDraw.Draw(m).polygon([(x * ss, y * ss) for x, y in pts], fill=255)
    return m.resize((w, h), Image.BOX)


BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def profile(n, stops):
    """Piecewise smoothstep down a row or a column, as floats. `stops` is [(position, level), ...].

    Smoothstep and not a linear ramp: a linear taper has a kink where it starts and where it stops,
    and on a near-black ground that kink shows up as a faint line running across the blade.
    """
    out = []
    for i in range(n):
        if i <= stops[0][0]:
            v = stops[0][1]
        elif i >= stops[-1][0]:
            v = stops[-1][1]
        else:
            for (a, va), (b, vb) in zip(stops, stops[1:]):
                if a <= i <= b:
                    t = 1.0 if b == a else (i - a) / (b - a)
                    t = t * t * (3 - 2 * t)
                    v = va + (vb - va) * t
                    break
        out.append(255.0 * v)
    return out


def v_mask(size, stops):
    """A vertical opacity profile - ordered-dithered, for the same reason the ground is.

    The blade's tail falls from solid blue to almost nothing across hundreds of rows. Composited
    from one flat alpha per row it contours exactly like an undithered gradient: the ground
    underneath is dithered and the plate lying on top of it is not, which looks worse than not
    tapering at all. Scattering the rounding error over a 4x4 cell costs one pass and fixes it.
    """
    w, h = size
    levels = profile(h, stops)
    m = Image.new("L", (w, h))
    px = m.load()
    for y in range(h):
        base = int(levels[y])
        frac = levels[y] - base
        for x in range(w):
            px[x, y] = base + (1 if frac > (BAYER[y & 3][x & 3] + 0.5) / 16.0 else 0)
    return m


def blade(img, c_top, half_w, tip, v_stops, edge_w, core_alpha):
    """THE diagonal. One, not five.

    A 45-degree band leaning "/" is the set of points with x + y between two bounds; `c_top` is
    where its centre line crosses y=0 and `half_w` is half its horizontal thickness.

    `tip` cuts the band with a line at right angles to itself (x - y = tip), which is the second
    45-degree angle in the piece and the only honest way to end a blade. It also does real work:
    the widest point the blade can ever reach is where that cut meets the band's leading edge, at
    x = (c_top + half_w + tip) / 2, and THAT is the number that keeps it off the wordmark. An
    earlier version faded the blade out horizontally instead; the fade read as haze, and a soft
    gradient is a promise about clearance that nothing in the code enforces.

    The vertical profile is what stops the blade turning into wallpaper: on PIC1 it drops to a
    tenth by the bottom edge, where the XMB stacks its game icons over the artwork.
    """
    size = img.size
    far = max(size) * 4                       # "half-plane", expressed as a very wide band
    keep = ImageChops.multiply(band_mask(size, tip - far, far, slope=+1.0), v_mask(size, v_stops))

    core = ImageChops.multiply(band_mask(size, c_top, half_w), keep)
    img = lay(img, core.point(lambda v: v * core_alpha // 255), BLADE)   # dither survives the scale

    # A lit line along the leading (upper-left) flank. Without it the blade is a flat field; with
    # it the shape has a direction, which is the whole point of putting a diagonal in.
    edge = ImageChops.multiply(band_mask(size, c_top - half_w + edge_w / 2.0, edge_w / 2.0), keep)
    return lay(img, edge, EDGELINE)


# ----------------------------------------------------------------- Type -----

def tracked_text(draw, xy, text, fnt, fill, tracking):
    """Letter-spaced text: Pillow has no tracking, and the wordmark needs it to read as a logo."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking
    return x - tracking


def tracked_width(draw, text, fnt, tracking):
    return sum(draw.textlength(c, font=fnt) for c in text) + tracking * (len(text) - 1)


def tracking_to_fit(draw, text, fnt, target_w):
    """The tracking that makes `text` exactly `target_w` wide.

    Several lines driven out to the same measure read as one designed block instead of three
    accidentally left-aligned lines.
    """
    return (target_w - tracked_width(draw, text, fnt, 0)) / max(1, len(text) - 1)


def ink_text(draw, x, ink_top, text, fnt, fill, tracking=0.0):
    """Draws so the INK starts at (x, ink_top); returns the ink box it actually covered.

    Pillow's origin is the ascender, and Montserrat's ascender sits well above the caps. Position
    by that and the vertical rhythm shifts on every size change. Measuring the real black box and
    subtracting the offset keeps the lines optically spaced - and hands back the true bounds, which
    is what the safe-area check needs.
    """
    bx0, by0, bx1, by1 = draw.textbbox((0, 0), text, font=fnt)
    origin = (x - bx0, ink_top - by0)
    if tracking:
        tracked_text(draw, origin, text, fnt, fill, tracking)
    else:
        draw.text(origin, text, font=fnt, fill=fill)
    return (x, ink_top, x + tracked_width(draw, text, fnt, tracking), ink_top + (by1 - by0))


def centred_text(draw, cx, ink_top, text, fnt, fill, tracking=0.0):
    w = tracked_width(draw, text, fnt, tracking)
    return ink_text(draw, cx - w / 2, ink_top, text, fnt, fill, tracking)


def right_text(draw, x_right, ink_top, text, fnt, fill, tracking=0.0):
    w = tracked_width(draw, text, fnt, tracking)
    return ink_text(draw, x_right - w, ink_top, text, fnt, fill, tracking)


# ----------------------------------------------------------------- Mark -----
#
# Base measurements of the mark, from its own top-left corner. Tile and background draw THE SAME
# geometry at different factors, so the two sizes cannot drift apart if someone tweaks it later.
SCREEN = (0, 0, 58, 41)          # the display, roughly 3:2
NECK = (23, 41, 36, 48)
FOOT = (13, 48, 45, 54)
SCREEN_CHAMFER = 7               # in the same units; ~1/6 of the screen height, so it reads at 1x
ARC_CENTRE = (58, 20)            # the arcs spring from the screen's right edge
ARCS = ((12, 7, 255), (21, 6, 178), (30, 5, 110))   # radius, stroke, opacity
MARK_W, MARK_H = 88, 54          # arcs included


def draw_mark(canvas, ox, oy, k, colour=None, alpha_scale=1.0, supersample=4,
              parts=("screen", "stand", "arcs")):
    colour = MARK_COLOUR if colour is None else colour
    """Monitor with a broadcast fan, drawn at 4x and scaled down.

    Pillow's `arc` does not antialias, and a hard-edged arc crawls as soon as the PS3 scales the
    tile up to a 1080p screen. Drawing oversized and shrinking once gives clean curves - and lets
    the stroke widths be stated in real small pixels without rounding to nothing.

    Angular treatment: the screen and the foot get 45-degree clipped corners instead of the radii
    the orange original used. The arcs stay curved. They are the one thing in the mark that means
    something - a signal leaving the display - and chevroning them turns the mark into decoration.
    """
    s = supersample
    layer = Image.new("RGBA", (canvas[0] * s, canvas[1] * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    p = lambda x, y: ((ox + x * k) * s, (oy + y * k) * s)
    fill = colour + (int(255 * alpha_scale),)

    def plate(rect, cut):
        d.polygon([p(x, y) for x, y in chamfer(rect, cut)], fill=fill)

    # `parts` exists because of the ghost mark on the background: a complete monitor at giant size
    # reads as an OBJECT standing behind the wordmark, which looks like an accident. The screen
    # alone is an abstract plane and stays what it is meant to be: structure.
    if "screen" in parts:
        plate(SCREEN, SCREEN_CHAMFER)
    if "stand" in parts:
        d.rectangle([p(NECK[0], NECK[1]), p(NECK[2], NECK[3])], fill=fill)
        plate(FOOT, 2)

    # Opacity falls off outwards so the eye reads a direction, not three rings. The weakest arc
    # stays at 110/255 rather than a prettier 60: a TV's contrast curve squashes the bottom end and
    # anything below that is simply absent from the sofa.
    cx, cy = ARC_CENTRE
    for radius, width, alpha in (ARCS if "arcs" in parts else ()):
        a, b = p(cx - radius, cy - radius), p(cx + radius, cy + radius)
        d.arc([a, b], start=-52, end=52,
              fill=colour + (int(alpha * alpha_scale),), width=max(1, int(width * k * s)))

    return layer.resize(canvas, Image.LANCZOS)


def sheared_bar(img, x0, y0, x1, y1, colour):
    """The rule under the wordmark, as a parallelogram leaning the same way as the blade.

    A plain rectangle here is the one square corner left in the design and it shows.
    """
    off = (y1 - y0)
    return Image.alpha_composite(
        img.convert("RGBA"),
        aa(img.size, lambda d, s: d.polygon(
            [(x * s, y * s) for x, y in
             ((x0 + off, y0), (x1 + off, y0), (x1, y1), (x0, y1))], fill=colour + (255,)))
    ).convert("RGB")


# ------------------------------------------------------------ Safe areas ----

def inside(box, area, slack=0):
    return (box[0] >= area[0] - slack and box[1] >= area[1] - slack
            and box[2] <= area[2] + slack and box[3] <= area[3] + slack)


def overlaps(box, area):
    return not (box[2] <= area[0] or box[0] >= area[2] or box[3] <= area[1] or box[1] >= area[3])


def check(label, legible, area, forbidden=None):
    """Assert every legible element sits inside `area` and clears every XMB overlay.

    Done by arithmetic on the measured ink boxes, not by looking at the render: a collision with
    the white XMB title is exactly the bug this concept exists to fix, and it is easy to miss by
    eye because the title is not in the PNG.
    """
    ok = True
    for name, box in legible:
        box = tuple(round(v) for v in box)
        note = ""
        if not inside(box, area):
            ok, note = False, "  <-- OUTSIDE SAFE AREA"
        for zone, rect in (forbidden or {}).items():
            if overlaps(box, rect):
                ok, note = False, f"  <-- HITS {zone}"
        print(f"    {name:<16} {box}{note}")
    print(f"    {label}: {'OK' if ok else 'FAILED'}")
    assert ok, f"{label} safe-area check failed"


# ------------------------------------------------------------------ Tile ----

def make_icon():
    W, H = 320, 176
    img = vertical_gradient((W, H), (14, 21, 40), (6, 9, 19))

    # The light sits under the mark, not in the middle: it lifts the blade off the ground and
    # leaves the right half dark, so the wordmark keeps its contrast.
    tint, mask = glow((W, H), (52, 96), 120, (26, 106, 255), 96)
    img = Image.composite(Image.blend(img, tint, 0.55), img, mask)

    # THE diagonal. Its centre line crosses y=0 at x=137, so it runs through the screen's centre
    # (44, 92). The cut at x - y = 40 puts the blade's tip at (107, 67) and its topmost corner at
    # (70, 29): ten pixels clear of the text column at 118, six clear of the strapline's ink,
    # without a single soft edge. The blade leaves through the bottom-left corner, so the bottom
    # strip - where the XMB writes the app's title right under the tile - stays quiet by itself.
    # No vertical taper at this size: the blade leaves through the LEFT edge between y=99 and
    # y=175 and only grazes the bottom-left corner, so the quiet strip is quiet already.
    img = blade(img, c_top=137, half_w=38, tip=40, v_stops=[(0, 1.0)],
                edge_w=3, core_alpha=235)

    img = Image.alpha_composite(img.convert("RGBA"), draw_mark((W, H), 15, 72, 1.0)).convert("RGB")
    d = ImageDraw.Draw(img)
    legible = []

    # The strapline goes at the TOP, centred across the full width, not at the bottom. The XMB
    # writes its own title immediately BELOW the tile; a line on the bottom edge sits practically
    # on that title and the two fight. The top edge is the only margin the XMB leaves alone.
    legible.append(("strapline", centred_text(d, W / 2, 13, PITCH, font(MEDIUM, 12), STEEL, 1.1)))

    # Text block: left at 118 (clear of the outermost arc, which ends at x=103), right at 302.
    TX, MEASURE = 118, 184

    legible.append(("TEE", ink_text(d, TX, 46, "TEE", font(HEAVY, 60), ACCENT, tracking=1.0)))

    remote = font(MEDIUM, 20)
    legible.append(("remote play", ink_text(d, TX, 104, "REMOTE PLAY", remote, ICE,
                                            tracking=tracking_to_fit(d, "REMOTE PLAY", remote, MEASURE))))

    # A rule instead of white space: it ties the two driven-out lines together and gives the
    # version label a shelf to sit on - from 3 m that matters more than it sounds.
    img = sheared_bar(img, TX, 130, TX + MEASURE - 4, 134, ACCENT)
    d = ImageDraw.Draw(img)

    # Right-aligned to the same measure so the block closes cleanly, and far enough above the
    # bottom edge to stay out of the XMB title's way.
    # The shelf carries two labels: what it can do on the left, which build it is on the right. They
    # share one baseline so the block still closes cleanly against the measure.
    shelf = font(MEDIUM, 13)
    legible.append(("capability", ink_text(d, TX, 143, CAPABILITY, shelf, DIM, tracking=1.0)))
    legible.append(("version", right_text(d, TX + MEASURE, 143, VERSION_LABEL, shelf,
                                          DIM, tracking=1.8)))
    # ...but only if they really do fit side by side; overlapping them would be worse than either
    cap_w = tracked_width(d, CAPABILITY, shelf, 1.0)
    ver_w = tracked_width(d, VERSION_LABEL, shelf, 1.8)
    assert cap_w + ver_w + 12 <= MEASURE, "tile shelf: %d + %d does not fit in %d" % (cap_w, ver_w, MEASURE)

    # The tile's own corners get clipped too - the concept applied to the frame itself. Cut with
    # near-black rather than transparency because the XMB composites the tile over its own
    # background and an alpha corner would show whatever is behind it.
    C = 15
    def frame(dr, s):
        for tri in (((0, 0), (C, 0), (0, C)), ((W - C, 0), (W, 0), (W, C)),
                    ((0, H - C), (0, H), (C, H)), ((W - C, H), (W, H), (W, H - C))):
            dr.polygon([(x * s, y * s) for x, y in tri], fill=NIGHT + (255,))
        dr.line([(x * s, y * s) for x, y in
                 chamfer((1, 1, W - 2, H - 2), C) + [chamfer((1, 1, W - 2, H - 2), C)[0]]],
                fill=CHROME + (255,), width=2 * s)
    img = Image.alpha_composite(img.convert("RGBA"), aa((W, H), frame)).convert("RGB")

    print("  ICON0")
    check("tile", legible, TILE_SAFE)
    img.save(ICON_PATH, optimize=True)
    return ICON_PATH, img.size


# ------------------------------------------------------------ Background ----

def make_background():
    W, H = 1920, 1080
    img = vertical_gradient((W, H), (12, 18, 36), (5, 8, 17))
    tint, mask = glow((W, H), (1000, 330), 560, (26, 106, 255), 52)
    img = Image.composite(Image.blend(img, tint, 0.5), img, mask)

    # A huge, very pale screen bleeding off BOTH the right and the bottom edge: only its clipped
    # top-left corner is in frame, so it reads as a plane, not as an object standing behind the
    # type. Parked below the lockup and below the XMB title band, where its faint edge crosses
    # nothing legible. 0.13 rather than a subtler 0.05 for the same reason the arcs stop at 110:
    # a TV eats the bottom of the range and anything fainter is not there at all.
    ghost = draw_mark((W, H), 1430, 578, 20.0, colour=ACCENT, alpha_scale=0.13, parts=("screen",))
    img = Image.alpha_composite(img.convert("RGBA"), ghost).convert("RGB")

    # Same blade, same 45 degrees, at background scale. Centre line through the screen's centre
    # (1004, 274). The tip lands at (1312, 96) - twenty pixels clear of the text column at 1332,
    # and eight clear of the clock overlay at x=1320, which it passes just underneath. Here the
    # blade enters through the TOP edge rather than starting inside the frame; there is room for
    # that at 1080p, and it stops the tip from looking like a floating shard.
    img = blade(img, c_top=1278, half_w=130, tip=1216,
                v_stops=[(0, 1.0), (430, 1.0), (800, 0.16), (1080, 0.10)],
                edge_w=7, core_alpha=235)

    # Lockup: mark at 4.2x (882..1252), an 80 px gap, then the text column (1332..1797). The
    # measure is 465 because that is how wide the strapline sets - the longest line owns the
    # column, and the two driven-out lines above it are trimmed to match. The whole block spans
    # 882..1797 and is therefore centred on x=1340, the
    # middle of the PRIMARY safe area - NOT the middle of the 1920 canvas, because the left third
    # is buried under the XMB's icon column.
    img = Image.alpha_composite(img.convert("RGBA"),
                                draw_mark((W, H), 882, 188, 4.2)).convert("RGB")
    d = ImageDraw.Draw(img)
    legible = []

    TX, MEASURE = 1332, 465

    legible.append(("TEE", ink_text(d, TX, 178, "TEE", font(HEAVY, 116), ACCENT, tracking=2.5)))

    remote = font(MEDIUM, 44)
    legible.append(("remote play", ink_text(d, TX, 288, "REMOTE PLAY", remote, ICE,
                                            tracking=tracking_to_fit(d, "REMOTE PLAY", remote, MEASURE))))

    img = sheared_bar(img, TX, 336, TX + MEASURE - 8, 344, ACCENT)
    d = ImageDraw.Draw(img)

    # 26, not 30: at 30 this line runs 77 px past the block's right edge and the driven-out
    # measure stops reading as a block at all.
    legible.append(("strapline", ink_text(d, TX, 362, PITCH_LONG, font(MEDIUM, 26), STEEL)))
    shelf = font(MEDIUM, 24)
    legible.append(("capability", ink_text(d, TX, 408, CAPABILITY_LONG, shelf, DIM, tracking=1.5)))
    legible.append(("version", right_text(d, TX + MEASURE, 408, VERSION_LABEL, shelf,
                                          DIM, tracking=3.0)))

    print("  PIC1  primary block")
    check("lockup", legible, PRIMARY, FORBIDDEN)

    # The link is the one quiet element, so it may use the secondary band. It has to clear the
    # CFW's network overlay, which starts at y=915.
    quiet = [("link", right_text(d, 1880, 856, LINK, font(MEDIUM, 24), DIM))]
    print("  PIC1  secondary")
    check("footer", quiet, SECONDARY, FORBIDDEN)

    img.save(PIC_PATH, optimize=True)
    return PIC_PATH, img.size


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for maker, expect in ((make_icon, (320, 176)), (make_background, (1920, 1080))):
        path, size = maker()
        assert size == expect, f"{path.name} is {size}, expected {expect}"
        print(f"  {path.name:<26} {size[0]}x{size[1]}  {path.stat().st_size:>8} bytes\n")
