"""Job -> laid-out items, at the geometry of one stock.

Everything that depends on the paper happens here: wrapping, column counts,
fitting barcodes and QR codes to the width, rasterising big text and images.
`escpos` and `preview` both consume the result, so the bytes and the preview
always agree.

Items are dicts with a "kind":
    line     text, w, h, bold, underline, invert, align
    raster   width, height, bits, label   (1-bit, `width` = full print width,
                                           alignment baked into the padding)
    barcode  data, symbology, height, module, hri, align, width_dots
    qr       data, module, ec, align, size_dots
    feed_lines n | feed_dots n | cut kind
"""

import base64
import binascii
import re

from . import config as cfg
from . import png, raster
from .text import fold, hang, wrap
from .util import Error, bad, f32, f32_display, io_error, rround, rust_lines

DEFAULT_FEED_LINES = 3
DEFAULT_BIG_MM = 10.0
DEFAULT_BARCODE_MM = 10.0
DEFAULT_BARCODE_MODULE = 3
DEFAULT_QR_MODULE = 6
# Margin left either side of a sideways banner, in dots.
BANNER_MARGIN = 16


def line(text, w=1, h=1, bold=False, underline=False, invert=False, align="left"):
    return {"kind": "line", "text": text, "w": w, "h": h, "bold": bold, "underline": underline, "invert": invert, "align": align}


def layout(job, config, stock_id, allow_paths):
    """Lay a parsed job out on one stock. Returns
    {"stock_id", "items", "warnings"}."""
    stk = cfg.stock(config, stock_id)
    ctx = _Ctx(config, stk, allow_paths)
    feed = job["feed"] if job["feed"] is not None else DEFAULT_FEED_LINES

    body = []
    for i, block in enumerate(job["blocks"]):
        try:
            body.extend(ctx.block(block, feed))
        except Error as e:
            raise Error(e.status, "block %d (%s): %s" % (i + 1, block["type"], e.message), e.hint)

    items = []
    for _ in range(max(job["copies"], 1)):
        items.extend(dict(it) for it in body)
        items.append({"kind": "feed_lines", "n": feed})
        if job["cut"] != "none":
            items.append({"kind": "cut", "cut": cut_for(config, job["cut"])})
    return {"stock_id": stock_id, "items": items, "warnings": ctx.warnings}


def cut_for(config, kind):
    """The cut the printer can actually make. Without a cutter it is a tear
    mark: the feed before it still brings the paper past the tear bar."""
    cutter = config["printer"]["cutter"]
    if cutter == "none":
        return "none"
    if cutter == "full" and kind == "partial":
        return "full"
    return kind


def _u32(x):
    """Rust's saturating float -> u32 cast."""
    if x != x or x <= 0:
        return 0
    return min(int(x), 0xFFFFFFFF)


