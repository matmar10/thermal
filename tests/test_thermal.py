"""Tests for the Python print-to-thermal package. Standard library only:

    python3 -m unittest discover -s tests

tests/parity.py compares it against the Rust build, when one is built.
"""

import ast
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, PLUGIN)
sys.dont_write_bytecode = True

from thermal import config as cfg  # noqa: E402
from thermal import escpos, job, layout, ops, png, preview, server, transport  # noqa: E402
from thermal.text import fold, hang, wrap  # noqa: E402
from thermal.util import NOT_CONFIGURED, Error  # noqa: E402


def config():
    c = cfg.load(FIXTURES)
    c["loaded"] = "receipt-80"
    return c


def prepare(doc, stock=None, allow_paths=True):
    return ops.prepare(FIXTURES, config(), doc, stock=stock, allow_paths=allow_paths)


def encode(doc, stock=None):
    return escpos.encode(prepare(doc, stock)["layout"], config())


def temp_home(test):
    d = tempfile.mkdtemp(prefix="thermal-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def make_png(w, h, pixel):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w)) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Text(unittest.TestCase):
    def test_wraps_at_word_boundaries_and_hard_breaks_only_overlong_words(self):
        self.assertEqual(wrap("the quick brown fox", 9), ["the quick", "brown fox"])
        self.assertEqual(wrap("a\n\nb", 5), ["a", "", "b"])
        self.assertEqual(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])
        self.assertEqual(hang("[ ] ", "one two three", 10), ["[ ] one", "    two", "    three"])

    def test_folds_to_ascii_before_wrapping(self):
        self.assertEqual(fold("425°F — ½ cup café"), "425F -- 1/2 cup cafe")
        self.assertEqual(fold("emoji 🎉 gone"), "emoji  gone")


class Jobs(unittest.TestCase):
    def test_template_fill_keeps_raw_values_and_expands_each(self):
        doc = {
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
        }
        data = {"count": 2, "size": 2, "items": [{"name": "Eggs", "qty": 12}, {"name": "Milk", "qty": "1L"}]}
        j = job.expand(doc, data)
        self.assertEqual(len(j["blocks"]), 3)
        self.assertEqual(j["blocks"][0]["text"], "List (2)")
        self.assertEqual(j["blocks"][0]["size"], (2, 2))
        self.assertEqual(j["blocks"][1]["left"], "1. Eggs")
        self.assertEqual(j["blocks"][1]["right"], "12")
        self.assertEqual(j["blocks"][2]["left"], "2. Milk")

    def test_missing_param_names_it(self):
        with self.assertRaises(Error) as cm:
            job.expand({"params": {"who": "Recipient"}, "blocks": [{"type": "text", "text": "Hi {{who}}"}]}, {})
        self.assertIn('"who"', cm.exception.message)
        self.assertIn("Recipient", cm.exception.message)

    def test_rejects_unknown_fields_and_oversized_text(self):
        with self.assertRaises(Error):
            prepare({"blocks": [{"type": "text", "txt": "x"}]})
        with self.assertRaises(Error):
            prepare({"blocks": [{"type": "nope"}]})
        with self.assertRaises(Error) as cm:
            prepare({"blocks": [{"type": "text", "text": "x", "size": 5}]})
        self.assertIn("big", cm.exception.hint)

    def test_newline_separated_items_are_a_list(self):
        j = job.expand({"blocks": [{"type": "each", "items": "{{items}}", "blocks": [{"type": "text", "text": "{{item}}"}]}]}, {"items": "a\n\n b \n"})
        self.assertEqual([b["text"] for b in j["blocks"]], ["a", "b"])


