use std::io::{Read, Write};
use std::net::IpAddr;
use std::process::ExitCode;

use serde_json::{Map, Value};

use thermal::config;
use thermal::error::{Error, Result};
use thermal::ops::{self, Options, Source};
use thermal::server::{self, ServeOptions};
use thermal::{transport, OPENAPI_YAML};

const USAGE: &str = "\
thermal: print jobs on an ESC/POS thermal printer, from the CLI or over HTTP

USAGE
  thermal preview <job> [options]   show the job as it will come off the roll
  thermal print   <job> [options]   check stock and paper, then send it
  thermal stocks                    printer limits, stocks, columns per size, loaded stock
  thermal stock <id>                record which roll is loaded
  thermal stock add id=WIDTH_MM[:DOTS][:adhesive]
  thermal stock rm <id>
  thermal status                    paper sensor: ok, low, out or unknown

SETUP
  thermal config                    the resolved configuration (exit 3 if there is none)
  thermal discover [--scan]         CUPS queues, USB/serial devices; --scan also looks
                                    for port 9100 on the local network
  thermal models                    printer presets
  thermal setup --printer CONN --model ID [--stock id=WIDTH_MM[:DOTS][:adhesive] ...] [--force]
                                    write printer.json; CONN is tcp://host:9100,
                                    cups:<queue> or a device path like /dev/usb/lp0
  thermal calibrate [--dry-run]     print a ruler; the last number fully on the paper
                                    is that roll's width in dots
  thermal set <key> <value>         change one printer setting (e.g. connection, qr, cutter)

  thermal templates [name]          list templates, or show one
  thermal serve [--bind ADDR] [--token T] [--allow IP,IP]
                                    run the HTTP API (default 0.0.0.0:8631)
  thermal openapi [--json]          print the OpenAPI spec

<job> is a .json file, - for stdin, inline JSON ('{...}'), or a template name.

OPTIONS
  --set key=value   template value; JSON is parsed (--set items='[\"a\",\"b\"]')
  --data FILE|JSON  template values as one object
  --stock ID        lay out for this stock (overrides the job's)
  --copies N
  --force           print even if the job's stock is not the loaded one
  --dry-run         do everything except send (with --hex, dump the bytes)
  --json            machine-readable output

ENVIRONMENT
  THERMAL_HOME      directory holding printer.json and templates/
                    (default ~/.config/thermal)
  THERMAL_PRINTER   connection, overriding printer.json
  THERMAL_TOKEN     bearer token for `serve`
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            if e.status == config::NOT_CONFIGURED {
                ExitCode::from(3)
            } else {
                ExitCode::FAILURE
            }
        }
    }
}

struct Args {
    positional: Vec<String>,
    flags: Vec<(String, Option<String>)>,
}

impl Args {
    fn parse(raw: Vec<String>) -> Result<Args> {
        const VALUED: &[&str] = &["--set", "--data", "--stock", "--copies", "--bind", "--token", "--allow", "--printer", "--model"];
        let mut positional = Vec::new();
        let mut flags = Vec::new();
        let mut it = raw.into_iter();
        while let Some(arg) = it.next() {
            if let Some((k, v)) = arg.split_once('=').filter(|(k, _)| k.starts_with("--")) {
                flags.push((k.to_string(), Some(v.to_string())));
            } else if VALUED.contains(&arg.as_str()) {
                let v = it.next().ok_or_else(|| Error::bad(format!("{arg} needs a value")))?;
                flags.push((arg, Some(v)));
            } else if arg.starts_with("--") {
                flags.push((arg, None));
            } else {
                positional.push(arg);
            }
        }
        Ok(Args { positional, flags })
    }

    fn has(&self, name: &str) -> bool {
        self.flags.iter().any(|(k, _)| k == name)
    }

    fn value(&self, name: &str) -> Option<&str> {
        self.flags.iter().rev().find(|(k, _)| k == name).and_then(|(_, v)| v.as_deref())
    }

    fn values<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a str> + 'a {
        self.flags.iter().filter(move |(k, _)| k == name).filter_map(|(_, v)| v.as_deref())
    }
}