class _Ctx:
    def __init__(self, config, stk, allow_paths):
        self.config = config
        self.printer = config["printer"]
        self.stock = stk
        self.allow_paths = allow_paths
        self.warnings = []

    def dots(self):
        return self.stock["print_dots"]

    def stock_name(self):
        return "%dmm stock" % self.stock["width_mm"]

    def printer_name(self):
        return self.printer["name"] or self.printer["model"]

    def size(self, size):
        w, h = size if size is not None else (1, 1)
        top = self.printer["max_text_size"]
        if w == 0 or h == 0 or w > top or h > top:
            raise bad(
                "text size %dx%d is outside 1..%d" % (w, h, top),
                'the %s clamps built-in text above %dx; use a "big" block for larger text' % (self.printer_name(), top),
            )
        return w, h

    def cols(self, w):
        return cfg.cols(self.config, self.stock, w)

    def block(self, b, feed):
        return getattr(self, "_" + b["type"])(b, feed)

    def _text(self, b, feed):
        w, h = self.size(b["size"])
        cols = self.cols(w)
        body = fold(b["text"].rstrip())
        if not body.strip():
            return []
        prefix = b["bullet"]
        if b["wrap"] is None or b["wrap"]:
            lines = hang(fold(prefix), body, cols) if prefix is not None else wrap(body, cols)
        else:
            prefixed = (fold(prefix) if prefix is not None else "") + body
            lines = rust_lines(prefixed)
            for ln in lines:
                if len(ln) > cols:
                    self.warnings.append('"%s" is %d chars; %d fit at %dx on %s' % (ln, len(ln), cols, w, self.stock_name()))
        align = b["align"] or "left"
        return [line(t, w, h, b["bold"], b["underline"], b["invert"], align) for t in lines]

    def _row(self, b, feed):
        w, h = self.size(b["size"])
        cols = self.cols(w)
        left, right = fold(b["left"]), fold(b["right"])
        fill = fold(b["fill"]) if b["fill"] is not None else ""
        fill = fill or " "
        room = max(cols - (len(right) + 1), 1)
        lines = wrap(left, room)
        first = lines.pop(0)
        gap = max(cols - (len(first) + len(right)), 0)
        if gap >= 2:
            filler = " " + (fill * gap)[: gap - 2] + " "
        else:
            filler = " " * gap
        out = [first + filler + right] + lines
        return [line(t, w, h, b["bold"]) for t in out]

    def _rule(self, b, feed):
        w, h = self.size(b["size"])
        cols = self.cols(w)
        pattern = fold(b["char"]) if b["char"] is not None else ""
        pattern = pattern or "-"
        text = (pattern * (cols // len(pattern) + 1))[:cols]
        return [line(text, w, h)]

    def _space(self, b, feed):
        if b["mm"] is not None:
            dots = rround(f32(f32(b["mm"]) * self.printer["dots_per_mm"]))
            return [{"kind": "feed_dots", "n": _u32(max(dots, 0.0))}]
        return [{"kind": "feed_lines", "n": b["lines"] if b["lines"] is not None else 1}]

    def _cut(self, b, feed):
        return [{"kind": "feed_lines", "n": feed}, {"kind": "cut", "cut": cut_for(self.config, b["kind"] or "partial")}]

    def _barcode(self, b, feed):
        data = b["data"].strip()
        if not data:
            return []
        sym = b["symbology"] or "CODE128"
        validate_barcode(sym, data)
        requested = min(max(b["module"] if b["module"] is not None else DEFAULT_BARCODE_MODULE, 2), 6)
        modules = barcode_modules(sym, len(data))
        fitted = requested
        while fitted > 2 and modules * fitted > self.dots():
            fitted -= 1
        width_dots = modules * fitted
        if width_dots > self.dots():
            raise bad(
                'barcode "%s" needs ~%d dots; %s has %d' % (data, width_dots, self.stock_name(), self.dots()),
                "shorten the data, use a qr block, or print on receipt-80",
            )
        if fitted < requested:
            self.warnings.append("barcode module reduced from %d to %d dots to fit %s" % (requested, fitted, self.stock_name()))
        mm = f32(b["height"]) if b["height"] is not None else DEFAULT_BARCODE_MM
        dots = rround(f32(mm * self.printer["dots_per_mm"]))
        dots = 0 if dots != dots else int(min(max(dots, 8.0), 255.0))
        return [
            {
                "kind": "barcode",
                "data": data,
                "symbology": sym,
                "height": dots,
                "module": fitted,
                "hri": b["hri"] or "below",
                "align": b["align"] or "center",
                "width_dots": width_dots,
            }
        ]

    def _qr(self, b, feed):
        data = b["data"].strip()
        if not data:
            return []
        ec = (b["ec"] if b["ec"] is not None else "M").strip().upper()
        if ec not in ("L", "M", "Q", "H"):
            raise bad('QR error correction "%s" is not one of L, M, Q, H' % ec)
        nbytes = len(data.encode("utf-8"))
        side = qr_modules(nbytes, ec)
        if side is None:
            raise bad("%d bytes is too much for one QR code" % nbytes)
        requested = min(max(b["module"] if b["module"] is not None else DEFAULT_QR_MODULE, 1), 16)
        fitted = requested
        while fitted > 1 and side * fitted > self.dots():
            fitted -= 1
        if fitted < requested:
            self.warnings.append("QR module reduced from %d to %d dots to fit %s" % (requested, fitted, self.stock_name()))
        align = b["align"] or "center"
        if self.printer["qr"] == "raster":
            return [self.qr_raster(data, ec, fitted, align)]
        return [{"kind": "qr", "data": data, "module": fitted, "ec": ec, "align": align, "size_dots": side * fitted}]

    def _big(self, b, feed):
        items = []
        for ln in (s.strip() for s in rust_lines(b["text"])):
            if not ln:
                continue
            img = self.big(ln, b["height"], b["fit"], b["sideways"])
            items.append(self.raster(img, b["align"] or "center", False, 'big "%s"' % ln))
        return items

    def _image(self, b, feed):
        if b["base64"] is not None:
            b64 = b["base64"].strip()
            if "base64," in b64:
                b64 = b64.split("base64,", 1)[1]
            try:
                data = base64.b64decode(b64.strip(), validate=True)
            except (binascii.Error, ValueError) as e:
                raise bad("image base64: %s" % e)
        elif b["path"] is not None and self.allow_paths:
            try:
                with open(b["path"], "rb") as f:
                    data = f.read()
            except OSError as e:
                raise bad("cannot read image %s: %s" % (b["path"], io_error(e)))
        elif b["path"] is not None:
            raise bad('image "path" is not allowed over HTTP', 'send the image as "base64"')
        else:
            raise bad('image needs "path" or "base64"')
        w, h, rgba = png.decode(data)
        img = raster.Gray(w, h, px=flatten(rgba))
        width = b["width"]
        if width is None:
            target = min(w, self.dots())
        elif isinstance(width, int):
            target = min(width, self.dots())
        elif width == "full":
            target = self.dots()
        elif re.match(r"\+?[0-9]+\Z", width) and int(width) <= 0xFFFFFFFF:
            target = min(int(width), self.dots())
        else:
            raise bad('image width "%s" should be dots or "full"' % width)
        target = max(target, 8)
        if target != w:
            nh = int(max(rround(f32(f32(f32(h) * target) / w)), 1.0))
            img = raster.resize(img, target, nh, "lanczos3")
        label = "image %dx%d dots" % (img.w, img.h)
        return [self.raster(img, b["align"] or "center", b["dither"], label)]

    def big(self, text, height_mm, fit, sideways):
        """Draw one line of text and return it trimmed to the ink, sized and
        (for banners) rotated to its final orientation."""
        dpm = self.printer["dots_per_mm"]
        cap = f32(raster.cap_ratio())
        max_across = max(self.dots() - 2 * BANNER_MARGIN, 0) if sideways else self.dots()
        # Requested cap height in dots; a fitted or default-sized banner is
        # drawn large and scaled to the width below.
        if height_mm is not None:
            height_mm = f32(height_mm)
            cap_dots = f32(height_mm * dpm)
        elif sideways or fit:
            cap_dots = float(max_across)
        else:
            cap_dots = f32(DEFAULT_BIG_MM * dpm)
        img = raster.draw_line(text, max(f32(cap_dots / cap), 8.0))
        if sideways:
            img = raster.rotate90(img)
        fill_width = fit or (sideways and height_mm is None)
        scale = f32(max_across / img.w) if fill_width or img.w > max_across else 1.0
        if not fill_width and height_mm is not None and scale < 0.95:
            # Say what will actually print rather than shrinking silently.
            self.warnings.append(
                'big "%s" shrunk to fit the width: capitals ~%.1fmm, not %smm'
                % (text, f32(height_mm * scale), f32_display(height_mm))
            )
        return scale_image(img, scale)

    def qr_raster(self, data, ec, module, align):
        """A QR code drawn here, for printers without `GS ( k`. A 4-module
        quiet zone either side, as the native command leaves."""
        from .qrcodegen import DataTooLongError, QrCode, QrSegment

        level = {"L": QrCode.Ecc.LOW, "Q": QrCode.Ecc.QUARTILE, "H": QrCode.Ecc.HIGH}.get(ec, QrCode.Ecc.MEDIUM)
        try:
            code = QrCode.encode_segments([QrSegment.make_bytes(data.encode("utf-8"))], level, boostecl=False)
        except DataTooLongError as e:
            raise bad("QR: %s" % e)
        n = code.get_size()
        while module > 1 and (n + 8) * module > self.dots():
            module -= 1
        side = (n + 8) * module
        px = bytearray(b"\xff") * (side * side)
        for my in range(n):
            for mx in range(n):
                if code.get_module(mx, my):
                    for dy in range(module):
                        y = (my + 4) * module + dy
                        start = y * side + (mx + 4) * module
                        px[start : start + module] = b"\x00" * module
        img = raster.Gray(side, side, px=px)
        return self.raster(img, align, False, "QR (image) %dx%d modules" % (n, n))

    def raster(self, img, align, dither, label):
        """Threshold or dither to 1 bit and pad to the full print width, with
        the alignment baked into the padding rather than left to `ESC a`."""
        width = (self.dots() + 7) // 8 * 8
        return pack(img, width, align, dither, label)


def pack(img, width, align, dither, label):
    ink_w = min(img.w, width)
    left = 0 if align == "left" else (width - ink_w) // 2 if align == "center" else width - ink_w
    black = raster.to_mono(img, dither)
    row_bytes = width // 8
    bits = bytearray()
    pad_right = width - left - ink_w
    for y in range(img.h):
        row = black[y * img.w : y * img.w + ink_w]
        s = "0" * left + "".join("1" if v else "0" for v in row) + "0" * pad_right
        bits += int(s, 2).to_bytes(row_bytes, "big")
    return {"kind": "raster", "width": width, "height": img.h, "bits": bytes(bits), "label": label}


_LUMA = (f32(0.299), f32(0.587), f32(0.114))


def flatten(rgba):
    """RGBA -> grey, flattened onto white first: a transparent background
    would otherwise greyscale to black and print as a solid slab. Single
    precision, as in the Rust build; cached per colour."""
    cache = {}
    out = bytearray(len(rgba) // 4)
    kr, kg, kb = _LUMA
    for i in range(len(out)):
        px = rgba[4 * i : 4 * i + 4]
        v = cache.get(px)
        if v is None:
            r, g, b, a8 = px
            a = f32(a8 / 255.0)
            lum = f32(f32(f32(kr * r) + f32(kg * g)) + f32(kb * b))
            v = rround(f32(f32(lum * a) + f32(255.0 * f32(1.0 - a))))
            v = cache[px] = int(min(max(v, 0.0), 255.0))
        out[i] = v
    return out


def scale_image(img, scale):
    if abs(scale - 1.0) < 0.001:
        return img
    w = int(max(rround(f32(img.w * scale)), 1.0))
    h = int(max(rround(f32(img.h * scale)), 1.0))
    return raster.resize(img, w, h, "triangle")


_HINTS = {
    "EAN13": "EAN13 takes 12 or 13 digits",
    "EAN8": "EAN8 takes 7 or 8 digits",
    "UPC_A": "UPC_A takes 11 or 12 digits",
    "UPC_E": "UPC_E takes 6 to 8 digits",
    "ITF": "ITF takes an even number of digits",
    "CODE39": "CODE39 takes A-Z, 0-9, space and - . $ / + %",
    "CODABAR": "CODABAR takes digits and - $ : / . +",
    "CODE128": "CODE128 takes printable ASCII",
}


def validate_barcode(sym, data):
    digits = all("0" <= c <= "9" for c in data)
    n = len(data.encode("utf-8"))
    if sym == "EAN13":
        ok = digits and 12 <= n <= 13
    elif sym == "EAN8":
        ok = digits and 7 <= n <= 8
    elif sym == "UPC_A":
        ok = digits and 11 <= n <= 12
    elif sym == "UPC_E":
        ok = digits and 6 <= n <= 8
    elif sym == "ITF":
        ok = digits and n % 2 == 0
    elif sym == "CODE39":
        ok = all("A" <= c <= "Z" or "0" <= c <= "9" or c in " -.$/+%" for c in data)
    elif sym == "CODABAR":
        ok = all("0" <= c <= "9" or c in "-$:/.+ABCD" for c in data)
    else:
        ok = all(" " <= c <= "~" for c in data)
    if not ok:
        raise bad('"%s" is not valid %s data' % (data, sym), _HINTS[sym])
    if n > 255:
        raise bad("barcode data is limited to 255 characters")


def barcode_modules(sym, length):
    """Approximate width of a barcode in modules, including quiet zones."""
    n = length
    return {
        "CODE128": 11 * (n + 2) + 13 + 20,
        "CODE39": 16 * (n + 2) + 20,
        "EAN13": 95 + 18,
        "UPC_A": 95 + 18,
        "EAN8": 67 + 14,
        "UPC_E": 51 + 18,
        "ITF": 9 * n + 9 + 20,
        "CODABAR": 12 * (n + 2) + 20,
    }[sym]


# Byte-mode capacity at EC level M for versions 1..=40.
_QR_M = [
    14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666,
    711, 779, 857, 911, 997, 1059, 1125, 1190, 1264, 1370, 1452, 1538, 1628, 1722, 1809, 1911, 1989,
    2099, 2213, 2331,
]  # fmt: skip


def qr_modules(length, ec):
    """Modules per side of the smallest QR symbol holding `length` bytes,
    plus the quiet zone, or None past version 40."""
    factor = f32({"L": 1.27, "Q": 0.72, "H": 0.55}.get(ec, 1.0))
    for v, cap in enumerate(_QR_M):
        if int(f32(cap * factor)) >= length:
            return 17 + 4 * (v + 1) + 8  # + 4-module quiet zone each side
    return None


def ruler(carriage):
    """The calibration ruler: `carriage` dots wide, a short tick every 8 dots
    and a tall, numbered one every 48. Each number ends at its own dot, so the
    last number fully on the paper is the printable width."""
    px = f32(16.0 / f32(raster.cap_ratio()))
    label_h, tall, short = 24, 28, 12
    height = label_h + tall
    img = raster.Gray(carriage, height)

    def ink(x0, x1, y0, y1):
        for y in range(y0, min(y1, height)):
            for x in range(x0, min(x1, carriage)):
                img.px[y * carriage + x] = 0

    # Baseline along the bottom.
    ink(0, carriage, height - 2, height)
    at = 8
    while at <= carriage:
        numbered = at % 48 == 0
        ink(at - 2, at, height - (tall if numbered else short), height)
        if numbered:
            label = raster.draw_line(str(at), px)
            left = max(at - label.w, 0)
            for y in range(min(label.h, label_h)):
                for x in range(label.w):
                    if label.px[y * label.w + x] < 128 and left + x < carriage:
                        img.px[y * carriage + left + x] = 0
        at += 8
    width = (carriage + 7) // 8 * 8
    return pack(img, width, "left", False, "ruler, %d dots" % carriage)