class Bytes(unittest.TestCase):
    def test_every_job_pins_the_print_area_to_the_stock(self):
        b = encode({"blocks": [{"type": "text", "text": "x"}]}, "label-60-plain")
        self.assertTrue(b.startswith(bytes([0x1B, 0x40, 0x1D, 0x4C, 0, 0, 0x1D, 0x57, 0x80, 0x01])))  # 384 dots
        b = encode({"blocks": [{"type": "text", "text": "x"}]})
        self.assertIn(bytes([0x1D, 0x57, 0x40, 0x02]), b)  # 576 dots

    def test_text_bytes_match_quartermaster(self):
        b = encode({"blocks": [{"type": "text", "text": "Hi", "size": 2, "bold": True, "align": "center"}]})
        self.assertIn(bytes([0x1B, 0x61, 0x01, 0x1D, 0x21, 0x11, 0x1B, 0x45, 0x01]) + b"Hi\n" + bytes([0x1B, 0x45, 0x00, 0x1D, 0x21, 0x00]), b)
        b = encode({"blocks": [{"type": "text", "text": "x", "size": [3, 1]}]})
        self.assertIn(bytes([0x1D, 0x21, 0x20]), b)
        # Ends with the default feed and a partial cut.
        self.assertTrue(b.endswith(bytes([0x1B, 0x64, 3, 0x1D, 0x56, 0x01])))

    def test_never_sends_esc_k(self):
        b = encode({"blocks": [{"type": "text", "text": "K K K"}, {"type": "big", "text": "KKK"}, {"type": "qr", "data": "KKKK"}]})
        # ESC K is fine inside raster data, but never as a command right after text.
        self.assertNotIn(bytes([0x0A, 0x1B, 0x4B]), b)

    def test_barcode_and_qr_bytes_match_quartermaster(self):
        b = encode({"blocks": [{"type": "barcode", "data": "ABC", "height": 10, "module": 3, "hri": "below"}]})
        self.assertIn(bytes([0x1D, 0x68, 80, 0x1D, 0x77, 3, 0x1D, 0x48, 2]), b)
        self.assertIn(bytes([0x1D, 0x6B, 73, 5]) + b"{BABC", b)
        b = encode({"blocks": [{"type": "qr", "data": "hello", "module": 4, "ec": "H"}]})
        self.assertIn(bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, 4]), b)
        self.assertIn(bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x33]), b)
        self.assertIn(bytes([0x1D, 0x28, 0x6B, 8, 0, 0x31, 0x50, 0x30]) + b"hello", b)

    def test_barcodes_shrink_to_fit_or_refuse(self):
        p = prepare({"blocks": [{"type": "barcode", "data": "BIN-0042"}]}, "label-60-plain")
        self.assertTrue(any("module reduced" in w for w in p["layout"]["warnings"]))
        with self.assertRaises(Error):
            prepare({"blocks": [{"type": "barcode", "data": "THIS-IS-FAR-TOO-LONG-FOR-60MM"}]}, "label-60-plain")
        with self.assertRaises(Error):
            prepare({"blocks": [{"type": "barcode", "data": "12345", "symbology": "EAN13"}]})

    def test_big_text_is_a_full_width_raster_that_fits(self):
        p = prepare({"blocks": [{"type": "big", "text": "WEDNESDAY", "fit": True}]}, "label-60-adhesive")
        item = p["layout"]["items"][0]
        self.assertEqual(item["kind"], "raster")
        self.assertEqual(item["width"], 384)
        self.assertTrue(20 < item["height"] < 120, item["height"])
        bits = item["bits"]
        self.assertEqual(len(bits), 48 * item["height"])
        rows = [bits[i : i + 48] for i in range(0, len(bits), 48)]
        # Fitted to the width: ink reaches both edges.
        self.assertTrue(any(r[0] for r in rows) and any(r[47] for r in rows))
        dump = os.environ.get("THERMAL_DUMP")
        if dump:
            # Eyeball it: a PBM opens in Preview.app.
            with open(os.path.join(dump, "big.pbm"), "wb") as f:
                f.write(b"P4\n%d %d\n" % (item["width"], item["height"]) + bits)

    def test_oversized_big_text_shrinks_with_a_warning(self):
        p = prepare({"blocks": [{"type": "big", "text": "A VERY LONG LINE", "height": 20}]}, "label-60-plain")
        self.assertTrue(any("shrunk" in w for w in p["layout"]["warnings"]))

    def test_sideways_banner_runs_along_the_strip(self):
        p = prepare({"blocks": [{"type": "big", "text": "SALE", "sideways": True}]})
        item = p["layout"]["items"][0]
        self.assertGreater(item["height"], item["width"])

    def test_copies_and_mid_job_cuts(self):
        p = prepare({"copies": 2, "blocks": [{"type": "text", "text": "a"}, {"type": "cut"}, {"type": "text", "text": "b"}]})
        self.assertEqual(sum(1 for i in p["layout"]["items"] if i["kind"] == "cut"), 4)

    def test_no_cutter_means_no_cut_bytes(self):
        c = config()
        c["printer"]["cutter"] = "none"
        j = job.expand({"blocks": [{"type": "text", "text": "a"}, {"type": "cut"}, {"type": "text", "text": "b"}]}, {})
        lay = layout.layout(j, c, "receipt-80", True)
        self.assertNotIn(bytes([0x1D, ord("V")]), escpos.encode(lay, c))
        self.assertIn("Tear here", preview.render(lay, c)[0])

    def test_qr_falls_back_to_an_image(self):
        c = config()
        c["printer"]["qr"] = "raster"
        j = job.expand({"blocks": [{"type": "qr", "data": "https://example.com"}]}, {})
        lay = layout.layout(j, c, "label-60-plain", True)
        self.assertEqual(lay["items"][0]["kind"], "raster")
        self.assertEqual(lay["items"][0]["width"], 384)
        self.assertNotIn(bytes([0x1D, ord("("), ord("k")]), escpos.encode(lay, c))

    def test_calibration_ruler_spans_the_carriage(self):
        ruler, p = ops.calibration(config())
        widths = [i["width"] for i in p["layout"]["items"] if i["kind"] == "raster"]
        self.assertEqual(widths, [576])
        self.assertEqual(ruler["stocks"]["calibration"]["print_dots"], 576)


