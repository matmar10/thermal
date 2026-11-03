//! Text folding and wrapping for the printer's built-in font.

/// Typographic characters mapped to printable ASCII. The MHT-P80E prints
/// nothing for most non-ASCII (the degree sign included), so text is folded
/// BEFORE wrapping: several replacements are longer than what they replace
/// ('½' -> '1/2') and would otherwise push wrapped lines past the edge.
const FOLDING: &[(char, &str)] = &[
    ('°', ""),
    ('—', "--"),
    ('–', "-"),
    ('−', "-"),
    ('×', "x"),
    ('‘', "'"),
    ('’', "'"),
    ('“', "\""),
    ('”', "\""),
    ('…', "..."),
    ('½', "1/2"),
    ('¼', "1/4"),
    ('¾', "3/4"),
    ('⅓', "1/3"),
    ('⅔', "2/3"),
    ('⅛', "1/8"),
    ('\u{a0}', " "),
    ('•', "*"),
    ('✓', "v"),
    ('☐', "[ ]"),
    ('☑', "[x]"),
    ('→', "->"),
    ('←', "<-"),
];

/// Fold to printable ASCII. Accented letters lose their accent; anything else
/// outside 0x20-0x7E is dropped, because a missing character beats a wrong one.
pub fn fold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        if let Some((_, rep)) = FOLDING.iter().find(|(c, _)| *c == ch) {
            out.push_str(rep);
        } else if ch == '\n' || (' '..='~').contains(&ch) {
            out.push(ch);
        } else if ch == '\t' {
            out.push_str("  ");
        } else if let Some(base) = strip_accent(ch) {
            out.push(base);
        }
    }
    out
}

fn strip_accent(ch: char) -> Option<char> {
    const MAP: &[(&str, char)] = &[
        ("àáâãäåā", 'a'),
        ("ÀÁÂÃÄÅĀ", 'A'),
        ("çć", 'c'),
        ("ÇĆ", 'C'),
        ("èéêëē", 'e'),
        ("ÈÉÊËĒ", 'E'),
        ("ìíîïī", 'i'),
        ("ÌÍÎÏĪ", 'I'),
        ("ñń", 'n'),
        ("ÑŃ", 'N'),
        ("òóôõöøō", 'o'),
        ("ÒÓÔÕÖØŌ", 'O'),
        ("ùúûüū", 'u'),
        ("ÙÚÛÜŪ", 'U'),
        ("ýÿ", 'y'),
        ("ÝŸ", 'Y'),
        ("ß", 's'),
    ];
    MAP.iter().find(|(set, _)| set.contains(ch)).map(|(_, base)| *base)
}

/// Wrap to `cols` characters, breaking at spaces. Existing newlines are hard
/// breaks. A word longer than the whole line is split where it overflows,
/// without a hyphen, rather than hyphenated mid-word at a guessed point.
pub fn wrap(text: &str, cols: usize) -> Vec<String> {
    let cols = cols.max(1);
    let mut lines = Vec::new();
    for para in text.split('\n') {
        let mut line = String::new();
        for word in para.split(' ').filter(|w| !w.is_empty()) {
            let mut word = word.to_string();
            let need = if line.is_empty() { word.len() } else { line.len() + 1 + word.len() };
            if need <= cols {
                if !line.is_empty() {
                    line.push(' ');
                }
                line.push_str(&word);
                continue;
            }
            if !line.is_empty() {
                lines.push(std::mem::take(&mut line));
            }
            while word.len() > cols {
                let rest = word.split_off(cols);
                lines.push(word);
                word = rest;
            }
            line = word;
        }
        lines.push(line);
    }
    lines
}

/// Wrap `body` under a `prefix` with a hanging indent, so continuation lines
/// sit under the text rather than under the checkbox or bullet.
pub fn hang(prefix: &str, body: &str, cols: usize) -> Vec<String> {
    let indent = " ".repeat(prefix.len());
    wrap(body, cols.saturating_sub(prefix.len()).max(1))
        .into_iter()
        .enumerate()
        .map(|(i, line)| if i == 0 { format!("{prefix}{line}") } else { format!("{indent}{line}") })
        .collect()
}
