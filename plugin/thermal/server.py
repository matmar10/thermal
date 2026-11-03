"""`print-to-thermal serve`: the same operations as the CLI, over HTTP."""

import ipaddress
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_plus

from . import __version__
from . import config as cfg
from . import ops, transport
from .util import Error, bad, dumps

_HERE = os.path.dirname(os.path.abspath(__file__))

# Every route the server answers. A test checks each one is in openapi.yaml.
ROUTES = [
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
]

# Routes that need neither the token nor an allowlisted address.
OPEN = ["/health", "/openapi.yaml", "/openapi.json", "/docs", "/"]

MAX_BODY = 32 * 1024 * 1024

DOCS_HTML = """<!DOCTYPE html>
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
"""


def openapi_yaml():
    with open(os.path.join(_HERE, "openapi.yaml"), encoding="utf-8") as f:
        return f.read()


def openapi_json():
    """The spec as JSON. openapi.json is generated from openapi.yaml
    (`make openapi-json`), since the standard library has no YAML parser."""
    try:
        with open(os.path.join(_HERE, "openapi.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Error(500, "openapi.json: %s" % e)


def parse_query(q):
    out = {}
    for pair in q.split("&"):
        if not pair:
            continue
        k, v = pair.split("=", 1) if "=" in pair else (pair, "true")
        out[unquote_plus(k)] = unquote_plus(v)
    return out


def flag(query, key):
    return query.get(key) in ("true", "1", "yes")


class _State:
    def __init__(self, home, token, allow):
        self.home = home
        self.token = token
        self.allow = allow  # empty = any address
        # Held while talking to the printer, so two jobs never interleave bytes.
        self.printer = threading.Lock()


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        server_version = "print-to-thermal/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def _dispatch(self, method):
            try:
                status, ctype, body, extra = self._handle(method)
            except Error as e:
                status, ctype, body, extra = e.status, "application/json", dumps(e.to_json()).encode(), {}
            except Exception as e:  # never let one request take the server down
                status, ctype, body, extra = 500, "application/json", dumps({"error": str(e), "hint": None}).encode(), {}
            sys.stderr.write("%s %s -> %d\n" % (method, self.path, status))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value, status=200):
            return status, "application/json", dumps(value).encode(), {}

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                raise bad("body is larger than 32 MiB")
            raw = self.rfile.read(length) if length > 0 else b""
            text = raw.decode("utf-8", "replace")
            if not text.strip():
                raise bad("expected a JSON body")
            try:
                return json.loads(text)
            except ValueError as e:
                raise bad("body is not valid JSON: %s" % e)

        def _authorize(self):
            if state.allow:
                try:
                    ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
                except ValueError:
                    ip = None
                mapped = getattr(ip, "ipv4_mapped", None) if ip is not None else None
                if ip is None or not any(a == ip or (mapped is not None and a == mapped) for a in state.allow):
                    raise Error(403, "%s is not on the allowlist" % (ip if ip is not None else ""))
            if state.token:
                if self.headers.get("Authorization") != "Bearer %s" % state.token:
                    raise Error(401, "missing or wrong bearer token", "send Authorization: Bearer <token>")

        def _job_request(self, query):
            """A job request body is either a job document, or
            {"template": "<name>" | {...}, "data": {...}}. Stock, copies and
            force come from the query string."""
            body = self._read_json()
            copies = query.get("copies")
            opts = {
                "stock": query.get("stock"),
                "copies": int(copies) if copies is not None and copies.isdigit() else None,
                "allow_paths": False,
                "data": {},
            }
            tpl = body.get("template") if isinstance(body, dict) else None
            if isinstance(body, dict) and "template" in body:
                if isinstance(tpl, str) or isinstance(tpl, dict):
                    source = tpl
                else:
                    raise bad('"template" must be a template name or a template object')
                if "data" in body:
                    if not isinstance(body["data"], dict):
                        raise bad('"data" must be an object')
                    opts["data"] = body["data"]
            else:
                source = body
            return source, opts

        def _handle(self, method):
            url = self.path
            path, _, query = url.partition("?")
            path = path.rstrip("/") or "/"
            query = parse_query(query)
            if path not in OPEN:
                self._authorize()
            home = state.home
            seg = path.lstrip("/").split("/")

            if method == "GET" and path == "/":
                return 302, "text/plain", b"", {"Location": "/docs"}
            if method == "GET" and seg == ["health"]:
                return self._json({"ok": True, "version": __version__})
            if method == "GET" and seg == ["status"]:
                config = cfg.load(home)
                with state.printer:
                    return self._json(transport.status(config["printer"]))
            if method == "GET" and seg == ["config"]:
                return self._json(ops.config_report(home))
            if method == "GET" and seg == ["stocks"]:
                return self._json(ops.stocks(cfg.load(home)))
            if method == "PUT" and seg == ["stock"]:
                body = self._read_json()
                sid = body.get("id") if isinstance(body, dict) else None
                if not isinstance(sid, str):
                    raise bad('send {"id": "<stock>"}')
                return self._json(ops.set_stock(home, sid))
            if method == "GET" and seg == ["templates"]:
                return self._json(ops.list_templates(home))
            if len(seg) == 2 and seg[0] == "templates":
                name = unquote_plus(seg[1])
                if method == "GET":
                    return self._json(ops.load_template(home, name))
                if method == "PUT":
                    ops.save_template(home, name, self._read_json())
                    return self._json({"saved": name})
                if method == "DELETE":
                    ops.delete_template(home, name)
                    return self._json({"deleted": name})
            if method == "POST" and seg == ["preview"]:
                config = cfg.load(home)
                source, opts = self._job_request(query)
                prepared = ops.prepare(home, config, source, **opts)
                report = ops.preview_report(config, prepared)
                if query.get("format") == "text":
                    text = report["preview"] + "".join("\nwarning: %s" % w for w in report["warnings"])
                    return 200, "text/plain; charset=utf-8", text.encode("utf-8"), {}
                return self._json(report)
            if method == "POST" and seg == ["print"]:
                config = cfg.load(home)
                source, opts = self._job_request(query)
                prepared = ops.prepare(home, config, source, **opts)
                with state.printer:
                    report, _ = ops.print_job(config, prepared, flag(query, "force"), flag(query, "dry_run"))
                return self._json(report)
            if method == "GET" and seg == ["openapi.yaml"]:
                return 200, "application/yaml", openapi_yaml().encode("utf-8"), {}
            if method == "GET" and seg == ["openapi.json"]:
                return self._json(openapi_json())
            if method == "GET" and seg == ["docs"]:
                return 200, "text/html; charset=utf-8", DOCS_HTML.encode("utf-8"), {}
            raise Error(404, "no route %s %s" % (method, path), "see /docs or /openapi.json")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(home, bind, token, allow):
    cfg.load(home)
    host, _, port = bind.rpartition(":")
    host = host.strip("[]")
    try:
        port = int(port)
    except ValueError:
        raise Error(500, "cannot listen on %s: invalid port" % bind)
    server_cls = _Server
    if ":" in host:
        import socket

        class _Server6(_Server):
            address_family = socket.AF_INET6

        server_cls = _Server6
    try:
        httpd = server_cls((host, port), _make_handler(_State(home, token, allow)))
    except OSError as e:
        raise Error(500, "cannot listen on %s: %s" % (bind, e))
    sys.stderr.write(
        "print-to-thermal: listening on http://%s (home %s, token %s, allowlist %s)\n"
        % (bind, home, "required" if token else "off", [str(a) for a in allow] if allow else "off")
    )
    sys.stderr.write("print-to-thermal: API docs at http://%s/docs\n" % bind)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
