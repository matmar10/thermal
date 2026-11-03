"""`printer.json`: the device, the stocks that fit it, and which one is loaded.

A resolved config is a dict:
    {"printer": {...every printer field...}, "stocks": {id: {...}}, "loaded": id}
"""

import json
import os
import re

from .util import NOT_CONFIGURED, Error, bad, expected, f32, f32_json

_HERE = os.path.dirname(os.path.abspath(__file__))

# Printer presets, shipped in models/. A user's `printer.json` names one with
# `printer.model` and overrides any of its fields.
MODELS = ["generic-80mm", "generic-58mm", "epson-tm-t20", "epson-tm-t88", "mht-p80e"]
DEFAULT_MODEL = "generic-80mm"

# Printer fields `print-to-thermal set` may change.
SETTABLE = [
    "connection",
    "model",
    "name",
    "paper_mm",
    "dots_per_mm",
    "carriage_dots",
    "cell_dots",
    "line_dots",
    "max_text_size",
    "dead_zone_mm",
    "cutter",
    "qr",
    "status",
]

_UINT = re.compile(r"\+?[0-9]+\Z")


class _Invalid(Exception):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _uint(bits):
    top = (1 << bits) - 1

    def read(v):
        if _is_int(v) and 0 <= v <= top:
            return v
        raise _Invalid("invalid type: %s, expected u%d" % (json.dumps(v), bits))

    return read


