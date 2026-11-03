CARGO ?= $(shell command -v cargo || echo /opt/homebrew/opt/rustup/bin/cargo)
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -n 1)
REPO := matmar10/thermal

target/release/print-to-thermal: $(wildcard src/*.rs) Cargo.toml openapi.yaml assets/font.ttf $(wildcard models/*.json templates/*.json)
	$(CARGO) build --release

.PHONY: test lint validate vendor dev
test:
	$(CARGO) test --release

lint:
	npx -y @redocly/cli@latest lint openapi.yaml

validate:
	claude plugin validate ./plugin

# After the release workflow has published v$(VERSION): copy its binaries into
# plugin/libexec/, check them against the release SHA256SUMS, and record the sums.
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
