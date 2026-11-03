"""Greyscale bitmaps: drawing text from the glyph outlines in font.json,
resizing, rotating, and reducing to one bit per dot.

The rasterizer follows ab_glyph's (exact-area coverage accumulated along each
row) and the resize follows the `image` crate's separable filters, so the
results match the Rust build's dot for dot or very nearly.
"""

import json
import math
import os
from array import array
from itertools import accumulate

_HERE = os.path.dirname(os.path.abspath(__file__))


class Gray:
    """An 8-bit greyscale image, row-major, 0 = black, 255 = white."""

    __slots__ = ("w", "h", "px")

    def __init__(self, w, h, fill=255, px=None):
        self.w = w
        self.h = h
        self.px = px if px is not None else bytearray([fill]) * (w * h)


class Font:
    def __init__(self, path=os.path.join(_HERE, "font.json")):
        with open(path, encoding="ascii") as f:
            doc = json.load(f)
        self.ascent = doc["ascent"]
        self.descent = doc["descent"]
        self.glyphs = {}
        for ch, (adv, contours) in doc["glyphs"].items():
            self.glyphs[ch] = self._glyph(adv, contours)
        self.notdef = self._glyph(*doc["notdef"])

    @staticmethod
    def _glyph(adv, contours):
        pts = [[(c[i], c[i + 1], c[i + 2]) for i in range(0, len(c), 3)] for c in contours]
        curves = _curves(pts)
        xs = [p[0] for c in pts for p in c]
        ys = [p[1] for c in pts for p in c]
        bounds = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        return adv, curves, bounds

    def glyph(self, ch):
        return self.glyphs.get(ch, self.notdef)

    def height_unscaled(self):
        return self.ascent - self.descent


_FONT = None


def font():
    global _FONT
    if _FONT is None:
        _FONT = Font()
    return _FONT


def _curves(contours):
    """TrueType points -> ("L", p0, p1) / ("Q", p0, c, p1) curves, building
    implied on-curve points as ttf-parser does."""
    out = []
    for contour in contours:
        first_on = first_off = last_off = None
        last = move = None

        def move_to(p):
            nonlocal last, move
            last = move = p

        def line_to(p):
            nonlocal last
            out.append(("L", last, p))
            last = p

        def quad_to(c, p):
            nonlocal last
            out.append(("Q", last, c, p))
            last = p

        n = len(contour)
        for i, (x, y, on) in enumerate(contour):
            p = (float(x), float(y))
            if first_on is None:
                if on:
                    first_on = p
                    move_to(p)
                elif first_off is not None:
                    mid = ((first_off[0] + p[0]) / 2, (first_off[1] + p[1]) / 2)
                    first_on = mid
                    last_off = p
                    move_to(mid)
                else:
                    first_off = p
            elif last_off is not None:
                if on:
                    quad_to(last_off, p)
                    last_off = None
                else:
                    mid = ((last_off[0] + p[0]) / 2, (last_off[1] + p[1]) / 2)
                    quad_to(last_off, mid)
                    last_off = p
            elif on:
                line_to(p)
            else:
                last_off = p
            if i == n - 1:
                if first_off is not None and last_off is not None:
                    mid = ((last_off[0] + first_off[0]) / 2, (last_off[1] + first_off[1]) / 2)
                    quad_to(last_off, mid)
                    last_off = None
                if first_on is not None and first_off is not None:
                    quad_to(first_off, first_on)
                elif first_on is not None and last_off is not None:
                    quad_to(last_off, first_on)
                elif move is not None:
                    out.append(("L", last, move))
    return out


