"""Text folding and wrapping for the printer's built-in font."""

# Typographic characters mapped to printable ASCII. The MHT-P80E prints
# nothing for most non-ASCII (the degree sign included), so text is folded
# BEFORE wrapping: several replacements are longer than what they replace
# ('½' -> '1/2') and would otherwise push wrapped lines past the edge.
FOLDING = {
    "°": "",
    "—": "--",
    "–": "-",
    "−": "-",
    "×": "x",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    "½": "1/2",
    "¼": "1/4",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    " ": " ",
    "•": "*",
    "✓": "v",
    "☐": "[ ]",
    "☑": "[x]",
    "→": "->",
    "←": "<-",
}

_ACCENTS = [
    ("àáâãäåā", "a"),
    ("ÀÁÂÃÄÅĀ", "A"),
    ("çć", "c"),
    ("ÇĆ", "C"),
    ("èéêëē", "e"),
    ("ÈÉÊËĒ", "E"),
    ("ìíîïī", "i"),
    ("ÌÍÎÏĪ", "I"),
    ("ñń", "n"),
    ("ÑŃ", "N"),
    ("òóôõöøō", "o"),
    ("ÒÓÔÕÖØŌ", "O"),
    ("ùúûüū", "u"),
    ("ÙÚÛÜŪ", "U"),
    ("ýÿ", "y"),
    ("ÝŸ", "Y"),
    ("ß", "s"),
]
STRIP_ACCENT = {ch: base for chars, base in _ACCENTS for ch in chars}


def fold(text):
    """Fold to printable ASCII. Accented letters lose their accent; anything
    else outside 0x20-0x7E is dropped, because a missing character beats a
    wrong one."""
    out = []
    for ch in text:
        if ch in FOLDING:
            out.append(FOLDING[ch])
        elif ch == "\n" or " " <= ch <= "~":
            out.append(ch)
        elif ch == "\t":
            out.append("  ")
        elif ch in STRIP_ACCENT:
            out.append(STRIP_ACCENT[ch])
    return "".join(out)


def wrap(text, cols):
    """Wrap to `cols` characters, breaking at spaces. Existing newlines are
    hard breaks. A word longer than the whole line is split where it
    overflows, without a hyphen."""
    cols = max(cols, 1)
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in (w for w in para.split(" ") if w):
            need = len(word) if not line else len(line) + 1 + len(word)
            if need <= cols:
                line = word if not line else line + " " + word
                continue
            if line:
                lines.append(line)
                line = ""
            while len(word) > cols:
                lines.append(word[:cols])
                word = word[cols:]
            line = word
        lines.append(line)
    return lines


def hang(prefix, body, cols):
    """Wrap `body` under a `prefix` with a hanging indent, so continuation
    lines sit under the text rather than under the checkbox or bullet."""
    indent = " " * len(prefix)
    lines = wrap(body, max(cols - len(prefix), 1))
    return [(prefix if i == 0 else indent) + line for i, line in enumerate(lines)]
