"""The operations both front ends expose. The CLI and the HTTP server are thin
adapters over these, so they cannot drift apart."""

import json
import os
import re

from . import config as cfg
from . import escpos, job, layout, preview, transport
from .util import Error, bad, f32, f32_json, rround

_HERE = os.path.dirname(os.path.abspath(__file__))

# Templates shipped with the plugin. A user template of the same name, in
# `<home>/templates/`, replaces one.
BUILTIN_TEMPLATES = ["day", "folder-tab", "item-label", "note", "shopping-list"]


def prepare(home, config, source, data=None, stock=None, copies=None, allow_paths=False):
    """Expand and lay out a job. `source` is a job/template document (dict)
    or a saved template's name (str). Returns {job, layout, mismatch}."""
    doc = load_template(home, source) if isinstance(source, str) else source
    j = job.expand(doc, data or {})
    if copies is not None:
        j["copies"] = copies
    stock_id = stock or j["stock"] or config["loaded"]
    mismatch = None
    if stock_id != config["loaded"]:
        mismatch = "this job is for %s but %s is recorded as loaded" % (stock_id, config["loaded"])
    lay = layout.layout(j, config, stock_id, allow_paths)
    return {"job": j, "layout": lay, "mismatch": mismatch}


def preview_report(config, prepared):
    lay = prepared["layout"]
    text, labels = preview.render(lay, config)
    warnings = list(lay["warnings"])
    if prepared["mismatch"]:
        warnings.append(prepared["mismatch"])
    return {
        "stock": lay["stock_id"],
        "adhesive": config["stocks"][lay["stock_id"]]["adhesive"],
        "preview": text,
        "labels_mm": [f32_json(rround(f32(mm * 10.0)) / 10.0) for mm in labels],
        "warnings": warnings,
    }


def print_job(config, prepared, force, dry_run):
    """Check the stock and the paper, then send. `dry_run` does everything
    but send. Returns (report, bytes). `sent` means the printer accepted the
    bytes; it does NOT confirm paper came out."""
    lay = prepared["layout"]
    if prepared["mismatch"] and not force:
        sid = lay["stock_id"]
        raise Error(
            409,
            "refusing to print: %s" % prepared["mismatch"],
            "load the %s roll and record it (`print-to-thermal stock %s` / PUT /stock), or force it (--force / ?force=true)"
            % (sid, sid),
        )
    data = escpos.encode(lay, config)
    warnings = list(lay["warnings"])
    printer = config["printer"]
    try:
        target = transport.describe(transport.parse(printer["connection"]))
    except Error:
        target = printer["connection"]
    if dry_run:
        paper = "not checked (dry run)"
    else:
        st = transport.status(printer)
        if st["paper"] == "out":
            raise Error(503, "the printer reports it is out of paper", "load a roll, then try again")
        if st["paper"] == "low":
            warnings.append("paper is near the end of the roll")
        if st["paper"] == "unknown" and st.get("error"):
            warnings.append("paper status unknown (%s)" % st["error"])
        transport.send(printer, data)
        paper = st["paper"]
    report = {
        "sent": not dry_run,
        "bytes": len(data),
        "stock": lay["stock_id"],
        "adhesive": config["stocks"][lay["stock_id"]]["adhesive"],
        "copies": prepared["job"]["copies"],
        "paper": paper,
        "target": target,
        "warnings": warnings,
    }
    return report, data


def stocks(config):
    """The printer, every stock with its column counts, and the loaded stock:
    everything needed to design a job."""
    top = config["printer"]["max_text_size"]
    out = {}
    for sid, s in config["stocks"].items():
        v = dict(s)
        v["columns"] = {"%dx" % n: cfg.cols(config, s, n) for n in range(1, top + 1)}
        v["loaded"] = sid == config["loaded"]
        out[sid] = v
    return {"printer": cfg.printer_json(config["printer"]), "loaded": config["loaded"], "stocks": out}


def set_stock(home, sid):
    cfg.set_loaded(home, sid)
    config = cfg.load(home)
    s = cfg.stock(config, sid)
    return {"loaded": sid, "adhesive": s["adhesive"], "description": s["description"]}


def _templates_dir(home):
    return os.path.join(home, "templates")


def _template_name(name):
    while name.endswith(".json"):
        name = name[: -len(".json")]
    if not name or not re.match(r"[A-Za-z0-9_-]+\Z", name):
        raise bad('bad template name "%s"' % name, "use letters, digits, - and _")
    return name


def _template_path(home, name):
    return os.path.join(_templates_dir(home), _template_name(name) + ".json")


def _builtin(name):
    if name not in BUILTIN_TEMPLATES:
        return None
    with open(os.path.join(_HERE, "templates", name + ".json"), encoding="utf-8") as f:
        return f.read()


