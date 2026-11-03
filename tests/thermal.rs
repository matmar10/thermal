use std::path::PathBuf;

use serde_json::{json, Map, Value};
use thermal::config::{self, Config};
use thermal::layout::Item;
use thermal::ops::{self, Options, Source};
use thermal::text::{fold, hang, wrap};
use thermal::{escpos, job, server};

fn home() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn config() -> Config {
    let mut c = config::load(&home()).unwrap();
    c.loaded = "receipt-80".into();
    c
}

fn prepare(doc: Value, stock: Option<&str>) -> thermal::error::Result<ops::Prepared> {
    let opts = Options { stock: stock.map(String::from), allow_paths: true, ..Default::default() };
    ops::prepare(&home(), &config(), Source::Doc(doc), &opts)
}

fn bytes(doc: Value, stock: Option<&str>) -> Vec<u8> {
    escpos::encode(&prepare(doc, stock).unwrap().layout, &config())
}

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

#[test]
fn wraps_at_word_boundaries_and_hard_breaks_only_overlong_words() {
    assert_eq!(wrap("the quick brown fox", 9), vec!["the quick", "brown fox"]);
    assert_eq!(wrap("a\n\nb", 5), vec!["a", "", "b"]);
    assert_eq!(wrap("abcdefghij", 4), vec!["abcd", "efgh", "ij"]);
    assert_eq!(hang("[ ] ", "one two three", 10), vec!["[ ] one", "    two", "    three"]);
}

#[test]
fn folds_to_ascii_before_wrapping() {
    assert_eq!(fold("425°F — ½ cup café"), "425F -- 1/2 cup cafe");
    assert_eq!(fold("emoji 🎉 gone"), "emoji  gone");
}

#[test]
fn template_fill_keeps_raw_values_and_expands_each() {
    let doc = json!({
        "defaults": { "title": "List" },
        "blocks": [
            { "type": "text", "text": "{{title}} ({{count}})", "size": "{{size}}" },
            { "type": "each", "items": "{{items}}", "as": "it", "blocks": [
                { "type": "row", "left": "{{index}}. {{it.name}}", "right": "{{it.qty}}" }
            ] }
        ]
    });
    let data: Map<String, Value> = serde_json::from_value(json!({
        "count": 2, "size": 2, "items": [{ "name": "Eggs", "qty": 12 }, { "name": "Milk", "qty": "1L" }]
    })).unwrap();
    let job = job::expand(&doc, &data).unwrap();
    assert_eq!(job.blocks.len(), 3);
    let text = serde_json::to_value(&job.blocks).unwrap();
    assert_eq!(text[0]["text"], "List (2)");
    assert_eq!(text[1]["left"], "1. Eggs");
    assert_eq!(text[1]["right"], "12");
    assert_eq!(text[2]["left"], "2. Milk");
}

#[test]
fn missing_param_names_it() {
    let err = job::expand(&json!({ "params": { "who": "Recipient" }, "blocks": [{ "type": "text", "text": "Hi {{who}}" }] }), &Map::new()).unwrap_err();
    assert!(err.message.contains("\"who\"") && err.message.contains("Recipient"), "{}", err.message);
}

#[test]
fn rejects_unknown_fields_and_oversized_text() {
    assert!(prepare(json!({ "blocks": [{ "type": "text", "txt": "x" }] }), None).is_err());
    assert!(prepare(json!({ "blocks": [{ "type": "nope" }] }), None).is_err());
    let err = prepare(json!({ "blocks": [{ "type": "text", "text": "x", "size": 5 }] }), None).err().unwrap();
    assert!(err.hint.unwrap().contains("big"));
}

#[test]
fn every_job_pins_the_print_area_to_the_stock() {
    let b = bytes(json!({ "blocks": [{ "type": "text", "text": "x" }] }), Some("label-60-plain"));
    assert!(b.starts_with(&[0x1b, 0x40, 0x1d, 0x4c, 0, 0, 0x1d, 0x57, 0x80, 0x01])); // 384 dots
    let b = bytes(json!({ "blocks": [{ "type": "text", "text": "x" }] }), None);
    assert!(contains(&b, &[0x1d, 0x57, 0x40, 0x02])); // 576 dots
}