class _Coverage:
    """Signed-area accumulation buffer (ab_glyph_rasterizer)."""

    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.a = [0.0] * (w * h + 4)

    def line(self, p0, p1):
        if abs(p0[1] - p1[1]) <= 1.1920929e-07:
            return
        if p0[1] < p1[1]:
            d_ir = 1.0
        else:
            d_ir = -1.0
            p0, p1 = p1, p0
        a = self.a
        w = self.w
        dxdy = (p1[0] - p0[0]) / (p1[1] - p0[1])
        x = p0[0]
        y0 = int(p0[1]) if p0[1] > 0 else 0
        if p0[1] < 0.0:
            x -= p0[1] * dxdy
        y_end = min(self.h, int(math.ceil(p1[1])))
        size = len(a)
        for y in range(y0, y_end):
            linestart = y * w
            dy = min(y + 1.0, p1[1]) - max(float(y), p0[1])
            xnext = x + dxdy * dy
            d = dy * d_ir
            if x < xnext:
                x0, x1 = x, xnext
            else:
                x0, x1 = xnext, x
            x0floor = math.floor(x0)
            x0i = int(x0floor)
            x1ceil = math.ceil(x1)
            x1i = int(x1ceil)
            i0 = linestart + x0i
            if i0 < 0:
                continue  # as in ab_glyph_rasterizer, x is not advanced
            if x1i <= x0i + 1:
                xmf = 0.5 * (x + xnext) - x0floor
                if i0 + 1 < size:
                    a[i0] += d - d * xmf
                    a[i0 + 1] += d * xmf
            else:
                s = 1.0 / (x1 - x0)
                x0f = x0 - x0floor
                a0 = 0.5 * s * (1.0 - x0f) * (1.0 - x0f)
                x1f = x1 - x1ceil + 1.0
                am = 0.5 * s * x1f * x1f
                if linestart + x1i >= size:
                    continue
                a[i0] += d * a0
                if x1i == x0i + 2:
                    a[i0 + 1] += d * (1.0 - a0 - am)
                else:
                    a1 = s * (1.5 - x0f)
                    a[i0 + 1] += d * (a1 - a0)
                    ds = d * s
                    for xi in range(linestart + x0i + 2, linestart + x1i - 1):
                        a[xi] += ds
                    a2 = a1 + (x1i - x0i - 3) * s
                    a[linestart + x1i - 1] += d * (1.0 - a2 - am)
                a[linestart + x1i] += d * am
            x = xnext

    def quad(self, p0, p1, p2):
        devx = p0[0] - 2.0 * p1[0] + p2[0]
        devy = p0[1] - 2.0 * p1[1] + p2[1]
        devsq = devx * devx + devy * devy
        if devsq < 0.333:
            self.line(p0, p2)
            return
        n = 1 + int(math.floor(math.sqrt(math.sqrt(3.0 * devsq))))
        p = p0
        step = 1.0 / n
        t = 0.0
        for _ in range(n - 1):
            t += step
            ax = p0[0] + t * (p1[0] - p0[0])
            ay = p0[1] + t * (p1[1] - p0[1])
            bx = p1[0] + t * (p2[0] - p1[0])
            by = p1[1] + t * (p2[1] - p1[1])
            pn = (ax + t * (bx - ax), ay + t * (by - ay))
            self.line(p, pn)
            p = pn
        self.line(p, p2)


def draw_line(text, px):
    """Draw one line of text at `px` pixels per em-height (ab_glyph's PxScale)
    and return it trimmed to the ink."""
    f = font()
    factor = px / f.height_unscaled()
    ascent = f.ascent * factor
    descent = f.descent * factor
    placed = []
    x = 0.0
    for ch in text:
        adv, curves, bounds = f.glyph(ch)
        placed.append((x, curves, bounds))
        x += adv * factor
    w = int(math.ceil(x)) + 4
    h = int(math.ceil(ascent - descent)) + 4
    img = Gray(w, h)
    out = img.px
    for gx, curves, bounds in placed:
        if bounds is None:
            continue
        xmin, ymin, xmax, ymax = bounds
        bx0 = math.floor(xmin * factor + gx)
        by0 = math.floor(-ymax * factor + ascent)
        bx1 = math.ceil(xmax * factor + gx)
        by1 = math.ceil(-ymin * factor + ascent)
        bw, bh = int(bx1 - bx0), int(by1 - by0)
        if bw <= 0 or bh <= 0:
            continue
        ox, oy = gx - bx0, ascent - by0
        cov = _Coverage(bw, bh)

        def at(p):
            return (p[0] * factor + ox, -p[1] * factor + oy)

        for c in curves:
            if c[0] == "L":
                cov.line(at(c[1]), at(c[2]))
            else:
                cov.quad(at(c[1]), at(c[2]), at(c[3]))
        accs = list(accumulate(cov.a[: bw * bh]))
        bx0, by0 = int(bx0), int(by0)
        # Columns of this glyph's box that land inside the line image.
        c0, c1 = max(0, -bx0), min(bw, w - bx0)
        if c0 >= c1:
            continue
        for gy in range(max(0, -by0), min(bh, h - by0)):
            seg = accs[gy * bw + c0 : gy * bw + c1]
            ink = bytes(255 - int((c if c < 1.0 else 1.0) * 255.0) for c in map(abs, seg))
            start = (by0 + gy) * w + bx0 + c0
            end = start + (c1 - c0)
            out[start:end] = bytes(map(min, out[start:end], ink))
    return trim(img)


def cap_ratio():
    """Cap height of the font as a fraction of its pixel size, from 'H'."""
    f = font()
    _, _, bounds = f.glyph("H")
    if bounds is None:
        return 0.73
    factor = 100.0 / f.height_unscaled()
    ascent = f.ascent * factor
    top = math.floor(-bounds[3] * factor + ascent)
    bottom = math.ceil(-bounds[1] * factor + ascent)
    return (bottom - top) / 100.0


