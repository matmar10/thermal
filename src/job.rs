//! The job format, and the template expansion that turns a template plus data
//! into a job.

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};

use crate::error::{Error, Result};

/// A print job. A template is the same document with `{{placeholders}}`,
/// `params` describing them, `defaults` for optional ones, and `each` blocks.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// Template parameters: name -> what to pass.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub params: Map<String, Value>,
    /// Values used when the caller does not supply a parameter.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub defaults: Map<String, Value>,
    /// Stock this job is laid out for. Omit to use whatever is loaded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stock: Option<String>,
    #[serde(default = "one", deserialize_with = "de_u32")]
    pub copies: u32,
    #[serde(default)]
    pub cut: CutKind,
    /// Lines fed before each cut (default 3).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feed: Option<u8>,
    pub blocks: Vec<Block>,
}

fn one() -> u32 {
    1
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CutKind {
    #[default]
    Partial,
    Full,
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Align {
    Left,
    Center,
    Right,
}

/// A built-in text size: `2` for 2x2, or `[w, h]` for a stretched glyph.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Size {
    Uniform(u8),
    Stretch([u8; 2]),
    Text(#[serde(with = "size_str")] u8),
}

mod size_str {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(v: &u8, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_u8(*v)
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<u8, D::Error> {
        let s = String::deserialize(d)?;
        s.trim().parse().map_err(serde::de::Error::custom)
    }
}

impl Size {
    pub fn wh(self) -> (u8, u8) {
        match self {
            Size::Uniform(n) | Size::Text(n) => (n, n),
            Size::Stretch([w, h]) => (w, h),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Hri {
    None,
    Above,
    Below,
    Both,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[allow(clippy::upper_case_acronyms, non_camel_case_types)]
pub enum Symbology {
    CODE128,
    CODE39,
    EAN13,
    EAN8,
    UPC_A,
    UPC_E,
    ITF,
    CODABAR,
}

/// Image width: dots, or `"full"` for the whole print width.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Width {
    Dots(u32),
    Named(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum Block {
    /// Text in the printer's built-in font, wrapped to the stock width.
    Text {
        #[serde(deserialize_with = "de_str")]
        text: String,
        #[serde(default)]
        size: Option<Size>,
        #[serde(default, deserialize_with = "de_bool")]
        bold: bool,
        #[serde(default, deserialize_with = "de_bool")]
        underline: bool,
        #[serde(default, deserialize_with = "de_bool")]
        invert: bool,
        #[serde(default)]
        align: Option<Align>,
        /// Prefix for the first line; continuation lines are indented to match.
        #[serde(default, deserialize_with = "de_opt_str")]
        bullet: Option<String>,
        /// Set false to send lines exactly as given (overflow is flagged).
        #[serde(default)]
        wrap: Option<bool>,
    },
    /// Text drawn as a bitmap: any height, any character the font has.
    Big {
        #[serde(deserialize_with = "de_str")]
        text: String,
        /// Height of capital letters in mm (default 10).
        #[serde(default, deserialize_with = "de_opt_f32")]
        height: Option<f32>,
        /// Scale each line to fill the print width exactly.
        #[serde(default, deserialize_with = "de_bool")]
        fit: bool,
        /// Print along the strip instead of across it (a banner).
        #[serde(default, deserialize_with = "de_bool")]
        sideways: bool,
        #[serde(default)]
        align: Option<Align>,
    },
    /// A full-width line of a repeated character.
    Rule {
        #[serde(default, deserialize_with = "de_opt_str")]
        char: Option<String>,
        #[serde(default)]
        size: Option<Size>,
    },
    /// Left and right text on one line with a fill between: "Milk ....... 2".
    Row {
        #[serde(deserialize_with = "de_str")]
        left: String,
        #[serde(deserialize_with = "de_str")]
        right: String,
        #[serde(default, deserialize_with = "de_opt_str")]
        fill: Option<String>,
        #[serde(default)]
        size: Option<Size>,
        #[serde(default, deserialize_with = "de_bool")]
        bold: bool,
    },
    /// Vertical space, in 1x lines or mm.
    Space {
        #[serde(default)]
        lines: Option<u8>,
        #[serde(default, deserialize_with = "de_opt_f32")]
        mm: Option<f32>,
    },
    Barcode {
        #[serde(deserialize_with = "de_str")]
        data: String,
        #[serde(default)]
        symbology: Option<Symbology>,
        /// Bar height in mm (default 10).
        #[serde(default, deserialize_with = "de_opt_f32")]
        height: Option<f32>,
        /// Narrow bar width in dots, 2-6 (default 3; reduced to fit).
        #[serde(default)]
        module: Option<u8>,
        #[serde(default)]
        hri: Option<Hri>,
        #[serde(default)]
        align: Option<Align>,
    },
    Qr {
        #[serde(deserialize_with = "de_str")]
        data: String,
        /// Dots per QR module, 1-16 (default 6; reduced to fit).
        #[serde(default)]
        module: Option<u8>,
        /// Error correction: L, M, Q or H (default M).
        #[serde(default)]
        ec: Option<String>,
        #[serde(default)]
        align: Option<Align>,
    },
    Image {
        /// File path (CLI only).
        #[serde(default, deserialize_with = "de_opt_str")]
        path: Option<String>,
        /// Base64-encoded PNG or JPEG.
        #[serde(default, deserialize_with = "de_opt_str")]
        base64: Option<String>,
        #[serde(default)]
        width: Option<Width>,
        #[serde(default)]
        align: Option<Align>,
        /// Floyd-Steinberg dithering, for photographs. Off: hard threshold.
        #[serde(default, deserialize_with = "de_bool")]
        dither: bool,
    },
    /// Cut here, in the middle of a job: one job, several labels.
    Cut {
        #[serde(default)]
        kind: Option<CutKind>,
    },
}

// Template substitution can turn a placeholder into a number or bool, so
// string fields accept scalars and numeric fields accept numeric strings.

fn scalar_to_string(v: Value) -> std::result::Result<String, String> {
    match v {
        Value::String(s) => Ok(s),
        Value::Number(n) => Ok(n.to_string()),
        Value::Bool(b) => Ok(b.to_string()),
        Value::Null => Ok(String::new()),
        other => Err(format!("expected text, got {other}")),
    }
}

fn de_str<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<String, D::Error> {
    scalar_to_string(Value::deserialize(d)?).map_err(serde::de::Error::custom)
}

fn de_opt_str<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Option<String>, D::Error> {
    match Value::deserialize(d)? {
        Value::Null => Ok(None),
        v => scalar_to_string(v).map(Some).map_err(serde::de::Error::custom),
    }
}

fn de_bool<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<bool, D::Error> {
    match Value::deserialize(d)? {
        Value::Bool(b) => Ok(b),
        Value::Null => Ok(false),
        Value::String(s) => match s.trim() {
            "true" | "yes" | "1" => Ok(true),
            "false" | "no" | "0" | "" => Ok(false),
            other => Err(serde::de::Error::custom(format!("expected true/false, got \"{other}\""))),
        },
        other => Err(serde::de::Error::custom(format!("expected true/false, got {other}"))),
    }
}

fn de_opt_f32<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Option<f32>, D::Error> {
    match Value::deserialize(d)? {
        Value::Null => Ok(None),
        Value::Number(n) => Ok(n.as_f64().map(|f| f as f32)),
        Value::String(s) if s.trim().is_empty() => Ok(None),
        Value::String(s) => s.trim().parse().map(Some).map_err(serde::de::Error::custom),
        other => Err(serde::de::Error::custom(format!("expected a number, got {other}"))),
    }
}

fn de_u32<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<u32, D::Error> {
    match Value::deserialize(d)? {
        Value::Number(n) => n.as_u64().map(|n| n as u32).ok_or_else(|| serde::de::Error::custom("expected a whole number")),
        Value::String(s) => s.trim().parse().map_err(serde::de::Error::custom),
        other => Err(serde::de::Error::custom(format!("expected a whole number, got {other}"))),
    }
}

/// Block types a template may use, including the template-only `each`.
pub const BLOCK_TYPES: &[&str] = &["text", "big", "rule", "row", "space", "barcode", "qr", "image", "cut", "each"];

/// Turn a job or template document plus data into a `Job`.
///
/// - `"{{name}}"` as a WHOLE string is replaced by the raw value, so arrays
///   and numbers pass through (`"items": "{{items}}"`).
/// - `{{name}}` inside a longer string is replaced by its text.
/// - `{{a.b}}` reads a field of an object, e.g. `{{item.qty}}` inside `each`.
/// - `{"type": "each", "items": "{{items}}", "as": "item", "blocks": [...]}`
///   repeats its blocks once per item, with `{{item}}` and `{{index}}` set.
pub fn expand(doc: &Value, data: &Map<String, Value>) -> Result<Job> {
    let obj = doc.as_object().ok_or_else(|| Error::bad("a job must be a JSON object"))?;
    let mut ctx = obj.get("defaults").and_then(Value::as_object).cloned().unwrap_or_default();
    for (k, v) in data {
        ctx.insert(k.clone(), v.clone());
    }
    let params = obj.get("params").and_then(Value::as_object);

    let mut out = Map::new();
    for (key, value) in obj {
        let filled = match key.as_str() {
            "blocks" => Value::Array(expand_blocks(value, &ctx, params)?),
            "params" | "defaults" | "description" => value.clone(),
            _ => fill(value, &ctx, params)?,
        };
        out.insert(key.clone(), filled);
    }
    if !out.contains_key("blocks") {
        return Err(Error::bad("a job needs a \"blocks\" array").hint("{\"blocks\": [{\"type\": \"text\", \"text\": \"Hello\"}]}"));
    }
    Ok(serde_json::from_value(Value::Object(out))?)
}

fn expand_blocks(blocks: &Value, ctx: &Map<String, Value>, params: Option<&Map<String, Value>>) -> Result<Vec<Value>> {
    let list = blocks.as_array().ok_or_else(|| Error::bad("\"blocks\" must be an array"))?;
    let mut out = Vec::new();
    for block in list {
        if block.get("type").and_then(Value::as_str) != Some("each") {
            out.push(fill(block, ctx, params)?);
            continue;
        }
        let items = match fill(block.get("items").unwrap_or(&Value::Null), ctx, params)? {
            Value::Array(items) => items,
            Value::Null => Vec::new(),
            Value::String(s) if s.trim().is_empty() => Vec::new(),
            // A newline-separated string is a list too, which suits --set.
            Value::String(s) => s.lines().filter(|l| !l.trim().is_empty()).map(|l| Value::String(l.trim().to_string())).collect(),
            other => return Err(Error::bad(format!("\"each\" needs an array of items, got {other}"))),
        };
        let name = block.get("as").and_then(Value::as_str).unwrap_or("item");
        let inner = block.get("blocks").ok_or_else(|| Error::bad("\"each\" needs \"blocks\""))?;
        for (i, item) in items.into_iter().enumerate() {
            let mut scope = ctx.clone();
            scope.insert(name.to_string(), item);
            scope.insert("index".to_string(), Value::from(i + 1));
            out.extend(expand_blocks(inner, &scope, params)?);
        }
    }
    Ok(out)
}

fn fill(value: &Value, ctx: &Map<String, Value>, params: Option<&Map<String, Value>>) -> Result<Value> {
    Ok(match value {
        Value::String(s) => fill_str(s, ctx, params)?,
        Value::Array(a) => Value::Array(a.iter().map(|v| fill(v, ctx, params)).collect::<Result<_>>()?),
        Value::Object(o) => {
            let mut m = Map::new();
            for (k, v) in o {
                m.insert(k.clone(), fill(v, ctx, params)?);
            }
            Value::Object(m)
        }
        other => other.clone(),
    })
}

fn fill_str(s: &str, ctx: &Map<String, Value>, params: Option<&Map<String, Value>>) -> Result<Value> {
    if !s.contains("{{") {
        return Ok(Value::String(s.to_string()));
    }
    let trimmed = s.trim();
    if trimmed.starts_with("{{") && trimmed.ends_with("}}") && trimmed.matches("{{").count() == 1 {
        return lookup(&trimmed[2..trimmed.len() - 2], ctx, params);
    }
    let mut out = String::new();
    let mut rest = s;
    while let Some(start) = rest.find("{{") {
        out.push_str(&rest[..start]);
        let end = rest[start..].find("}}").ok_or_else(|| Error::bad(format!("unclosed {{{{ in \"{s}\"")))? + start;
        let value = lookup(&rest[start + 2..end], ctx, params)?;
        out.push_str(&scalar_to_string(value).map_err(|e| Error::bad(format!("in \"{s}\": {e}")))?);
        rest = &rest[end + 2..];
    }
    out.push_str(rest);
    Ok(Value::String(out))
}

fn lookup(path: &str, ctx: &Map<String, Value>, params: Option<&Map<String, Value>>) -> Result<Value> {
    let path = path.trim();
    let mut parts = path.split('.');
    let root = parts.next().unwrap_or_default();
    let missing = || {
        let about = params.and_then(|p| p.get(root)).map(|d| format!(" ({})", d.as_str().unwrap_or(&d.to_string()))).unwrap_or_default();
        Error::bad(format!("missing value for \"{root}\"{about}")).hint(format!("pass it with --set {root}=... (CLI) or in \"data\" (HTTP)"))
    };
    let mut current = ctx.get(root).ok_or_else(missing)?;
    for part in parts {
        current = match current {
            Value::Object(o) => o.get(part).unwrap_or(&Value::Null),
            Value::Array(a) => part.parse::<usize>().ok().and_then(|i| a.get(i)).unwrap_or(&Value::Null),
            _ => &Value::Null,
        };
    }
    Ok(current.clone())
}
