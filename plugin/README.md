# Thermal Printer for Claude Code

Ask Claude to print a shopping list, a to-do list, a jar label, a folder
tab, a note, a barcode or a QR code, and it composes the print from simple
building blocks, previews it against your paper, and sends it to your ESC/POS
thermal printer, over the network or by USB.

## Install

```
/plugin marketplace add matmar10/thermal
/plugin install thermal-printer@thermal
```

Then ask Claude to "set up my thermal printer". It will:

1. find the printer: network (`tcp://ip:9100`), a USB printer in CUPS
   (`cups:<queue>`), or a Linux device (`/dev/usb/lp0`);
2. pick the closest model preset (generic 80mm and 58mm, Epson TM-T20/T88, and others);
3. print a calibration ruler, so you can tell it how wide each of your rolls
   really prints;
4. record your rolls, plain or adhesive, so it can warn before printing a
   receipt on sticker stock.

Settings live in `~/.config/thermal/printer.json` (or `$THERMAL_HOME`), and
your own templates in `~/.config/thermal/templates/`. Updating or removing
the plugin leaves them alone.

## What works

- Most ESC/POS printers: Epson TM series and the many compatible 58mm and
  80mm printers (Xprinter, Rongta, Munbyn, HOIN, Milestone and others).
  StarPRNT-only Star printers are not supported.
- macOS (Apple Silicon and Intel), Linux (x86_64 and arm64, including Raspberry
  Pi) and Windows (network printers only).
- Text in four sizes, big lettering in any size, rules, two-column rows,
  barcodes, QR codes, images, multi-label strips and cuts.

## The binary

`bin/thermal` is a short shell script. On first use it downloads the
`thermal` program for your platform from this version's GitHub release and
refuses to run it unless its SHA-256 matches the one pinned in
`checksums.txt`. The program is a single Rust binary with no runtime to
install; its source is at https://github.com/matmar10/thermal.

To use a binary you built yourself, set `THERMAL_BIN=/path/to/thermal`.

## License

MIT. The embedded DejaVu Sans Mono font is under its own license; see
`FONT-LICENSE`.