fn run(raw: Vec<String>) -> Result<()> {
    let args = Args::parse(raw)?;
    let home = config::home();
    let command = args.positional.first().map(String::as_str).unwrap_or("help");

    match command {
        "preview" | "print" => {
            let config = config::load(&home)?;
            let (source, opts) = job_args(&args)?;
            let prepared = ops::prepare(&home, &config, source, &opts)?;
            if command == "preview" {
                let report = ops::preview(&config, &prepared);
                if args.has("--json") {
                    return out_json(&serde_json::to_value(report).unwrap_or_default());
                }
                println!("{}", report.preview);
                println!("stock: {} ({})", report.stock, if report.adhesive { "ADHESIVE" } else { "non-adhesive" });
                for w in &report.warnings {
                    println!("warning: {w}");
                }
                return Ok(());
            }
            send(&args, &config, &prepared, opts.force)
        }
        "calibrate" => {
            let (ruler, prepared) = ops::calibration(&config::load(&home)?)?;
            send(&args, &ruler, &prepared, true)?;
            if !args.has("--dry-run") {
                println!("Read the last number fully on the paper: that is the roll's width in dots.");
                println!("Record it: thermal stock add <id>=<width_mm>:<dots>[:adhesive]");
            }
            Ok(())
        }
        "stocks" => out_json(&ops::stocks(&config::load(&home)?)),
        "stock" => match args.positional.get(1).map(String::as_str) {
            Some("add") => {
                let spec = args.positional.get(2).ok_or_else(|| Error::bad("usage: thermal stock add id=WIDTH_MM[:DOTS][:adhesive]"))?;
                out_json(&ops::stocks(&config::add_stock(&home, spec)?))
            }
            Some("rm") => {
                let id = args.positional.get(2).ok_or_else(|| Error::bad("usage: thermal stock rm <id>"))?;
                out_json(&ops::stocks(&config::remove_stock(&home, id)?))
            }
            Some(id) => out_json(&ops::set_stock(&home, id)?),
            None => Err(Error::bad("usage: thermal stock <id> | add | rm").hint("see `thermal stocks` for ids")),
        },
        "status" => out_json(&serde_json::to_value(transport::status(&config::load(&home)?.printer)).unwrap_or_default()),
        "config" => out_json(&ops::config_report(&home)?),
        "models" => out_json(&ops::models()),
        "discover" => out_json(&serde_json::to_value(transport::discover(args.has("--scan"))).unwrap_or_default()),
        "setup" => {
            let printer = args.value("--printer").ok_or_else(|| Error::bad("setup needs --printer").hint("tcp://host:9100, cups:<queue> or a device path; `thermal discover` lists candidates"))?;
            let model = args.value("--model").ok_or_else(|| Error::bad("setup needs --model").hint("`thermal models` lists presets"))?;
            let stocks: Vec<String> = args.values("--stock").map(String::from).collect();
            let config = config::setup(&home, printer, model, &stocks, args.has("--force"))?;
            eprintln!("wrote {}", config::path(&home).display());
            out_json(&ops::stocks(&config))
        }
        "set" => {
            let (Some(key), Some(value)) = (args.positional.get(1), args.positional.get(2)) else {
                return Err(Error::bad("usage: thermal set <key> <value>").hint(format!("keys: {}", config::SETTABLE.join(", "))));
            };
            out_json(&ops::config_report_of(&home, &config::set(&home, key, value)?))
        }
        "templates" => match args.positional.get(1) {
            Some(name) => out_json(&ops::load_template(&home, name)?),
            None => out_json(&Value::Array(ops::list_templates(&home))),
        },
        "serve" => {
            let allow = args
                .value("--allow")
                .unwrap_or("")
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(|s| s.parse::<IpAddr>().map_err(|e| Error::bad(format!("--allow {s}: {e}"))))
                .collect::<Result<Vec<_>>>()?;
            let token = args.value("--token").map(String::from).or_else(|| std::env::var("THERMAL_TOKEN").ok()).filter(|t| !t.is_empty());
            let bind = args.value("--bind").unwrap_or("0.0.0.0:8631").to_string();
            server::serve(home, ServeOptions { bind, token, allow })
        }
        "openapi" => {
            if args.has("--json") {
                out_json(&server::openapi_json()?)
            } else {
                print!("{OPENAPI_YAML}");
                Ok(())
            }
        }
        "help" | "-h" | "--help" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(Error::bad(format!("unknown command \"{other}\"")).hint("run `thermal help`")),
    }
}

