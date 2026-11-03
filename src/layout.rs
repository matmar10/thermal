//! Job -> laid-out items, at the geometry of one stock.
//!
//! Everything that depends on the paper happens here: wrapping, column
//! counts, fitting barcodes and QR codes to the width, rasterising big text
//! and images. `escpos` and `preview` both consume the result, so the bytes
//! and the preview always agree.

use ab_glyph::{point, Font, FontRef, PxScale, ScaleFont};
use base64::Engine;
use image::{imageops, GrayImage, Luma};

use crate::config::{Config, Cutter, Render, Stock};
use crate::error::{Error, Result};
use crate::job::{Align, Block, CutKind, Hri, Job, Size, Symbology, Width};
use crate::text::{fold, hang, wrap};

const FONT: &[u8] = include_bytes!("../assets/font.ttf");

const DEFAULT_FEED_LINES: u8 = 3;
const DEFAULT_BIG_MM: f32 = 10.0;
const DEFAULT_BARCODE_MM: f32 = 10.0;
const DEFAULT_BARCODE_MODULE: u8 = 3;
const DEFAULT_QR_MODULE: u8 = 6;
/// Margin left either side of a sideways banner, in dots.
const BANNER_MARGIN: u32 = 16;

#[derive(Debug, Clone)]
pub enum Item {
    Line { text: String, w: u8, h: u8, bold: bool, underline: bool, invert: bool, align: Align },
    /// 1-bit image exactly `width` (the stock print width) wide, alignment baked in.
    Raster { width: u32, height: u32, bits: Vec<u8>, label: String },
    Barcode { data: String, symbology: Symbology, height: u8, module: u8, hri: Hri, align: Align, width_dots: u32 },
    Qr { data: String, module: u8, ec: char, align: Align, size_dots: u32 },
    FeedLines(u8),
    FeedDots(u32),
    Cut(CutKind),
}

#[derive(Debug, Clone)]
pub struct Layout {
    pub stock_id: String,
    pub items: Vec<Item>,
    pub warnings: Vec<String>,
}

pub struct Options {
    /// Whether `image.path` may read the filesystem (CLI yes, HTTP no).
    pub allow_paths: bool,
}

pub fn layout(job: &Job, config: &Config, stock_id: &str, opts: &Options) -> Result<Layout> {
    let stock = config.stock(stock_id)?;
    let mut ctx = Ctx { config, stock, warnings: Vec::new(), opts };
    let feed = job.feed.unwrap_or(DEFAULT_FEED_LINES);

    let mut body = Vec::new();
    for (i, block) in job.blocks.iter().enumerate() {
        let items = ctx.block(block, feed).map_err(|e| Error { message: format!("block {} ({}): {}", i + 1, kind(block), e.message), ..e })?;
        body.extend(items);
    }

    let mut items = Vec::new();
    for _ in 0..job.copies.max(1) {
        items.extend(body.iter().cloned());
        items.push(Item::FeedLines(feed));
        if job.cut != CutKind::None {
            items.push(Item::Cut(cut_for(config, job.cut)));
        }
    }
    Ok(Layout { stock_id: stock_id.to_string(), items, warnings: ctx.warnings })
}

/// The cut the printer can actually make. Without a cutter it is a tear
/// mark: the feed before it still brings the paper past the tear bar.
fn cut_for(config: &Config, kind: CutKind) -> CutKind {
    match (config.printer.cutter, kind) {
        (Cutter::None, _) => CutKind::None,
        (Cutter::Full, CutKind::Partial) => CutKind::Full,
        (_, kind) => kind,
    }
}

fn kind(block: &Block) -> &'static str {
    match block {
        Block::Text { .. } => "text",
        Block::Big { .. } => "big",
        Block::Rule { .. } => "rule",
        Block::Row { .. } => "row",
        Block::Space { .. } => "space",
        Block::Barcode { .. } => "barcode",
        Block::Qr { .. } => "qr",
        Block::Image { .. } => "image",
        Block::Cut { .. } => "cut",
    }
}

struct Ctx<'a> {
    config: &'a Config,
    stock: &'a Stock,
    warnings: Vec<String>,
    opts: &'a Options,
}

