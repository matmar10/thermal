//! Laid-out items -> ESC/POS bytes.
//!
//! NEVER emit `ESC K` (1B 4B). On this firmware it is "select bit image", not
//! reverse feed: a stray one makes the printer swallow thousands of following
//! bytes as bitmap data, and every later job vanishes until a power cycle.

use crate::config::Config;
use crate::job::{Align, CutKind, Hri, Symbology};
use crate::layout::{Item, Layout};

const ESC: u8 = 0x1b;
const GS: u8 = 0x1d;
const DLE: u8 = 0x10;
const EOT: u8 = 0x04;

/// `DLE EOT 4`: real-time paper sensor status. Answered immediately, ahead of
/// queued data; the only command here the printer replies to.
pub const PAPER_STATUS: [u8; 3] = [DLE, EOT, 0x04];

/// Rows per `GS v 0` band. Long rasters are split so no single command asks
/// the printer to buffer an unbounded image.
const RASTER_BAND_ROWS: u32 = 128;

pub fn encode(layout: &Layout, config: &Config) -> Vec<u8> {
    let stock = &config.stocks[&layout.stock_id];
    let mut out = vec![ESC, b'@'];
    // GS L 0 + GS W n: pin the print area to the stock, so ESC a centres
    // within 60mm paper instead of the 80mm carriage.
    out.extend([GS, b'L', 0, 0]);
    let w = stock.print_dots as u16;
    out.extend([GS, b'W', (w & 0xff) as u8, (w >> 8) as u8]);

    for item in &layout.items {
        match item {
            Item::Line { text, w, h, bold, underline, invert, align } => {
                out.extend(align_cmd(*align));
                let size = ((w - 1) << 4) | (h - 1);
                if size != 0 {
                    out.extend([GS, b'!', size]);
                }
                if *bold {
                    out.extend([ESC, b'E', 1]);
                }
                if *underline {
                    out.extend([ESC, b'-', 1]);
                }
                if *invert {
                    out.extend([GS, b'B', 1]);
                }
                out.extend(device_bytes(text, config));
                out.push(b'\n');
                if *invert {
                    out.extend([GS, b'B', 0]);
                }
                if *underline {
                    out.extend([ESC, b'-', 0]);
                }
                if *bold {
                    out.extend([ESC, b'E', 0]);
                }
                if size != 0 {
                    out.extend([GS, b'!', 0]);
                }
            }
            Item::Raster { width, height, bits, .. } => {
                out.extend(align_cmd(Align::Left));
                let row_bytes = (width / 8) as usize;
                let mut y = 0;
                while y < *height {
                    let rows = RASTER_BAND_ROWS.min(height - y);
                    out.extend([GS, b'v', b'0', 0, (row_bytes & 0xff) as u8, (row_bytes >> 8) as u8, (rows & 0xff) as u8, (rows >> 8) as u8]);
                    let start = y as usize * row_bytes;
                    out.extend(&bits[start..start + rows as usize * row_bytes]);
                    y += rows;
                }
            }
            Item::Barcode { data, symbology, height, module, hri, align, .. } => {
                out.extend(align_cmd(*align));
                out.extend([GS, b'h', *height, GS, b'w', *module, GS, b'H', hri_byte(*hri)]);
                let mut payload = data.as_bytes().to_vec();
                if *symbology == Symbology::CODE128 {
                    // Code set B: printable ASCII.
                    payload.splice(0..0, [b'{', b'B']);
                }
                out.extend([GS, b'k', symbology_byte(*symbology), payload.len() as u8]);
                out.extend(payload);
                out.push(b'\n');
            }
            Item::Qr { data, module, ec, align, .. } => {
                out.extend(align_cmd(*align));
                // Model 2, module size, error correction.
                out.extend([GS, b'(', b'k', 4, 0, 0x31, 0x41, 0x32, 0x00]);
                out.extend([GS, b'(', b'k', 3, 0, 0x31, 0x43, *module]);
                let ec_byte = match ec {
                    'L' => 0x30,
                    'Q' => 0x32,
                    'H' => 0x33,
                    _ => 0x31,
                };
                out.extend([GS, b'(', b'k', 3, 0, 0x31, 0x45, ec_byte]);
                // Store (pL pH = data + 3), then print.
                let bytes = data.as_bytes();
                let n = bytes.len() + 3;
                out.extend([GS, b'(', b'k', (n & 0xff) as u8, (n >> 8) as u8, 0x31, 0x50, 0x30]);
                out.extend(bytes);
                out.extend([GS, b'(', b'k', 3, 0, 0x31, 0x51, 0x30]);
                out.push(b'\n');
            }
            Item::FeedLines(n) => out.extend([ESC, b'd', *n]),
            Item::FeedDots(dots) => {
                // ESC J n feeds n dots; split anything over 255.
                let mut left = *dots;
                while left > 0 {
                    let step = left.min(255);
                    out.extend([ESC, b'J', step as u8]);
                    left -= step;
                }
            }
            Item::Cut(kind) => match kind {
                CutKind::Full => out.extend([GS, b'V', 0]),
                CutKind::Partial => out.extend([GS, b'V', 1]),
                CutKind::None => {}
            },
        }
    }
    out
}

fn align_cmd(align: Align) -> [u8; 3] {
    [ESC, b'a', match align {
        Align::Left => 0,
        Align::Center => 1,
        Align::Right => 2,
    }]
}

fn hri_byte(hri: Hri) -> u8 {
    match hri {
        Hri::None => 0,
        Hri::Above => 1,
        Hri::Below => 2,
        Hri::Both => 3,
    }
}

/// `GS k` format-2 type codes.
fn symbology_byte(sym: Symbology) -> u8 {
    match sym {
        Symbology::UPC_A => 65,
        Symbology::UPC_E => 66,
        Symbology::EAN13 => 67,
        Symbology::EAN8 => 68,
        Symbology::CODE39 => 69,
        Symbology::ITF => 70,
        Symbology::CODABAR => 71,
        Symbology::CODE128 => 73,
    }
}

/// Apply the model's substitution map and encode one byte per character.
/// Text is already folded to ASCII; a substitution may map to a Latin-1
/// character (the legacy printer draws 0xC4 as a rule) which encodes as-is.
fn device_bytes(text: &str, config: &Config) -> Vec<u8> {
    let subs = &config.printer.substitutions;
    let mut out = Vec::with_capacity(text.len());
    for ch in text.chars() {
        let mut buf = [0u8; 4];
        let key: &str = ch.encode_utf8(&mut buf);
        match subs.get(key) {
            Some(rep) => out.extend(rep.chars().map(latin1)),
            None => out.push(latin1(ch)),
        }
    }
    out
}

fn latin1(ch: char) -> u8 {
    u32::from(ch).try_into().unwrap_or(b'?')
}
