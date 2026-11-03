---
name: thermal
description: Print on the user's ESC/POS thermal receipt or label printer — shopping and to-do lists, agendas, jar/bin/box labels, folder tabs, notes, tickets, barcodes, QR codes and images. Compose a job from primitives (text sizes, big bitmap text, rules, rows, space, barcode, QR, image, cut), preview it against the paper, then print. Also sets the printer up the first time (find it, pick a model, measure the paper), records which roll is loaded, checks for paper, and saves reusable print templates. TRIGGER when the user asks to print something, make or print a label or sticker, print a list or receipt, label a jar/bin/folder, or mentions a thermal, receipt or label printer.
allowed-tools: Bash(thermal *)
---

# Thermal printer

`thermal` is on PATH while this plugin is enabled: one self-contained binary,
fetched and checksum-verified on first use. It keeps the user's printer
settings and templates in `~/.config/thermal/` (or `$THERMAL_HOME`), so plugin
updates never lose them.

Printer connection entered at install (blank or a `${...}` placeholder if
none): `${user_config.printer}`

## First run: set up the printer

Run `thermal config`. If it exits with code 3 ("not configured"), set the
printer up before printing anything. Take it a step at a time with the user:

1. **How is it connected?** If a connection was entered at install (above),
   use it and skip to step 2. Otherwise: network (Wi-Fi/Ethernet) or USB. Run
   `thermal discover` to list CUPS queues and USB/serial devices. Office laser
   and inkjet printers show up too: ask which one is the receipt printer, and
   never send to one of those. For a
   network printer, ask for its IP (often on a self-test page: hold FEED
   while switching it on), or with their OK run `thermal discover --scan` to
   look for port 9100 on the local network. The connection is one of:
   - `tcp://<ip>:9100`: a network printer
   - `cups:<queue>`: a USB printer added to CUPS (macOS/Linux) as a raw queue
   - `/dev/usb/lp0` or a serial device: a USB printer on Linux
2. **Which model?** Run `thermal models` and pick the closest preset. Most
   clones are `generic-80mm` or `generic-58mm`.
3. **Write the config:** `thermal setup --printer <conn> --model <id>`. This
   creates one roll at the printer's full width.
4. **Measure the paper.** Ask the user to load a roll, then, with their OK,
   run `thermal calibrate`. It prints a numbered ruler. Ask for the last number
   that is fully on the paper: that is the roll's width in dots. Never guess
   it, because printers do not print edge to edge.
5. **Record each roll they own:** `thermal stock add <id>=<width_mm>:<dots>[:adhesive]`.
   Use short, descriptive ids (`receipt-80`, `labels-50-sticky`), mark sticky
   label stock `adhesive`, and remove the placeholder (`thermal stock rm roll-80`)
   once a real roll is loaded (`thermal stock <id>`).
6. Print a short test, such as the `note` template, and ask how it looks.

If a connection was entered at install and differs from the one
`thermal config` shows, the user changed it: run `thermal set connection <it>`.

Settings can be changed later with `thermal set <key> <value>`. The keys are
`connection`, `model`, `max_text_size`, `cutter` (`none`, `partial`, `full`),
`qr` (`native`, `raster`), `status` and the other printer fields. Use it when a
test print shows something is off: no cut means `cutter none`, a missing QR
code means `qr raster`, and so on.

## Printing

1. **Know the medium.** `thermal stocks` gives the printer's limits, every
   roll with its characters per line at each text size, and which roll is
   `loaded`.
2. **Check the roll when it matters.** The printer cannot sense which roll is
   in it, so `loaded` is only what was last recorded. If the job needs a
   particular stock, ask; anything that must stick needs an adhesive roll.
   When they swap rolls, run `thermal stock <id>`.
3. **Compose** an ad-hoc job, or use a template (`thermal templates`).
4. **Preview.** `thermal preview <job>`. Fix every `warning:` and `OVERFLOW`.
5. **Print.** `thermal print <job>` checks the paper sensor (network printers
   only), then sends it. "sent" means the printer accepted the bytes, not that
   paper came out. Say so.

`<job>` is inline JSON (`'{...}'`), `-` for stdin, a `.json` file, or a
template name. Template values: `--set key=value` (JSON parses, so
`--set items='["a","b"]'` works) or `--data '{...}'`. Other flags: `--stock`,
`--copies`, `--force`, `--dry-run`, `--json`.

A job naming a `stock` other than the loaded one is **refused** without
`--force`. Name the stock when it matters, so this protects the adhesive roll.

## Limits that apply to every printer

