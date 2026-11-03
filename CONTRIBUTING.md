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

1. On a clean `main`, run `make release VERSION=X.Y.Z`. It bumps `Cargo.toml`
   and `plugin/.claude-plugin/plugin.json`, runs the tests, commits
   `Release vX.Y.Z` and pushes. Don't push a tag yourself.
2. The release workflow sees a version on `main` with no release yet. It
   builds macOS (arm64 and x86_64), Linux (musl, x86_64 and arm64) and Windows
   binaries, then publishes them with `SHA256SUMS`, creating the tag
   `vX.Y.Z` on the commit it built.
3. Its `vendor` job commits the binaries to `main` in `plugin/libexec/`,
   checked against `SHA256SUMS`, with `plugin/checksums.txt`. Until that
   commit lands, the plugin still ships the previous version's binaries. If
   the job fails, run `make vendor` and commit those two paths.

If a release run fails, fix it and re-run it from the Actions tab (it can
also be started by hand there).

CI checks on every push that each file in `plugin/libexec/` matches
`plugin/checksums.txt` and that the launcher never downloads anything.
