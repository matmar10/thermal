"""Laid-out items -> a text picture of the roll.

Columns, wrapping and alignment are exact, because they come from the same
layout the bytes do. Bold is not shown; a 2x glyph is drawn letter-spaced so
it occupies its real width. Bitmaps, barcodes and QR codes appear as boxes of
roughly their printed height. Lengths are estimates.
"""

from . import config as cfg
from .util import f32, f32_display, rround


def _blen(s):
    return len(s.encode("utf-8"))


def render(lay, config):
    """Returns (text, labels_mm): the picture, and the estimated length of
    each label (each piece between cuts) in mm, excluding the dead zone."""
    stk = config["stocks"][lay["stock_id"]]
    printer = config["printer"]
    cols = cfg.cols(config, stk, 1)
    line_dots = printer["line_dots"]
    dpm = printer["dots_per_mm"]
    edge = "+%s+" % ("-" * cols)
    out = ["%s  %s (%d cols at 1x)" % (edge, lay["stock_id"], cols)]
    labels = []
    dots = 0

    def push_box(label, width_cols, height_dots, align):
        rows = int(max(rround(f32(f32(height_dots) / line_dots)), 1.0))
        width = min(max(width_cols, max(min(_blen(label), cols), 4)), cols)
        pad = 0 if align == "left" else (cols - width) // 2 if align == "center" else cols - width
        for r in range(rows):
            if r == rows // 2:
                lab = label[: max(width - 2, 0)]
                room = width - 2
                fill = max(room - len(lab), 0)
                inner = "[" + " " * (fill // 2) + lab + " " * (fill - fill // 2) + "]"
            else:
                inner = "[" + " " * (width - 2) + "]"
            out.append("|" + " " * pad + inner + " " * (cols - pad - width) + "|")

    for it in lay["items"]:
        kind = it["kind"]
        if kind == "line":
            w, h = it["w"], it["h"]
            spaced = "".join(c + " " * (w - 1) for c in it["text"]) if w > 1 else it["text"]
            spaced = spaced.rstrip()
            visible = min(_blen(spaced), cols)
            align = it["align"]
            pad = 0 if align == "left" else (cols - visible) // 2 if align == "center" else cols - visible
            shown = spaced[:cols]
            notes = []
            if w > 1 or h > 1:
                notes.append("%dx" % w if w == h else "%dx%d" % (w, h))
            if it["bold"]:
                notes.append("bold")
            if it["underline"]:
                notes.append("underline")
            if it["invert"]:
                notes.append("inverted")
            if _blen(spaced) > cols:
                notes.append("OVERFLOW")
            note = "  " + " ".join(notes) if notes else ""
            out.append("|" + " " * pad + shown + " " * max(cols - pad - _blen(shown), 0) + "|" + note)
            for _ in range(1, h):
                out.append("|" + " " * cols + "|")
            dots += 24 * h + (line_dots - 24)
        elif kind == "raster":
            # Find the ink's horizontal extent to draw the box where it prints.
            row_bytes = it["width"] // 8
            bits = it["bits"]
            x0, x1 = None, 0
            for start in range(0, len(bits), row_bytes):
                row = bits[start : start + row_bytes]
                nz = [i for i, b in enumerate(row) if b]
                if nz:
                    x0 = nz[0] * 8 if x0 is None else min(x0, nz[0] * 8)
                    x1 = max(x1, nz[-1] * 8 + 8)
            cell = printer["cell_dots"]
            if x0 is None:
                start_c, end_c = 0, cols
            else:
                start_c, end_c = x0 // cell, min(-(-x1 // cell), cols)
            width_cols = max(end_c - start_c, 4)
            label = "%s, %.0fmm tall" % (it["label"], f32(f32(it["height"]) / dpm))
            align = "left" if start_c == 0 else "right" if end_c >= cols else "center"
            push_box(label, width_cols, it["height"], align)
            dots += it["height"]
        elif kind == "barcode":
            label = "%s %s" % (it["symbology"], it["data"])
            cols_w = it["width_dots"] // printer["cell_dots"]
            total = it["height"] + (0 if it["hri"] == "none" else 24)
            push_box(label, cols_w, total, it["align"])
            dots += total + 6
        elif kind == "qr":
            label = "QR %s" % it["data"]
            push_box(label, it["size_dots"] // printer["cell_dots"], it["size_dots"], it["align"])
            dots += it["size_dots"]
        elif kind == "feed_lines":
            for _ in range(it["n"]):
                out.append("|" + " " * cols + "|")
            dots += it["n"] * line_dots
        elif kind == "feed_dots":
            for _ in range(int(rround(f32(f32(it["n"]) / line_dots)))):
                out.append("|" + " " * cols + "|")
            dots += it["n"]
        elif kind == "cut":
            mm = f32(f32(dots) / dpm)
            labels.append(mm)
            what = "Tear here" if it["cut"] == "none" else "%s cut" % it["cut"].capitalize()
            out.append("%s  %s, ~%.0fmm" % (edge, what, mm))
            dots = 0
    if dots > 0:
        mm = f32(f32(dots) / dpm)
        labels.append(mm)
        out.append("%s  no cut, ~%.0fmm" % (edge, mm))
    if printer["dead_zone_mm"] > 0.0:
        out.append(
            "Each piece also starts with ~%smm of blank paper (the cutter sits past the print head)."
            % f32_display(printer["dead_zone_mm"])
        )
    return "\n".join(out), labels