impl Ctx<'_> {
    fn dots(&self) -> u32 {
        self.stock.print_dots
    }

    fn size(&self, size: Option<Size>) -> Result<(u8, u8)> {
        let (w, h) = size.map(Size::wh).unwrap_or((1, 1));
        let max = self.config.printer.max_text_size;
        if w == 0 || h == 0 || w > max || h > max {
            return Err(Error::bad(format!("text size {w}x{h} is outside 1..{max}"))
                .hint(format!("the {} clamps built-in text above {max}x; use a \"big\" block for larger text", self.printer_name())));
        }
        Ok((w, h))
    }

    fn block(&mut self, block: &Block, feed: u8) -> Result<Vec<Item>> {
        Ok(match block {
            Block::Text { text, size, bold, underline, invert, align, bullet, wrap: wrap_on } => {
                let (w, h) = self.size(*size)?;
                let cols = self.config.cols(self.stock, w);
                let body = fold(text.trim_end());
                if body.trim().is_empty() {
                    return Ok(vec![]);
                }
                let lines = match (bullet, wrap_on.unwrap_or(true)) {
                    (Some(prefix), true) => hang(&fold(prefix), &body, cols),
                    (None, true) => wrap(&body, cols),
                    (prefix, false) => {
                        let prefixed = format!("{}{body}", prefix.as_deref().map(fold).unwrap_or_default());
                        let lines: Vec<String> = prefixed.lines().map(str::to_string).collect();
                        for line in lines.iter().filter(|l| l.len() > cols) {
                            self.warnings.push(format!("\"{line}\" is {} chars; {cols} fit at {w}x on {}", line.len(), self.stock_name()));
                        }
                        lines
                    }
                };
                let align = align.unwrap_or(Align::Left);
                lines.into_iter().map(|text| Item::Line { text, w, h, bold: *bold, underline: *underline, invert: *invert, align }).collect()
            }
            Block::Row { left, right, fill, size, bold } => {
                let (w, h) = self.size(*size)?;
                let cols = self.config.cols(self.stock, w);
                let (left, right) = (fold(left), fold(right));
                let fill = fill.as_deref().map(fold).filter(|f| !f.is_empty()).unwrap_or_else(|| " ".to_string());
                let room = cols.saturating_sub(right.len() + 1).max(1);
                let mut lines = wrap(&left, room);
                let first = lines.remove(0);
                let gap = cols.saturating_sub(first.len() + right.len());
                let filler: String = if gap >= 2 {
                    format!(" {} ", fill.repeat(gap).chars().take(gap - 2).collect::<String>())
                } else {
                    " ".repeat(gap)
                };
                let mut out = vec![format!("{first}{filler}{right}")];
                out.extend(lines);
                out.into_iter().map(|text| Item::Line { text, w, h, bold: *bold, underline: false, invert: false, align: Align::Left }).collect()
            }
            Block::Rule { char, size } => {
                let (w, h) = self.size(*size)?;
                let cols = self.config.cols(self.stock, w);
                let pattern = char.as_deref().map(fold).filter(|c| !c.is_empty()).unwrap_or_else(|| "-".to_string());
                let text: String = pattern.chars().cycle().take(cols).collect();
                vec![Item::Line { text, w, h, bold: false, underline: false, invert: false, align: Align::Left }]
            }
            Block::Space { lines, mm } => match (lines, mm) {
                (_, Some(mm)) => vec![Item::FeedDots((mm * self.config.printer.dots_per_mm).round().max(0.0) as u32)],
                (Some(n), None) => vec![Item::FeedLines(*n)],
                (None, None) => vec![Item::FeedLines(1)],
            },
            Block::Cut { kind } => vec![Item::FeedLines(feed), Item::Cut(cut_for(self.config, kind.unwrap_or(CutKind::Partial)))],
            Block::Barcode { data, symbology, height, module, hri, align } => {
                let data = data.trim();
                if data.is_empty() {
                    return Ok(vec![]);
                }
                let symbology = symbology.unwrap_or(Symbology::CODE128);
                validate_barcode(symbology, data)?;
                let requested = module.unwrap_or(DEFAULT_BARCODE_MODULE).clamp(2, 6);
                let modules = barcode_modules(symbology, data.len());
                let mut fitted = requested;
                while fitted > 2 && modules * fitted as u32 > self.dots() {
                    fitted -= 1;
                }
                let width_dots = modules * fitted as u32;
                if width_dots > self.dots() {
                    return Err(Error::bad(format!("barcode \"{data}\" needs ~{width_dots} dots; {} has {}", self.stock_name(), self.dots()))
                        .hint("shorten the data, use a qr block, or print on receipt-80"));
                }
                if fitted < requested {
                    self.warnings.push(format!("barcode module reduced from {requested} to {fitted} dots to fit {}", self.stock_name()));
                }
                let dots = (height.unwrap_or(DEFAULT_BARCODE_MM) * self.config.printer.dots_per_mm).round().clamp(8.0, 255.0) as u8;
                vec![Item::Barcode {
                    data: data.to_string(),
                    symbology,
                    height: dots,
                    module: fitted,
                    hri: hri.unwrap_or(Hri::Below),
                    align: align.unwrap_or(Align::Center),
                    width_dots,
                }]
            }
            Block::Qr { data, module, ec, align } => {
                let data = data.trim();
                if data.is_empty() {
                    return Ok(vec![]);
                }
                let ec = match ec.as_deref().unwrap_or("M").trim().to_ascii_uppercase().as_str() {
                    "L" => 'L',
                    "M" => 'M',
                    "Q" => 'Q',
                    "H" => 'H',
                    other => return Err(Error::bad(format!("QR error correction \"{other}\" is not one of L, M, Q, H"))),
                };
                let side = qr_modules(data.len(), ec).ok_or_else(|| Error::bad(format!("{} bytes is too much for one QR code", data.len())))?;
                let requested = module.unwrap_or(DEFAULT_QR_MODULE).clamp(1, 16);
                let mut fitted = requested;
                while fitted > 1 && side * fitted as u32 > self.dots() {
                    fitted -= 1;
                }
                if fitted < requested {
                    self.warnings.push(format!("QR module reduced from {requested} to {fitted} dots to fit {}", self.stock_name()));
                }
                let align = align.unwrap_or(Align::Center);
                if self.config.printer.qr == Render::Raster {
                    return Ok(vec![self.qr_raster(data, ec, fitted, align)?]);
                }
                vec![Item::Qr { data: data.to_string(), module: fitted, ec, align, size_dots: side * fitted as u32 }]
            }
            Block::Big { text, height, fit, sideways, align } => {
                let mut items = Vec::new();
                for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
                    let img = self.big(line, *height, *fit, *sideways)?;
                    let label = format!("big \"{line}\"");
                    items.push(self.raster(&img, align.unwrap_or(Align::Center), false, label));
                }
                items
            }
            Block::Image { path, base64, width, align, dither } => {
                let bytes = match (path, base64) {
                    (_, Some(b64)) => {
                        let b64 = b64.trim();
                        let b64 = b64.split_once("base64,").map(|(_, d)| d).unwrap_or(b64);
                        base64::engine::general_purpose::STANDARD.decode(b64.trim()).map_err(|e| Error::bad(format!("image base64: {e}")))?
                    }
                    (Some(path), None) if self.opts.allow_paths => {
                        std::fs::read(path).map_err(|e| Error::bad(format!("cannot read image {path}: {e}")))?
                    }
                    (Some(_), None) => return Err(Error::bad("image \"path\" is not allowed over HTTP").hint("send the image as \"base64\"")),
                    (None, None) => return Err(Error::bad("image needs \"path\" or \"base64\"")),
                };
                let decoded = image::load_from_memory(&bytes).map_err(|e| Error::bad(format!("cannot decode image: {e}")))?;
                let rgba = decoded.to_rgba8();
                // Flatten onto white first: a transparent background would
                // otherwise greyscale to black and print as a solid slab.
                let gray = GrayImage::from_fn(rgba.width(), rgba.height(), |x, y| {
                    let p = rgba.get_pixel(x, y).0;
                    let a = p[3] as f32 / 255.0;
                    let l = 0.299 * p[0] as f32 + 0.587 * p[1] as f32 + 0.114 * p[2] as f32;
                    Luma([(l * a + 255.0 * (1.0 - a)).round() as u8])
                });
                let target = match width {
                    None => gray.width().min(self.dots()),
                    Some(Width::Dots(d)) => (*d).min(self.dots()),
                    Some(Width::Named(n)) if n == "full" => self.dots(),
                    Some(Width::Named(n)) => n.parse::<u32>().map_err(|_| Error::bad(format!("image width \"{n}\" should be dots or \"full\"")))?.min(self.dots()),
                };
                let target = target.max(8);
                let resized = if target == gray.width() {
                    gray
                } else {
                    let h = ((gray.height() as f32) * target as f32 / gray.width() as f32).round().max(1.0) as u32;
                    imageops::resize(&gray, target, h, imageops::FilterType::Lanczos3)
                };
                vec![self.raster(&resized, align.unwrap_or(Align::Center), *dither, format!("image {}x{} dots", resized.width(), resized.height()))]
            }
        })
    }

    fn printer_name(&self) -> &str {
        let p = &self.config.printer;
        if p.name.is_empty() { &p.model } else { &p.name }
    }

    fn stock_name(&self) -> String {
        format!("{}mm stock", self.stock.width_mm)
    }

    /// Draw one line of text with the embedded font and return it trimmed to
    /// the ink, sized and (for banners) rotated to its final orientation.
    fn big(&mut self, text: &str, height_mm: Option<f32>, fit: bool, sideways: bool) -> Result<GrayImage> {
        let font = FontRef::try_from_slice(FONT).map_err(|e| Error::new(500, format!("font: {e}")))?;
        let dots_per_mm = self.config.printer.dots_per_mm;
        let cap = cap_ratio(&font);
        let max_across = if sideways { self.dots().saturating_sub(2 * BANNER_MARGIN) } else { self.dots() };

        // Requested cap height in dots; a fitted or default-sized banner is
        // drawn large and scaled to the width below.
        let cap_dots = match height_mm {
            Some(mm) => mm * dots_per_mm,
            None if sideways || fit => max_across as f32,
            None => DEFAULT_BIG_MM * dots_per_mm,
        };
        let img = draw_line(&font, text, (cap_dots / cap).max(8.0));
        let img = if sideways { imageops::rotate90(&img) } else { img };

        let fill_width = fit || (sideways && height_mm.is_none());
        let scale = if fill_width || img.width() > max_across { max_across as f32 / img.width() as f32 } else { 1.0 };
        if let (false, Some(mm)) = (fill_width, height_mm) {
            if scale < 0.95 {
                // Say what will actually print rather than shrinking silently.
                self.warnings.push(format!("big \"{text}\" shrunk to fit the width: capitals ~{:.1}mm, not {mm}mm", mm * scale));
            }
        }
        Ok(scale_image(img, scale))
    }

    /// A QR code drawn here, for printers without `GS ( k`. A 4-module quiet
    /// zone either side, as the native command leaves.
    fn qr_raster(&self, data: &str, ec: char, module: u8, align: Align) -> Result<Item> {
        let level = match ec {
            'L' => qrcode::EcLevel::L,
            'Q' => qrcode::EcLevel::Q,
            'H' => qrcode::EcLevel::H,
            _ => qrcode::EcLevel::M,
        };
        let code = qrcode::QrCode::with_error_correction_level(data.as_bytes(), level).map_err(|e| Error::bad(format!("QR: {e}")))?;
        let n = code.width() as u32;
        let mut module = module as u32;
        while module > 1 && (n + 8) * module > self.dots() {
            module -= 1;
        }
        let side = (n + 8) * module;
        let colors = code.to_colors();
        let img = GrayImage::from_fn(side, side, |x, y| {
            let (mx, my) = ((x / module) as i64 - 4, (y / module) as i64 - 4);
            let dark = mx >= 0 && my >= 0 && (mx as u32) < n && (my as u32) < n && colors[(my as u32 * n + mx as u32) as usize] == qrcode::Color::Dark;
            Luma([if dark { 0 } else { 255 }])
        });
        Ok(self.raster(&img, align, false, format!("QR (image) {n}x{n} modules")))
    }

    /// Threshold or dither to 1 bit and pad to the full print width, with the
    /// alignment baked into the padding rather than left to `ESC a`.
    fn raster(&self, img: &GrayImage, align: Align, dither: bool, label: String) -> Item {
        let width = self.dots().div_ceil(8) * 8;
        let ink_w = img.width().min(width);
        let left = match align {
            Align::Left => 0,
            Align::Center => (width - ink_w) / 2,
            Align::Right => width - ink_w,
        };
        let black = to_mono(img, dither);
        let height = img.height();
        let row_bytes = (width / 8) as usize;
        let mut bits = vec![0u8; row_bytes * height as usize];
        for y in 0..height {
            for x in 0..ink_w {
                if black[(y * img.width() + x) as usize] {
                    let px = (left + x) as usize;
                    bits[y as usize * row_bytes + px / 8] |= 0x80 >> (px % 8);
                }
            }
        }
        Item::Raster { width, height, bits, label }
    }
}

