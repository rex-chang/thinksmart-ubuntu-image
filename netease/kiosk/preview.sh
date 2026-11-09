#!/usr/bin/env bash
# Local UI preview of the complete client's web/v2; no client, no device.
# CLIENT_DIR must be the integrated client checkout (the same one netease/kiosk/deploy.sh ships).
set -euo pipefail

if [ -z "${CLIENT_DIR:-}" ]; then
    echo "error: set CLIENT_DIR to the integrated client checkout" >&2
    echo "       e.g. CLIENT_DIR=../xiaozhi-thinksmart-client-integration $0" >&2
    exit 2
fi
CLIENT_DIR=$(cd "$CLIENT_DIR" && pwd) || {
    echo "error: CLIENT_DIR=$CLIENT_DIR is not a directory" >&2
    exit 2
}
WEB=$CLIENT_DIR/web/v2
[ -f "$WEB/index.html" ] || {
    echo "error: $WEB/index.html not found; CLIENT_DIR is not a complete client checkout" >&2
    exit 2
}

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DIR="$ROOT/.tmp/ui-preview"
PORT=${PORT:-8765}
rm -rf "$DIR"
mkdir -p "$DIR/static"
ln -s "$WEB" "$DIR/static/v2"
cp "$WEB/index.html" "$DIR/index.html"
echo "main:   http://127.0.0.1:$PORT/index.html?demo"
echo "player: http://127.0.0.1:$PORT/index.html?demo=music"
cd "$DIR"
exec python3 -m http.server "$PORT"
