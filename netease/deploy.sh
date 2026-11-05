#!/bin/bash
# Deploy the NetEase package to the hub on 10.9.1.60.
# Run from anywhere: ./netease/deploy.sh
set -euo pipefail

HOST=${HOST:-ubuntu@10.9.1.60}
SRV=/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
ROOT=$(cd "$(dirname "$0")/.." && pwd)

ssh "$HOST" "cp -a /home/ubuntu/xiaozhi-hub-server /home/ubuntu/xiaozhi-hub-server.bak-\$(date +%F-%H%M)"

rsync -av "$ROOT/netease/hub/plugins_func/netease/" "$HOST:$SRV/plugins_func/netease/"
rsync -av "$ROOT/netease/hub/plugins_func/functions/play_netease.py" \
    "$HOST:$SRV/plugins_func/functions/"
rsync -av "$ROOT/netease/hub/scripts/netease-login.py" \
    "$ROOT/netease/hub/scripts/netease-selftest.py" "$HOST:$SRV/scripts/"
rsync -av "$ROOT/netease/hub/patch/apply_abort_hook.py" "$HOST:$SRV/scripts/"

ssh "$HOST" "sudo apt-get install -y --no-install-recommends mpv qrencode >/dev/null && mpv --version | head -1 && qrencode --version | head -1"

echo "files copied. next: config.yaml allowlist + plugin block, abort hook, restart hub"
