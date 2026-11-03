"""The `print-to-thermal` command line."""

import ipaddress
import json
import os
import sys

from . import config as cfg
from . import ops, transport
from .util import NOT_CONFIGURED, Error, bad, dumps, io_error

USAGE = """\
print-to-thermal: print jobs on an ESC/POS thermal printer, from the CLI or over HTTP

USAGE
  print-to-thermal preview <job> [options]   show the job as it will come off the roll
  print-to-thermal print   <job> [options]   check stock and paper, then send it
  print-to-thermal stocks                    printer limits, stocks, columns per size, loaded stock
  print-to-thermal stock <id>                record which roll is loaded
  print-to-thermal stock add id=WIDTH_MM[:DOTS][:adhesive]
  print-to-thermal stock rm <id>
  print-to-thermal status                    paper sensor: ok, low, out or unknown

SETUP
  print-to-thermal config                    the resolved configuration (exit 3 if there is none)
  print-to-thermal discover [--scan]         CUPS queues, Windows printers, USB/serial devices;
                                             --scan also looks for port 9100 on the local network
  print-to-thermal models                    printer presets
  print-to-thermal setup --printer CONN --model ID [--stock id=WIDTH_MM[:DOTS][:adhesive] ...] [--force]
                                             write printer.json; CONN is tcp://host:9100,
                                             cups:<queue>, win32:<printer> or a device path
                                             like /dev/usb/lp0
  print-to-thermal calibrate [--dry-run]     print a ruler; the last number fully on the paper
                                             is that roll's width in dots
  print-to-thermal set <key> <value>         change one printer setting (e.g. connection, qr, cutter)

  print-to-thermal templates [name]          list templates, or show one
  print-to-thermal serve [--bind ADDR] [--token T] [--allow IP,IP]
                                             run the HTTP API (default 0.0.0.0:8631)
  print-to-thermal openapi [--json]          print the OpenAPI spec

<job> is a .json file, - for stdin, inline JSON ('{...}'), or a template name.

OPTIONS
  --set key=value   template value; JSON is parsed (--set items='["a","b"]')
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
"""

VALUED = ["--set", "--data", "--stock", "--copies", "--bind", "--token", "--allow", "--printer", "--model"]


class Args:
    def __init__(self, raw):
        self.positional = []
        self.flags = []
        it = iter(raw)
        for arg in it:
            if arg.startswith("--") and "=" in arg:
                k, v = arg.split("=", 1)
                self.flags.append((k, v))
            elif arg in VALUED:
                try:
                    self.flags.append((arg, next(it)))
                except StopIteration:
                    raise bad("%s needs a value" % arg)
            elif arg.startswith("--"):
                self.flags.append((arg, None))
            else:
                self.positional.append(arg)

    def has(self, name):
        return any(k == name for k, _ in self.flags)

    def value(self, name):
        for k, v in reversed(self.flags):
            if k == name:
                return v
        return None

    def values(self, name):
        return [v for k, v in self.flags if k == name and v is not None]

    def pos(self, i):
        return self.positional[i] if i < len(self.positional) else None


def out_json(value):
    sys.stdout.write(dumps(value) + "\n")