fn scale_image(img: GrayImage, scale: f32) -> GrayImage {
    if (scale - 1.0).abs() < 0.001 {
        return img;
    }
    let w = ((img.width() as f32) * scale).round().max(1.0) as u32;
    let h = ((img.height() as f32) * scale).round().max(1.0) as u32;
    imageops::resize(&img, w, h, imageops::FilterType::Triangle)
}

/// Cap height of the font as a fraction of its pixel size, from 'H'.
fn cap_ratio(font: &FontRef) -> f32 {
    let scale = PxScale::from(100.0);
    let glyph = font.glyph_id('H').with_scale_and_position(scale, point(0.0, 0.0));
    font.outline_glyph(glyph).map(|g| g.px_bounds().height() / 100.0).unwrap_or(0.73)
}

fn draw_line(font: &FontRef, text: &str, px: f32) -> GrayImage {
    let scale = PxScale::from(px);
    let scaled = font.as_scaled(scale);
    let mut glyphs = Vec::new();
    let mut x = 0.0f32;
    let mut prev = None;
    for ch in text.chars() {
        let id = scaled.glyph_id(ch);
        if let Some(p) = prev {
            x += scaled.kern(p, id);
        }
        glyphs.push(id.with_scale_and_position(scale, point(x, scaled.ascent())));
        x += scaled.h_advance(id);
        prev = Some(id);
    }
    let w = x.ceil() as u32 + 4;
    let h = (scaled.ascent() - scaled.descent()).ceil() as u32 + 4;
    let mut img = GrayImage::from_pixel(w, h, Luma([255]));
    for glyph in glyphs {
        if let Some(outline) = font.outline_glyph(glyph) {
            let b = outline.px_bounds();
            outline.draw(|gx, gy, cov| {
                let px = b.min.x as i32 + gx as i32;
                let py = b.min.y as i32 + gy as i32;
                if px >= 0 && py >= 0 && (px as u32) < w && (py as u32) < h {
                    let v = 255 - (cov.clamp(0.0, 1.0) * 255.0) as u8;
                    let p = img.get_pixel_mut(px as u32, py as u32);
                    p.0[0] = p.0[0].min(v);
                }
            });
        }
    }
    trim(&img)
}