def load_template(home, name):
    path = _template_path(home, name)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raw = _builtin(_template_name(name))
        if raw is None:
            known = [t["name"] for t in list_templates(home)]
            raise Error(404, 'no template "%s"' % name, "templates: %s" % ", ".join(known))
    try:
        return json.loads(raw)
    except ValueError as e:
        raise Error(500, "template %s: %s" % (name, e))


def list_templates(home):
    docs = {}
    for name in BUILTIN_TEMPLATES:
        try:
            docs[name] = ("built-in", json.loads(_builtin(name)))
        except ValueError:
            docs[name] = ("built-in", None)
    try:
        entries = os.listdir(_templates_dir(home))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        try:
            with open(os.path.join(_templates_dir(home), entry), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            doc = None
        docs[entry[: -len(".json")]] = ("user", doc)
    out = []
    for name in sorted(docs):
        source, doc = docs[name]
        d = doc if isinstance(doc, dict) else {}
        out.append(
            {
                "name": name,
                "source": source,
                "description": d.get("description"),
                "stock": d.get("stock"),
                "params": d.get("params"),
                "defaults": d.get("defaults"),
            }
        )
    return out


def validate_template(doc):
    """Check a template's shape. Full validation needs data, so this checks
    what can be checked without it, then trial-expands with its defaults plus
    a placeholder for every declared param."""
    blocks = doc.get("blocks") if isinstance(doc, dict) else None
    if not isinstance(blocks, list):
        raise bad('a template needs a "blocks" array')

    def check(blocks):
        for i, b in enumerate(blocks):
            t = b.get("type") if isinstance(b, dict) else None
            t = t if isinstance(t, str) else ""
            if t not in job.BLOCK_TYPES:
                raise bad('block %d: unknown type "%s"' % (i + 1, t), "types: %s" % ", ".join(job.BLOCK_TYPES))
            if t == "each":
                inner = b.get("blocks")
                check(inner if isinstance(inner, list) else [])

    check(blocks)
    sample = {}
    if isinstance(doc.get("params"), dict):
        for name in doc["params"]:
            sample[name] = ["sample"]
    if isinstance(doc.get("defaults"), dict):
        sample.update(doc["defaults"])
    # Only structural errors matter here; missing or oddly-typed sample
    # values do not.
    try:
        job.expand(doc, sample)
    except Error as e:
        if e.message.startswith("invalid job") and "unknown" in e.message:
            raise


def save_template(home, name, doc):
    validate_template(doc)
    path = _template_path(home, name)
    os.makedirs(_templates_dir(home), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def delete_template(home, name):
    path = _template_path(home, name)
    try:
        os.remove(path)
    except OSError:
        trimmed = name
        while trimmed.endswith(".json"):
            trimmed = trimmed[: -len(".json")]
        if trimmed in BUILTIN_TEMPLATES:
            raise bad('"%s" is built in and cannot be deleted' % name, "save a template of the same name to replace it")
        raise Error(404, 'no template "%s"' % name)


def config_report(home, config=None):
    """The resolved configuration and where it came from."""
    if config is None:
        config = cfg.load(home)
    return {
        "file": cfg.path(home),
        "connection": config["printer"]["connection"],
        "printer": cfg.printer_json(config["printer"]),
        "stocks": config["stocks"],
        "loaded": config["loaded"],
    }


def models():
    """The bundled printer presets, briefly."""
    out = []
    for mid in cfg.MODELS:
        try:
            m = cfg.model(mid)
        except Error:
            continue
        out.append(
            {
                "model": mid,
                "name": m.get("name"),
                "paper_mm": m.get("paper_mm"),
                "carriage_dots": m.get("carriage_dots"),
                "dots_per_mm": m.get("dots_per_mm"),
                "cutter": m.get("cutter"),
                "qr": m.get("qr"),
                "notes": m.get("notes"),
            }
        )
    return out


def calibration(config):
    """A ruler across the whole carriage: a tick every 8 dots, a numbered
    tick every 48. Whatever number is the last one fully on the paper is the
    stock's printable width in dots. Returns (config, prepared)."""
    carriage = config["printer"]["carriage_dots"]
    ruler_cfg = dict(config)
    ruler_cfg["stocks"] = dict(config["stocks"])
    ruler_cfg["stocks"]["calibration"] = {
        "description": "full carriage",
        "width_mm": config["printer"]["paper_mm"],
        "print_dots": carriage,
        "adhesive": False,
        "uses": [],
        "notes": [],
    }
    j = job.expand({"blocks": []}, {})
    lay = layout.layout(j, ruler_cfg, "calibration", False)
    lay["items"] = [
        layout.line("CALIBRATION", bold=True),
        layout.line("Last number fully"),
        layout.line("on the paper = dots"),
        layout.ruler(carriage),
    ] + lay["items"]
    return ruler_cfg, {"job": j, "layout": lay, "mismatch": None}