#[test]
fn text_bytes_match_quartermaster() {
    let b = bytes(json!({ "blocks": [{ "type": "text", "text": "Hi", "size": 2, "bold": true, "align": "center" }] }), None);
    assert!(contains(&b, &[0x1b, 0x61, 0x01, 0x1d, 0x21, 0x11, 0x1b, 0x45, 0x01, b'H', b'i', b'\n', 0x1b, 0x45, 0x00, 0x1d, 0x21, 0x00]));
    let b = bytes(json!({ "blocks": [{ "type": "text", "text": "x", "size": [3, 1] }] }), None);
    assert!(contains(&b, &[0x1d, 0x21, 0x20]));
    // Ends with the default feed and a partial cut.
    assert!(b.ends_with(&[0x1b, 0x64, 3, 0x1d, 0x56, 0x01]));
}

#[test]
fn never_sends_esc_k() {
    let b = bytes(json!({ "blocks": [
        { "type": "text", "text": "K K K" }, { "type": "big", "text": "KKK" }, { "type": "qr", "data": "KKKK" }
    ] }), None);
    // ESC K is fine inside raster data, but never as a command right after text.
    assert!(!contains(&b, &[0x0a, 0x1b, 0x4b]));
}

#[test]
fn barcode_and_qr_bytes_match_quartermaster() {
    let b = bytes(json!({ "blocks": [{ "type": "barcode", "data": "ABC", "height": 10, "module": 3, "hri": "below" }] }), None);
    assert!(contains(&b, &[0x1d, 0x68, 80, 0x1d, 0x77, 3, 0x1d, 0x48, 2]));
    assert!(contains(&b, &[0x1d, 0x6b, 73, 5, b'{', b'B', b'A', b'B', b'C']));
    let b = bytes(json!({ "blocks": [{ "type": "qr", "data": "hello", "module": 4, "ec": "H" }] }), None);
    assert!(contains(&b, &[0x1d, 0x28, 0x6b, 0x03, 0x00, 0x31, 0x43, 4]));
    assert!(contains(&b, &[0x1d, 0x28, 0x6b, 0x03, 0x00, 0x31, 0x45, 0x33]));
    assert!(contains(&b, &[0x1d, 0x28, 0x6b, 8, 0, 0x31, 0x50, 0x30, b'h', b'e', b'l', b'l', b'o']));
}

#[test]
fn barcodes_shrink_to_fit_or_refuse() {
    let p = prepare(json!({ "blocks": [{ "type": "barcode", "data": "BIN-0042" }] }), Some("label-60-plain")).unwrap();
    assert!(p.layout.warnings.iter().any(|w| w.contains("module reduced")));
    assert!(prepare(json!({ "blocks": [{ "type": "barcode", "data": "THIS-IS-FAR-TOO-LONG-FOR-60MM" }] }), Some("label-60-plain")).is_err());
    assert!(prepare(json!({ "blocks": [{ "type": "barcode", "data": "12345", "symbology": "EAN13" }] }), None).is_err());
}

#[test]
fn big_text_is_a_full_width_raster_that_fits() {
    let p = prepare(json!({ "blocks": [{ "type": "big", "text": "WEDNESDAY", "fit": true }] }), Some("label-60-adhesive")).unwrap();
    let Item::Raster { width, height, bits, .. } = &p.layout.items[0] else { panic!("not a raster") };
    assert_eq!(*width, 384);
    assert!(*height > 20 && *height < 120, "height {height}");
    assert_eq!(bits.len(), 48 * *height as usize);
    // Fitted to the width: ink reaches both edges.
    let first_col_inked = bits.chunks(48).any(|row| row[0] != 0);
    let last_col_inked = bits.chunks(48).any(|row| row[47] != 0);
    assert!(first_col_inked && last_col_inked);
    if let Ok(dir) = std::env::var("THERMAL_DUMP") {
        // Eyeball it: a PBM opens in Preview.app.
        let mut pbm = format!("P4\n{width} {height}\n").into_bytes();
        pbm.extend(bits);
        std::fs::write(format!("{dir}/big.pbm"), pbm).unwrap();
    }
}

