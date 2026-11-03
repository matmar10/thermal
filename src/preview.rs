//! Laid-out items -> a text picture of the roll.
//!
//! Columns, wrapping and alignment are exact, because they come from the same
//! layout the bytes do. Bold is not shown; a 2x glyph is drawn letter-spaced
//! so it occupies its real width. Bitmaps, barcodes and QR codes appear as
//! boxes of roughly their printed height. Lengths are estimates.

use crate::config::Config;
use crate::job::{Align, CutKind};
use crate::layout::{Item, Layout};

pub struct Preview {
    pub text: String,
    /// Estimated length of each label (each piece between cuts), in mm,
    /// excluding the mechanical dead zone at its leading edge.
    pub labels_mm: Vec<f32>,
}

pub fn render(layout: &Layout, config: &Config) -> Preview {
    let stock = &config.stocks[&layout.stock_id];
    let cols = config.cols(stock, 1);
    let line_dots = config.printer.line_dots;
    let dpm = config.printer.dots_per_mm;
    let edge = format!("+{}+", "-".repeat(cols));
    let mut out = vec![format!("{edge}  {} ({cols} cols at 1x)", layout.stock_id)];
    let mut labels = Vec::new();
    let mut dots = 0u32;

    let push_box = |out: &mut Vec<String>, label: &str, width_cols: usize, height_dots: u32, align: Align| {
        let rows = (height_dots as f32 / line_dots as f32).round().max(1.0) as usize;
        let width = width_cols.clamp(label.len().min(cols).max(4), cols);
        let pad = match align {
            Align::Left => 0,
            Align::Center => (cols - width) / 2,
            Align::Right => cols - width,
        };
        for r in 0..rows {
            let inner = if r == rows / 2 {
                let l: String = label.chars().take(width.saturating_sub(2)).collect();
                format!("[{l:^w$}]", w = width - 2)
            } else {
                format!("[{}]", " ".repeat(width - 2))
            };
            out.push(format!("|{}{}{}|", " ".repeat(pad), inner, " ".repeat(cols - pad - width)));
        }
    };

    for item in &layout.items {
        match item {
            Item::Line { text, w, h, invert, align, bold, underline } => {
                let w = *w as usize;
                let spaced: String = if w > 1 { text.chars().flat_map(|c| std::iter::once(c).chain(std::iter::repeat_n(' ', w - 1))).collect() } else { text.clone() };
                let spaced = spaced.trim_end().to_string();
                let visible = spaced.len().min(cols);
                let pad = match align {
                    Align::Left => 0,
                    Align::Center => (cols - visible) / 2,
                    Align::Right => cols - visible,
                };
                let shown: String = spaced.chars().take(cols).collect();
                let mut notes = Vec::new();
                if w > 1 || *h > 1 {
                    notes.push(if w as u8 == *h { format!("{w}x") } else { format!("{w}x{h}") });
                }
                if *bold {
                    notes.push("bold".into());
                }
                if *underline {
                    notes.push("underline".into());
                }
                if *invert {
                    notes.push("inverted".into());
                }
                if spaced.len() > cols {
                    notes.push("OVERFLOW".into());
                }
                let notes = if notes.is_empty() { String::new() } else { format!("  {}", notes.join(" ")) };
                out.push(format!("|{}{}{}|{notes}", " ".repeat(pad), shown, " ".repeat(cols - pad - shown.len())));
                for _ in 1..*h {
                    out.push(format!("|{}|", " ".repeat(cols)));
                }
                dots += 24 * *h as u32 + (line_dots - 24);
            }
            Item::Raster { height, label, width, bits } => {
                // Find the ink's horizontal extent to draw the box where it prints.
                let row_bytes = (*width / 8) as usize;
                let (mut x0, mut x1) = (usize::MAX, 0);
                for row in bits.chunks(row_bytes) {
                    for (i, b) in row.iter().enumerate() {
                        if *b != 0 {
                            x0 = x0.min(i * 8);
                            x1 = x1.max(i * 8 + 8);
                        }
                    }
                }
                let cell = config.printer.cell_dots as usize;
                let (start, end) = if x0 == usize::MAX { (0, cols) } else { (x0 / cell, x1.div_ceil(cell).min(cols)) };
                let width_cols = (end - start).max(4);
                let label = format!("{label}, {:.0}mm tall", *height as f32 / dpm);
                let align = if start == 0 { Align::Left } else if end >= cols { Align::Right } else { Align::Center };
                push_box(&mut out, &label, width_cols, *height, align);
                dots += height;
            }
            Item::Barcode { data, symbology, height, align, width_dots, hri, .. } => {
                let label = format!("{symbology:?} {data}");
                let cols_w = (*width_dots / config.printer.cell_dots) as usize;
                let total = *height as u32 + if *hri == crate::job::Hri::None { 0 } else { 24 };
                push_box(&mut out, &label, cols_w, total, *align);
                dots += total + 6;
            }
            Item::Qr { data, align, size_dots, .. } => {
                let label = format!("QR {data}");
                push_box(&mut out, &label, (*size_dots / config.printer.cell_dots) as usize, *size_dots, *align);
                dots += size_dots;
            }
            Item::FeedLines(n) => {
                for _ in 0..*n {
                    out.push(format!("|{}|", " ".repeat(cols)));
                }
                dots += *n as u32 * line_dots;
            }
            Item::FeedDots(d) => {
                let rows = (*d as f32 / line_dots as f32).round() as usize;
                for _ in 0..rows {
                    out.push(format!("|{}|", " ".repeat(cols)));
                }
                dots += d;
            }
            Item::Cut(kind) => {
                let mm = dots as f32 / dpm;
                labels.push(mm);
                let what = match kind {
                    CutKind::None => "Tear here".to_string(),
                    kind => format!("{kind:?} cut"),
                };
                out.push(format!("{edge}  {what}, ~{mm:.0}mm"));
                dots = 0;
            }
        }
    }
    if dots > 0 {
        let mm = dots as f32 / dpm;
        labels.push(mm);
        out.push(format!("{edge}  no cut, ~{mm:.0}mm"));
    }
    if config.printer.dead_zone_mm > 0.0 {
        out.push(format!(
            "Each piece also starts with ~{}mm of blank paper (the cutter sits past the print head).",
            config.printer.dead_zone_mm
        ));
    }
    Preview { text: out.join("\n"), labels_mm: labels }
}
