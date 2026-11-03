# Thermal

## The program stays small: primitives only

`print-to-thermal` (the Python package in `plugin/thermal/`, launched by
`plugin/bin/print-to-thermal`) exposes **primitives** only: the block types
(`text`, `big`, `rule`, `row`, `space`, `barcode`, `qr`, `image`, `cut`), job
layout and preview, transport to the printer, and printer and roll
configuration. Keep it as small as possible.

Anything about a particular kind of print, such as a grocery list grouped by
store department, a recipe card or a meal plan, belongs in a **skill** under
`plugin/skills/<name>/`. The skill composes the job from those primitives. Do
not add built-in templates, block types or subcommands to the program for a
single use case.

- **Why:** every user's plugin carries the package, and every change to it
  must keep three things true: it stays standard-library only, it still runs
  on Python 3.9 on macOS, Linux and Windows, and it matches the Rust reference
  build byte for byte (`tests/parity.py`). Layout written in a skill's prose
  has none of those costs and changes freely.
- **How to apply:** if a new feature seems to need code, first check whether
  a skill can build it from existing blocks. Change the package only for a
  genuinely new primitive that many prints would use, or for a general bug in
  layout or transport. The existing built-in templates (`shopping-list`,
  `item-label`, `folder-tab`, `note`, `day`) are generic layouts; don't add
  more of them.

## Two implementations, one behaviour

The plugin ships the Python package only: readable source, no compiled
binaries, nothing downloaded. The Rust code in `src/` is the reference it was
ported from. Both read the same data files in `plugin/thermal/` (`models/`,
`templates/`, `openapi.yaml`).

- A change to job format, layout, bytes or messages goes into **both**, and
  `make test` (Python tests, `cargo test`, then `tests/parity.py`) must pass.
- Standard library only in `plugin/thermal/`. Pillow is the one optional
  import (JPEG input); PNG is decoded in `png.py`.
- `big` text is drawn from `plugin/thermal/font.json`, glyph outlines
  extracted from `assets/font.ttf` by `make font`. After editing
  `openapi.yaml`, run `make openapi-json`.
- Every file in `plugin/` stays under 256 KiB, and no compiled files go in
  it (the plugin directory holds anything else for manual review).
