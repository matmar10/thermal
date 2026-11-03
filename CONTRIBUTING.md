# Contributing

Thanks for helping. Bug reports, printer compatibility reports and new
printer presets are the most useful contributions.

## Layout

| path | what |
|---|---|
| `src/` | the Rust binary: `job` (format, templates) → `layout` (paper geometry) → `escpos` (bytes) → `transport` (TCP, CUPS, device) |
| `models/` | printer presets, compiled in |
| `templates/` | built-in templates, compiled in |
| `assets/font.ttf` | DejaVu Sans Mono Bold for `big` text, compiled in |
| `plugin/` | the Claude Code plugin bundle: skills, launcher, and the release binaries in `libexec/` with their checksums |
| `.claude-plugin/marketplace.json` | makes this repo a plugin marketplace |
| `docs/assets/` | logo, banner and README imagery (SVG) |

User state lives outside the repo: `~/.config/thermal/printer.json` and
`~/.config/thermal/templates/` (or `$THERMAL_HOME`).

## Develop

```sh
make                # build target/release/print-to-thermal (needs rustup)
make test           # cargo test
make dev            # claude --plugin-dir ./plugin, using the local build
make validate       # claude plugin validate ./plugin
```

## Adding a printer preset

Copy the nearest file in `models/` and add it to `MODELS` in `src/config.rs`.
Fill in `carriage_dots` from the spec sheet (print width in mm × dots per mm),
the cutter (`none`, `partial`, `full`), and whether the printer has native QR
(`GS ( k`). Measure the width with `print-to-thermal calibrate` before trusting it.

## Release

1. Bump `version` in `Cargo.toml` and `plugin/.claude-plugin/plugin.json`. A
   test fails if they differ.
2. Commit, tag `vX.Y.Z` and push the tag. The release workflow builds macOS
   (arm64 and x86_64), Linux (musl, x86_64 and arm64) and Windows binaries,
   and publishes them with `SHA256SUMS`.
3. The workflow's `vendor` job then commits the release binaries to `main` in
   `plugin/libexec/`, checked against the release `SHA256SUMS`, along with
   `plugin/checksums.txt`. Until that commit lands, the plugin still ships the
   previous version's binaries. If the job fails, run `make vendor` locally
   and commit the same two paths.

CI checks on every push that each file in `plugin/libexec/` matches
`plugin/checksums.txt` and that the launcher never downloads anything.
