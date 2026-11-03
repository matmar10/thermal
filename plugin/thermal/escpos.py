"""Laid-out items -> ESC/POS bytes.

NEVER emit `ESC K` (1B 4B). On this firmware it is "select bit image", not
reverse feed: a stray one makes the printer swallow thousands of following
bytes as bitmap data, and every later job vanishes until a power cycle.
"""

ESC = 0x1B
GS = 0x1D
DLE = 0x10
EOT = 0x04

# `DLE EOT 4`: real-time paper sensor status. Answered immediately, ahead of
# queued data; the only command here the printer replies to.
PAPER_STATUS = bytes([DLE, EOT, 0x04])

# Rows per `GS v 0` band. Long rasters are split so no single command asks
# the printer to buffer an unbounded image.
RASTER_BAND_ROWS = 128

ALIGN = {"left": 0, "center": 1, "right": 2}
HRI = {"none": 0, "above": 1, "below": 2, "both": 3}
# `GS k` format-2 type codes.
SYMBOLOGY = {"UPC_A": 65, "UPC_E": 66, "EAN13": 67, "EAN8": 68, "CODE39": 69, "ITF": 70, "CODABAR": 71, "CODE128": 73}
QR_EC = {"L": 0x30, "M": 0x31, "Q": 0x32, "H": 0x33}


def _align(a):
    return bytes([ESC, ord("a"), ALIGN[a]])


def encode(lay, config):
    stk = config["stocks"][lay["stock_id"]]
    out = bytearray([ESC, ord("@")])
    # GS L 0 + GS W n: pin the print area to the stock, so ESC a centres
    # within 60mm paper instead of the 80mm carriage.
    out += bytes([GS, ord("L"), 0, 0])
    w = stk["print_dots"] & 0xFFFF
    out += bytes([GS, ord("W"), w & 0xFF, w >> 8])

    for it in lay["items"]:
        kind = it["kind"]
        if kind == "line":
            out += _align(it["align"])
            size = (((it["w"] - 1) << 4) | (it["h"] - 1)) & 0xFF
            if size:
                out += bytes([GS, ord("!"), size])
            if it["bold"]:
                out += bytes([ESC, ord("E"), 1])
            if it["underline"]:
                out += bytes([ESC, ord("-"), 1])
            if it["invert"]:
                out += bytes([GS, ord("B"), 1])
            out += device_bytes(it["text"], config)
            out.append(0x0A)
            if it["invert"]:
                out += bytes([GS, ord("B"), 0])
            if it["underline"]:
                out += bytes([ESC, ord("-"), 0])
            if it["bold"]:
                out += bytes([ESC, ord("E"), 0])
            if size:
                out += bytes([GS, ord("!"), 0])
        elif kind == "raster":
            out += _align("left")
            row_bytes = it["width"] // 8
            height = it["height"]
            bits = it["bits"]
            y = 0
            while y < height:
                rows = min(RASTER_BAND_ROWS, height - y)
                out += bytes([GS, ord("v"), ord("0"), 0, row_bytes & 0xFF, row_bytes >> 8, rows & 0xFF, rows >> 8])
                out += bits[y * row_bytes : (y + rows) * row_bytes]
                y += rows
        elif kind == "barcode":
            out += _align(it["align"])
            out += bytes([GS, ord("h"), it["height"], GS, ord("w"), it["module"], GS, ord("H"), HRI[it["hri"]]])
            payload = it["data"].encode("utf-8")
            if it["symbology"] == "CODE128":
                # Code set B: printable ASCII.
                payload = b"{B" + payload
            out += bytes([GS, ord("k"), SYMBOLOGY[it["symbology"]], len(payload) & 0xFF])
            out += payload
            out.append(0x0A)
        elif kind == "qr":
            out += _align(it["align"])
            # Model 2, module size, error correction.
            out += bytes([GS, ord("("), ord("k"), 4, 0, 0x31, 0x41, 0x32, 0x00])
            out += bytes([GS, ord("("), ord("k"), 3, 0, 0x31, 0x43, it["module"]])
            out += bytes([GS, ord("("), ord("k"), 3, 0, 0x31, 0x45, QR_EC.get(it["ec"], 0x31)])
            # Store (pL pH = data + 3), then print.
            data = it["data"].encode("utf-8")
            n = len(data) + 3
            out += bytes([GS, ord("("), ord("k"), n & 0xFF, (n >> 8) & 0xFF, 0x31, 0x50, 0x30])
            out += data
            out += bytes([GS, ord("("), ord("k"), 3, 0, 0x31, 0x51, 0x30])
            out.append(0x0A)
        elif kind == "feed_lines":
            out += bytes([ESC, ord("d"), it["n"]])
        elif kind == "feed_dots":
            # ESC J n feeds n dots; split anything over 255.
            left = it["n"]
            while left > 0:
                step = min(left, 255)
                out += bytes([ESC, ord("J"), step])
                left -= step
        elif kind == "cut":
            if it["cut"] == "full":
                out += bytes([GS, ord("V"), 0])
            elif it["cut"] == "partial":
                out += bytes([GS, ord("V"), 1])
    return bytes(out)


def device_bytes(text, config):
    """Apply the model's substitution map and encode one byte per character.
    Text is already folded to ASCII; a substitution may map to a Latin-1
    character (the legacy printer draws 0xC4 as a rule) which encodes as-is."""
    subs = config["printer"]["substitutions"]
    out = bytearray()
    for ch in text:
        rep = subs.get(ch)
        for c in rep if rep is not None else ch:
            o = ord(c)
            out.append(o if o < 256 else ord("?"))
    return bytes(out)