/// Crop to the bounding box of the ink.
fn trim(img: &GrayImage) -> GrayImage {
    let (mut x0, mut y0, mut x1, mut y1) = (u32::MAX, u32::MAX, 0, 0);
    for (x, y, p) in img.enumerate_pixels() {
        if p.0[0] < 128 {
            x0 = x0.min(x);
            y0 = y0.min(y);
            x1 = x1.max(x);
            y1 = y1.max(y);
        }
    }
    if x0 == u32::MAX {
        return GrayImage::from_pixel(8, 1, Luma([255]));
    }
    imageops::crop_imm(img, x0, y0, x1 - x0 + 1, y1 - y0 + 1).to_image()
}

/// true = print a dot.
fn to_mono(img: &GrayImage, dither: bool) -> Vec<bool> {
    let (w, h) = (img.width() as usize, img.height() as usize);
    if !dither {
        return img.pixels().map(|p| p.0[0] < 128).collect();
    }
    let mut buf: Vec<f32> = img.pixels().map(|p| p.0[0] as f32).collect();
    let mut out = vec![false; w * h];
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            let old = buf[i];
            let new = if old < 128.0 { 0.0 } else { 255.0 };
            out[i] = new == 0.0;
            let err = old - new;
            if x + 1 < w {
                buf[i + 1] += err * 7.0 / 16.0;
            }
            if y + 1 < h {
                if x > 0 {
                    buf[i + w - 1] += err * 3.0 / 16.0;
                }
                buf[i + w] += err * 5.0 / 16.0;
                if x + 1 < w {
                    buf[i + w + 1] += err / 16.0;
                }
            }
        }
    }
    out
}

