#!/usr/bin/env python3
"""Run the same jobs and commands through the Rust binary and the Python
package and compare what comes out: ESC/POS bytes, previews, JSON reports,
error messages and exit codes.

    python3 tests/parity.py [path/to/print-to-thermal]

The binary defaults to target/release/print-to-thermal (`cargo build
--release`). Byte streams must match exactly, except bitmaps drawn from
fonts or resized images, which may differ in a few edge dots (the Rust build
rasterises in single precision); those are reported with the share of dots
that differ and fail above MAX_RASTER_DIFF.
"""

import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
MAX_RASTER_DIFF = 0.002  # share of dots in one bitmap


def png(w, h, pixel, ctype=6):
    """A PNG from pixel(x, y) -> tuple, for test images."""
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)[:channels]) for x in range(w)) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def b64(data):
    return base64.b64encode(data).decode()


GRADIENT = png(300, 120, lambda x, y: (x * 255 // 299, y * 2, 128, 255))
TRANSPARENT = png(64, 64, lambda x, y: (0, 0, 0, 255 if (x - 32) ** 2 + (y - 32) ** 2 < 400 else 0))
GREY = png(40, 30, lambda x, y: ((x * 7 + y * 3) % 256,), ctype=0)
WIDE = png(900, 50, lambda x, y: (255 * ((x // 30) % 2), 0, 0, 255))

JOBS = {
    "text sizes and styles": {
        "blocks": [
            {"type": "text", "text": "Plain text that is long enough to wrap across more than one line of the receipt."},
            {"type": "text", "text": "Hi", "size": 2, "bold": True, "align": "center"},
            {"type": "text", "text": "Stretched", "size": [3, 1], "underline": True, "align": "right"},
            {"type": "text", "text": "Inverted", "invert": True, "size": "2"},
            {"type": "text", "text": "Buy milk and eggs and bread and butter", "bullet": "[ ] "},
            {"type": "text", "text": "no wrap here, this line is deliberately far too long to fit", "wrap": False, "size": 2},
            {"type": "text", "text": "425°F — ½ cup café… “quoted” • → ✓ ☐ 🎉\ttab"},
            {"type": "text", "text": "   "},
        ]
    },
    "rows rules space": {
        "blocks": [
            {"type": "row", "left": "Milk", "right": "2"},
            {"type": "row", "left": "A very long item name that has to wrap to the next line", "right": "$12.99", "fill": "."},
            {"type": "row", "left": "Big", "right": "x", "size": 2, "bold": True, "fill": "-="},
            {"type": "rule"},
            {"type": "rule", "char": "=-", "size": 2},
            {"type": "space"},
            {"type": "space", "lines": 2},
            {"type": "space", "mm": 7.3},
            {"type": "space", "mm": "40"},
        ]
    },
    "barcodes": {
        "blocks": [
            {"type": "barcode", "data": "ABC"},
            {"type": "barcode", "data": "BIN-0042", "height": 12.5, "module": 6, "hri": "both", "align": "left"},
            {"type": "barcode", "data": "123456789012", "symbology": "EAN13"},
            {"type": "barcode", "data": "1234567", "symbology": "EAN8", "hri": "none"},
            {"type": "barcode", "data": "CODE-39", "symbology": "CODE39"},
            {"type": "barcode", "data": "123456", "symbology": "ITF"},
        ]
    },
    "barcode shrinks on 60mm": {"stock": "label-60-plain", "blocks": [{"type": "barcode", "data": "BIN-0042"}]},
    "qr native": {
        "blocks": [
            {"type": "qr", "data": "https://example.com"},
            {"type": "qr", "data": "hello", "module": 4, "ec": "h", "align": "right"},
            {"type": "qr", "data": "x" * 500, "module": 16},
        ]
    },
    "big default": {"blocks": [{"type": "big", "text": "Hello"}]},
    "big fit 60mm": {"stock": "label-60-adhesive", "blocks": [{"type": "big", "text": "WEDNESDAY", "fit": True}]},
    "big height": {"blocks": [{"type": "big", "text": "Jars\nFLOUR", "height": 15, "align": "left"}]},
    "big shrunk": {"stock": "label-60-plain", "blocks": [{"type": "big", "text": "A VERY LONG LINE", "height": 20}]},
    "big sideways": {"blocks": [{"type": "big", "text": "SALE", "sideways": True}]},
    "big sideways height": {"blocks": [{"type": "big", "text": "Banner", "sideways": True, "height": 30}]},
    "big unicode": {"blocks": [{"type": "big", "text": "350°F · ½ cup · Ünïcødé €5 — ok", "align": "right"}]},
    "image gradient": {"blocks": [{"type": "image", "base64": b64(GRADIENT)}]},
    "image dither full": {"blocks": [{"type": "image", "base64": b64(GRADIENT), "width": "full", "dither": True}]},
    "image transparent": {"blocks": [{"type": "image", "base64": "data:image/png;base64," + b64(TRANSPARENT), "width": 200}]},
    "image grey small": {"blocks": [{"type": "image", "base64": b64(GREY), "align": "left"}]},
    "image wide": {"stock": "label-60-plain", "blocks": [{"type": "image", "base64": b64(WIDE), "align": "right"}]},
    "copies cuts feed": {
        "copies": 2,
        "feed": 5,
        "cut": "full",
        "blocks": [{"type": "text", "text": "a"}, {"type": "cut"}, {"type": "text", "text": "b"}, {"type": "cut", "kind": "none"}],
    },
    "no final cut": {"cut": "none", "blocks": [{"type": "text", "text": "tear me"}]},
    "template each": {
        "defaults": {"title": "List"},
        "blocks": [
            {"type": "text", "text": "{{title}} ({{count}})", "size": "{{size}}"},
            {
                "type": "each",
                "items": "{{items}}",
                "as": "it",
                "blocks": [{"type": "row", "left": "{{index}}. {{it.name}}", "right": "{{it.qty}}"}],
            },
        ],
    },
}

TEMPLATE_DATA = {"count": 2, "size": 2, "items": [{"name": "Eggs", "qty": 12}, {"name": "Milk", "qty": "1L"}]}

ERRORS = {
    "unknown field": {"blocks": [{"type": "text", "txt": "x"}]},
    "unknown type": {"blocks": [{"type": "nope"}]},
    "size too big": {"blocks": [{"type": "text", "text": "x", "size": 5}]},
    "missing param": {"params": {"who": "Recipient"}, "blocks": [{"type": "text", "text": "Hi {{who}}"}]},
    "bad ean": {"blocks": [{"type": "barcode", "data": "12345", "symbology": "EAN13"}]},
    "too long barcode": {"stock": "label-60-plain", "blocks": [{"type": "barcode", "data": "THIS-IS-FAR-TOO-LONG-FOR-60MM"}]},
    "bad ec": {"blocks": [{"type": "qr", "data": "x", "ec": "Z"}]},
    "no blocks": {"copies": 1},
    "wrong stock": {"stock": "label-60-adhesive", "blocks": [{"type": "text", "text": "x"}]},
    "unknown stock": {"stock": "nope", "blocks": [{"type": "text", "text": "x"}]},
}

TEMPLATE_SAMPLES = {
    "shopping-list": {"items": ["Eggs", "Milk"]},
    "folder-tab": {"text": "Grades & Enrollment", "size": 3},
    "item-label": {"title": "CABLES", "code": "BIN-42"},
    "note": {"body": "Back at 5"},
    "day": {"day": "MONDAY", "date": "2 November 2026"},
}


class Runner:
    def __init__(self, rust_bin, home):
        self.rust = [rust_bin]
        self.py = [sys.executable, "-B", "-m", "thermal"]
        self.env = dict(os.environ, THERMAL_HOME=home, PYTHONPATH=PLUGIN)
        self.env.pop("THERMAL_PRINTER", None)

    def run(self, cmd, args, stdin=None):
        p = subprocess.run(cmd + args, input=stdin, capture_output=True, env=self.env, cwd=ROOT)
        return p.returncode, p.stdout, p.stderr

    def both(self, args, stdin=None):
        return self.run(self.rust, args, stdin), self.run(self.py, args, stdin)


def split_items(data):
    """Split an ESC/POS stream into (raster bands, everything else) so a
    bitmap that differs in a few dots can be measured instead of failing."""
    rasters, rest, i = [], bytearray(), 0
    while i < len(data):
        if data[i : i + 4] == b"\x1dv0\x00":
            xl, xh, yl, yh = data[i + 4 : i + 8]
            n = (xl | xh << 8) * (yl | yh << 8)
            rest += data[i : i + 8]
            rasters.append(data[i + 8 : i + 8 + n])
            i += 8 + n
        else:
            rest.append(data[i])
            i += 1
    return rasters, bytes(rest)


def compare_bytes(name, a, b):
    if a == b:
        return True, "identical, %d bytes" % len(a)
    ra, rest_a = split_items(a)
    rb, rest_b = split_items(b)
    if rest_a != rest_b or len(ra) != len(rb) or any(len(x) != len(y) for x, y in zip(ra, rb)):
        at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return False, "DIFFER at byte %d (rust %d bytes, python %d bytes)" % (at, len(a), len(b))
    worst = 0.0
    for x, y in zip(ra, rb):
        diff = sum(bin(p ^ q).count("1") for p, q in zip(x, y))
        worst = max(worst, diff / (8 * len(x)))
    return worst <= MAX_RASTER_DIFF, "commands identical; bitmaps differ in %.3f%% of dots" % (100 * worst)


def main():
    rust_bin = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "target", "release", "print-to-thermal")
    if not os.path.exists(rust_bin):
        sys.exit("no Rust binary at %s: run `cargo build --release` or pass a path" % rust_bin)
    tmp = tempfile.mkdtemp(prefix="thermal-parity-")
    failures = 0

    def report(ok, name, detail):
        nonlocal failures
        failures += 0 if ok else 1
        print("%s %-34s %s" % ("ok  " if ok else "FAIL", name, detail))

    try:
        homes = {}
        for label, model, qr in (("mht", None, None), ("raster-qr", "generic-58mm", "raster"), ("no-cutter", None, None)):
            home = os.path.join(tmp, label)
            os.makedirs(home)
            shutil.copy(os.path.join(FIXTURES, "printer.json"), home)
            doc = json.load(open(os.path.join(home, "printer.json")))
            if model:
                doc["printer"]["model"] = model
                doc["printer"]["qr"] = qr
            if label == "no-cutter":
                doc["printer"]["cutter"] = "none"
            json.dump(doc, open(os.path.join(home, "printer.json"), "w"), indent=2)
            homes[label] = home

        r = Runner(rust_bin, homes["mht"])
        print("== jobs: bytes and previews")
        for name, job in JOBS.items():
            text = json.dumps(job)
            extra = []
            if "{{" in text:
                extra = ["--data", json.dumps(TEMPLATE_DATA)]
            (rc1, out1, err1), (rc2, out2, err2) = r.both(["print", text, "--dry-run", "--hex", "--force"] + extra)
            if rc1 or rc2:
                report(False, name, "exit %d / %d: %s | %s" % (rc1, rc2, err1.decode().strip(), err2.decode().strip()))
                continue
            ok, detail = compare_bytes(name, bytes.fromhex(out1.decode()), bytes.fromhex(out2.decode()))
            report(ok, name, detail)
            (_, p1, _), (_, p2, _) = r.both(["preview", text, "--json"] + extra)
            report(p1 == p2, name + " (preview)", "identical" if p1 == p2 else "DIFFER")
            if p1 != p2:
                print(p1.decode())
                print(p2.decode())

        for label in ("raster-qr", "no-cutter"):
            rr = Runner(rust_bin, homes[label])
            job = json.dumps(
                {"blocks": [{"type": "qr", "data": "https://example.com/a?b=1"}, {"type": "qr", "data": "HELLO 123", "ec": "Q"}, {"type": "cut"}]}
            )
            (rc1, out1, e1), (rc2, out2, e2) = rr.both(["print", job, "--dry-run", "--hex", "--force"])
            if rc1 or rc2:
                report(False, label, "%s | %s" % (e1.decode().strip(), e2.decode().strip()))
            else:
                ok, detail = compare_bytes(label, bytes.fromhex(out1.decode()), bytes.fromhex(out2.decode()))
                if label == "raster-qr" and not ok and "commands identical" in detail:
                    # Known: the Rust `qrcode` crate segments data and scores
                    # masks its own way; Python uses the standard's (qrcodegen).
                    # Both are valid codes for the same data and size class.
                    ok, detail = True, detail + " (known: different valid mask/segments)"
                report(ok, label, detail)
            (_, p1, _), (_, p2, _) = rr.both(["preview", job])
            report(p1 == p2, label + " (preview)", "identical" if p1 == p2 else "DIFFER")

        print("== templates")
        for name, data in TEMPLATE_SAMPLES.items():
            for stock in ("receipt-80", "label-60-adhesive"):
                args = [name, "--data", json.dumps(data), "--stock", stock, "--force"]
                (rc1, out1, e1), (rc2, out2, e2) = r.both(["print"] + args + ["--dry-run", "--hex"])
                if rc1 or rc2:
                    report(False, name, "%s | %s" % (e1.decode().strip(), e2.decode().strip()))
                    continue
                ok, detail = compare_bytes(name, bytes.fromhex(out1.decode()), bytes.fromhex(out2.decode()))
                report(ok, "%s on %s" % (name, stock), detail)
                (_, p1, _), (_, p2, _) = r.both(["preview"] + args)
                report(p1 == p2, "%s on %s (preview)" % (name, stock), "identical" if p1 == p2 else "DIFFER")

        print("== calibration ruler")
        (_, out1, _), (_, out2, _) = r.both(["calibrate", "--dry-run", "--hex"])
        ok, detail = compare_bytes("calibrate", bytes.fromhex(out1.decode()), bytes.fromhex(out2.decode()))
        report(ok, "calibrate", detail)

        print("== errors")
        for name, job in ERRORS.items():
            (rc1, out1, e1), (rc2, out2, e2) = r.both(["print", json.dumps(job), "--dry-run"])
            same = (rc1, out1, e1) == (rc2, out2, e2)
            report(same, name, "identical (exit %d)" % rc1 if same else "DIFFER:\n    rust:   %r\n    python: %r" % (e1, e2))

        print("== reports")
        for args in (["stocks"], ["models"], ["config"], ["templates"], ["templates", "note"], ["status"], ["help"], ["openapi"], ["openapi", "--json"]):
            (rc1, out1, e1), (rc2, out2, e2) = r.both(args)
            same = (rc1, out1, e1) == (rc2, out2, e2)
            if args == ["help"]:
                # Python adds win32: to the usage text.
                same = rc1 == rc2 == 0
            report(same, " ".join(args), "identical" if same else "DIFFER")
            if not same and args != ["help"]:
                for a, b in zip(out1.decode().splitlines(), out2.decode().splitlines()):
                    if a != b:
                        print("    rust:   %s\n    python: %s" % (a, b))
                        break

        print("== setup and editing")
        for impl in ("rust", "python"):
            home = os.path.join(tmp, "setup-" + impl)
            os.makedirs(home)
        outs = {}
        for impl, cmd in (("rust", r.rust), ("python", r.py)):
            env = dict(r.env, THERMAL_HOME=os.path.join(tmp, "setup-" + impl))
            steps = [
                ["setup", "--printer", "192.168.1.9", "--model", "generic-58mm", "--stock", "tags=58:384", "--stock", "stickers=40:adhesive"],
                ["setup", "--printer", "tcp://x:9100", "--model", "generic-58mm"],
                ["set", "qr", "raster"],
                ["set", "qr", "sideways"],
                ["set", "dots_per_mm", "8.5"],
                ["set", "connection", "10.0.0.7:9101"],
                ["stock", "rm", "tags"],
                ["stock", "rm", "stickers"],
                ["stock", "add", "labels=50:400:adhesive"],
                ["stock", "labels"],
                ["stock", "nope"],
                ["stocks"],
            ]
            log = []
            for step in steps:
                p = subprocess.run(cmd + step, capture_output=True, env=env, cwd=ROOT)
                err = p.stderr.decode().replace(os.path.join(tmp, "setup-" + impl), "<home>")
                out = p.stdout.decode().replace(os.path.join(tmp, "setup-" + impl), "<home>")
                log.append((step, p.returncode, out, err))
            log.append(("file", open(os.path.join(tmp, "setup-" + impl, "printer.json")).read()))
            outs[impl] = log
        for a, b in zip(outs["rust"], outs["python"]):
            report(a == b, " ".join(a[0]) if isinstance(a[0], list) else "printer.json", "identical" if a == b else "DIFFER:\n    rust:   %r\n    python: %r" % (a[1:], b[1:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("all match" if not failures else "%d difference(s)" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
