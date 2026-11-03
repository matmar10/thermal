CARGO ?= $(shell command -v cargo || echo /opt/homebrew/opt/rustup/bin/cargo)
# rustup's cargo proxy needs rustc on PATH too.
export PATH := $(dir $(CARGO)):$(PATH)
PYTHON ?= python3
CURRENT := $(shell sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)
VERSION ?= $(CURRENT)
PKG := plugin/thermal

# The Rust build is the reference implementation the plugin's Python package
# is checked against (tests/parity.py). The plugin itself ships only Python.
target/release/print-to-thermal: $(wildcard src/*.rs) Cargo.toml $(PKG)/openapi.yaml assets/font.ttf $(wildcard $(PKG)/models/*.json $(PKG)/templates/*.json)
	$(CARGO) build --release

.PHONY: test test-python parity lint validate release dev font openapi-json
test: test-python
	$(CARGO) test --release
	$(MAKE) parity

test-python:
	$(PYTHON) -m unittest discover -s tests

# Python vs Rust: bytes, previews, reports and errors for the same jobs.
parity: target/release/print-to-thermal
	$(PYTHON) tests/parity.py target/release/print-to-thermal

lint:
	npx -y @redocly/cli@latest lint $(PKG)/openapi.yaml

validate:
	claude plugin validate ./plugin

# Regenerate the glyph outlines `big` text is drawn from (after changing
# assets/font.ttf or the character set in scripts/gen_font.py).
font:
	$(PYTHON) scripts/gen_font.py

# The server serves openapi.json; the standard library cannot read YAML, so
# it is generated from openapi.yaml after every edit.
openapi-json: target/release/print-to-thermal
	target/release/print-to-thermal openapi --json > $(PKG)/openapi.json

# Cut a release: make release VERSION=X.Y.Z. Bumps Cargo.toml, plugin.json and
# the Python package, tests, commits and pushes to main. The release workflow
# then tags vX.Y.Z on that commit. Don't tag by hand.
release:
	@[ "$(VERSION)" != "$(CURRENT)" ] || { echo "usage: make release VERSION=X.Y.Z (now $(CURRENT))"; exit 1; }
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION must be X.Y.Z"; exit 1; }
	@[ "$$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release from main"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "commit or stash your changes first"; exit 1; }
	git pull --ff-only origin main
	sed -i.bak 's/^version = "$(CURRENT)"/version = "$(VERSION)"/' Cargo.toml && rm Cargo.toml.bak
	sed -i.bak 's/"version": "$(CURRENT)"/"version": "$(VERSION)"/' plugin/.claude-plugin/plugin.json && rm plugin/.claude-plugin/plugin.json.bak
	sed -i.bak 's/^__version__ = "$(CURRENT)"/__version__ = "$(VERSION)"/' $(PKG)/__init__.py && rm $(PKG)/__init__.py.bak
	$(MAKE) test
	git add Cargo.toml Cargo.lock plugin/.claude-plugin/plugin.json $(PKG)/__init__.py
	git commit -m "Release v$(VERSION)"
	git push origin main

# Run Claude with the plugin from this checkout.
dev:
	claude --plugin-dir ./plugin