fn validate_barcode(sym: Symbology, data: &str) -> Result<()> {
    let digits = data.chars().all(|c| c.is_ascii_digit());
    let ok = match sym {
        Symbology::EAN13 => digits && (12..=13).contains(&data.len()),
        Symbology::EAN8 => digits && (7..=8).contains(&data.len()),
        Symbology::UPC_A => digits && (11..=12).contains(&data.len()),
        Symbology::UPC_E => digits && (6..=8).contains(&data.len()),
        Symbology::ITF => digits && data.len() % 2 == 0,
        Symbology::CODE39 => data.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || " -.$/+%".contains(c)),
        Symbology::CODABAR => data.chars().all(|c| c.is_ascii_digit() || "-$:/.+ABCD".contains(c)),
        Symbology::CODE128 => data.chars().all(|c| (' '..='~').contains(&c)),
    };
    if !ok {
        return Err(Error::bad(format!("\"{data}\" is not valid {sym:?} data")).hint(match sym {
            Symbology::EAN13 => "EAN13 takes 12 or 13 digits",
            Symbology::EAN8 => "EAN8 takes 7 or 8 digits",
            Symbology::UPC_A => "UPC_A takes 11 or 12 digits",
            Symbology::UPC_E => "UPC_E takes 6 to 8 digits",
            Symbology::ITF => "ITF takes an even number of digits",
            Symbology::CODE39 => "CODE39 takes A-Z, 0-9, space and - . $ / + %",
            Symbology::CODABAR => "CODABAR takes digits and - $ : / . +",
            Symbology::CODE128 => "CODE128 takes printable ASCII",
        }));
    }
    if data.len() > 255 {
        return Err(Error::bad("barcode data is limited to 255 characters"));
    }
    Ok(())
}

