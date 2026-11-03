"""Getting bytes to the printer, by whichever route `printer.connection` names:

- `tcp://host:port`: raw TCP ("JetDirect", usually port 9100).
- `cups:<queue>`: a CUPS queue, sent raw with `lp -o raw`. This is how a USB
  printer is reached on macOS and Linux without a USB library.
- `win32:<printer>`: a Windows printer queue, sent raw through the spooler.
  This is how a USB printer is reached on Windows.
- `/dev/...` or `file:<path>`: written straight to a device, e.g. Linux
  `/dev/usb/lp0` or a serial port.

Only TCP can ask the paper sensor; the others report "unknown".
"""

import concurrent.futures
import ipaddress
import os
import re
import socket
import subprocess

from .escpos import PAPER_STATUS
from .util import NOT_CONFIGURED, Error, bad, io_error


def _trim_slashes(s):
    """Drop leading "//" pairs, so file:///tmp/x is /tmp/x."""
    while s.startswith("//"):
        s = s[2:]
    return s


def parse(conn):
    """-> (kind, target) with kind one of tcp, cups, win32, device."""
    conn = conn.strip()
    if not conn:
        raise Error(
            NOT_CONFIGURED,
            "no printer connection is configured",
            "`print-to-thermal set connection tcp://<ip>:9100` (or cups:<queue>, win32:<printer>, or a device path); "
            "`print-to-thermal discover` lists candidates",
        )
    if conn.startswith("tcp://"):
        addr = conn[len("tcp://") :].rstrip("/")
        if ":" not in addr:
            addr += ":9100"
        return ("tcp", addr)
    if conn.startswith("cups:"):
        queue = _trim_slashes(conn[len("cups:") :])
        if not queue:
            raise bad("cups: needs a queue name, e.g. cups:Receipt_Printer", "`lpstat -v` lists queues")
        return ("cups", queue)
    if conn.startswith("win32:"):
        name = _trim_slashes(conn[len("win32:") :])
        if not name:
            raise bad("win32: needs a printer name, e.g. win32:POS-80", "`print-to-thermal discover` lists printers")
        return ("win32", name)
    if conn.startswith("file:"):
        return ("device", _trim_slashes(conn[len("file:") :]))
    if conn.startswith("/") or re.match(r"(?i)(COM|LPT)[0-9]+:?\Z", conn):
        return ("device", conn)
    raise bad(
        'cannot read printer connection "%s"' % conn,
        "use tcp://host:port, cups:<queue>, win32:<printer>, or a device path like /dev/usb/lp0",
    )


def describe(t):
    kind, target = t
    if kind == "tcp":
        return "tcp://" + target
    if kind in ("cups", "win32"):
        return "%s:%s" % (kind, target)
    return target


def _split_addr(addr):
    host, _, port = addr.rpartition(":")
    host = host.strip("[]")
    try:
        return host, int(port)
    except ValueError:
        raise Error(502, "cannot resolve printer %s: invalid port value" % addr)


def _connect(addr, timeout):
    host, port = _split_addr(addr)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise Error(502, "cannot resolve printer %s: %s" % (addr, io_error(e)))
    if not infos:
        raise Error(502, "no address for printer %s" % addr)
    family, kind, proto, _, sockaddr = infos[0]
    s = socket.socket(family, kind, proto)
    s.settimeout(timeout)
    try:
        s.connect(sockaddr)
    except OSError as e:
        s.close()
        raise Error(
            502,
            "cannot reach printer at %s: %s" % (addr, io_error(e)),
            "is it on and on the network? check the connection with `print-to-thermal config`",
        )
    return s


def status(printer):
    """Ask the paper sensor. In the reply, bits 5-6 set = out, bits 2-3 = near
    end. "unknown" means nobody answered, NOT that anything is wrong."""

    def unknown(target, error=None):
        out = {"target": target, "paper": "unknown"}
        if error:
            out["error"] = error
        return out

    try:
        t = parse(printer["connection"])
    except Error as e:
        return unknown(printer["connection"], e.message)
    target = describe(t)
    if t[0] != "tcp" or not printer["status"]:
        return unknown(target)
    try:
        s = _connect(t[1], 3)
    except Error as e:
        return unknown(target, e.message)
    try:
        s.settimeout(1.5)
        s.sendall(PAPER_STATUS)
        try:
            reply = s.recv(1)
        except OSError:
            reply = b""
    except OSError as e:
        return unknown(target, io_error(e))
    finally:
        s.close()
    if len(reply) != 1:
        # Silence is an answer too: firmware that ignores DLE EOT.
        return unknown(target)
    raw = reply[0]
    if raw & 0x60 == 0x60:
        paper = "out"
    elif raw & 0x0C == 0x0C:
        paper = "low"
    else:
        paper = "ok"
    return {"target": target, "paper": paper, "raw": raw}


