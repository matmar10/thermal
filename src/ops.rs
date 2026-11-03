//! The operations both front ends expose. The CLI and the HTTP server are thin
//! adapters over these, so they cannot drift apart.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::{json, Map, Value};

use crate::config::{self, Config};
use crate::error::{Error, Result};
use crate::job::{self, Job, BLOCK_TYPES};
use crate::layout::{self, Layout};
use crate::{escpos, preview, transport};

/// Where a job comes from.
pub enum Source {
    /// A job or template document, already parsed.
    Doc(Value),
    /// A saved template, by name.
    Template(String),
}

#[derive(Default)]
pub struct Options {
    pub data: Map<String, Value>,
    pub stock: Option<String>,
    pub copies: Option<u32>,
    /// Print even though the job's stock is not the one recorded as loaded.
    pub force: bool,
    pub allow_paths: bool,
}

pub struct Prepared {
    pub job: Job,
    pub layout: Layout,
    /// Set when the job asked for a stock other than the loaded one.
    pub mismatch: Option<String>,
}

pub fn prepare(home: &Path, config: &Config, source: Source, opts: &Options) -> Result<Prepared> {
    let doc = match source {
        Source::Doc(doc) => doc,
        Source::Template(name) => load_template(home, &name)?,
    };
    let mut job = job::expand(&doc, &opts.data)?;
    if let Some(copies) = opts.copies {
        job.copies = copies;
    }
    let stock_id = opts.stock.clone().or_else(|| job.stock.clone()).unwrap_or_else(|| config.loaded.clone());
    let mismatch = (stock_id != config.loaded).then(|| {
        format!("this job is for {stock_id} but {} is recorded as loaded", config.loaded)
    });
    let layout = layout::layout(&job, config, &stock_id, &layout::Options { allow_paths: opts.allow_paths })?;
    Ok(Prepared { job, layout, mismatch })
}

#[derive(Debug, Serialize)]
pub struct PreviewReport {
    pub stock: String,
    pub adhesive: bool,
    pub preview: String,
    pub labels_mm: Vec<f32>,
    pub warnings: Vec<String>,
}

pub fn preview(config: &Config, prepared: &Prepared) -> PreviewReport {
    let rendered = preview::render(&prepared.layout, config);
    let mut warnings = prepared.layout.warnings.clone();
    warnings.extend(prepared.mismatch.clone());
    PreviewReport {
        stock: prepared.layout.stock_id.clone(),
        adhesive: config.stocks[&prepared.layout.stock_id].adhesive,
        preview: rendered.text,
        labels_mm: rendered.labels_mm.iter().map(|mm| (mm * 10.0).round() / 10.0).collect(),
        warnings,
    }
}

#[derive(Debug, Serialize)]
pub struct PrintReport {
    /// The printer accepted the bytes. It does NOT confirm paper came out.
    pub sent: bool,
    pub bytes: usize,
    pub stock: String,
    pub adhesive: bool,
    pub copies: u32,
    pub paper: String,
    pub target: String,
    pub warnings: Vec<String>,
}

/// Check the stock and the paper, then send. `dry_run` does everything but send.
pub fn print(config: &Config, prepared: &Prepared, force: bool, dry_run: bool) -> Result<(PrintReport, Vec<u8>)> {
    if let (Some(why), false) = (&prepared.mismatch, force) {
        return Err(Error::new(409, format!("refusing to print: {why}")).hint(format!(
            "load the {stock} roll and record it (`print-to-thermal stock {stock}` / PUT /stock), or force it (--force / ?force=true)",
            stock = prepared.layout.stock_id
        )));
    }
    let bytes = escpos::encode(&prepared.layout, config);
    let mut warnings = prepared.layout.warnings.clone();
    let target = transport::Transport::parse(&config.printer.connection).map(|t| t.to_string()).unwrap_or_else(|_| config.printer.connection.clone());
    let paper = if dry_run {
        "not checked (dry run)".to_string()
    } else {
        let status = transport::status(&config.printer);
        match status.paper {
            "out" => return Err(Error::new(503, "the printer reports it is out of paper").hint("load a roll, then try again")),
            "low" => warnings.push("paper is near the end of the roll".into()),
            "unknown" => {
                if let Some(e) = status.error {
                    warnings.push(format!("paper status unknown ({e})"));
                }
            }
            _ => {}
        }
        transport::send(&config.printer, &bytes)?;
        status.paper.to_string()
    };
    let report = PrintReport {
        sent: !dry_run,
        bytes: bytes.len(),
        stock: prepared.layout.stock_id.clone(),
        adhesive: config.stocks[&prepared.layout.stock_id].adhesive,
        copies: prepared.job.copies,
        paper,
        target,
        warnings,
    };
    Ok((report, bytes))
}

