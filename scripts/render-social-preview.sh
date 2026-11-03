#!/bin/sh
# Render docs/assets/hero.svg to the 1280x640 PNG GitHub uses as the
# repository's social preview (Settings -> Social preview). Needs resvg
# (brew install resvg / cargo install resvg).
set -eu
cd "$(dirname "$0")/.."
out=docs/assets/social-preview.png
resvg --width 1280 --height 640 \
  --sans-serif-family "Helvetica Neue" --monospace-family "Menlo" \
  docs/assets/hero.svg "$out"
echo "wrote $out ($(wc -c < "$out" | tr -d ' ') bytes)"