def _float(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f32(float(v))
    raise _Invalid("invalid type: %s, expected f32" % json.dumps(v))


def _string(v):
    if isinstance(v, str):
        return v
    raise _Invalid("invalid type: %s, expected a string" % json.dumps(v))


def _bool(v):
    if isinstance(v, bool):
        return v
    raise _Invalid("invalid type: %s, expected a boolean" % json.dumps(v))


def _strings(v):
    if isinstance(v, list) and all(isinstance(s, str) for s in v):
        return list(v)
    raise _Invalid("invalid type: expected a sequence of strings")


def _string_map(v):
    if isinstance(v, dict) and all(isinstance(s, str) for s in v.values()):
        return dict(sorted(v.items()))
    raise _Invalid("invalid type: expected a map of strings")


def _enum(names):
    def read(v):
        if isinstance(v, str) and v in names:
            return v
        if isinstance(v, str):
            raise _Invalid("unknown variant `%s`, %s" % (v, expected(names)))
        raise _Invalid("invalid type: %s, expected an enum" % json.dumps(v))

    return read


# (field, reader, default or _REQUIRED), in the order fields are written out.
_REQUIRED = object()
PRINTER_FIELDS = [
    ("model", _string, ""),  # the preset this profile is based on
    ("name", _string, ""),
    ("connection", _string, ""),  # tcp://host:port, cups:<queue>, or a device path
    ("paper_mm", _uint(32), 0),  # widest paper the printer takes
    ("dots_per_mm", _float, _REQUIRED),
    ("carriage_dots", _uint(32), _REQUIRED),
    ("cell_dots", _uint(32), _REQUIRED),  # width of one built-in character at 1x
    ("line_dots", _uint(32), _REQUIRED),  # how far one 1x line advances the paper
    ("max_text_size", _uint(8), _REQUIRED),  # largest GS ! multiplier that prints larger
    ("dead_zone_mm", _float, 0.0),
    ("cutter", _enum(["none", "partial", "full"]), "partial"),
    ("qr", _enum(["native", "raster"]), "native"),
    ("status", _bool, True),  # ask the paper sensor before printing
    ("substitutions", _string_map, {}),  # characters the device cannot draw
    ("notes", _strings, []),
]

STOCK_FIELDS = [
    ("description", _string, _REQUIRED),
    ("width_mm", _uint(32), _REQUIRED),
    ("print_dots", _uint(32), _REQUIRED),  # measured, not derived from width_mm
    ("adhesive", _bool, _REQUIRED),
    ("uses", _strings, []),
    ("notes", _strings, []),
]


def _struct(obj, fields):
    if not isinstance(obj, dict):
        raise _Invalid("invalid type: expected a map")
    out = {}
    for name, read, default in fields:
        if name in obj:
            out[name] = read(obj[name])
        elif default is _REQUIRED:
            raise _Invalid("missing field `%s`" % name)
        else:
            out[name] = default.copy() if isinstance(default, (list, dict)) else default
    return out


def printer_json(p):
    """A printer as JSON, floats in their single-precision form."""
    out = dict(p)
    out["dots_per_mm"] = f32_json(p["dots_per_mm"])
    out["dead_zone_mm"] = f32_json(p["dead_zone_mm"])
    return out


def stock(config, sid):
    if sid not in config["stocks"]:
        raise bad('unknown stock "%s"' % sid, "known stocks: %s" % ", ".join(config["stocks"]))
    return config["stocks"][sid]


def cols(config, stk, size):
    """Characters per line on a stock at width multiplier `size`."""
    return stk["print_dots"] // (config["printer"]["cell_dots"] * max(size, 1))


def model(mid):
    """A preset by id, as JSON."""
    if mid not in MODELS:
        raise bad(
            'unknown printer model "%s"' % mid,
            "models: %s; or pick the generic one closest to yours" % ", ".join(MODELS),
        )
    path = os.path.join(_HERE, "models", mid + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Error(500, "models/%s.json: %s" % (mid, e))


def env(name):
    """An environment variable, ignoring empty values and unsubstituted
    placeholders (`${user_config.printer}` when the user left it blank)."""
    v = os.environ.get(name)
    if v is None or not v.strip() or "${" in v:
        return None
    return v


def user_dir():
    """`$XDG_CONFIG_HOME/thermal`, else `~/.config/thermal`
    (`%APPDATA%\\thermal` on Windows)."""
    xdg = env("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "thermal")
    if os.name == "nt":
        appdata = env("APPDATA")
        if appdata:
            return os.path.join(appdata, "thermal")
    home = env("HOME") or env("USERPROFILE")
    if home:
        return os.path.join(home, ".config", "thermal")
    return "."


def home():
    """Where `printer.json` and the user's `templates/` live.

    `THERMAL_HOME` wins. Then the user config directory, if it holds a
    `printer.json`. Then a `printer.json` above this package, for a
    development checkout. Otherwise the user config directory, which is where
    `print-to-thermal setup` writes."""
    explicit = env("THERMAL_HOME")
    if explicit:
        return explicit
    user = user_dir()
    if os.path.isfile(os.path.join(user, "printer.json")):
        return user
    d = os.path.dirname(os.path.realpath(_HERE))
    while True:
        if os.path.isfile(os.path.join(d, "printer.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return user


def path(home_dir):
    return os.path.join(home_dir, "printer.json")


def normalize_connection(conn):
    """`host:port` with no scheme is TCP, as the old `THERMAL_PRINTER` was."""
    conn = conn.strip()
    bare = (
        "://" not in conn
        and not conn.startswith(("cups:", "win32:", "file:", "/"))
        and not re.match(r"(?i)(COM|LPT)[0-9]+:?\Z", conn)
    )
    if not bare:
        return conn
    host, port = conn, "9100"
    if ":" in conn:
        h, p = conn.rsplit(":", 1)
        if _UINT.match(p) and int(p) <= 65535:
            host, port = h, p
    return "tcp://%s:%s" % (host, port)


def _read_doc(home_dir):
    p = path(home_dir)
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raise Error(
            NOT_CONFIGURED,
            "thermal is not configured: no %s" % p,
            "run `print-to-thermal setup --printer <connection> --model <model>` "
            "(see `print-to-thermal discover` and `print-to-thermal models`)",
        )
    except OSError as e:
        raise Error(500, "cannot read %s: %s" % (p, e))
    try:
        return json.loads(raw)
    except ValueError as e:
        raise Error(500, "%s: %s" % (p, e))


def resolve(doc):
    """Build a config from a `printer.json` document: the preset, overlaid
    with the file's own printer fields."""
    own = doc.get("printer") if isinstance(doc, dict) else None
    own = dict(own) if isinstance(own, dict) else {}
    # Before presets, the file held host and port.
    if "connection" not in own and isinstance(own.get("host"), str):
        port = own.get("port")
        port = port if isinstance(port, int) and not isinstance(port, bool) and port >= 0 else 9100
        own["connection"] = "tcp://%s:%d" % (own["host"], port)
    own.pop("host", None)
    own.pop("port", None)
    named = own.get("model") if isinstance(own.get("model"), str) else None
    mid = named if named in MODELS else DEFAULT_MODEL
    if named is not None and named != mid:
        # Not a preset (e.g. a free-text model name from an old file): keep
        # it as the display name.
        own.setdefault("name", named)
    merged = model(mid)
    if not isinstance(merged, dict):
        merged = {}
    merged.update(own)
    merged["model"] = mid
    try:
        printer = _struct(merged, PRINTER_FIELDS)
    except _Invalid as e:
        raise Error(500, "printer.json printer: %s" % e)
    target = env("THERMAL_PRINTER")
    if target:
        printer["connection"] = target
    printer["connection"] = normalize_connection(printer["connection"])
    raw_stocks = doc.get("stocks", {}) if isinstance(doc, dict) else {}
    stocks = {}
    try:
        if not isinstance(raw_stocks, dict):
            raise _Invalid("invalid type: expected a map")
        for sid in sorted(raw_stocks):
            stocks[sid] = _struct(raw_stocks[sid], STOCK_FIELDS)
    except _Invalid as e:
        raise Error(500, "printer.json stocks: %s" % e)
    loaded = doc.get("loaded") if isinstance(doc, dict) else None
    if not isinstance(loaded, str):
        loaded = next(iter(stocks), "")
    return {"printer": printer, "stocks": stocks, "loaded": loaded}


def load(home_dir):
    return resolve(_read_doc(home_dir))


def _write(home_dir, doc):
    with open(path(home_dir), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def edit(home_dir, change):
    """Change `printer.json` in place, keeping hand-written notes and fields
    this version does not know about. The result must still load, or nothing
    is written."""
    doc = _read_doc(home_dir)
    change(doc)
    config = resolve(doc)
    if config["loaded"] not in config["stocks"]:
        raise bad('loaded stock "%s" is not one of the stocks' % config["loaded"])
    _write(home_dir, doc)
    return config


def set_loaded(home_dir, sid):
    """Record which stock is loaded."""

    def change(doc):
        stocks = doc.get("stocks") if isinstance(doc.get("stocks"), dict) else {}
        if sid not in stocks:
            raise bad('unknown stock "%s"' % sid, "known stocks: %s" % ", ".join(stocks))
        doc["loaded"] = sid

    edit(home_dir, change)


def parse_stock(spec, printer):
    """Parse `id=width_mm[:dots][:adhesive]`, e.g. `label-60=60:384:adhesive`.
    Without dots the width is estimated from the printer and flagged, since
    only a calibration print can say what really prints."""

    def usage():
        return bad(
            'stock "%s": expected id=width_mm[:dots][:adhesive]' % spec,
            "e.g. receipt-80=80:576 or label-60=60:384:adhesive",
        )

    if "=" not in spec:
        raise usage()
    sid, rest = spec.split("=", 1)
    sid = sid.strip()
    if not sid or not re.match(r"[A-Za-z0-9_-]+\Z", sid):
        raise usage()
    parts = [p.strip() for p in rest.split(":")]
    width = re.sub(r"(mm)+\Z", "", parts[0])
    if not _UINT.match(width) or int(width) > 0xFFFFFFFF:
        raise usage()
    dots, adhesive = None, False
    for part in parts[1:]:
        if part == "adhesive":
            adhesive = True
        elif part in ("plain", ""):
            pass
        elif _UINT.match(part) and int(part) <= 0xFFFFFFFF:
            dots = int(part)
        else:
            raise usage()
    return sid, new_stock(int(width), dots, adhesive, printer)


def new_stock(width_mm, dots, adhesive, printer):
    estimated = dots is None
    print_dots = min(dots if dots is not None else _estimate_dots(width_mm, printer), printer["carriage_dots"])
    return {
        "description": "%dmm %s roll" % (width_mm, "adhesive" if adhesive else "plain"),
        "width_mm": width_mm,
        "print_dots": print_dots,
        "adhesive": adhesive,
        "uses": [],
        "notes": (
            ["print_dots is estimated, not measured: run `print-to-thermal calibrate` and record what prints."]
            if estimated
            else []
        ),
    }


def _estimate_dots(width_mm, printer):
    """Paper at the printer's full width uses the whole carriage; narrower
    paper scales down, rounded to a whole byte of dots."""
    if printer["paper_mm"] == 0 or width_mm >= printer["paper_mm"]:
        return printer["carriage_dots"]
    return (printer["carriage_dots"] * width_mm // printer["paper_mm"]) // 8 * 8


def setup(home_dir, connection, model_id, stock_specs, force):
    """Write a fresh `printer.json`. Refuses to replace one unless `force`."""
    file = path(home_dir)
    if os.path.exists(file) and not force:
        raise Error(
            409,
            "%s already exists" % file,
            "change it with `print-to-thermal set` / `print-to-thermal stock add`, or pass --force to start over",
        )
    model(model_id)
    doc = {"printer": {"model": model_id, "connection": normalize_connection(connection)}, "stocks": {}, "loaded": ""}
    printer = resolve(doc)["printer"]
    entries = {}
    for spec in stock_specs:
        sid, stk = parse_stock(spec, printer)
        entries[sid] = stk
    if not entries:
        w = max(printer["paper_mm"], 1)
        entries["roll-%d" % w] = new_stock(w, printer["carriage_dots"], False, printer)
    doc["loaded"] = next(iter(entries), "")
    doc["stocks"] = entries
    os.makedirs(home_dir, exist_ok=True)
    _write(home_dir, doc)
    return load(home_dir)


def _no_constant(name):
    raise ValueError(name)


def set_field(home_dir, key, value):
    """Set one printer field. The value is parsed as JSON when it can be, so
    numbers and booleans come through; anything else is a string."""
    if key not in SETTABLE:
        raise bad('cannot set "%s"' % key, "settable: %s" % ", ".join(SETTABLE))
    try:
        parsed = json.loads(value, parse_constant=_no_constant)
        if not isinstance(parsed, (bool, int, float)):
            raise ValueError
    except ValueError:
        parsed = normalize_connection(value) if key == "connection" else value

    def change(doc):
        if not isinstance(doc.get("printer"), dict):
            doc["printer"] = {}
        doc["printer"][key] = parsed

    return edit(home_dir, change)


def add_stock(home_dir, spec):
    printer = load(home_dir)["printer"]
    sid, stk = parse_stock(spec, printer)

    def change(doc):
        if not isinstance(doc.get("stocks"), dict):
            doc["stocks"] = {}
        doc["stocks"][sid] = stk
        if not isinstance(doc.get("loaded"), str) or not doc["loaded"]:
            doc["loaded"] = sid

    return edit(home_dir, change)


def remove_stock(home_dir, sid):
    def change(doc):
        if doc.get("loaded") == sid:
            raise bad('"%s" is the loaded stock' % sid, "record another roll as loaded first (`print-to-thermal stock <id>`)")
        stocks = doc.get("stocks")
        if not isinstance(stocks, dict) or sid not in stocks:
            raise Error(404, 'no stock "%s"' % sid)
        del stocks[sid]

    return edit(home_dir, change)