def trim(img):
    """Crop to the bounding box of the ink."""
    w, h, px = img.w, img.h, img.px
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = px[y * w : (y + 1) * w]
        xs = [x for x, v in enumerate(row) if v < 128]
        if xs:
            y0 = min(y0, y)
            y1 = y
            x0 = min(x0, xs[0])
            x1 = max(x1, xs[-1])
    if x1 < 0:
        return Gray(8, 1)
    nw, nh = x1 - x0 + 1, y1 - y0 + 1
    out = bytearray()
    for y in range(y0, y1 + 1):
        out += px[y * w + x0 : y * w + x0 + nw]
    return Gray(nw, nh, px=out)


def rotate90(img):
    """Clockwise quarter turn."""
    w, h, px = img.w, img.h, img.px
    out = bytearray(w * h)
    for y in range(h):
        row = px[y * w : (y + 1) * w]
        dx = h - 1 - y
        # Source (x, y) -> destination (h - 1 - y, x), in an h-wide image.
        out[dx::h] = row
    return Gray(h, w, px=out)


def _triangle(x):
    x = abs(x)
    return 1.0 - x if x < 1.0 else 0.0


def _sinc(t):
    if t == 0.0:
        return 1.0
    a = t * math.pi
    return math.sin(a) / a


def _lanczos3(x):
    if abs(x) < 3.0:
        return _sinc(x) * _sinc(x / 3.0)
    return 0.0


FILTERS = {"triangle": (_triangle, 1.0), "lanczos3": (_lanczos3, 3.0)}


def _weights(src, dst, kernel, support):
    """Per output index: (first source index, normalised weights)."""
    ratio = src / dst
    sratio = 1.0 if ratio < 1.0 else ratio
    src_support = support * sratio
    out = []
    for o in range(dst):
        center = (o + 0.5) * ratio
        left = int(math.floor(center - src_support))
        left = min(max(left, 0), src - 1)
        right = int(math.ceil(center + src_support))
        right = min(max(right, left + 1), src)
        center -= 0.5
        ws = [kernel((i - center) / sratio) for i in range(left, right)]
        total = sum(ws)
        out.append((left, [w / total for w in ws]))
    return out


def resize(img, nw, nh, filter_name):
    """Resample to nw x nh: vertical pass first (kept in floats), then
    horizontal, rounding and clamping to 0-255 at the end."""
    if (img.w, img.h) == (nw, nh):
        return Gray(nw, nh, px=bytearray(img.px))
    kernel, support = FILTERS[filter_name]
    w, h, px = img.w, img.h, img.px
    rows = [px[y * w : (y + 1) * w] for y in range(h)]
    vert = []
    for top, ws in _weights(h, nh, kernel, support):
        acc = [0.0] * w
        for k, wt in enumerate(ws):
            if wt == 0.0:
                continue
            src = rows[top + k]
            acc = [a + v * wt for a, v in zip(acc, src)]
        vert.append(acc)
    # Horizontal pass on columns, so each tap is one list operation.
    cols = list(zip(*vert))
    outcols = []
    for left, ws in _weights(w, nw, kernel, support):
        acc = [0.0] * nh
        for k, wt in enumerate(ws):
            if wt == 0.0:
                continue
            acc = [a + v * wt for a, v in zip(acc, cols[left + k])]
        outcols.append(acc)
    out = bytearray(nw * nh)
    i = 0
    for row in zip(*outcols):
        out[i : i + nw] = bytes(0 if t <= 0.0 else 255 if t >= 255.0 else int(t + 0.5) for t in row)
        i += nw
    return Gray(nw, nh, px=out)


def to_mono(img, dither):
    """One bool per pixel: True = print a dot. Dithering is Floyd-Steinberg
    in single precision (every store into an array('f') rounds to f32), so
    the error spreads exactly as in the Rust build."""
    w, h = img.w, img.h
    if not dither:
        return [v < 128 for v in img.px]
    buf = array("f", list(img.px))
    t = array("f", [0.0])
    out = [False] * (w * h)
    for y in range(h):
        last_row = y + 1 >= h
        for x in range(w):
            i = y * w + x
            old = buf[i]
            if old < 128.0:
                out[i] = True
                t[0] = old
            else:
                t[0] = old - 255.0
            err = t[0]
            if x + 1 < w:
                t[0] = err * 7.0
                buf[i + 1] += t[0] / 16.0
            if not last_row:
                if x > 0:
                    t[0] = err * 3.0
                    buf[i + w - 1] += t[0] / 16.0
                t[0] = err * 5.0
                buf[i + w] += t[0] / 16.0
                if x + 1 < w:
                    buf[i + w + 1] += err / 16.0
    return out
