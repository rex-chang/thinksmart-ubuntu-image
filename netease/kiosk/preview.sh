#!/usr/bin/env bash
# Local UI preview; no client, no device. Open the printed URL in a desktop browser.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
DIR="$ROOT/../../.tmp/ui-preview"
PORT=${PORT:-8765}
rm -rf "$DIR"
mkdir -p "$DIR/static"
ln -s "$ROOT/web/v2" "$DIR/static/v2"
cp "$ROOT/web/v2/index.html" "$DIR/index.html"
echo "main:   http://127.0.0.1:$PORT/index.html?demo"
echo "player: http://127.0.0.1:$PORT/index.html?demo=music"
cd "$DIR"
python3 -m http.server "$PORT"