/// The printer, every stock with its column counts, and the loaded stock:
/// everything needed to design a job.
pub fn stocks(config: &Config) -> Value {
    let max = config.printer.max_text_size;
    let stocks: Map<String, Value> = config
        .stocks
        .iter()
        .map(|(id, s)| {
            let cols: Map<String, Value> = (1..=max).map(|n| (format!("{n}x"), json!(config.cols(s, n)))).collect();
            let mut v = serde_json::to_value(s).unwrap_or_default();
            v["columns"] = Value::Object(cols);
            v["loaded"] = json!(id == &config.loaded);
            (id.clone(), v)
        })
        .collect();
    json!({ "printer": config.printer, "loaded": config.loaded, "stocks": stocks })
}

pub fn set_stock(home: &Path, id: &str) -> Result<Value> {
    config::set_loaded(home, id)?;
    let config = config::load(home)?;
    let stock = config.stock(id)?;
    Ok(json!({ "loaded": id, "adhesive": stock.adhesive, "description": stock.description }))
}

/// Templates compiled into the binary. A user template of the same name, in
/// `<home>/templates/`, replaces one.
pub const BUILTIN_TEMPLATES: &[(&str, &str)] = &[
    ("day", include_str!("../plugin/thermal/templates/day.json")),
    ("folder-tab", include_str!("../plugin/thermal/templates/folder-tab.json")),
    ("item-label", include_str!("../plugin/thermal/templates/item-label.json")),
    ("note", include_str!("../plugin/thermal/templates/note.json")),
    ("shopping-list", include_str!("../plugin/thermal/templates/shopping-list.json")),
];

fn templates_dir(home: &Path) -> PathBuf {
    home.join("templates")
}

fn template_name(name: &str) -> Result<&str> {
    let name = name.trim_end_matches(".json");
    if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err(Error::bad(format!("bad template name \"{name}\"")).hint("use letters, digits, - and _"));
    }
    Ok(name)
}

fn template_path(home: &Path, name: &str) -> Result<PathBuf> {
    Ok(templates_dir(home).join(format!("{}.json", template_name(name)?)))
}

fn builtin(name: &str) -> Option<&'static str> {
    BUILTIN_TEMPLATES.iter().find(|(n, _)| *n == name).map(|(_, raw)| *raw)
}

pub fn load_template(home: &Path, name: &str) -> Result<Value> {
    let path = template_path(home, name)?;
    let raw = match std::fs::read_to_string(&path) {
        Ok(raw) => raw,
        Err(_) => builtin(template_name(name)?).map(String::from).ok_or_else(|| {
            let known: Vec<String> = list_templates(home).into_iter().filter_map(|t| t["name"].as_str().map(String::from)).collect();
            Error::new(404, format!("no template \"{name}\"")).hint(format!("templates: {}", known.join(", ")))
        })?,
    };
    serde_json::from_str(&raw).map_err(|e| Error::new(500, format!("template {name}: {e}")))
}

pub fn list_templates(home: &Path) -> Vec<Value> {
    let mut docs: BTreeMap<String, (&str, Value)> = BUILTIN_TEMPLATES
        .iter()
        .map(|(name, raw)| (name.to_string(), ("built-in", serde_json::from_str(raw).unwrap_or(Value::Null))))
        .collect();
    if let Ok(entries) = std::fs::read_dir(templates_dir(home)) {
        for path in entries.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == "json")) {
            let name = path.file_stem().unwrap_or_default().to_string_lossy().to_string();
            let doc: Value = std::fs::read_to_string(&path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
            docs.insert(name, ("user", doc));
        }
    }
    docs.into_iter()
        .map(|(name, (source, doc))| {
            json!({
                "name": name,
                "source": source,
                "description": doc.get("description"),
                "stock": doc.get("stock"),
                "params": doc.get("params"),
                "defaults": doc.get("defaults"),
            })
        })
        .collect()
}
/// Check a template's shape. Full validation needs data, so this checks what
/// can be checked without it, then trial-expands with its defaults plus a
/// placeholder for every declared param.
pub fn validate_template(doc: &Value) -> Result<()> {
    let blocks = doc.get("blocks").and_then(Value::as_array).ok_or_else(|| Error::bad("a template needs a \"blocks\" array"))?;
    fn check(blocks: &[Value]) -> Result<()> {
        for (i, b) in blocks.iter().enumerate() {
            let t = b.get("type").and_then(Value::as_str).unwrap_or_default();
            if !BLOCK_TYPES.contains(&t) {
                return Err(Error::bad(format!("block {}: unknown type \"{t}\"", i + 1)).hint(format!("types: {}", BLOCK_TYPES.join(", "))));
            }
            if t == "each" {
                check(b.get("blocks").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]))?;
            }
        }
        Ok(())
    }
    check(blocks)?;
    let mut sample = Map::new();
    if let Some(params) = doc.get("params").and_then(Value::as_object) {
        for name in params.keys() {
            sample.insert(name.clone(), json!(["sample"]));
        }
    }
    if let Some(defaults) = doc.get("defaults").and_then(Value::as_object) {
        for (k, v) in defaults {
            sample.insert(k.clone(), v.clone());
        }
    }
    // Only structural errors matter here; missing or oddly-typed sample values do not.
    match job::expand(doc, &sample) {
        Err(e) if e.message.starts_with("invalid job") && e.message.contains("unknown") => Err(e),
        _ => Ok(()),
    }
}

