use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::error::{Error, Result};

/// Status used for "nothing configured yet"; the CLI exits 3 on it.
pub const NOT_CONFIGURED: u16 = 428;

/// Printer presets, compiled in. A user's `printer.json` names one with
/// `printer.model` and overrides any of its fields.
pub const MODELS: &[(&str, &str)] = &[
    ("generic-80mm", include_str!("../plugin/thermal/models/generic-80mm.json")),
    ("generic-58mm", include_str!("../plugin/thermal/models/generic-58mm.json")),
    ("epson-tm-t20", include_str!("../plugin/thermal/models/epson-tm-t20.json")),
    ("epson-tm-t88", include_str!("../plugin/thermal/models/epson-tm-t88.json")),
    ("mht-p80e", include_str!("../plugin/thermal/models/mht-p80e.json")),
];

const DEFAULT_MODEL: &str = "generic-80mm";

/// `printer.json`: the device, the stocks that fit it, and which one is loaded.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub printer: Printer,
    pub stocks: BTreeMap<String, Stock>,
    /// The roll currently in the printer. The printer cannot sense this, so it
    /// is whatever someone last recorded with `print-to-thermal stock <id>`.
    pub loaded: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Cutter {
    /// Tear bar only: cuts become a feed.
    None,
    /// Partial and full cuts both work.
    Partial,
    /// Only full cuts: partial cuts are sent as full.
    Full,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Render {
    /// The printer's own command (`GS ( k` for QR).
    Native,
    /// Drawn here and sent as an image, for printers without the command.
    Raster,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Printer {
    /// The preset this profile is based on.
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub name: String,
    /// Where the bytes go: `tcp://host:port`, `cups:<queue>`, or a device path.
    #[serde(default)]
    pub connection: String,
    /// Widest paper the printer takes.
    #[serde(default)]
    pub paper_mm: u32,
    pub dots_per_mm: f32,
    pub carriage_dots: u32,
    /// Width of one built-in character at 1x, in dots.
    pub cell_dots: u32,
    /// How far one 1x line advances the paper, in dots.
    pub line_dots: u32,
    /// Largest `GS !` multiplier that actually prints larger.
    pub max_text_size: u8,
    #[serde(default)]
    pub dead_zone_mm: f32,
    #[serde(default = "default_cutter")]
    pub cutter: Cutter,
    #[serde(default = "default_render")]
    pub qr: Render,
    /// Whether to ask the paper sensor (`DLE EOT 4`) before printing.
    #[serde(default = "default_true")]
    pub status: bool,
    /// Characters the device cannot draw, mapped to ones it can. Applied to
    /// the bytes after wrapping, so replacements should be one character.
    #[serde(default)]
    pub substitutions: BTreeMap<String, String>,
    #[serde(default)]
    pub notes: Vec<String>,
}

fn default_cutter() -> Cutter {
    Cutter::Partial
}

fn default_render() -> Render {
    Render::Native
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Stock {
    pub description: String,
    pub width_mm: u32,
    /// Printable width in dots. Measured, not derived from `width_mm`.
    pub print_dots: u32,
    pub adhesive: bool,
    #[serde(default)]
    pub uses: Vec<String>,
    #[serde(default)]
    pub notes: Vec<String>,
}

impl Config {
    pub fn stock(&self, id: &str) -> Result<&Stock> {
        self.stocks.get(id).ok_or_else(|| {
            Error::bad(format!("unknown stock \"{id}\"")).hint(format!(
                "known stocks: {}",
                self.stocks.keys().cloned().collect::<Vec<_>>().join(", ")
            ))
        })
    }

    /// Characters per line on `stock` at width multiplier `size`.
    pub fn cols(&self, stock: &Stock, size: u8) -> usize {
        (stock.print_dots / (self.printer.cell_dots * size.max(1) as u32)) as usize
    }
}

/// A preset by id, as JSON.
pub fn model(id: &str) -> Result<Value> {
    let (_, raw) = MODELS.iter().find(|(m, _)| *m == id).ok_or_else(|| {
        Error::bad(format!("unknown printer model \"{id}\""))
            .hint(format!("models: {}; or pick the generic one closest to yours", MODELS.iter().map(|(m, _)| *m).collect::<Vec<_>>().join(", ")))
    })?;
    serde_json::from_str(raw).map_err(|e| Error::new(500, format!("models/{id}.json: {e}")))
}

/// An environment variable, ignoring empty values and unsubstituted
/// placeholders (`${user_config.printer}` when the user left it blank).
fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty() && !v.contains("${"))
}

/// The per-user config directory: `$XDG_CONFIG_HOME/thermal`, else
/// `~/.config/thermal` (`%APPDATA%\thermal` on Windows).
pub fn user_dir() -> PathBuf {
    if let Some(xdg) = env("XDG_CONFIG_HOME") {
        return PathBuf::from(xdg).join("thermal");
    }
    if cfg!(windows) {
        if let Some(appdata) = env("APPDATA") {
            return PathBuf::from(appdata).join("thermal");
        }
    }
    match env("HOME").or_else(|| env("USERPROFILE")) {
        Some(home) => PathBuf::from(home).join(".config").join("thermal"),
        None => PathBuf::from("."),
    }
}

/// Where `printer.json` and the user's `templates/` live.
///
/// `THERMAL_HOME` wins. Then the user config directory, if it holds a
/// `printer.json`. Then a `printer.json` above the executable, for a
/// development checkout. Otherwise the user config directory, which is where
/// `print-to-thermal setup` writes.
pub fn home() -> PathBuf {
    if let Some(dir) = env("THERMAL_HOME") {
        return PathBuf::from(dir);
    }
    let user = user_dir();
    if user.join("printer.json").is_file() {
        return user;
    }
    if let Ok(exe) = std::env::current_exe().and_then(|p| p.canonicalize()) {
        for dir in exe.ancestors().skip(1) {
            if dir.join("printer.json").is_file() {
                return dir.to_path_buf();
            }
        }
    }
    user
}

pub fn path(home: &Path) -> PathBuf {
    home.join("printer.json")
}

/// `host:port` with no scheme is TCP, as the old `THERMAL_PRINTER` was.
pub fn normalize_connection(conn: &str) -> String {
    let conn = conn.trim();
    let bare_host_port = !conn.contains("://") && !conn.starts_with("cups:") && !conn.starts_with('/') && !conn.starts_with("file:");
    if bare_host_port {
        let (host, port) = conn.rsplit_once(':').filter(|(_, p)| p.parse::<u16>().is_ok()).unwrap_or((conn, "9100"));
        format!("tcp://{host}:{port}")
    } else {
        conn.to_string()
    }
}

fn read_doc(home: &Path) -> Result<Value> {
    let path = path(home);
    let raw = match std::fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(Error::new(NOT_CONFIGURED, format!("thermal is not configured: no {}", path.display()))
                .hint("run `print-to-thermal setup --printer <connection> --model <model>` (see `print-to-thermal discover` and `print-to-thermal models`)"));
        }
        Err(e) => return Err(Error::new(500, format!("cannot read {}: {e}", path.display()))),
    };
    serde_json::from_str(&raw).map_err(|e| Error::new(500, format!("{}: {e}", path.display())))
}

