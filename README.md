# thermal

Compose print jobs from a handful of primitives (text, big lettering, rules,
rows, barcodes, QR codes, images, cuts) and print them on an ESC/POS thermal
printer, from the command line, over HTTP, or through Claude Code.

- **Claude Code plugin:** [`plugin/`](plugin/README.md). Install it with
  `/plugin marketplace add matmar10/thermal`, then
  `/plugin install thermal-printer@thermal`.
- **CLI:** `print-to-thermal help`. Setup is `print-to-thermal discover`, then `print-to-thermal setup`,
  then `print-to-thermal calibrate`.
- **HTTP API:** `print-to-thermal serve`, with an OpenAPI 3.1 spec ([`openapi.yaml`](openapi.yaml)) and Swagger UI at `/docs`.
  [`docs/thermal.plist`](docs/thermal.plist) is an example macOS LaunchAgent.

## Layout

| path | what |
|---|---|
| `src/` | the Rust binary: `job` (format, templates) → `layout` (paper geometry) → `escpos` (bytes) → `transport` (TCP, CUPS, device) |
| `models/` | printer presets, compiled in |
| `templates/` | built-in templates, compiled in |
| `assets/font.ttf` | DejaVu Sans Mono Bold for `big` text, compiled in |
| `plugin/` | the Claude Code plugin bundle: skill, launcher, pinned checksums. Text only. |
| `.claude-plugin/marketplace.json` | makes this repo a plugin marketplace |

User state lives outside the repo: `~/.config/thermal/printer.json` and
`~/.config/thermal/templates/` (or `$THERMAL_HOME`).

## Develop

```sh
make                # build target/release/print-to-thermal (needs rustup)
make test           # cargo test
make dev            # claude --plugin-dir ./plugin, using the local build
make validate       # claude plugin validate ./plugin
```

## Release

1. Bump `version` in `Cargo.toml` and `plugin/.claude-plugin/plugin.json`. A
   test fails if they differ.
2. Commit, tag `vX.Y.Z` and push the tag. The release workflow builds macOS
   (arm64 and x86_64), Linux (musl, x86_64 and arm64) and Windows binaries,
   and publishes them with `SHA256SUMS`.
3. `make checksums`, then commit `plugin/checksums.txt` and push. Until this
   is done, the plugin's launcher refuses to fetch the new binaries.

## Adding a printer preset

Copy the nearest file in `models/` and add it to `MODELS` in `src/config.rs`.
Fill in `carriage_dots` from the spec sheet (print width in mm × dots per mm),
the cutter (`none`, `partial`, `full`), and whether the printer has native QR
(`GS ( k`). Measure the width with `print-to-thermal calibrate` before trusting it.

MIT licensed.
