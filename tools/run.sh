#!/usr/bin/env bash
# Wrapper som pekar ut de lokalt uppackade systembiblioteken.
# Skriptnamnet tolkas både relativt din nuvarande katalog och relativt tools/:
#   ./tools/run.sh mobile-shots.mjs 8940 ./shots efter gw table
#   ./tools/run.sh tools/mobile-shots.mjs 8940 ./shots efter gw table
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$1"; shift
[ -f "$SCRIPT" ] || SCRIPT="$DIR/$(basename "$SCRIPT")"
LD_LIBRARY_PATH="$HOME/.local/lib/playwright-deps:${LD_LIBRARY_PATH:-}" exec node "$SCRIPT" "$@"