/// Build a `Config` from a `printer.json` document: the preset, overlaid
/// with the file's own printer fields.
pub fn resolve(doc: &Value) -> Result<Config> {
    let mut own = doc.get("printer").and_then(Value::as_object).cloned().unwrap_or_default();
    // Before presets, the file held host and port.
    if !own.contains_key("connection") {
        if let Some(host) = own.get("host").and_then(Value::as_str) {
            let port = own.get("port").and_then(Value::as_u64).unwrap_or(9100);
            own.insert("connection".into(), json!(format!("tcp://{host}:{port}")));
        }
    }
    own.remove("host");
    own.remove("port");
    let id = own.get("model").and_then(Value::as_str).filter(|m| MODELS.iter().any(|(k, _)| k == m)).unwrap_or(DEFAULT_MODEL).to_string();
    if let Some(named) = own.get("model").and_then(Value::as_str).map(String::from) {
        if named != id {
            // Not a preset (e.g. a free-text model name from an old file):
            // keep it as the display name.
            own.entry("name").or_insert_with(|| json!(named));
        }
    }
    let mut merged = match model(&id)? {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    merged.extend(own);
    merged.insert("model".into(), json!(id));
    let mut printer: Printer = serde_json::from_value(Value::Object(merged)).map_err(|e| Error::new(500, format!("printer.json printer: {e}")))?;
    if let Some(target) = env("THERMAL_PRINTER") {
        printer.connection = target;
    }
    printer.connection = normalize_connection(&printer.connection);
    let stocks: BTreeMap<String, Stock> =
        serde_json::from_value(doc.get("stocks").cloned().unwrap_or(json!({}))).map_err(|e| Error::new(500, format!("printer.json stocks: {e}")))?;
    let loaded = doc.get("loaded").and_then(Value::as_str).map(String::from).or_else(|| stocks.keys().next().cloned()).unwrap_or_default();
    Ok(Config { printer, stocks, loaded })
}

pub fn load(home: &Path) -> Result<Config> {
    resolve(&read_doc(home)?)
}

/// Change `printer.json` in place, keeping hand-written notes and fields this
/// version does not know about. The result must still load, or nothing is
/// written.
pub fn edit(home: &Path, change: impl FnOnce(&mut Value) -> Result<()>) -> Result<Config> {
    let mut doc = read_doc(home)?;
    change(&mut doc)?;
    let config = resolve(&doc)?;
    if !config.stocks.contains_key(&config.loaded) {
        return Err(Error::bad(format!("loaded stock \"{}\" is not one of the stocks", config.loaded)));
    }
    std::fs::write(path(home), serde_json::to_string_pretty(&doc)? + "\n")?;
    Ok(config)
}

/// Record which stock is loaded.
pub fn set_loaded(home: &Path, id: &str) -> Result<()> {
    edit(home, |doc| {
        let known = doc.get("stocks").and_then(Value::as_object).is_some_and(|s| s.contains_key(id));
        if !known {
            let ids: Vec<String> = doc.get("stocks").and_then(Value::as_object).map(|s| s.keys().cloned().collect()).unwrap_or_default();
            return Err(Error::bad(format!("unknown stock \"{id}\"")).hint(format!("known stocks: {}", ids.join(", "))));
        }
        doc["loaded"] = json!(id);
        Ok(())
    })?;
    Ok(())
}

/// Parse `id=width_mm[:dots][:adhesive]`, e.g. `label-60=60:384:adhesive`.
/// Without dots the width is estimated from the printer and flagged, since
/// only a calibration print can say what really prints.
pub fn parse_stock(spec: &str, printer: &Printer) -> Result<(String, Stock)> {
    let usage = || Error::bad(format!("stock \"{spec}\": expected id=width_mm[:dots][:adhesive]")).hint("e.g. receipt-80=80:576 or label-60=60:384:adhesive");
    let (id, rest) = spec.split_once('=').ok_or_else(usage)?;
    let id = id.trim();
    if id.is_empty() || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(usage());
    }
    let mut parts = rest.split(':').map(str::trim);
    let width_mm: u32 = parts.next().and_then(|w| w.trim_end_matches("mm").parse().ok()).ok_or_else(usage)?;
    let mut dots = None;
    let mut adhesive = false;
    for part in parts {
        match part {
            "adhesive" => adhesive = true,
            "plain" | "" => {}
            n => dots = Some(n.parse::<u32>().map_err(|_| usage())?),
        }
    }
    Ok((id.to_string(), new_stock(width_mm, dots, adhesive, printer)))
}

