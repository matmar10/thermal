//! Getting bytes to the printer, by whichever route `printer.connection` names:
//!
//! - `tcp://host:port`: raw TCP ("JetDirect", usually port 9100).
//! - `cups:<queue>`: a CUPS queue, sent raw with `lp -o raw`. This is how a
//!   USB printer is reached on macOS and Linux without a USB library.
//! - `/dev/...` or `file:<path>`: written straight to a device, e.g. Linux
//!   `/dev/usb/lp0` or a serial port.
//!
//! Only TCP can ask the paper sensor; the others report "unknown".

use std::io::{Read, Write};
use std::net::{Shutdown, TcpStream, ToSocketAddrs};
use std::process::{Command, Stdio};
use std::time::Duration;

use serde::Serialize;

use crate::config::Printer;
use crate::error::{Error, Result};
use crate::escpos::PAPER_STATUS;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Transport {
    Tcp(String),
    Cups(String),
    Device(String),
}

impl Transport {
    pub fn parse(conn: &str) -> Result<Transport> {
        let conn = conn.trim();
        if conn.is_empty() {
            return Err(Error::new(crate::config::NOT_CONFIGURED, "no printer connection is configured")
                .hint("`print-to-thermal set connection tcp://<ip>:9100` (or cups:<queue>, or a device path); `print-to-thermal discover` lists candidates"));
        }
        if let Some(addr) = conn.strip_prefix("tcp://") {
            let addr = addr.trim_end_matches('/');
            let addr = if addr.contains(':') { addr.to_string() } else { format!("{addr}:9100") };
            return Ok(Transport::Tcp(addr));
        }
        if let Some(queue) = conn.strip_prefix("cups:") {
            let queue = queue.trim_start_matches("//");
            if queue.is_empty() {
                return Err(Error::bad("cups: needs a queue name, e.g. cups:Receipt_Printer").hint("`lpstat -v` lists queues"));
            }
            return Ok(Transport::Cups(queue.to_string()));
        }
        if let Some(path) = conn.strip_prefix("file:") {
            return Ok(Transport::Device(path.trim_start_matches("//").to_string()));
        }
        if conn.starts_with('/') {
            return Ok(Transport::Device(conn.to_string()));
        }
        Err(Error::bad(format!("cannot read printer connection \"{conn}\"")).hint("use tcp://host:port, cups:<queue>, or a device path like /dev/usb/lp0"))
    }
}

impl std::fmt::Display for Transport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Transport::Tcp(addr) => write!(f, "tcp://{addr}"),
            Transport::Cups(queue) => write!(f, "cups:{queue}"),
            Transport::Device(path) => write!(f, "{path}"),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Status {
    pub target: String,
    /// "unknown" means nobody answered, NOT that anything is wrong.
    pub paper: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw: Option<u8>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

fn connect(addr: &str, timeout: Duration) -> Result<TcpStream> {
    let sock = addr
        .to_socket_addrs()
        .map_err(|e| Error::new(502, format!("cannot resolve printer {addr}: {e}")))?
        .next()
        .ok_or_else(|| Error::new(502, format!("no address for printer {addr}")))?;
    TcpStream::connect_timeout(&sock, timeout).map_err(|e| {
        Error::new(502, format!("cannot reach printer at {addr}: {e}")).hint("is it on and on the network? check the connection with `print-to-thermal config`")
    })
}

/// Ask the paper sensor. In the reply, bits 5-6 set = out, bits 2-3 = near end.
pub fn status(p: &Printer) -> Status {
    let unknown = |target: String, error: Option<String>| Status { target, paper: "unknown", raw: None, error };
    let transport = match Transport::parse(&p.connection) {
        Ok(t) => t,
        Err(e) => return unknown(p.connection.clone(), Some(e.message)),
    };
    let target = transport.to_string();
    let Transport::Tcp(addr) = &transport else {
        return unknown(target, None);
    };
    if !p.status {
        return unknown(target, None);
    }
    let query = || -> Result<Option<u8>> {
        let mut s = connect(addr, Duration::from_secs(3))?;
        s.set_read_timeout(Some(Duration::from_millis(1500)))?;
        s.write_all(&PAPER_STATUS)?;
        let mut byte = [0u8; 1];
        match s.read(&mut byte) {
            Ok(1) => Ok(Some(byte[0])),
            // Silence is an answer too: firmware that ignores DLE EOT.
            _ => Ok(None),
        }
    };
    match query() {
        Ok(Some(raw)) => {
            let paper = if raw & 0x60 == 0x60 {
                "out"
            } else if raw & 0x0c == 0x0c {
                "low"
            } else {
                "ok"
            };
            Status { target, paper, raw: Some(raw), error: None }
        }
        Ok(None) => unknown(target, None),
        Err(e) => unknown(target, Some(e.message)),
    }
}

/// Send bytes. Returning Ok means the printer (or the spooler) ACCEPTED them,
/// not that paper came out.
pub fn send(p: &Printer, bytes: &[u8]) -> Result<()> {
    match Transport::parse(&p.connection)? {
        Transport::Tcp(addr) => {
            let mut s = connect(&addr, Duration::from_secs(5))?;
            s.set_write_timeout(Some(Duration::from_secs(30)))?;
            s.write_all(bytes).map_err(|e| Error::new(502, format!("printer write failed: {e}")))?;
            s.flush()?;
            let _ = s.shutdown(Shutdown::Write);
            Ok(())
        }
        Transport::Cups(queue) => {
            let mut child = Command::new("lp")
                .args(["-d", &queue, "-o", "raw", "-s"])
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|e| Error::new(502, format!("cannot run lp: {e}")).hint("cups: needs CUPS (macOS, Linux); on Windows use tcp://"))?;
            child.stdin.take().expect("piped stdin").write_all(bytes).map_err(|e| Error::new(502, format!("writing to lp: {e}")))?;
            let out = child.wait_with_output()?;
            if !out.status.success() {
                return Err(Error::new(502, format!("lp -d {queue} failed: {}", String::from_utf8_lossy(&out.stderr).trim()))
                    .hint("`lpstat -v` lists queues; the queue must accept raw data"));
            }
            Ok(())
        }
        Transport::Device(path) => {
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .open(&path)
                .map_err(|e| Error::new(502, format!("cannot open {path}: {e}")).hint("check the device exists and you may write to it (e.g. the lp group on Linux)"))?;
            f.write_all(bytes).map_err(|e| Error::new(502, format!("writing {path}: {e}")))?;
            f.flush()?;
            Ok(())
        }
    }
}