#[test]
fn oversized_big_text_shrinks_with_a_warning() {
    let p = prepare(json!({ "blocks": [{ "type": "big", "text": "A VERY LONG LINE", "height": 20 }] }), Some("label-60-plain")).unwrap();
    assert!(p.layout.warnings.iter().any(|w| w.contains("shrunk")));
}

#[test]
fn copies_and_mid_job_cuts() {
    let p = prepare(json!({ "copies": 2, "blocks": [{ "type": "text", "text": "a" }, { "type": "cut" }, { "type": "text", "text": "b" }] }), None).unwrap();
    let cuts = p.layout.items.iter().filter(|i| matches!(i, Item::Cut(_))).count();
    assert_eq!(cuts, 4);
}

#[test]
fn printing_on_the_wrong_stock_is_refused_unless_forced() {
    let p = prepare(json!({ "stock": "label-60-adhesive", "blocks": [{ "type": "text", "text": "x" }] }), None).unwrap();
    let err = ops::print(&config(), &p, false, true).unwrap_err();
    assert_eq!(err.status, 409);
    let (report, _) = ops::print(&config(), &p, true, true).unwrap();
    assert!(!report.sent && report.adhesive);
}

#[test]
fn image_paths_are_refused_over_http() {
    let opts = Options { allow_paths: false, ..Default::default() };
    let doc = json!({ "blocks": [{ "type": "image", "path": "/etc/hosts" }] });
    let err = ops::prepare(&home(), &config(), Source::Doc(doc), &opts).err().unwrap();
    assert!(err.message.contains("not allowed"));
}

#[test]
fn bundled_templates_validate_and_preview() {
    let samples = json!({
        "shopping-list": { "items": ["Eggs", "Milk"] },
        "folder-tab": { "text": "Grades & Enrollment", "size": 3 },
        "item-label": { "title": "CABLES", "code": "BIN-42" },
        "note": { "body": "Back at 5" },
        "day": { "day": "MONDAY", "date": "2 November 2026" }
    });
    for t in ops::list_templates(&home()) {
        let name = t["name"].as_str().unwrap();
        let doc = ops::load_template(&home(), name).unwrap();
        ops::validate_template(&doc).unwrap();
        let data = samples.get(name).and_then(Value::as_object).cloned().unwrap_or_else(|| panic!("no sample data for template {name}"));
        let opts = Options { data, force: true, ..Default::default() };
        let p = ops::prepare(&home(), &config(), Source::Template(name.into()), &opts).unwrap();
        assert!(!ops::preview(&config(), &p).preview.contains("OVERFLOW"), "{name}");
    }
}

#[test]
fn openapi_covers_every_route_and_its_examples_are_valid_jobs() {
    let spec = server::openapi_json().unwrap();
    for (method, path) in server::ROUTES {
        assert!(spec["paths"][path][method].is_object(), "{method} {path} missing from openapi.yaml");
    }
    let examples = &spec["components"]["requestBodies"]["JobRequest"]["content"]["application/json"]["examples"];
    for (name, ex) in examples.as_object().unwrap() {
        let value = &ex["value"];
        if value.get("template").is_some() {
            continue;
        }
        prepare(value.clone(), None).unwrap_or_else(|e| panic!("example {name}: {e}"));
    }
    let tpl = &spec["paths"]["/templates/{name}"]["put"]["requestBody"]["content"]["application/json"]["examples"]["pantry-jar"]["value"];
    ops::validate_template(tpl).unwrap();
    // Every block type in the job format has a schema.
    for t in job::BLOCK_TYPES {
        assert!(spec["components"]["schemas"]["Block"]["discriminator"]["mapping"][t].is_string(), "block {t} missing from spec");
    }
}

