//! `thermal serve`: the same operations as the CLI, over HTTP.

use std::io::{Cursor, Read};
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};
use tiny_http::{Header, Method, Request, Response, Server};

use crate::config;
use crate::error::{Error, Result};
use crate::ops::{self, Options, Source};
use crate::{transport, OPENAPI_YAML};

/// Every route the server answers. A test checks each one is in openapi.yaml.
pub const ROUTES: &[(&str, &str)] = &[
    ("get", "/health"),
    ("get", "/status"),
    ("get", "/config"),
    ("get", "/stocks"),
    ("put", "/stock"),
    ("get", "/templates"),
    ("get", "/templates/{name}"),
    ("put", "/templates/{name}"),
    ("delete", "/templates/{name}"),
    ("post", "/preview"),
    ("post", "/print"),
    ("get", "/openapi.yaml"),
    ("get", "/openapi.json"),
    ("get", "/docs"),
];

/// Routes that need neither the token nor an allowlisted address.
const OPEN: &[&str] = &["/health", "/openapi.yaml", "/openapi.json", "/docs", "/"];

pub struct ServeOptions {
    pub bind: String,
    pub token: Option<String>,
    /// Empty = any address.
    pub allow: Vec<IpAddr>,
}

struct State {
    home: PathBuf,
    opts: ServeOptions,
    /// Held while talking to the printer, so two jobs never interleave bytes.
    printer: Mutex<()>,
}

type Reply = Response<Cursor<Vec<u8>>>;

pub fn serve(home: PathBuf, opts: ServeOptions) -> Result<()> {
    config::load(&home)?;
    let server = Server::http(&opts.bind).map_err(|e| Error::new(500, format!("cannot listen on {}: {e}", opts.bind)))?;
    eprintln!(
        "thermal: listening on http://{} (home {}, token {}, allowlist {})",
        opts.bind,
        home.display(),
        if opts.token.is_some() { "required" } else { "off" },
        if opts.allow.is_empty() { "off".to_string() } else { format!("{:?}", opts.allow) }
    );
    eprintln!("thermal: API docs at http://{}/docs", opts.bind);
    let state = Arc::new(State { home, opts, printer: Mutex::new(()) });
    for mut request in server.incoming_requests() {
        let state = Arc::clone(&state);
        std::thread::spawn(move || {
            let reply = handle(&state, &mut request).unwrap_or_else(|e| json_reply(e.status, &e.to_json()));
            eprintln!("{} {} -> {}", request.method(), request.url(), reply.status_code().0);
            let _ = request.respond(reply);
        });
    }
    Ok(())
}

fn handle(state: &State, req: &mut Request) -> Result<Reply> {
    let url = req.url().to_string();
    let (path, query) = url.split_once('?').unwrap_or((&url, ""));
    let path = path.trim_end_matches('/');
    let path = if path.is_empty() { "/" } else { path };
    let query = parse_query(query);

    if !OPEN.contains(&path) {
        authorize(state, req)?;
    }

    let method = req.method().clone();
    let home = &state.home;
    let segments: Vec<&str> = path.trim_start_matches('/').split('/').collect();

    match (&method, segments.as_slice()) {
        (Method::Get, [""]) => Ok(Response::from_data(Vec::new()).with_status_code(302).with_header(header("Location", "/docs"))),
        (Method::Get, ["health"]) => Ok(json_reply(200, &json!({ "ok": true, "version": env!("CARGO_PKG_VERSION") }))),
        (Method::Get, ["status"]) => {
            let config = config::load(home)?;
            let _guard = state.printer.lock().unwrap_or_else(|e| e.into_inner());
            Ok(json_reply(200, &serde_json::to_value(transport::status(&config.printer)).unwrap_or_default()))
        }
        (Method::Get, ["config"]) => Ok(json_reply(200, &ops::config_report(home)?)),
        (Method::Get, ["stocks"]) => Ok(json_reply(200, &ops::stocks(&config::load(home)?))),
        (Method::Put, ["stock"]) => {
            let body = read_json(req)?;
            let id = body.get("id").and_then(Value::as_str).ok_or_else(|| Error::bad("send {\"id\": \"<stock>\"}"))?;
            Ok(json_reply(200, &ops::set_stock(home, id)?))
        }
        (Method::Get, ["templates"]) => Ok(json_reply(200, &Value::Array(ops::list_templates(home)))),
        (Method::Get, ["templates", name]) => Ok(json_reply(200, &ops::load_template(home, name)?)),
        (Method::Put, ["templates", name]) => {
            let body = read_json(req)?;
            ops::save_template(home, name, &body)?;
            Ok(json_reply(200, &json!({ "saved": name })))
        }
        (Method::Delete, ["templates", name]) => {
            ops::delete_template(home, name)?;
            Ok(json_reply(200, &json!({ "deleted": name })))
        }
        (Method::Post, ["preview"]) => {
            let config = config::load(home)?;
            let (source, opts) = job_request(req, &query)?;
            let prepared = ops::prepare(home, &config, source, &opts)?;
            let report = ops::preview(&config, &prepared);
            if query.get("format").and_then(Value::as_str) == Some("text") {
                let mut text = report.preview.clone();
                for w in &report.warnings {
                    text.push_str(&format!("\nwarning: {w}"));
                }
                return Ok(Response::from_data(text.into_bytes()).with_header(header("Content-Type", "text/plain; charset=utf-8")));
            }
            Ok(json_reply(200, &serde_json::to_value(report).unwrap_or_default()))
        }
        (Method::Post, ["print"]) => {
            let config = config::load(home)?;
            let (source, opts) = job_request(req, &query)?;
            let prepared = ops::prepare(home, &config, source, &opts)?;
            let dry_run = flag(&query, "dry_run");
            let _guard = state.printer.lock().unwrap_or_else(|e| e.into_inner());
            let (report, _) = ops::print(&config, &prepared, opts.force, dry_run)?;
            Ok(json_reply(200, &serde_json::to_value(report).unwrap_or_default()))
        }
        (Method::Get, ["openapi.yaml"]) => Ok(Response::from_data(OPENAPI_YAML.as_bytes().to_vec()).with_header(header("Content-Type", "application/yaml"))),
        (Method::Get, ["openapi.json"]) => Ok(json_reply(200, &openapi_json()?)),
        (Method::Get, ["docs"]) => Ok(Response::from_data(DOCS_HTML.as_bytes().to_vec()).with_header(header("Content-Type", "text/html; charset=utf-8"))),
        _ => Err(Error::new(404, format!("no route {method} {path}")).hint("see /docs or /openapi.json")),
    }
}