def run(raw):
    args = Args(raw)
    home = cfg.home()
    command = args.pos(0) or "help"

    if command in ("preview", "print"):
        config = cfg.load(home)
        source, opts = job_args(args)
        prepared = ops.prepare(home, config, source, **opts)
        if command == "preview":
            report = ops.preview_report(config, prepared)
            if args.has("--json"):
                return out_json(report)
            print(report["preview"])
            print("stock: %s (%s)" % (report["stock"], "ADHESIVE" if report["adhesive"] else "non-adhesive"))
            for w in report["warnings"]:
                print("warning: %s" % w)
            return
        return send(args, config, prepared, args.has("--force"))
    if command == "calibrate":
        ruler_cfg, prepared = ops.calibration(cfg.load(home))
        send(args, ruler_cfg, prepared, True)
        if not args.has("--dry-run"):
            print("Read the last number fully on the paper: that is the roll's width in dots.")
            print("Record it: print-to-thermal stock add <id>=<width_mm>:<dots>[:adhesive]")
        return
    if command == "stocks":
        return out_json(ops.stocks(cfg.load(home)))
    if command == "stock":
        sub = args.pos(1)
        if sub == "add":
            spec = args.pos(2)
            if spec is None:
                raise bad("usage: print-to-thermal stock add id=WIDTH_MM[:DOTS][:adhesive]")
            return out_json(ops.stocks(cfg.add_stock(home, spec)))
        if sub == "rm":
            sid = args.pos(2)
            if sid is None:
                raise bad("usage: print-to-thermal stock rm <id>")
            return out_json(ops.stocks(cfg.remove_stock(home, sid)))
        if sub is not None:
            return out_json(ops.set_stock(home, sub))
        raise bad("usage: print-to-thermal stock <id> | add | rm", "see `print-to-thermal stocks` for ids")
    if command == "status":
        return out_json(transport.status(cfg.load(home)["printer"]))
    if command == "config":
        return out_json(ops.config_report(home))
    if command == "models":
        return out_json(ops.models())
    if command == "discover":
        return out_json(transport.discover(args.has("--scan")))
    if command == "setup":
        printer = args.value("--printer")
        if printer is None:
            raise bad(
                "setup needs --printer",
                "tcp://host:9100, cups:<queue>, win32:<printer> or a device path; `print-to-thermal discover` lists candidates",
            )
        model = args.value("--model")
        if model is None:
            raise bad("setup needs --model", "`print-to-thermal models` lists presets")
        config = cfg.setup(home, printer, model, args.values("--stock"), args.has("--force"))
        sys.stderr.write("wrote %s\n" % cfg.path(home))
        return out_json(ops.stocks(config))
    if command == "set":
        key, value = args.pos(1), args.pos(2)
        if key is None or value is None:
            raise bad("usage: print-to-thermal set <key> <value>", "keys: %s" % ", ".join(cfg.SETTABLE))
        return out_json(ops.config_report(home, cfg.set_field(home, key, value)))
    if command == "templates":
        name = args.pos(1)
        if name is not None:
            return out_json(ops.load_template(home, name))
        return out_json(ops.list_templates(home))
    if command == "serve":
        from . import server

        allow = []
        for s in (args.value("--allow") or "").split(","):
            s = s.strip()
            if not s:
                continue
            try:
                allow.append(ipaddress.ip_address(s))
            except ValueError as e:
                raise bad("--allow %s: %s" % (s, e))
        token = args.value("--token") or os.environ.get("THERMAL_TOKEN") or None
        bind = args.value("--bind") or "0.0.0.0:8631"
        return server.serve(home, bind, token, allow)
    if command == "openapi":
        from . import server

        if args.has("--json"):
            return out_json(server.openapi_json())
        sys.stdout.write(server.openapi_yaml())
        return
    if command in ("help", "-h", "--help"):
        sys.stdout.write(USAGE)
        return
    raise bad('unknown command "%s"' % command, "run `print-to-thermal help`")


def send(args, config, prepared, force):
    """Send a prepared job (or dry-run it) and report what happened."""
    dry_run = args.has("--dry-run")
    report, data = ops.print_job(config, prepared, force, dry_run)
    if dry_run and args.has("--hex"):
        print(" ".join("%02x" % b for b in data))
        return
    if args.has("--json"):
        return out_json(report)
    if report["sent"]:
        what = "sent %d bytes to %s" % (report["bytes"], report["target"])
    else:
        what = "dry run: %d bytes, not sent" % report["bytes"]
    print(
        "%s\nstock: %s (%s), copies: %d, paper: %s"
        % (what, report["stock"], "ADHESIVE" if report["adhesive"] else "non-adhesive", report["copies"], report["paper"])
    )
    for w in report["warnings"]:
        print("warning: %s" % w)
    if report["sent"]:
        print("note: 'sent' means the printer accepted the bytes; it cannot confirm paper came out.")


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def job_args(args):
    spec = args.pos(1)
    if spec is None:
        raise bad("which job?", "a .json file, - for stdin, inline '{...}', or a template name (`print-to-thermal templates`)")
    if spec == "-":
        try:
            source = json.loads(sys.stdin.read())
        except ValueError as e:
            raise bad("stdin is not valid JSON: %s" % e)
    elif spec.lstrip().startswith("{"):
        try:
            source = json.loads(spec)
        except ValueError as e:
            raise bad("inline job is not valid JSON: %s" % e)
    elif spec.endswith(".json") or os.path.isfile(spec):
        try:
            text = _read_text(spec)
        except OSError as e:
            raise bad("cannot read %s: %s" % (spec, io_error(e)))
        try:
            source = json.loads(text)
        except ValueError as e:
            raise bad("%s: %s" % (spec, e))
    else:
        source = spec

    data = {}
    d = args.value("--data")
    if d is not None:
        if d.lstrip().startswith("{"):
            text = d
        else:
            try:
                text = _read_text(d)
            except OSError as e:
                raise bad("cannot read %s: %s" % (d, io_error(e)))
        try:
            parsed = json.loads(text)
        except ValueError as e:
            raise bad("--data: %s" % e)
        if not isinstance(parsed, dict):
            raise bad("--data must be a JSON object")
        data = parsed
    for pair in args.values("--set"):
        if "=" not in pair:
            raise bad("--set %s: expected key=value" % pair)
        k, v = pair.split("=", 1)
        # Parse JSON so arrays and numbers come through; anything else is a string.
        try:
            value = json.loads(v)
            if value is None or isinstance(value, str):
                value = v
        except ValueError:
            value = v
        data[k] = value

    copies = args.value("--copies")
    if copies is not None:
        try:
            copies = int(copies)
            if copies < 0:
                raise ValueError
        except ValueError:
            raise bad("--copies %s: not a number" % copies)
    opts = {"data": data, "stock": args.value("--stock"), "copies": copies, "allow_paths": True}
    return source, opts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        run(argv)
        return 0
    except Error as e:
        sys.stderr.write("error: %s\n" % e)
        return 3 if e.status == NOT_CONFIGURED else 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