pub fn new_stock(width_mm: u32, dots: Option<u32>, adhesive: bool, printer: &Printer) -> Stock {
    let estimated = dots.is_none();
    let print_dots = dots.unwrap_or_else(|| estimate_dots(width_mm, printer)).min(printer.carriage_dots);
    Stock {
        description: format!("{width_mm}mm {} roll", if adhesive { "adhesive" } else { "plain" }),
        width_mm,
        print_dots,
        adhesive,
        uses: Vec::new(),
        notes: if estimated { vec!["print_dots is estimated, not measured: run `print-to-thermal calibrate` and record what prints.".into()] } else { Vec::new() },
    }
}

/// Paper at the printer's full width uses the whole carriage; narrower paper
/// scales down, rounded to a whole byte of dots.
fn estimate_dots(width_mm: u32, printer: &Printer) -> u32 {
    if printer.paper_mm == 0 || width_mm >= printer.paper_mm {
        return printer.carriage_dots;
    }
    (printer.carriage_dots * width_mm / printer.paper_mm) / 8 * 8
}

/// Write a fresh `printer.json`. Refuses to replace one unless `force`.
pub fn setup(home: &Path, connection: &str, model_id: &str, stocks: &[String], force: bool) -> Result<Config> {
    let file = path(home);
    if file.exists() && !force {
        return Err(Error::new(409, format!("{} already exists", file.display())).hint("change it with `print-to-thermal set` / `print-to-thermal stock add`, or pass --force to start over"));
    }
    model(model_id)?;
    let mut doc = json!({ "printer": { "model": model_id, "connection": normalize_connection(connection) }, "stocks": {}, "loaded": "" });
    let printer = resolve(&doc)?.printer;
    let mut entries = Map::new();
    for spec in stocks {
        let (id, stock) = parse_stock(spec, &printer)?;
        entries.insert(id, serde_json::to_value(stock)?);
    }
    if entries.is_empty() {
        let w = printer.paper_mm.max(1);
        entries.insert(format!("roll-{w}"), serde_json::to_value(new_stock(w, Some(printer.carriage_dots), false, &printer))?);
    }
    doc["loaded"] = json!(entries.keys().next().cloned().unwrap_or_default());
    doc["stocks"] = Value::Object(entries);
    std::fs::create_dir_all(home)?;
    std::fs::write(&file, serde_json::to_string_pretty(&doc)? + "\n")?;
    load(home)
}

