# Thermal

## The binary stays small: primitives only

`print-to-thermal` (the Rust code in `src/`) exposes **primitives** only: the
block types (`text`, `big`, `rule`, `row`, `space`, `barcode`, `qr`, `image`,
`cut`), job layout and preview, transport to the printer, and printer and roll
configuration. Keep it as small as possible.

Anything about a particular kind of print, such as a grocery list grouped by
store department, a recipe card or a meal plan, belongs in a **skill** under
`plugin/skills/<name>/`. The skill composes the job from those primitives. Do
not add built-in templates, block types or subcommands to the binary for a
single use case.

- **Why:** each binary change needs a new release, re-vendored binaries
  (`make vendor` into `plugin/libexec/`, about 12 MB per release added to git
  history) and a larger plugin for every user. Layout
  written in a skill's prose can change and ship with only a plugin update.
- **How to apply:** if a new feature seems to need Rust, first check whether
  a skill can build it from existing blocks. Change the binary only for a
  genuinely new primitive that many prints would use, or for a general bug in
  layout or transport. The existing built-in templates (`shopping-list`,
  `item-label`, `folder-tab`, `note`, `day`) are generic layouts; don't add
  more of them.