/// One place a printer might be found.
#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
    pub connection: String,
    pub how: &'static str,
    pub detail: String,
}

/// List likely printers: CUPS queues, USB printer devices and, with `scan`,
/// hosts on the local /24 with port 9100 open. Nothing is sent to anything
/// found: a laser printer on 9100 would print stray bytes as a page.
pub fn discover(scan: bool) -> Vec<Candidate> {
    let mut found = Vec::new();
    if let Ok(out) = Command::new("lpstat").arg("-v").output() {
        for line in String::from_utf8_lossy(&out.stdout).lines() {
            // "device for NAME: URI"
            if let Some((name, uri)) = line.strip_prefix("device for ").and_then(|l| l.split_once(": ")) {
                found.push(Candidate { connection: format!("cups:{name}"), how: "cups", detail: uri.trim().to_string() });
            }
        }
    }
    for dir in ["/dev/usb", "/dev"] {
        let Ok(entries) = std::fs::read_dir(dir) else { continue };
        let mut paths: Vec<String> = entries
            .filter_map(|e| e.ok())
            .map(|e| e.path().display().to_string())
            .filter(|p| {
                let name = p.rsplit('/').next().unwrap_or_default();
                (dir == "/dev/usb" && name.starts_with("lp")) || (dir == "/dev" && (name.starts_with("ttyUSB") || name.starts_with("ttyACM")))
            })
            .collect();
        paths.sort();
        for p in paths {
            let how = if p.contains("tty") { "serial" } else { "usb" };
            found.push(Candidate { connection: p, how, detail: String::new() });
        }
    }
    if scan {
        found.extend(scan_9100());
    }
    found
}

fn scan_9100() -> Vec<Candidate> {
    // The address the default route would use; no packet is sent.
    let Some(std::net::IpAddr::V4(me)) = std::net::UdpSocket::bind("0.0.0.0:0").and_then(|s| s.connect("192.0.2.1:9").map(|_| s)).and_then(|s| s.local_addr()).ok().map(|a| a.ip()) else {
        return Vec::new();
    };
    let [a, b, c, _] = me.octets();
    let hosts: Vec<std::net::Ipv4Addr> = (1..=254u8).map(|d| std::net::Ipv4Addr::new(a, b, c, d)).filter(|ip| *ip != me).collect();
    let mut found: Vec<Candidate> = std::thread::scope(|s| {
        let workers: Vec<_> = hosts
            .chunks(32)
            .map(|chunk| {
                s.spawn(move || {
                    chunk
                        .iter()
                        .filter(|ip| TcpStream::connect_timeout(&(**ip, 9100).into(), Duration::from_millis(300)).is_ok())
                        .map(|ip| Candidate { connection: format!("tcp://{ip}:9100"), how: "tcp", detail: "port 9100 open (could also be an office printer)".into() })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        workers.into_iter().flat_map(|w| w.join().unwrap_or_default()).collect()
    });
    found.sort_by(|x, y| x.connection.cmp(&y.connection));
    found
}
