"""Decode images for the `image` block: PNG with the standard library, JPEG
through Pillow when it is installed.

`decode(data)` returns (width, height, rgba) with rgba a bytes object of
4 bytes per pixel.
"""

import struct
import zlib

from .util import bad

SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Adam7 passes: (x start, y start, x step, y step).
ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def decode(data):
    if data.startswith(SIGNATURE):
        return decode_png(data)
    if data[:3] == b"\xff\xd8\xff":
        return _decode_with_pillow(data, "JPEG")
    raise bad("cannot decode image: the image format could not be determined", "send a PNG or JPEG")


def _decode_with_pillow(data, kind):
    try:
        from PIL import Image  # optional
    except ImportError:
        raise bad(
            "cannot decode image: %s needs Pillow, which is not installed" % kind,
            "convert the image to PNG, or `pip install Pillow`",
        )
    import io

    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGBA")
        return im.width, im.height, im.tobytes()


def decode_png(data):
    pos = len(SIGNATURE)
    ihdr = None
    palette = b""
    trns = None
    idat = []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise bad("cannot decode image: PNG has no IHDR")
    width, height, depth, ctype, _, _, interlace = ihdr
    if ctype not in CHANNELS or depth not in (1, 2, 4, 8, 16):
        raise bad("cannot decode image: unsupported PNG (colour type %d, depth %d)" % (ctype, depth))
    if width == 0 or height == 0 or width * height > 64_000_000:
        raise bad("cannot decode image: PNG is %dx%d" % (width, height))
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise bad("cannot decode image: %s" % e)
    channels = CHANNELS[ctype]
    bpp = max(1, channels * depth // 8)  # bytes per complete pixel, for filters

    samples = [[0] * (width * channels) for _ in range(height)]
    if interlace:
        offset = 0
        for x0, y0, dx, dy in ADAM7:
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw == 0 or ph == 0:
                continue
            rows, offset = _unfilter(raw, offset, pw, ph, channels, depth, bpp)
            for j, row in enumerate(rows):
                vals = _samples(row, pw * channels, depth)
                target = samples[y0 + j * dy]
                for i in range(pw):
                    x = x0 + i * dx
                    target[x * channels : (x + 1) * channels] = vals[i * channels : (i + 1) * channels]
    else:
        rows, _ = _unfilter(raw, 0, width, height, channels, depth, bpp)
        samples = [_samples(row, width * channels, depth) for row in rows]

    return width, height, _to_rgba(samples, width, ctype, depth, palette, trns)


def _unfilter(raw, offset, w, h, channels, depth, bpp):
    stride = (w * channels * depth + 7) // 8
    prev = bytearray(stride)
    rows = []
    for _ in range(h):
        if offset + 1 + stride > len(raw):
            raise bad("cannot decode image: PNG data is truncated")
        ftype = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise bad("cannot decode image: bad PNG filter %d" % ftype)
        rows.append(line)
        prev = line
    return rows, offset


def _samples(row, count, depth):
    if depth == 8:
        return list(row[:count])
    if depth == 16:
        return [row[2 * i] << 8 | row[2 * i + 1] for i in range(count)]
    per = 8 // depth
    mask = (1 << depth) - 1
    out = []
    for byte in row:
        for k in range(per):
            out.append((byte >> (8 - depth * (k + 1))) & mask)
    return out[:count]


def _to_rgba(samples, width, ctype, depth, palette, trns):
    maxv = (1 << depth) - 1

    def scale(v):
        if depth == 8:
            return v
        if depth == 16:
            return (v + 128) // 257
        return v * 255 // maxv

    out = bytearray()
    for row in samples:
        if ctype == 6:
            out += bytes(scale(v) for v in row)
        elif ctype == 2:
            key = struct.unpack(">HHH", trns) if trns and len(trns) == 6 else None
            for i in range(width):
                r, g, b = row[3 * i : 3 * i + 3]
                a = 0 if key and (r, g, b) == key else 255
                out += bytes((scale(r), scale(g), scale(b), a))
        elif ctype == 0:
            key = struct.unpack(">H", trns)[0] if trns and len(trns) == 2 else None
            for v in row:
                g = scale(v)
                out += bytes((g, g, g, 0 if v == key else 255))
        elif ctype == 4:
            for i in range(width):
                g, a = scale(row[2 * i]), scale(row[2 * i + 1])
                out += bytes((g, g, g, a))
        else:  # palette
            for v in row:
                if 3 * v + 3 > len(palette):
                    raise bad("cannot decode image: PNG palette index out of range")
                a = trns[v] if trns and v < len(trns) else 255
                out += palette[3 * v : 3 * v + 3] + bytes((a,))
    return bytes(out)
