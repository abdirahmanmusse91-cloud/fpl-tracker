#!/usr/bin/env bash
# Installerar verktygen. Kräver ingen root.
set -euo pipefail
cd "$(dirname "$0")"

npm install

# Chromium hamnar i ~/.cache/ms-playwright och ligger kvar mellan sessioner
npx playwright install chromium

# Chromium behöver fyra systembibliotek som inte finns i WSL-imagen. apt-get
# download kräver ingen root, så vi packar upp dem i hemkatalogen i stället för
# att installera dem systemvitt.
DEPS="$HOME/.local/lib/playwright-deps"
if [ ! -f "$DEPS/libnss3.so" ]; then
  echo "Hämtar systembibliotek…"
  TMP=$(mktemp -d)
  (cd "$TMP" && apt-get download libnss3 libnspr4 libasound2t64 && for f in *.deb; do dpkg -x "$f" root; done)
  mkdir -p "$DEPS"
  cp -r "$TMP"/root/usr/lib/x86_64-linux-gnu/* "$DEPS/"
  rm -rf "$TMP"
fi

# Emoji-font, annars renderas medaljerna i ligatabellen som tomma rutor
if ! fc-list | grep -qi emoji; then
  echo "Hämtar emoji-font…"
  TMP=$(mktemp -d)
  (cd "$TMP" && apt-get download fonts-noto-color-emoji && dpkg -x fonts-noto-color-emoji_*.deb root)
  mkdir -p "$HOME/.local/share/fonts"
  cp "$TMP"/root/usr/share/fonts/truetype/noto/*.ttf "$HOME/.local/share/fonts/"
  fc-cache -f >/dev/null
  rm -rf "$TMP"
fi

echo "Klart. Kör verktygen med ./run.sh <skript.mjs> [argument]"
