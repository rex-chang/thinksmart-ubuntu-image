#!/bin/bash
# Deploy the NetEase package to the hub on 10.9.1.60.
# Run from anywhere: ./netease/deploy.sh
set -euo pipefail

HOST=${HOST:-ubuntu@10.9.1.60}
SRV=/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
# The Hub runs its own virtualenv; the system python3 has no requests/aiohttp
PYTHON=$SRV/.venv/bin/python3
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# Extra options for ssh/scp (e.g. a dedicated key and known_hosts file: see ts.sh)
read -r -a SSH_OPTS <<< "${SSH_OPTS:-}"
SSH=(ssh "${SSH_OPTS[@]}")
SCP=(scp "${SSH_OPTS[@]}")

# Back up only what this deployment can change: the Hub checkout holds a ~1 GB .venv, and a full
# copy filled the device's 6.4 GB root partition (2026-11-09).
"${SSH[@]}" "$HOST" 'set -e; cd /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server; stamp=$(date +%F-%H%M%S); paths=""; for p in core/admin/admin_handler.py core/handle/textMessageType.py core/handle/textMessageHandlerRegistry.py core/handle/textHandler plugins_func scripts; do [ -e "$p" ] && paths="$paths $p"; done; sudo tar -czf /home/ubuntu/xiaozhi-hub-server.deploy-backup-$stamp.tgz $paths; sudo ls -lh /home/ubuntu/xiaozhi-hub-server.deploy-backup-$stamp.tgz'

"${SSH[@]}" "$HOST" "mkdir -p $SRV/plugins_func/netease"
"${SCP[@]}" -r "$ROOT/netease/hub/plugins_func/netease/." "$HOST:$SRV/plugins_func/netease/"
"${SCP[@]}" "$ROOT/netease/hub/plugins_func/functions/play_netease.py" "$HOST:$SRV/plugins_func/functions/"
"${SCP[@]}" "$ROOT/netease/hub/scripts/netease-login.py" \
    "$ROOT/netease/hub/scripts/netease-selftest.py" "$HOST:$SRV/scripts/"
"${SCP[@]}" "$ROOT/netease/hub/patch/apply_abort_hook.py" \
    "$ROOT/netease/hub/patch/apply_music_handler.py" \
    "$ROOT/netease/hub/patch/apply_netease_login_api.py" "$HOST:$SRV/scripts/"

"${SSH[@]}" "$HOST" "cd $SRV && python3 scripts/apply_music_handler.py . && python3 scripts/apply_netease_login_api.py ."

"${SSH[@]}" "$HOST" "sudo apt-get install -y --no-install-recommends mpv qrencode >/dev/null && mpv --version | head -1 && qrencode --version | head -1"
"${SSH[@]}" "$HOST" "cd $SRV && $PYTHON -m py_compile plugins_func/netease/*.py scripts/netease-*.py core/admin/admin_handler.py"
"${SSH[@]}" "$HOST" "cd $SRV && $PYTHON scripts/netease-selftest.py --login-api"
"${SSH[@]}" "$HOST" "systemctl --user restart xiaozhi-hub.service && systemctl --user is-active --quiet xiaozhi-hub.service"

# This script changes only the Hub. The kiosk UI (QR login card, now-playing bar, Bluetooth
# settings) is part of the complete client release: copying single files over the installed
# /opt/thinksmart/client/current would leave the device running two mixed versions. Deploy a
# complete client instead; it goes through the device updater's self-test and rollback.
echo "NetEase Hub backend deployed; client code was not changed."
echo "Complete client (UI + Bluetooth): CLIENT_DIR=../xiaozhi-thinksmart-client-integration ./netease/kiosk/deploy.sh"
echo "CLIENT_DIR must be a committed checkout whose __version__ is newer than the device's client."
echo "CLI login: ssh $HOST 'cd $SRV && python3 scripts/netease-login.py'"
echo "Kiosk API: GET http://127.0.0.1:16688/kiosk/netease/login/status"
echo "Voice trigger: 登录网易云；card stays in the current conversation."
