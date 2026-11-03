# Contributing

Thanks for helping. Bug reports, printer compatibility reports and new
printer presets are the most useful contributions.

## Layout

| path | what |
|---|---|
| `plugin/thermal/` | the program the plugin runs, Python standard library only: `job` (format, templates) → `layout` (paper geometry) → `escpos` (bytes) → `transport` (TCP, CUPS, Windows spooler, device) |
| `plugin/thermal/models/` | printer presets |
| `plugin/thermal/templates/` | built-in templates |
| `plugin/thermal/openapi.yaml` | the HTTP API; `openapi.json` is generated from it (`make openapi-json`) |
| `plugin/thermal/font.json` | glyph outlines for `big` text, generated from `assets/font.ttf` (`make font`) |
| `plugin/` | the Claude Code plugin bundle: skills, launchers in `bin/`, and the package above |
| `src/` | the Rust reference implementation the package is checked against |
| `tests/` | `test_thermal.py` (the package), `thermal.rs` (Rust), `parity.py` (the two compared) |
| `.claude-plugin/marketplace.json` | makes this repo a plugin marketplace |
| `docs/assets/` | logo, banner and README imagery (SVG) |

User state lives outside the repo: `~/.config/thermal/printer.json` and
`~/.config/thermal/templates/` (or `$THERMAL_HOME`).

## Develop

```sh
make test-python    # the package's tests; needs only Python 3.9+
make test           # those, cargo test, and tests/parity.py (needs rustup)
make dev            # claude --plugin-dir ./plugin
make validate       # claude plugin validate ./plugin
```

Run the program from the checkout with `plugin/bin/print-to-thermal`.

The package must stay standard-library only and run on Python 3.9; CI runs
the tests on 3.9 and 3.13 on macOS, Linux and Windows. A change to the job
format, layout, bytes or messages goes into both the Python package and
`src/`, and `tests/parity.py` must still pass.

## Adding a printer preset

Copy the nearest file in `plugin/thermal/models/` and add its name to `MODELS`
in both `plugin/thermal/config.py` and `src/config.rs`. Fill in
`carriage_dots` from the spec sheet (print width in mm × dots per mm), the
cutter (`none`, `partial`, `full`), and whether the printer has native QR
(`GS ( k`). Measure the width with `print-to-thermal calibrate` before trusting it.

## Release

1. On a clean `main`, run `make release VERSION=X.Y.Z`. It bumps `Cargo.toml`,
   `plugin/.claude-plugin/plugin.json` and `plugin/thermal/__init__.py`, runs
   the tests, commits `Release vX.Y.Z` and pushes. Don't push a tag yourself.
2. The release workflow sees a version on `main` with no release yet, checks
   the three versions agree, runs the tests and publishes release notes,
   creating the tag `vX.Y.Z` on that commit. There is nothing to build: the
   plugin ships its source.

If a release run fails, fix it and re-run it from the Actions tab (it can
also be started by hand there).

CI checks on every push that the launcher never downloads anything and that
no compiled files are in `plugin/`.