- Built-in `text` sizes run from 1 up to the printer's `max_text_size`
  (usually 4); characters per line at each size come from `thermal stocks`.
  For anything larger, use `big`.
- Built-in text is **ASCII only**. `° — ½ é` and the like are folded to ASCII
  automatically. If a symbol matters, use `big`, which draws any character.
- Some printers leave a blank margin before the first line (`dead_zone_mm`).
  It is mechanical; do not try to remove it.
- Without a cutter (`cutter: none`), a cut becomes a feed to the tear bar.

## Job format

```json
{ "stock": "receipt-80", "copies": 1, "cut": "partial", "feed": 3,
  "blocks": [ ... ] }
```

`stock`, `copies` (each copy is cut separately), `cut` (`partial`|`full`|`none`)
and `feed` (lines before each cut) are all optional.

| block | fields (all optional except the first) |
|---|---|
| `text` | `text`; `size` 1-max or `[w,h]`; `bold`, `underline`, `invert`; `align` left/center/right; `bullet` (hanging indent, e.g. `"[ ] "`); `wrap` (default true) |
| `big` | `text`; `height` (capital height in mm, default 10, shrunk to fit with a warning); `fit` (fill the width); `sideways` (a banner along the strip); `align` (default center). Each newline starts a new line. |
| `rule` | `char` (default `-`; patterns such as `"= "` work); `size` |
| `row` | `left`, `right`; `fill` (e.g. `"."`); `size`; `bold`. Gives `Milk ........ 2` |
| `space` | `lines` or `mm` |
| `barcode` | `data`; `symbology` (CODE128 by default, plus EAN13, EAN8, UPC_A, UPC_E, CODE39, ITF, CODABAR); `height` mm; `module` 2-6; `hri` none/above/below/both; `align`. Shrinks to fit; refused if it cannot fit. |
| `qr` | `data`; `module` 1-16 (default 6, shrinks to fit); `ec` L/M/Q/H |
| `image` | `path` (CLI) or `base64`; `width` (dots or `"full"`); `align`; `dither` (for photos; line art looks better without it) |
| `cut` | `kind`: cut mid-job, to print several labels in one job |

A block whose text or data is empty is skipped, so optional template values
simply vanish. Unknown fields are rejected, so a typo fails loudly instead of
printing wrong.

Design tips: wider text sizes make lists readable at a glance (size 2 bold
headings, size 2 items work well on 80mm), checkboxes come from
`"bullet": "[ ] "`, and a `big` title stands out.

## Templates

A template is a job plus `description`, `params` (name -> what to pass) and
`defaults`. Any string can hold `{{name}}`. A string that is *exactly*
`"{{name}}"` passes the raw value, so arrays and numbers work. An `each` block
loops over a list:

```json
{ "type": "each", "items": "{{items}}", "as": "item", "blocks": [
  { "type": "row", "left": "{{index}}. {{item.name}}", "right": "{{item.qty}}", "fill": "." } ] }
```

Built in: `shopping-list`, `folder-tab`, `item-label`, `note`, `day`.
`thermal templates` lists them with the user's own; `thermal templates <name>`
shows one. Save a template in `~/.config/thermal/templates/<name>.json` (a
user template replaces a built-in one of the same name).

Default to ad hoc. Save a template only when the user wants to reuse a layout,
or the same kind of print keeps coming up.

## Example

```sh
thermal print - <<'EOF'
{ "blocks": [
  { "type": "big", "text": "Saturday", "height": 8 },
  { "type": "rule", "char": "=" },
  { "type": "text", "text": "ERRANDS", "size": 2, "bold": true },
  { "type": "text", "text": "Hardware store", "bullet": "[ ] ", "size": 2 },
  { "type": "text", "text": "Post office", "bullet": "[ ] ", "size": 2 },
  { "type": "space" },
  { "type": "row", "left": "Eggs", "right": "2 doz", "fill": "." },
  { "type": "qr", "data": "https://example.com/list" }
] }
EOF
```

## Over the network

`thermal serve [--bind 0.0.0.0:8631] [--token T] [--allow ip,ip]` runs the same
operations as an HTTP API with an OpenAPI spec (`/openapi.json`, Swagger UI at
`/docs`). When `THERMAL_URL` is set, call it with curl: `GET /stocks`,
`POST /preview`, `POST /print?force=&dry_run=&copies=&stock=` (the body is a
job, or `{"template": "<name>", "data": {...}}`), with
`Authorization: Bearer <token>` if a token is set. Over HTTP, images must be
`base64`. Setup is CLI-only.
