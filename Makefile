CARGO ?= $(shell command -v cargo || echo /opt/homebrew/opt/rustup/bin/cargo)
# rustup's cargo proxy needs rustc on PATH too.
export PATH := $(dir $(CARGO)):$(PATH)
CURRENT := $(shell sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)
VERSION ?= $(CURRENT)
REPO := matmar10/thermal

target/release/print-to-thermal: $(wildcard src/*.rs) Cargo.toml openapi.yaml assets/font.ttf $(wildcard models/*.json templates/*.json)
	$(CARGO) build --release

.PHONY: test lint validate release vendor dev
test:
	$(CARGO) test --release

lint:
	npx -y @redocly/cli@latest lint openapi.yaml

validate:
	claude plugin validate ./plugin

# Cut a release: make release VERSION=X.Y.Z. Bumps Cargo.toml and plugin.json,
# tests, commits and pushes to main. The release workflow then tags vX.Y.Z on
# that commit, builds, publishes and vendors the binaries. Don't tag by hand.
release:
	@[ "$(VERSION)" != "$(CURRENT)" ] || { echo "usage: make release VERSION=X.Y.Z (now $(CURRENT))"; exit 1; }
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION must be X.Y.Z"; exit 1; }
	@[ "$$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "release from main"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "commit or stash your changes first"; exit 1; }
	git pull --ff-only origin main
	sed -i.bak 's/^version = "$(CURRENT)"/version = "$(VERSION)"/' Cargo.toml && rm Cargo.toml.bak
	sed -i.bak 's/"version": "$(CURRENT)"/"version": "$(VERSION)"/' plugin/.claude-plugin/plugin.json && rm plugin/.claude-plugin/plugin.json.bak
	$(CARGO) test --release
	git add Cargo.toml Cargo.lock plugin/.claude-plugin/plugin.json
	git commit -m "Release v$(VERSION)"
	git push origin main

# Fallback if the workflow's vendor job fails: copy the v$(VERSION) release
# binaries into plugin/libexec/, check them against its SHA256SUMS, record the sums.
vendor:
	rm -rf plugin/libexec && mkdir -p plugin/libexec
	gh release download v$(VERSION) --repo $(REPO) --dir plugin/libexec --pattern 'print-to-thermal-*' --pattern SHA256SUMS
	cd plugin/libexec && shasum -a 256 -c SHA256SUMS
	@printf '# sha256  binary in libexec/ for v%s, from the release SHA256SUMS (`make vendor`)\n' "$(VERSION)" > plugin/checksums.txt
	cat plugin/libexec/SHA256SUMS >> plugin/checksums.txt && rm plugin/libexec/SHA256SUMS
	chmod +x plugin/libexec/print-to-thermal-*
	cat plugin/checksums.txt

# Run Claude with the plugin from this checkout and the locally built binary.
dev: target/release/print-to-thermal
	THERMAL_BIN="$(CURDIR)/target/release/print-to-thermal" claude --plugin-dir ./plugin
