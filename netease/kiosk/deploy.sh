#!/usr/bin/env bash
# Copy the patched kiosk client/UI to the device and restart it.
# The client package is versioned: update BASE after a client update.
set -euo pipefail

HOST=${HOST:-ubuntu@10.9.1.60}
BASE=${BASE:-/opt/thinksmart/client/0.1.3}
ROOT=$(cd "$(dirname "$0")" && pwd)
TMP=/tmp/thinksmart-kiosk-ui

ssh "$HOST" "rm -rf $TMP && mkdir -p $TMP"
scp -r "$ROOT/thinksmart_client" "$ROOT/web" "$HOST:$TMP/"

ssh "$HOST" "set -e
cd '$BASE'
ts=\$(date +%F-%H%M)
sudo cp -a thinksmart_client/app.py thinksmart_client/app.py.bak-\$ts
sudo cp -a thinksmart_client/web.py thinksmart_client/web.py.bak-\$ts
sudo tar -C web -cf /tmp/kiosk-web-v2-bak-\$ts.tar v2
sudo cp $TMP/thinksmart_client/app.py thinksmart_client/app.py
sudo cp $TMP/thinksmart_client/web.py thinksmart_client/web.py
sudo cp $TMP/web/v2/index.html web/v2/index.html
sudo cp $TMP/web/v2/app.js web/v2/app.js
sudo cp $TMP/web/v2/app.css web/v2/app.css
sudo python3 -m py_compile thinksmart_client/app.py thinksmart_client/web.py
sudo systemctl restart thinksmart-client.service
sudo systemctl restart thinksmart-kiosk.service
echo 'kiosk ui deployed; client and browser restarted'
"