pub fn openapi_json() -> Result<Value> {
    serde_yaml::from_str(OPENAPI_YAML).map_err(|e| Error::new(500, format!("openapi.yaml: {e}")))
}

fn authorize(state: &State, req: &Request) -> Result<()> {
    if !state.opts.allow.is_empty() {
        let ip = req.remote_addr().map(|a| a.ip());
        let allowed = ip.is_some_and(|ip| state.opts.allow.iter().any(|a| *a == ip || ip.to_canonical() == *a));
        if !allowed {
            return Err(Error::new(403, format!("{} is not on the allowlist", ip.map(|i| i.to_string()).unwrap_or_default())));
        }
    }
    if let Some(token) = &state.opts.token {
        let expected = format!("Bearer {token}");
        let given = req.headers().iter().find(|h| h.field.equiv("Authorization")).map(|h| h.value.as_str().to_string());
        if given.as_deref() != Some(expected.as_str()) {
            return Err(Error::new(401, "missing or wrong bearer token").hint("send Authorization: Bearer <token>"));
        }
    }
    Ok(())
}

/// A job request body is either a job document, or
/// `{"template": "<name>" | {...}, "data": {...}}`.
/// Stock, copies and force come from the query string.
fn job_request(req: &mut Request, query: &Map<String, Value>) -> Result<(Source, Options)> {
    let body = read_json(req)?;
    let mut opts = Options {
        stock: query.get("stock").and_then(Value::as_str).map(String::from),
        copies: query.get("copies").and_then(Value::as_str).and_then(|c| c.parse().ok()),
        force: flag(query, "force"),
        allow_paths: false,
        ..Default::default()
    };
    let source = match body.get("template") {
        Some(Value::String(name)) => Source::Template(name.clone()),
        Some(doc @ Value::Object(_)) => Source::Doc(doc.clone()),
        Some(_) => return Err(Error::bad("\"template\" must be a template name or a template object")),
        None => Source::Doc(body.clone()),
    };
    if body.get("template").is_some() {
        if let Some(data) = body.get("data") {
            opts.data = data.as_object().cloned().ok_or_else(|| Error::bad("\"data\" must be an object"))?;
        }
    }
    Ok((source, opts))
}

fn read_json(req: &mut Request) -> Result<Value> {
    let mut body = String::new();
    req.as_reader().take(32 * 1024 * 1024).read_to_string(&mut body).map_err(|e| Error::bad(format!("cannot read body: {e}")))?;
    if body.trim().is_empty() {
        return Err(Error::bad("expected a JSON body"));
    }
    serde_json::from_str(&body).map_err(|e| Error::bad(format!("body is not valid JSON: {e}")))
}

fn parse_query(q: &str) -> Map<String, Value> {
    q.split('&')
        .filter(|p| !p.is_empty())
        .map(|pair| {
            let (k, v) = pair.split_once('=').unwrap_or((pair, "true"));
            (decode(k), Value::String(decode(v)))
        })
        .collect()
}

fn decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < bytes.len() => {
                match std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()).ok_or(()) {
                    Ok(b) => {
                        out.push(b);
                        i += 2;
                    }
                    Err(_) => out.push(b'%'),
                }
            }
            b => out.push(b),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn flag(query: &Map<String, Value>, key: &str) -> bool {
    matches!(query.get(key).and_then(Value::as_str), Some("true" | "1" | "yes"))
}

fn header(name: &str, value: &str) -> Header {
    Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("valid header")
}

fn json_reply(status: u16, value: &Value) -> Reply {
    let body = serde_json::to_vec_pretty(value).unwrap_or_default();
    Response::from_data(body).with_status_code(status).with_header(header("Content-Type", "application/json"))
}

const DOCS_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Thermal printer API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>SwaggerUIBundle({ url: "/openapi.json", dom_id: "#ui" });</script>
</body>
</html>
"##;
