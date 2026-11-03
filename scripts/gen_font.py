#!/usr/bin/env python3
"""Extract glyph outlines from assets/font.ttf into plugin/thermal/font.json.

The plugin draws `big` text from these outlines with a pure-Python
rasterizer, so it ships readable JSON instead of a binary font. Composite
glyphs are flattened and only the characters in CHARSET are kept, which also
keeps the file under the plugin directory's 256 KiB per-file limit.

Standard library only. Run from the repo root after changing the font or
CHARSET:  python3 scripts/gen_font.py
"""

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "font.ttf"
OUT = ROOT / "plugin" / "thermal" / "font.json"

# Characters `big` can draw. Anything else draws as the font's .notdef box,
# as the Rust build does for characters the font lacks.
CHARSET = (
    list(range(0x20, 0x7F))  # ASCII
    + list(range(0xA0, 0x180))  # Latin-1 and Latin Extended-A
    + list(range(0x2010, 0x2028))  # dashes, quotes, bullet, ellipsis
    + list(range(0x2030, 0x203B))  # per mille, primes, guillemets
    + [0x20AC, 0x2116, 0x2122, 0x2212, 0x2126]  # euro, numero, TM, minus, ohm
    + list(range(0x2150, 0x215F))  # vulgar fractions
    + list(range(0x2190, 0x2196))  # arrows
    + [0x2610, 0x2611, 0x2612, 0x2713, 0x2714, 0x2717, 0x2718]  # boxes, checks
    + [0x2605, 0x2606, 0x2665, 0x25CF, 0x25CB, 0x25A0, 0x25A1]  # stars, hearts, shapes
    + list(range(0x0391, 0x03CA))  # Greek
    + list(range(0x0400, 0x0460))  # Cyrillic
    + list(range(0x2500, 0x2580))  # box drawing
)


