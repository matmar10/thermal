"""The job format, and the template expansion that turns a template plus data
into a job.

A job is a dict:
    {"description", "params", "defaults", "stock", "copies", "cut", "feed", "blocks"}
and each block a dict with "type" and that type's fields, every field present
(None where not given) after `parse_job`.
"""

import copy
import math
import re

from .util import bad, compact, expected, rust_lines

# Block types a template may use, including the template-only `each`.
BLOCK_TYPES = ["text", "big", "rule", "row", "space", "barcode", "qr", "image", "cut", "each"]

ALIGNS = ["left", "center", "right"]
HRIS = ["none", "above", "below", "both"]
CUTS = ["partial", "full", "none"]
SYMBOLOGIES = ["CODE128", "CODE39", "EAN13", "EAN8", "UPC_A", "UPC_E", "ITF", "CODABAR"]


class _Invalid(Exception):
    pass


_UINT = re.compile(r"\+?[0-9]+\Z")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _describe(v):
    """How serde names a JSON value in "invalid type" errors."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean `%s`" % ("true" if v else "false")
    if _is_int(v):
        return "integer `%d`" % v
    if isinstance(v, float):
        return "floating point `%s`" % _num_text(v)
    if isinstance(v, str):
        return "string %s" % compact(v)
    if isinstance(v, list):
        return "sequence"
    return "map"


def _num_text(v):
    if _is_int(v):
        return str(v)
    if math.isfinite(v) and v == int(v) and abs(v) < 1e16:
        return "%d.0" % v
    return repr(v)


def scalar_to_string(v):
    # Template substitution can turn a placeholder into a number or bool, so
    # string fields accept scalars and numeric fields accept numeric strings.
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if _is_num(v):
        return _num_text(v)
    if v is None:
        return ""
    raise _Invalid("expected text, got %s" % compact(v))


# Field readers, each like one serde attribute in the Rust job format.


def de_str(v):
    return scalar_to_string(v)


def de_opt_str(v):
    return None if v is None else scalar_to_string(v)


def de_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0", ""):
            return False
        raise _Invalid('expected true/false, got "%s"' % s)
    raise _Invalid("expected true/false, got %s" % compact(v))


def de_opt_f32(v):
    if v is None:
        return None
    if _is_num(v):
        return float(v)
    if isinstance(v, str):
        if not v.strip():
            return None
        try:
            return float(v.strip())
        except ValueError:
            raise _Invalid("invalid float literal")
    raise _Invalid("expected a number, got %s" % compact(v))


def de_u32(v):
    if _is_int(v) and v >= 0:
        return v & 0xFFFFFFFF
    if isinstance(v, str):
        s = v.strip()
        if _UINT.match(s) and int(s) <= 0xFFFFFFFF:
            return int(s)
        raise _Invalid("invalid digit found in string")
    if _is_num(v):
        raise _Invalid("expected a whole number")
    raise _Invalid("expected a whole number, got %s" % compact(v))


def opt_u8(v):
    if v is None:
        return None
    if _is_int(v) and 0 <= v <= 255:
        return v
    if _is_int(v):
        raise _Invalid("invalid value: integer `%d`, expected u8" % v)
    raise _Invalid("invalid type: %s, expected u8" % _describe(v))


def opt_bool(v):
    if v is None or isinstance(v, bool):
        return v
    raise _Invalid("invalid type: %s, expected a boolean" % _describe(v))


def opt_string(v):
    if v is None or isinstance(v, str):
        return v
    raise _Invalid("invalid type: %s, expected a string" % _describe(v))


def _enum(names, what):
    def read(v):
        if v is None:
            return None
        if isinstance(v, str) and v in names:
            return v
        if isinstance(v, str):
            raise _Invalid("unknown variant `%s`, %s" % (v, expected(names)))
        raise _Invalid("invalid type: %s, expected %s" % (_describe(v), what))

    return read


opt_align = _enum(ALIGNS, "enum Align")
opt_hri = _enum(HRIS, "enum Hri")
opt_cut = _enum(CUTS, "enum CutKind")
opt_symbology = _enum(SYMBOLOGIES, "enum Symbology")


def opt_size(v):
    """A built-in text size: `2` for 2x2, `[w, h]` for a stretched glyph, or
    "2". Returned as (w, h)."""
    if v is None:
        return None
    if _is_int(v) and 0 <= v <= 255:
        return (v, v)
    if isinstance(v, list) and len(v) == 2 and all(_is_int(n) and 0 <= n <= 255 for n in v):
        return (v[0], v[1])
    if isinstance(v, str):
        s = v.strip()
        if _UINT.match(s) and int(s) <= 255:
            return (int(s), int(s))
    raise _Invalid("data did not match any variant of untagged enum Size")


def opt_width(v):
    """Image width: dots, or "full" for the whole print width."""
    if v is None:
        return None
    if _is_int(v) and 0 <= v <= 0xFFFFFFFF:
        return v
    if isinstance(v, str):
        return v
    raise _Invalid("data did not match any variant of untagged enum Width")


# Field name -> (reader, required). Required fields have no default.
BLOCKS = {
    # Text in the printer's built-in font, wrapped to the stock width.
    "text": {
        "text": (de_str, True),
        "size": (opt_size, False),
        "bold": (de_bool, False),
        "underline": (de_bool, False),
        "invert": (de_bool, False),
        "align": (opt_align, False),
        "bullet": (de_opt_str, False),
        "wrap": (opt_bool, False),
    },
    # Text drawn as a bitmap: any height, any character the font has.
    "big": {
        "text": (de_str, True),
        "height": (de_opt_f32, False),
        "fit": (de_bool, False),
        "sideways": (de_bool, False),
        "align": (opt_align, False),
    },
    # A full-width line of a repeated character.
    "rule": {"char": (de_opt_str, False), "size": (opt_size, False)},
    # Left and right text on one line with a fill between: "Milk ....... 2".
    "row": {
        "left": (de_str, True),
        "right": (de_str, True),
        "fill": (de_opt_str, False),
        "size": (opt_size, False),
        "bold": (de_bool, False),
    },
    # Vertical space, in 1x lines or mm.
    "space": {"lines": (opt_u8, False), "mm": (de_opt_f32, False)},
    "barcode": {
        "data": (de_str, True),
        "symbology": (opt_symbology, False),
        "height": (de_opt_f32, False),
        "module": (opt_u8, False),
        "hri": (opt_hri, False),
        "align": (opt_align, False),
    },
    "qr": {
        "data": (de_str, True),
        "module": (opt_u8, False),
        "ec": (opt_string, False),
        "align": (opt_align, False),
    },
    "image": {
        "path": (de_opt_str, False),
        "base64": (de_opt_str, False),
        "width": (opt_width, False),
        "align": (opt_align, False),
        "dither": (de_bool, False),
    },
    # Cut here, in the middle of a job: one job, several labels.
    "cut": {"kind": (opt_cut, False)},
}

JOB_FIELDS = ["description", "params", "defaults", "stock", "copies", "cut", "feed", "blocks"]


def _parse_block(raw):
    if not isinstance(raw, dict):
        raise _Invalid("invalid type: %s, expected internally tagged enum Block" % _describe(raw))
    if "type" not in raw:
        raise _Invalid("missing field `type`")
    kind = raw["type"]
    if not isinstance(kind, str) or kind not in BLOCKS:
        if isinstance(kind, str):
            raise _Invalid("unknown variant `%s`, %s" % (kind, expected(list(BLOCKS))))
        raise _Invalid("invalid type: %s, expected variant identifier" % _describe(kind))
    fields = BLOCKS[kind]
    block = {"type": kind}
    for key, value in raw.items():
        if key == "type":
            continue
        if key not in fields:
            raise _Invalid("unknown field `%s`, %s" % (key, expected(list(fields))))
        reader, _ = fields[key]
        block[key] = reader(value)
    for key, (reader, required) in fields.items():
        if key not in block:
            if required:
                raise _Invalid("missing field `%s`" % key)
            block[key] = reader(None)
    return block


def parse_job(doc):
    """Check a filled-in job document against the format and normalise it."""
    try:
        if not isinstance(doc, dict):
            raise _Invalid("invalid type: %s, expected struct Job" % _describe(doc))
        for key in doc:
            if key not in JOB_FIELDS:
                raise _Invalid("unknown field `%s`, %s" % (key, expected(JOB_FIELDS)))
        if "blocks" not in doc:
            raise _Invalid("missing field `blocks`")
        job = {
            "description": opt_string(doc.get("description")),
            "params": doc.get("params") or {},
            "defaults": doc.get("defaults") or {},
            "stock": opt_string(doc.get("stock")),
            "copies": de_u32(doc["copies"]) if "copies" in doc else 1,
            "cut": "partial",
            "feed": opt_u8(doc.get("feed")),
            "blocks": None,
        }
        for key in ("params", "defaults"):
            if not isinstance(job[key], dict):
                raise _Invalid("invalid type: %s, expected a map" % _describe(doc[key]))
        if "cut" in doc:
            cut = doc["cut"]
            if cut is None:
                raise _Invalid("invalid type: null, expected enum CutKind")
            job["cut"] = opt_cut(cut)
        if not isinstance(doc["blocks"], list):
            raise _Invalid("invalid type: %s, expected a sequence" % _describe(doc["blocks"]))
        job["blocks"] = [_parse_block(b) for b in doc["blocks"]]
        return job
    except _Invalid as e:
        raise bad("invalid job: %s" % e)


def expand(doc, data):
    """Turn a job or template document plus data into a job.

    - "{{name}}" as a WHOLE string is replaced by the raw value, so arrays
      and numbers pass through ("items": "{{items}}").
    - {{name}} inside a longer string is replaced by its text.
    - {{a.b}} reads a field of an object, e.g. {{item.qty}} inside `each`.
    - {"type": "each", "items": "{{items}}", "as": "item", "blocks": [...]}
      repeats its blocks once per item, with {{item}} and {{index}} set.
    """
    if not isinstance(doc, dict):
        raise bad("a job must be a JSON object")
    defaults = doc.get("defaults")
    ctx = dict(defaults) if isinstance(defaults, dict) else {}
    ctx.update(data)
    params = doc.get("params") if isinstance(doc.get("params"), dict) else None

    out = {}
    for key, value in doc.items():
        if key == "blocks":
            out[key] = _expand_blocks(value, ctx, params)
        elif key in ("params", "defaults", "description"):
            out[key] = copy.deepcopy(value)
        else:
            out[key] = _fill(value, ctx, params)
    if "blocks" not in out:
        raise bad('a job needs a "blocks" array', '{"blocks": [{"type": "text", "text": "Hello"}]}')
    return parse_job(out)


def _expand_blocks(blocks, ctx, params):
    if not isinstance(blocks, list):
        raise bad('"blocks" must be an array')
    out = []
    for block in blocks:
        if not (isinstance(block, dict) and block.get("type") == "each"):
            out.append(_fill(block, ctx, params))
            continue
        items = _fill(block.get("items"), ctx, params)
        if isinstance(items, list):
            pass
        elif items is None:
            items = []
        elif isinstance(items, str):
            # A newline-separated string is a list too, which suits --set.
            items = [line.strip() for line in rust_lines(items) if line.strip()]
        else:
            raise bad('"each" needs an array of items, got %s' % compact(items))
        name = block.get("as") if isinstance(block.get("as"), str) else "item"
        if "blocks" not in block:
            raise bad('"each" needs "blocks"')
        inner = block["blocks"]
        for i, item in enumerate(items):
            scope = dict(ctx)
            scope[name] = item
            scope["index"] = i + 1
            out.extend(_expand_blocks(inner, scope, params))
    return out


def _fill(value, ctx, params):
    if isinstance(value, str):
        return _fill_str(value, ctx, params)
    if isinstance(value, list):
        return [_fill(v, ctx, params) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, ctx, params) for k, v in value.items()}
    return value


def _fill_str(s, ctx, params):
    if "{{" not in s:
        return s
    trimmed = s.strip()
    if trimmed.startswith("{{") and trimmed.endswith("}}") and trimmed.count("{{") == 1:
        return _lookup(trimmed[2:-2], ctx, params)
    out = []
    rest = s
    while "{{" in rest:
        start = rest.index("{{")
        out.append(rest[:start])
        end = rest.find("}}", start)
        if end < 0:
            raise bad('unclosed {{ in "%s"' % s)
        value = _lookup(rest[start + 2 : end], ctx, params)
        try:
            out.append(scalar_to_string(value))
        except _Invalid as e:
            raise bad('in "%s": %s' % (s, e))
        rest = rest[end + 2 :]
    out.append(rest)
    return "".join(out)


def _lookup(path, ctx, params):
    parts = path.strip().split(".")
    root = parts[0]
    if root not in ctx:
        about = ""
        if params is not None and root in params:
            d = params[root]
            about = " (%s)" % (d if isinstance(d, str) else compact(d))
        raise bad(
            'missing value for "%s"%s' % (root, about),
            "pass it with --set %s=... (CLI) or in \"data\" (HTTP)" % root,
        )
    current = ctx[root]
    for part in parts[1:]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            i = int(part) if _UINT.match(part) else None
            current = current[i] if i is not None and i < len(current) else None
        else:
            current = None
    return copy.deepcopy(current)