pub fn save_template(home: &Path, name: &str, doc: &Value) -> Result<()> {
    validate_template(doc)?;
    let path = template_path(home, name)?;
    std::fs::create_dir_all(templates_dir(home))?;
    std::fs::write(path, serde_json::to_string_pretty(doc)? + "\n")?;
    Ok(())
}

pub fn delete_template(home: &Path, name: &str) -> Result<()> {
    let path = template_path(home, name)?;
    std::fs::remove_file(&path).map_err(|_| match builtin(name.trim_end_matches(".json")) {
        Some(_) => Error::bad(format!("\"{name}\" is built in and cannot be deleted")).hint("save a template of the same name to replace it"),
        None => Error::new(404, format!("no template \"{name}\"")),
    })
}

/// The resolved configuration and where it came from.
pub fn config_report(home: &Path) -> Result<Value> {
    Ok(config_report_of(home, &config::load(home)?))
}

pub fn config_report_of(home: &Path, config: &Config) -> Value {
    json!({
        "file": config::path(home),
        "connection": config.printer.connection,
        "printer": config.printer,
        "stocks": config.stocks,
        "loaded": config.loaded,
    })
}

/// The bundled printer presets, briefly.
pub fn models() -> Value {
    Value::Array(
        config::MODELS
            .iter()
            .filter_map(|(id, _)| config::model(id).ok().map(|m| (id, m)))
            .map(|(id, m)| {
                json!({
                    "model": id,
                    "name": m["name"],
                    "paper_mm": m["paper_mm"],
                    "carriage_dots": m["carriage_dots"],
                    "dots_per_mm": m["dots_per_mm"],
                    "cutter": m["cutter"],
                    "qr": m["qr"],
                    "notes": m["notes"],
                })
            })
            .collect(),
    )
}

/// A ruler across the whole carriage: a tick every 8 dots, a numbered tick
/// every 48. Whatever number is the last one fully on the paper is the
/// stock's printable width in dots.
pub fn calibration(config: &Config) -> Result<(Config, Prepared)> {
    let carriage = config.printer.carriage_dots;
    let mut ruler = config.clone();
    let stock = config::Stock {
        description: "full carriage".into(),
        width_mm: config.printer.paper_mm,
        print_dots: carriage,
        adhesive: false,
        uses: vec![],
        notes: vec![],
    };
    ruler.stocks.insert("calibration".into(), stock);
    let job = job::expand(&json!({ "blocks": [] }), &Map::new())?;
    let mut layout = layout::layout(&job, &ruler, "calibration", &layout::Options { allow_paths: false })?;
    let mut items = vec![
        layout::Item::Line { text: "CALIBRATION".into(), w: 1, h: 1, bold: true, underline: false, invert: false, align: crate::job::Align::Left },
        layout::Item::Line { text: "Last number fully".into(), w: 1, h: 1, bold: false, underline: false, invert: false, align: crate::job::Align::Left },
        layout::Item::Line { text: "on the paper = dots".into(), w: 1, h: 1, bold: false, underline: false, invert: false, align: crate::job::Align::Left },
    ];
    items.push(layout::ruler(carriage)?);
    items.append(&mut layout.items);
    layout.items = items;
    Ok((ruler, Prepared { job, layout, mismatch: None }))
}