class Font:
    def __init__(self, data):
        self.d = data
        n = struct.unpack(">H", data[4:6])[0]
        self.tables = {}
        for i in range(n):
            tag, _, off, ln = struct.unpack(">4sIII", data[12 + 16 * i : 28 + 16 * i])
            self.tables[tag.decode("latin-1")] = (off, ln)
        head = self.table("head")
        self.units_per_em = struct.unpack(">H", head[18:20])[0]
        self.loca_long = struct.unpack(">h", head[50:52])[0] == 1
        maxp = self.table("maxp")
        self.num_glyphs = struct.unpack(">H", maxp[4:6])[0]
        hhea = self.table("hhea")
        self.hhea_ascent, self.hhea_descent = struct.unpack(">hh", hhea[4:8])
        self.num_hmetrics = struct.unpack(">H", hhea[34:36])[0]
        self.ascent, self.descent = self._metrics()
        self.cmap = self._cmap()
        self._loca()

    def table(self, tag):
        off, ln = self.tables[tag]
        return self.d[off : off + ln]

    def _metrics(self):
        """Ascender/descender as ttf-parser (and so ab_glyph) reads them."""
        asc, desc = self.hhea_ascent, self.hhea_descent
        if "OS/2" in self.tables:
            os2 = self.table("OS/2")
            fs_selection = struct.unpack(">H", os2[62:64])[0]
            typo_asc, typo_desc = struct.unpack(">hh", os2[68:72])
            if fs_selection & (1 << 7):
                return typo_asc, typo_desc
            if asc == 0:
                asc = typo_asc
            if desc == 0:
                desc = typo_desc
        return asc, desc

    def _cmap(self):
        cmap = self.table("cmap")
        n = struct.unpack(">H", cmap[2:4])[0]
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", cmap[4 + 8 * i : 12 + 8 * i])
            fmt = struct.unpack(">H", cmap[off : off + 2])[0]
            if (pid, eid, fmt) in ((3, 10, 12), (0, 4, 12), (0, 6, 12)):
                best = (fmt, off)
                break
            if fmt == 4 and (pid == 3 and eid == 1 or pid == 0):
                best = (fmt, off)
        if best is None:
            sys.exit("no usable cmap subtable")
        fmt, off = best
        out = {}
        if fmt == 4:
            segx2 = struct.unpack(">H", cmap[off + 6 : off + 8])[0]
            seg = segx2 // 2
            ends = struct.unpack(">%dH" % seg, cmap[off + 14 : off + 14 + segx2])
            base = off + 16 + segx2
            starts = struct.unpack(">%dH" % seg, cmap[base : base + segx2])
            deltas = struct.unpack(">%dh" % seg, cmap[base + segx2 : base + 2 * segx2])
            ro_base = base + 2 * segx2
            ranges = struct.unpack(">%dH" % seg, cmap[ro_base : ro_base + segx2])
            for i in range(seg):
                for c in range(starts[i], ends[i] + 1):
                    if c == 0xFFFF:
                        continue
                    if ranges[i] == 0:
                        g = (c + deltas[i]) & 0xFFFF
                    else:
                        at = ro_base + 2 * i + ranges[i] + 2 * (c - starts[i])
                        g = struct.unpack(">H", cmap[at : at + 2])[0]
                        if g:
                            g = (g + deltas[i]) & 0xFFFF
                    if g:
                        out[c] = g
        else:
            ngroups = struct.unpack(">I", cmap[off + 12 : off + 16])[0]
            for i in range(ngroups):
                s, e, g = struct.unpack(">III", cmap[off + 16 + 12 * i : off + 28 + 12 * i])
                for c in range(s, e + 1):
                    out[c] = g + c - s
        return out

    def _loca(self):
        loca = self.table("loca")
        n = self.num_glyphs + 1
        if self.loca_long:
            self.offsets = list(struct.unpack(">%dI" % n, loca[: 4 * n]))
        else:
            self.offsets = [o * 2 for o in struct.unpack(">%dH" % n, loca[: 2 * n])]
        self.glyf_off = self.tables["glyf"][0]

    def advance(self, gid):
        hmtx = self.table("hmtx")
        i = min(gid, self.num_hmetrics - 1)
        return struct.unpack(">H", hmtx[4 * i : 4 * i + 2])[0]

    def contours(self, gid, transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0), depth=0):
        """The glyph's contours as lists of (x, y, on_curve), composites flattened."""
        start, end = self.offsets[gid], self.offsets[gid + 1]
        if end <= start:
            return []
        g = self.d[self.glyf_off + start : self.glyf_off + end]
        ncont = struct.unpack(">h", g[:2])[0]
        if ncont >= 0:
            return [[self._apply(transform, x, y) + (on,) for x, y, on in c] for c in self._simple(g, ncont)]
        if depth > 8:
            return []
        out = []
        p = 10
        while True:
            flags, child = struct.unpack(">HH", g[p : p + 4])
            p += 4
            if flags & 0x0001:
                a1, a2 = struct.unpack(">hh", g[p : p + 4])
                p += 4
            else:
                a1, a2 = struct.unpack(">bb", g[p : p + 2])
                p += 2
            a, b, c, d = 1.0, 0.0, 0.0, 1.0
            if flags & 0x0008:
                a = d = struct.unpack(">h", g[p : p + 2])[0] / 16384.0
                p += 2
            elif flags & 0x0040:
                a, d = (v / 16384.0 for v in struct.unpack(">hh", g[p : p + 4]))
                p += 4
            elif flags & 0x0080:
                a, b, c, d = (v / 16384.0 for v in struct.unpack(">hhhh", g[p : p + 8]))
                p += 8
            # Only "args are x/y offsets" is supported (as in ttf-parser).
            e, f = (float(a1), float(a2)) if flags & 0x0002 else (0.0, 0.0)
            ta, tb, tc, td, te, tf = transform
            # Child transform composed under the parent's.
            combined = (
                ta * a + tc * b,
                tb * a + td * b,
                ta * c + tc * d,
                tb * c + td * d,
                ta * e + tc * f + te,
                tb * e + td * f + tf,
            )
            out.extend(self.contours(child, combined, depth + 1))
            if not flags & 0x0020:
                break
        return out

    @staticmethod
    def _apply(t, x, y):
        a, b, c, d, e, f = t
        nx, ny = a * x + c * y + e, b * x + d * y + f
        return (int(nx) if nx == int(nx) else round(nx, 3), int(ny) if ny == int(ny) else round(ny, 3))

    @staticmethod
    def _simple(g, ncont):
        ends = struct.unpack(">%dH" % ncont, g[10 : 10 + 2 * ncont])
        npts = ends[-1] + 1 if ncont else 0
        p = 10 + 2 * ncont
        ilen = struct.unpack(">H", g[p : p + 2])[0]
        p += 2 + ilen
        flags = []
        while len(flags) < npts:
            f = g[p]
            p += 1
            flags.append(f)
            if f & 0x08:
                r = g[p]
                p += 1
                flags.extend([f] * r)
        xs, ys = [], []
        v = 0
        for f in flags:
            if f & 0x02:
                dx = g[p]
                p += 1
                v += dx if f & 0x10 else -dx
            elif not f & 0x10:
                v += struct.unpack(">h", g[p : p + 2])[0]
                p += 2
            xs.append(v)
        v = 0
        for f in flags:
            if f & 0x04:
                dy = g[p]
                p += 1
                v += dy if f & 0x20 else -dy
            elif not f & 0x20:
                v += struct.unpack(">h", g[p : p + 2])[0]
                p += 2
            ys.append(v)
        out, s = [], 0
        for e in ends:
            out.append([(xs[i], ys[i], 1 if flags[i] & 1 else 0) for i in range(s, e + 1)])
            s = e + 1
        return out


def main():
    font = Font(SRC.read_bytes())

    def glyph(gid):
        flat = [[v for pt in c for v in pt] for c in font.contours(gid)]
        return [font.advance(gid), flat]

    chars = {}
    for cp in CHARSET:
        gid = font.cmap.get(cp)
        if gid:
            chars[chr(cp)] = glyph(gid)
    doc = {
        "about": "Glyph outlines from DejaVu Sans Mono Bold (see FONT-LICENSE), generated by scripts/gen_font.py. "
        "Each glyph is [advance, contours]; a contour is a flat list of x, y, on_curve triples in font units, y up.",
        "units_per_em": font.units_per_em,
        "ascent": font.ascent,
        "descent": font.descent,
        "notdef": glyph(0),
        "glyphs": chars,
    }
    text = "{\n"
    head = {k: v for k, v in doc.items() if k != "glyphs"}
    for k, v in head.items():
        text += "%s: %s,\n" % (json.dumps(k), json.dumps(v, separators=(",", ":")))
    text += '"glyphs": {\n'
    text += ",\n".join("%s: %s" % (json.dumps(c, ensure_ascii=True), json.dumps(g, separators=(",", ":"))) for c, g in chars.items())
    text += "\n}\n}\n"
    json.loads(text)
    OUT.write_text(text, encoding="ascii")
    missing = [hex(cp) for cp in CHARSET if chr(cp) not in chars]
    print("wrote %s: %d glyphs, %d bytes" % (OUT.relative_to(ROOT), len(chars), len(text)))
    if missing:
        print("not in the font:", " ".join(missing))


if __name__ == "__main__":
    main()