/// Printer fields `print-to-thermal set` may change.
pub const SETTABLE: &[&str] = &[
    "connection", "model", "name", "paper_mm", "dots_per_mm", "carriage_dots", "cell_dots", "line_dots", "max_text_size", "dead_zone_mm", "cutter", "qr",
    "status",
];

/// Set one printer field. The value is parsed as JSON when it can be, so
/// numbers and booleans come through; anything else is a string.
pub fn set(home: &Path, key: &str, value: &str) -> Result<Config> {
    if !SETTABLE.contains(&key) {
        return Err(Error::bad(format!("cannot set \"{key}\"")).hint(format!("settable: {}", SETTABLE.join(", "))));
    }
    let parsed = match serde_json::from_str::<Value>(value) {
        Ok(v @ (Value::Number(_) | Value::Bool(_))) => v,
        _ if key == "connection" => json!(normalize_connection(value)),
        _ => json!(value),
    };
    edit(home, |doc| {
        if !doc.get("printer").is_some_and(Value::is_object) {
            doc["printer"] = json!({});
        }
        doc["printer"][key] = parsed;
        Ok(())
    })
}

pub fn add_stock(home: &Path, spec: &str) -> Result<Config> {
    let printer = load(home)?.printer;
    let (id, stock) = parse_stock(spec, &printer)?;
    edit(home, |doc| {
        if !doc.get("stocks").is_some_and(Value::is_object) {
            doc["stocks"] = json!({});
        }
        doc["stocks"][&id] = serde_json::to_value(stock)?;
        if doc.get("loaded").and_then(Value::as_str).is_none_or(str::is_empty) {
            doc["loaded"] = json!(id);
        }
        Ok(())
    })
}

pub fn remove_stock(home: &Path, id: &str) -> Result<Config> {
    edit(home, |doc| {
        if doc.get("loaded").and_then(Value::as_str) == Some(id) {
            return Err(Error::bad(format!("\"{id}\" is the loaded stock")).hint("record another roll as loaded first (`print-to-thermal stock <id>`)"));
        }
        let removed = doc.get_mut("stocks").and_then(Value::as_object_mut).and_then(|s| s.remove(id));
        if removed.is_none() {
            return Err(Error::new(404, format!("no stock \"{id}\"")));
        }
        Ok(())
    })
}