def send(printer, data):
    """Send bytes. Returning means the printer (or the spooler) ACCEPTED
    them, not that paper came out."""
    kind, target = parse(printer["connection"])
    if kind == "tcp":
        s = _connect(target, 5)
        try:
            s.settimeout(30)
            s.sendall(data)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError as e:
            raise Error(502, "printer write failed: %s" % io_error(e))
        finally:
            s.close()
    elif kind == "cups":
        try:
            proc = subprocess.run(
                ["lp", "-d", target, "-o", "raw", "-s"],
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            raise Error(502, "cannot run lp: %s" % io_error(e), "cups: needs CUPS (macOS, Linux); on Windows use win32:<printer> or tcp://")
        if proc.returncode != 0:
            raise Error(
                502,
                "lp -d %s failed: %s" % (target, proc.stderr.decode("utf-8", "replace").strip()),
                "`lpstat -v` lists queues; the queue must accept raw data",
            )
    elif kind == "win32":
        _win32_send(target, data)
    else:
        try:
            # Write-only, no create, no truncate: a device, or a file that exists.
            fd = os.open(target, os.O_WRONLY | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
        except OSError as e:
            raise Error(
                502,
                "cannot open %s: %s" % (target, io_error(e)),
                "check the device exists and you may write to it (e.g. the lp group on Linux)",
            )


def _win32_send(name, data):
    """Hand the bytes to the Windows spooler as a RAW document."""
    if os.name != "nt":
        raise Error(502, "win32: printers are only reachable on Windows", "use tcp:// or cups:<queue> here")
    import ctypes
    from ctypes import wintypes

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class DOC_INFO_1(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    handle = wintypes.HANDLE()
    if not winspool.OpenPrinterW(name, ctypes.byref(handle), None):
        raise Error(
            502,
            'cannot open printer "%s": %s' % (name, ctypes.FormatError(ctypes.get_last_error())),
            "`print-to-thermal discover` lists printer names",
        )
    try:
        doc = DOC_INFO_1("thermal", None, "RAW")
        if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc)):
            raise Error(502, "cannot start a print job on %s: %s" % (name, ctypes.FormatError(ctypes.get_last_error())))
        try:
            winspool.StartPagePrinter(handle)
            written = wintypes.DWORD()
            buf = ctypes.create_string_buffer(data, len(data))
            ok = winspool.WritePrinter(handle, buf, len(data), ctypes.byref(written))
            winspool.EndPagePrinter(handle)
            if not ok or written.value != len(data):
                raise Error(502, "printer write failed: %s" % ctypes.FormatError(ctypes.get_last_error()))
        finally:
            winspool.EndDocPrinter(handle)
    finally:
        winspool.ClosePrinter(handle)


def _win32_printers():
    import ctypes
    from ctypes import wintypes

    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class PRINTER_INFO_4(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]

    flags = 0x2 | 0x4  # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed, count = wintypes.DWORD(), wintypes.DWORD()
    winspool.EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(count))
    if not needed.value:
        return []
    buf = (ctypes.c_byte * needed.value)()
    if not winspool.EnumPrintersW(flags, None, 4, buf, needed, ctypes.byref(needed), ctypes.byref(count)):
        return []
    infos = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_4))
    return [infos[i].pPrinterName for i in range(count.value)]


def discover(scan):
    """List likely printers: CUPS queues, Windows printers, USB printer
    devices and, with `scan`, hosts on the local /24 with port 9100 open.
    Nothing is sent to anything found: a laser printer on 9100 would print
    stray bytes as a page."""
    found = []
    try:
        out = subprocess.run(["lpstat", "-v"], capture_output=True).stdout.decode("utf-8", "replace")
    except OSError:
        out = ""
    for ln in out.splitlines():
        # "device for NAME: URI"
        if ln.startswith("device for ") and ": " in ln:
            name, uri = ln[len("device for ") :].split(": ", 1)
            found.append({"connection": "cups:" + name, "how": "cups", "detail": uri.strip()})
    if os.name == "nt":
        try:
            for name in _win32_printers():
                found.append({"connection": "win32:" + name, "how": "win32", "detail": ""})
        except OSError:
            pass
    for d in ("/dev/usb", "/dev"):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        paths = sorted(
            os.path.join(d, n)
            for n in names
            if (d == "/dev/usb" and n.startswith("lp")) or (d == "/dev" and n.startswith(("ttyUSB", "ttyACM")))
        )
        for p in paths:
            found.append({"connection": p, "how": "serial" if "tty" in p else "usb", "detail": ""})
    if scan:
        found.extend(_scan_9100())
    return found


def _scan_9100():
    # The address the default route would use; no packet is sent.
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(("192.0.2.1", 9))
        me = ipaddress.IPv4Address(u.getsockname()[0])
        u.close()
    except OSError:
        return []
    net = ipaddress.IPv4Network("%s/24" % me, strict=False)
    hosts = [ip for ip in net.hosts() if ip != me]

    def probe(ip):
        try:
            with socket.create_connection((str(ip), 9100), timeout=0.3):
                return ip
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        hits = [ip for ip in pool.map(probe, hosts) if ip is not None]
    return [
        {"connection": "tcp://%s:9100" % ip, "how": "tcp", "detail": "port 9100 open (could also be an office printer)"}
        for ip in sorted(hits)
    ]