class Images(unittest.TestCase):
    def test_png_images_decode_and_fit_the_width(self):
        data = make_png(700, 20, lambda x, y: (0, 0, 0, 255) if x < 350 else (255, 255, 255, 0))
        p = prepare({"blocks": [{"type": "image", "base64": base64.b64encode(data).decode()}]})
        item = p["layout"]["items"][0]
        self.assertEqual((item["width"], item["kind"]), (576, "raster"))
        self.assertEqual(item["label"], "image 576x16 dots")

    def test_png_decoder_handles_palette_grey_and_interlace(self):
        w, h, rgba = png.decode(make_png(3, 2, lambda x, y: (x * 100, y * 100, 7, 200)))
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(rgba[4 * 4 : 4 * 5], bytes((100, 100, 7, 200)))

    def test_image_paths_are_refused_over_http(self):
        with self.assertRaises(Error) as cm:
            prepare({"blocks": [{"type": "image", "path": "/etc/hosts"}]}, allow_paths=False)
        self.assertIn("not allowed", cm.exception.message)

    def test_jpeg_without_pillow_says_so(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            with self.assertRaises(Error) as cm:
                png.decode(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
            self.assertIn("Pillow", cm.exception.message)


class Printing(unittest.TestCase):
    def test_printing_on_the_wrong_stock_is_refused_unless_forced(self):
        p = prepare({"stock": "label-60-adhesive", "blocks": [{"type": "text", "text": "x"}]})
        with self.assertRaises(Error) as cm:
            ops.print_job(config(), p, False, True)
        self.assertEqual(cm.exception.status, 409)
        report, _ = ops.print_job(config(), p, True, True)
        self.assertFalse(report["sent"])
        self.assertTrue(report["adhesive"])

    def test_device_transport_writes_the_bytes(self):
        out = os.path.join(temp_home(self), "out.bin")
        open(out, "wb").close()
        c = config()
        c["printer"]["connection"] = "file:" + out
        p = prepare({"blocks": [{"type": "text", "text": "hi"}]})
        report, data = ops.print_job(c, p, False, False)
        self.assertTrue(report["sent"])
        with open(out, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_tcp_transport_sends_and_reads_paper_status(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        received = []

        def printer():
            for _ in range(2):
                conn, _ = listener.accept()
                with conn:
                    chunks = []
                    while True:
                        data = conn.recv(65536)
                        if data == escpos.PAPER_STATUS:
                            conn.sendall(bytes([0x0C]))  # near end
                            break
                        if not data:
                            break
                        chunks.append(data)
                    received.append(b"".join(chunks))

        t = threading.Thread(target=printer, daemon=True)
        t.start()
        c = config()
        c["printer"]["connection"] = "tcp://127.0.0.1:%d" % port
        p = prepare({"blocks": [{"type": "text", "text": "hi"}]})
        report, data = ops.print_job(c, p, False, False)
        t.join(5)
        listener.close()
        self.assertEqual(report["paper"], "low")
        self.assertIn("paper is near the end of the roll", report["warnings"])
        self.assertEqual(received[-1], data)

    def test_connections_parse(self):
        self.assertEqual(transport.parse("tcp://p.local"), ("tcp", "p.local:9100"))
        self.assertEqual(transport.parse("cups:Receipt_Printer"), ("cups", "Receipt_Printer"))
        self.assertEqual(transport.parse("win32:POS-80"), ("win32", "POS-80"))
        self.assertEqual(transport.parse("/dev/usb/lp0"), ("device", "/dev/usb/lp0"))
        self.assertEqual(transport.parse("file:///tmp/out.bin"), ("device", "/tmp/out.bin"))
        with self.assertRaises(Error) as cm:
            transport.parse("")
        self.assertEqual(cm.exception.status, NOT_CONFIGURED)
        with self.assertRaises(Error):
            transport.parse("usb:whatever")
        self.assertEqual(cfg.normalize_connection("10.0.0.5:9101"), "tcp://10.0.0.5:9101")
        self.assertEqual(cfg.normalize_connection("win32:POS-80"), "win32:POS-80")
        self.assertEqual(cfg.normalize_connection("COM3"), "COM3")


class Config(unittest.TestCase):
    def test_unconfigured_home_is_reported_as_not_configured(self):
        with self.assertRaises(Error) as cm:
            cfg.load(temp_home(self))
        self.assertEqual(cm.exception.status, NOT_CONFIGURED)
        self.assertIn("print-to-thermal setup", cm.exception.hint)

    def test_setup_writes_a_config_and_refuses_to_overwrite(self):
        d = temp_home(self)
        c = cfg.setup(d, "192.168.1.9", "generic-58mm", ["tags=58:384", "stickers=40:adhesive"], False)
        self.assertEqual(c["printer"]["connection"], "tcp://192.168.1.9:9100")
        self.assertEqual(c["printer"]["carriage_dots"], 384)
        self.assertEqual(c["stocks"]["tags"]["print_dots"], 384)
        # No dots given: estimated from the printer, and flagged as such.
        self.assertTrue(c["stocks"]["stickers"]["adhesive"])
        self.assertEqual(c["stocks"]["stickers"]["print_dots"], 264)
        self.assertIn("calibrate", c["stocks"]["stickers"]["notes"][0])
        with self.assertRaises(Error) as cm:
            cfg.setup(d, "tcp://x:9100", "generic-58mm", [], False)
        self.assertEqual(cm.exception.status, 409)

        c = cfg.set_field(d, "qr", "native")
        self.assertEqual(c["printer"]["qr"], "native")
        with self.assertRaises(Error):
            cfg.set_field(d, "qr", "sideways")
        self.assertEqual(cfg.load(d)["printer"]["qr"], "native", "an invalid value must not be written")
        with self.assertRaises(Error):
            cfg.remove_stock(d, c["loaded"])
        cfg.remove_stock(d, "stickers")
        c = cfg.add_stock(d, "labels=50:400:adhesive")
        self.assertEqual(c["stocks"]["labels"]["print_dots"], 384, "never wider than the carriage")

    def test_setup_without_stocks_uses_the_full_carriage(self):
        c = cfg.setup(temp_home(self), "cups:Receipt", "generic-80mm", [], False)
        self.assertEqual(c["loaded"], "roll-80")
        self.assertEqual(c["stocks"]["roll-80"]["print_dots"], 576)

    def test_legacy_host_and_port_become_a_connection_and_presets_merge(self):
        doc = {
            "printer": {
                "model": "Milestone MHT-P80E",
                "host": "10.0.0.5",
                "port": 9100,
                "dots_per_mm": 8,
                "carriage_dots": 576,
                "cell_dots": 12,
                "line_dots": 30,
                "max_text_size": 4,
                "dead_zone_mm": 7.5,
            },
            "stocks": {"r": {"description": "r", "width_mm": 80, "print_dots": 576, "adhesive": False}},
            "loaded": "r",
        }
        c = cfg.resolve(doc)
        self.assertEqual(c["printer"]["connection"], "tcp://10.0.0.5:9100")
        self.assertEqual(c["printer"]["name"], "Milestone MHT-P80E")
        c = cfg.resolve({"printer": {"model": "epson-tm-t88", "max_text_size": 6}, "stocks": {}, "loaded": ""})
        self.assertEqual((c["printer"]["carriage_dots"], c["printer"]["max_text_size"]), (512, 6))


class Templates(unittest.TestCase):
    SAMPLES = {
        "shopping-list": {"items": ["Eggs", "Milk"]},
        "folder-tab": {"text": "Grades & Enrollment", "size": 3},
        "item-label": {"title": "CABLES", "code": "BIN-42"},
        "note": {"body": "Back at 5"},
        "day": {"day": "MONDAY", "date": "2 November 2026"},
    }

    def test_bundled_templates_validate_and_preview(self):
        for t in ops.list_templates(FIXTURES):
            name = t["name"]
            doc = ops.load_template(FIXTURES, name)
            ops.validate_template(doc)
            p = ops.prepare(FIXTURES, config(), name, data=self.SAMPLES[name])
            self.assertNotIn("OVERFLOW", ops.preview_report(config(), p)["preview"], name)

    def test_user_templates_override_built_in_ones(self):
        d = temp_home(self)
        ops.load_template(d, "note")  # built-ins load with no templates dir
        ops.save_template(d, "note", {"description": "mine", "blocks": [{"type": "text", "text": "x"}]})
        self.assertEqual(ops.load_template(d, "note")["description"], "mine")
        note = next(t for t in ops.list_templates(d) if t["name"] == "note")
        self.assertEqual(note["source"], "user")
        ops.delete_template(d, "note")
        with self.assertRaises(Error) as cm:
            ops.delete_template(d, "note")
        self.assertEqual(cm.exception.status, 400, "built-ins cannot be deleted")


class Api(unittest.TestCase):
    def test_openapi_covers_every_route_and_its_examples_are_valid_jobs(self):
        spec = server.openapi_json()
        for method, path in server.ROUTES:
            self.assertIsInstance(spec["paths"][path].get(method), dict, "%s %s missing from openapi.yaml" % (method, path))
        examples = spec["components"]["requestBodies"]["JobRequest"]["content"]["application/json"]["examples"]
        for name, ex in examples.items():
            if "template" in ex["value"]:
                continue
            prepare(ex["value"])
        tpl = spec["paths"]["/templates/{name}"]["put"]["requestBody"]["content"]["application/json"]["examples"]["pantry-jar"]["value"]
        ops.validate_template(tpl)
        # Every block type in the job format has a schema.
        for t in job.BLOCK_TYPES:
            self.assertIsInstance(spec["components"]["schemas"]["Block"]["discriminator"]["mapping"].get(t), str, t)

    def test_openapi_json_is_generated_from_the_yaml(self):
        try:
            import yaml  # optional, only to check the generated file
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(os.path.join(PLUGIN, "thermal", "openapi.yaml"), encoding="utf-8") as f:
            self.assertEqual(yaml.safe_load(f), server.openapi_json(), "run `make openapi-json`")

    def test_server_routes_auth_and_jobs(self):
        d = temp_home(self)
        shutil.copy(os.path.join(FIXTURES, "printer.json"), d)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        t = threading.Thread(target=server.serve, args=(d, "127.0.0.1:%d" % port, "secret", []), daemon=True)
        t.start()
        base = "http://127.0.0.1:%d" % port

        def call(method, path, body=None, token="secret"):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            for _ in range(50):
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.read().decode()
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, e.read().decode()
                except urllib.error.URLError:
                    threading.Event().wait(0.05)
            self.fail("server did not start")

        status, body = call("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(call("GET", "/stocks", token=None)[0], 401)
        self.assertEqual(call("GET", "/stocks", token="wrong")[0], 401)
        status, body = call("GET", "/stocks")
        self.assertEqual(json.loads(body)["loaded"], "receipt-80")
        status, body = call("POST", "/preview?format=text", {"template": "note", "data": {"body": "hi"}})
        self.assertEqual(status, 200)
        self.assertIn("|hi ", body)
        status, body = call("POST", "/print?dry_run=true", {"blocks": [{"type": "text", "text": "x"}]})
        self.assertEqual((status, json.loads(body)["sent"]), (200, False))
        status, body = call("POST", "/print?dry_run=1", {"blocks": [{"type": "image", "path": "/etc/hosts"}]})
        self.assertEqual(status, 400)
        self.assertIn("not allowed", json.loads(body)["error"])
        self.assertEqual(call("PUT", "/templates/mine", {"blocks": [{"type": "text", "text": "x"}]})[0], 200)
        self.assertEqual(call("DELETE", "/templates/mine")[0], 200)
        self.assertEqual(call("GET", "/nope")[0], 404)


class Bundle(unittest.TestCase):
    def test_package_is_standard_library_only_and_python_3_9(self):
        pkg = os.path.join(PLUGIN, "thermal")
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        optional = {"PIL", "yaml"}
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name), encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src, filename=name, feature_version=(3, 9))
            for node in ast.walk(tree):
                if isinstance(node, ast.Match if hasattr(ast, "Match") else ()):
                    self.fail("%s uses match (Python 3.10+)" % name)
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods = [node.module]
                for m in mods:
                    top = m.split(".")[0]
                    if stdlib:
                        self.assertTrue(top in stdlib or top in optional, "%s imports %s" % (name, m))

    def test_plugin_bundle_meets_directory_limits(self):
        files = []
        for dirpath, dirnames, filenames in os.walk(PLUGIN):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for n in filenames:
                p = os.path.join(dirpath, n)
                self.assertFalse(os.path.islink(p), "symlink in plugin bundle: %s" % p)
                files.append(p)
        self.assertLessEqual(len(files), 512)
        # Directory pre-submission checklist: non-image files of 256 KiB or
        # more, and compiled executables, are held for a reviewer.
        for p in files:
            size = os.path.getsize(p)
            self.assertLess(size, 256 * 1024, "%s is %d bytes" % (p, size))
            self.assertNotEqual(os.path.basename(p), ".DS_Store")
            if not p.endswith((".png", ".ttf")):
                with open(p, "rb") as f:
                    head = f.read(4)
                self.assertNotIn(head, (b"\x7fELF", b"MZ\x90\x00", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"), "%s is a binary" % p)
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        from thermal import __version__

        self.assertEqual(manifest["version"], __version__, "plugin.json and thermal/__init__.py versions must match")

    def test_launcher_runs_the_package(self):
        if os.name == "nt":
            self.skipTest("POSIX launcher")
        env = dict(os.environ, THERMAL_HOME=FIXTURES)
        p = subprocess.run([os.path.join(PLUGIN, "bin", "print-to-thermal"), "preview", "note", "--set", "body=hi"], capture_output=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"|hi ", p.stdout)
        p = subprocess.run([os.path.join(PLUGIN, "bin", "print-to-thermal"), "config"], capture_output=True, env=dict(env, THERMAL_HOME=temp_home(self)))
        self.assertEqual(p.returncode, 3)


if __name__ == "__main__":
    unittest.main()