/// Approximate width of a barcode in modules, including quiet zones.
fn barcode_modules(sym: Symbology, len: usize) -> u32 {
    let n = len as u32;
    match sym {
        Symbology::CODE128 => 11 * (n + 2) + 13 + 20,
        Symbology::CODE39 => 16 * (n + 2) + 20,
        Symbology::EAN13 | Symbology::UPC_A => 95 + 18,
        Symbology::EAN8 => 67 + 14,
        Symbology::UPC_E => 51 + 18,
        Symbology::ITF => 9 * n + 9 + 20,
        Symbology::CODABAR => 12 * (n + 2) + 20,
    }
}

/// Modules per side of the smallest QR symbol holding `len` bytes, or None
/// past version 40.
fn qr_modules(len: usize, ec: char) -> Option<u32> {
    // Byte-mode capacity at EC level M for versions 1..=40.
    const M: [u32; 40] = [
        14, 26, 42, 62, 84, 106, 122, 152, 180, 213, 251, 287, 331, 362, 412, 450, 504, 560, 624, 666, 711, 779, 857, 911, 997, 1059,
        1125, 1190, 1264, 1370, 1452, 1538, 1628, 1722, 1809, 1911, 1989, 2099, 2213, 2331,
    ];
    let factor = match ec {
        'L' => 1.27,
        'Q' => 0.72,
        'H' => 0.55,
        _ => 1.0,
    };
    M.iter()
        .position(|&cap| (cap as f32 * factor) as usize >= len)
        .map(|v| 17 + 4 * (v as u32 + 1) + 8) // + 4-module quiet zone each side
}

/// The calibration ruler: `carriage` dots wide, a short tick every 8 dots and
/// a tall, numbered one every 48. Each number ends at its own dot, so the
/// last number fully on the paper is the printable width.
pub fn ruler(carriage: u32) -> Result<Item> {
    let font = FontRef::try_from_slice(FONT).map_err(|e| Error::new(500, format!("font: {e}")))?;
    let px = 16.0 / cap_ratio(&font);
    let (label_h, tall, short) = (24u32, 28u32, 12u32);
    let height = label_h + tall;
    let mut img = GrayImage::from_pixel(carriage, height, Luma([255]));
    let ink = |img: &mut GrayImage, x0: u32, x1: u32, y0: u32, y1: u32| {
        for y in y0..y1.min(height) {
            for x in x0..x1.min(carriage) {
                img.put_pixel(x, y, Luma([0]));
            }
        }
    };
    // Baseline along the bottom.
    ink(&mut img, 0, carriage, height - 2, height);
    let mut at = 8;
    while at <= carriage {
        let numbered = at % 48 == 0;
        let len = if numbered { tall } else { short };
        ink(&mut img, at - 2, at, height - len, height);
        if numbered {
            let label = draw_line(&font, &at.to_string(), px);
            let left = at.saturating_sub(label.width());
            for (x, y, p) in label.enumerate_pixels() {
                if p.0[0] < 128 && left + x < carriage && y < label_h {
                    img.put_pixel(left + x, y, Luma([0]));
                }
            }
        }
        at += 8;
    }
    let width = carriage.div_ceil(8) * 8;
    let row_bytes = (width / 8) as usize;
    let mut bits = vec![0u8; row_bytes * height as usize];
    for (x, y, p) in img.enumerate_pixels() {
        if p.0[0] == 0 {
            bits[y as usize * row_bytes + x as usize / 8] |= 0x80 >> (x % 8);
        }
    }
    Ok(Item::Raster { width, height, bits, label: format!("ruler, {carriage} dots") })
}