/// Send a prepared job (or dry-run it) and report what happened.
fn send(args: &Args, config: &thermal::config::Config, prepared: &ops::Prepared, force: bool) -> Result<()> {
    let dry_run = args.has("--dry-run");
    let (report, bytes) = ops::print(config, prepared, force, dry_run)?;
    if dry_run && args.has("--hex") {
        println!("{}", bytes.iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().join(" "));
        return Ok(());
    }
    if args.has("--json") {
        return out_json(&serde_json::to_value(report).unwrap_or_default());
    }
    let what = if report.sent { format!("sent {} bytes to {}", report.bytes, report.target) } else { format!("dry run: {} bytes, not sent", report.bytes) };
    println!(
        "{what}\nstock: {} ({}), copies: {}, paper: {}",
        report.stock,
        if report.adhesive { "ADHESIVE" } else { "non-adhesive" },
        report.copies,
        report.paper
    );
    for w in &report.warnings {
        println!("warning: {w}");
    }
    if report.sent {
        println!("note: 'sent' means the printer accepted the bytes; it cannot confirm paper came out.");
    }
    Ok(())
}

fn job_args(args: &Args) -> Result<(Source, Options)> {
    let spec = args.positional.get(1).ok_or_else(|| Error::bad("which job?").hint("a .json file, - for stdin, inline '{...}', or a template name (`thermal templates`)"))?;
    let source = if spec == "-" {
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s)?;
        Source::Doc(serde_json::from_str(&s).map_err(|e| Error::bad(format!("stdin is not valid JSON: {e}")))?)
    } else if spec.trim_start().starts_with('{') {
        Source::Doc(serde_json::from_str(spec).map_err(|e| Error::bad(format!("inline job is not valid JSON: {e}")))?)
    } else if spec.ends_with(".json") || std::path::Path::new(spec).is_file() {
        let s = std::fs::read_to_string(spec).map_err(|e| Error::bad(format!("cannot read {spec}: {e}")))?;
        Source::Doc(serde_json::from_str(&s).map_err(|e| Error::bad(format!("{spec}: {e}")))?)
    } else {
        Source::Template(spec.clone())
    };

    let mut data = Map::new();
    if let Some(d) = args.value("--data") {
        let text = if d.trim_start().starts_with('{') { d.to_string() } else { std::fs::read_to_string(d).map_err(|e| Error::bad(format!("cannot read {d}: {e}")))? };
        let parsed: Value = serde_json::from_str(&text).map_err(|e| Error::bad(format!("--data: {e}")))?;
        data = parsed.as_object().cloned().ok_or_else(|| Error::bad("--data must be a JSON object"))?;
    }
    for pair in args.values("--set") {
        let (k, v) = pair.split_once('=').ok_or_else(|| Error::bad(format!("--set {pair}: expected key=value")))?;
        // Parse JSON so arrays and numbers come through; anything else is a string.
        let value = match serde_json::from_str::<Value>(v) {
            Ok(parsed @ (Value::Array(_) | Value::Object(_) | Value::Number(_) | Value::Bool(_))) => parsed,
            _ => Value::String(v.to_string()),
        };
        data.insert(k.to_string(), value);
    }

    let copies = args.value("--copies").map(|c| c.parse().map_err(|_| Error::bad(format!("--copies {c}: not a number")))).transpose()?;
    Ok((source, Options { data, stock: args.value("--stock").map(String::from), copies, force: args.has("--force"), allow_paths: true }))
}

fn out_json(value: &Value) -> Result<()> {
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{}", serde_json::to_string_pretty(value).unwrap_or_default())?;
    Ok(())
}
