"""Small helpers shared by every module: the error type, Rust-compatible
number handling, and JSON output."""

import json
import math
import struct


class Error(Exception):
    """One error type for CLI and HTTP: a message, an optional hint on what to
    do about it, and the HTTP status it maps to."""

    def __init__(self, status, message, hint=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.hint = hint

    def with_hint(self, hint):
        self.hint = hint
        return self

    def to_json(self):
        return {"error": self.message, "hint": self.hint}

    def __str__(self):
        if self.hint:
            return "%s\n  hint: %s" % (self.message, self.hint)
        return self.message


def bad(message, hint=None):
    """A problem with the job itself."""
    return Error(400, message, hint)


# Status used for "nothing configured yet"; the CLI exits 3 on it.
NOT_CONFIGURED = 428


# Paper geometry is single precision throughout, so values that land on a
# rounding boundary round the same way everywhere.


def f32(x):
    """Round a float to single precision."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def rround(x):
    """Round half away from zero (Python's round() rounds half to even)."""
    return math.copysign(math.floor(abs(x) + 0.5), x)


def f32_short(x):
    """The shortest decimal that reads back as the same single-precision value."""
    x = f32(x)
    if x == 0 or math.isinf(x) or math.isnan(x):
        return repr(float(x))
    for digits in range(1, 10):
        s = "%.*g" % (digits, x)
        if f32(float(s)) == x:
            return s
    return repr(x)


def f32_json(x):
    """A single-precision value as JSON shows it: widened to double, as
    serde_json::Value holds it (18.8 -> 18.799999237060547)."""
    return f32(x)


def f32_display(x):
    """A single-precision float as text, without a trailing ".0"."""
    s = f32_short(x)
    if "e" in s or "E" in s:
        s = format(float(s), "f").rstrip("0").rstrip(".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def io_error(e):
    """An OSError worded as Rust words io::Error."""
    import socket

    if isinstance(e, socket.timeout):
        return "connection timed out"
    if getattr(e, "errno", None) and getattr(e, "strerror", None):
        return "%s (os error %d)" % (e.strerror, e.errno)
    return str(e)


def expected(names):
    """serde's "expected ..." for a set of variants."""
    ticks = ["`%s`" % n for n in names]
    if len(ticks) == 1:
        return "expected %s" % ticks[0]
    if len(ticks) == 2:
        return "expected %s or %s" % tuple(ticks)
    return "expected one of %s" % ", ".join(ticks)


def dumps(value):
    """Pretty JSON, two-space indent, non-ASCII kept."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def compact(value):
    """Compact JSON, as error messages quote values."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def rust_lines(s):
    """str::lines: split on newlines, drop a trailing \\r, no final empty line."""
    if not s:
        return []
    parts = s.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]