fn temp_home(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("thermal-test-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn unconfigured_home_is_reported_as_not_configured() {
    let err = config::load(&temp_home("empty")).err().unwrap();
    assert_eq!(err.status, config::NOT_CONFIGURED);
    assert!(err.hint.unwrap().contains("thermal setup"));
}

#[test]
fn setup_writes_a_config_and_refuses_to_overwrite() {
    let dir = temp_home("setup");
    let c = config::setup(&dir, "192.168.1.9", "generic-58mm", &["tags=58:384".into(), "stickers=40:adhesive".into()], false).unwrap();
    assert_eq!(c.printer.connection, "tcp://192.168.1.9:9100");
    assert_eq!(c.printer.carriage_dots, 384);
    assert_eq!(c.stocks["tags"].print_dots, 384);
    // No dots given: estimated from the printer, and flagged as such.
    assert!(c.stocks["stickers"].adhesive);
    assert_eq!(c.stocks["stickers"].print_dots, 264);
    assert!(c.stocks["stickers"].notes[0].contains("calibrate"));
    assert_eq!(config::setup(&dir, "tcp://x:9100", "generic-58mm", &[], false).err().unwrap().status, 409);

    let c = config::set(&dir, "qr", "native").unwrap();
    assert_eq!(c.printer.qr, config::Render::Native);
    assert!(config::set(&dir, "qr", "sideways").is_err(), "an invalid value must not be written");
    assert_eq!(config::load(&dir).unwrap().printer.qr, config::Render::Native);
    assert!(config::remove_stock(&dir, &c.loaded).is_err(), "cannot remove the loaded roll");
    config::remove_stock(&dir, "stickers").unwrap();
    let c = config::add_stock(&dir, "labels=50:400:adhesive").unwrap();
    assert_eq!(c.stocks["labels"].print_dots, 384, "never wider than the carriage");
}

#[test]
fn setup_without_stocks_uses_the_full_carriage() {
    let c = config::setup(&temp_home("default-stock"), "cups:Receipt", "generic-80mm", &[], false).unwrap();
    assert_eq!(c.loaded, "roll-80");
    assert_eq!(c.stocks["roll-80"].print_dots, 576);
}

#[test]
fn legacy_host_and_port_become_a_connection_and_presets_merge() {
    let doc = json!({
        "printer": { "model": "Milestone MHT-P80E", "host": "10.0.0.5", "port": 9100, "dots_per_mm": 8, "carriage_dots": 576,
                     "cell_dots": 12, "line_dots": 30, "max_text_size": 4, "dead_zone_mm": 7.5 },
        "stocks": { "r": { "description": "r", "width_mm": 80, "print_dots": 576, "adhesive": false } },
        "loaded": "r"
    });
    let c = config::resolve(&doc).unwrap();
    assert_eq!(c.printer.connection, "tcp://10.0.0.5:9100");
    assert_eq!(c.printer.name, "Milestone MHT-P80E");
    let c = config::resolve(&json!({ "printer": { "model": "epson-tm-t88", "max_text_size": 6 }, "stocks": {}, "loaded": "" })).unwrap();
    assert_eq!((c.printer.carriage_dots, c.printer.max_text_size), (512, 6));
}

#[test]
fn connections_parse() {
    use thermal::transport::Transport;
    assert_eq!(Transport::parse("tcp://p.local").unwrap(), Transport::Tcp("p.local:9100".into()));
    assert_eq!(Transport::parse("cups:Receipt_Printer").unwrap(), Transport::Cups("Receipt_Printer".into()));
    assert_eq!(Transport::parse("/dev/usb/lp0").unwrap(), Transport::Device("/dev/usb/lp0".into()));
    assert_eq!(Transport::parse("file:///tmp/out.bin").unwrap(), Transport::Device("/tmp/out.bin".into()));
    assert_eq!(Transport::parse("").err().unwrap().status, config::NOT_CONFIGURED);
    assert!(Transport::parse("usb:whatever").is_err());
    assert_eq!(config::normalize_connection("10.0.0.5:9101"), "tcp://10.0.0.5:9101");
}

#[test]
fn device_transport_writes_the_bytes() {
    let dir = temp_home("device");
    let out = dir.join("out.bin");
    std::fs::write(&out, b"").unwrap();
    let mut c = config();
    c.printer.connection = out.display().to_string();
    let p = prepare(json!({ "blocks": [{ "type": "text", "text": "hi" }] }), None).unwrap();
    let (report, bytes) = ops::print(&c, &p, false, false).unwrap();
    assert!(report.sent);
    assert_eq!(std::fs::read(&out).unwrap(), bytes);
}

#[test]
fn no_cutter_means_no_cut_bytes() {
    let mut c = config();
    c.printer.cutter = config::Cutter::None;
    let job = job::expand(&json!({ "blocks": [{ "type": "text", "text": "a" }, { "type": "cut" }, { "type": "text", "text": "b" }] }), &Map::new()).unwrap();
    let layout = thermal::layout::layout(&job, &c, "receipt-80", &thermal::layout::Options { allow_paths: true }).unwrap();
    let bytes = escpos::encode(&layout, &c);
    assert!(!contains(&bytes, &[0x1d, b'V']));
    assert!(thermal::preview::render(&layout, &c).text.contains("Tear here"));
}

#[test]
fn qr_falls_back_to_an_image() {
    let mut c = config();
    c.printer.qr = config::Render::Raster;
    let job = job::expand(&json!({ "blocks": [{ "type": "qr", "data": "https://example.com" }] }), &Map::new()).unwrap();
    let layout = thermal::layout::layout(&job, &c, "label-60-plain", &thermal::layout::Options { allow_paths: true }).unwrap();
    assert!(matches!(&layout.items[0], Item::Raster { width: 384, .. }));
    assert!(!contains(&escpos::encode(&layout, &c), &[0x1d, b'(', b'k']));
}

#[test]
fn calibration_ruler_spans_the_carriage() {
    let (ruler, p) = ops::calibration(&config()).unwrap();
    let raster = p.layout.items.iter().find_map(|i| match i { Item::Raster { width, .. } => Some(*width), _ => None });
    assert_eq!(raster, Some(576));
    assert_eq!(ruler.stocks["calibration"].print_dots, 576);
}

#[test]
fn user_templates_override_built_in_ones() {
    let dir = temp_home("templates");
    assert!(ops::load_template(&dir, "note").is_ok(), "built-ins load with no templates dir");
    ops::save_template(&dir, "note", &json!({ "description": "mine", "blocks": [{ "type": "text", "text": "x" }] })).unwrap();
    assert_eq!(ops::load_template(&dir, "note").unwrap()["description"], "mine");
    let listed = ops::list_templates(&dir);
    let note = listed.iter().find(|t| t["name"] == "note").unwrap();
    assert_eq!(note["source"], "user");
    ops::delete_template(&dir, "note").unwrap();
    assert_eq!(ops::delete_template(&dir, "note").err().unwrap().status, 400, "built-ins cannot be deleted");
}

#[test]
fn plugin_bundle_meets_directory_limits() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("plugin");
    fn walk(dir: &std::path::Path, files: &mut Vec<PathBuf>) {
        for e in std::fs::read_dir(dir).unwrap().flatten() {
            let ft = e.file_type().unwrap();
            assert!(!ft.is_symlink(), "symlink in plugin bundle: {}", e.path().display());
            if ft.is_dir() { walk(&e.path(), files) } else { files.push(e.path()) }
        }
    }
    let mut files = Vec::new();
    walk(&root, &mut files);
    assert!(files.len() <= 512);
    for f in &files {
        let len = std::fs::metadata(f).unwrap().len();
        assert!(len < 256 * 1024, "{} is {len} bytes", f.display());
        assert_ne!(f.file_name().unwrap(), ".DS_Store");
    }
    let manifest: Value = serde_json::from_str(&std::fs::read_to_string(root.join(".claude-plugin/plugin.json")).unwrap()).unwrap();
    assert_eq!(manifest["version"], env!("CARGO_PKG_VERSION"), "plugin.json and Cargo.toml versions must match");
}
