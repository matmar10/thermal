CARGO ?= $(shell command -v cargo || echo /opt/homebrew/opt/rustup/bin/cargo)
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)
REPO := matmar10/thermal

target/release/thermal: $(wildcard src/*.rs) Cargo.toml openapi.yaml assets/font.ttf $(wildcard models/*.json templates/*.json)
	$(CARGO) build --release

.PHONY: test lint validate checksums dev
test:
	$(CARGO) test --release

lint:
	npx -y @redocly/cli@latest lint openapi.yaml

validate:
	claude plugin validate ./plugin

# After the release workflow has published v$(VERSION): pin its checksums.
checksums:
	@printf '# sha256  asset for v%s, from the release SHA256SUMS (`make checksums`)\n' "$(VERSION)" > plugin/checksums.txt
	gh release download v$(VERSION) --repo $(REPO) --pattern SHA256SUMS --output - >> plugin/checksums.txt
	cat plugin/checksums.txt

# Run Claude with the plugin from this checkout and the locally built binary.
dev: target/release/thermal
	THERMAL_BIN="$(CURDIR)/target/release/thermal" claude --plugin-dir ./plugin
