#!/bin/bash
# Deploy the home dashboard layer to the device: the /opt/thinksmart-addons venv, the
# system-layer files (home/, intercom/, airplay/, usb/ and the upgraded kiosk/ + screen/),
# and the units. Everything goes through the device updater's system-apply, so each file is
# backed up and only changed services restart.
#
#   ./home/deploy.sh                  venv + home dashboard + upgraded kiosk/screen
#   ./home/deploy.sh --airplay        also enable the AirPlay receiver (needs `uxplay`)
#   ./home/deploy.sh --intercom       also enable 可视对讲 (camera + GStreamer, tunnel keys
#                                     under /etc/thinksmart-intercom, INTERCOM_REMOTE_HOST)
#
# Environment: HOST (default ubuntu@172.16.42.1, the USB cable; HOST=ubuntu@<Wi-Fi IP> when the
#              device isn't on the cable), SSH_OPTS (see ts.sh), PIP_INDEX_URL.
set -euo pipefail

HOST=${HOST:-ubuntu@172.16.42.1}
STAGE=/var/tmp/thinksmart-home-deploy
VENV=/opt/thinksmart-addons/venv
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENABLE_AIRPLAY=0
ENABLE_INTERCOM=0
for arg in "$@"; do
    case "$arg" in
        --airplay) ENABLE_AIRPLAY=1 ;;
        --intercom) ENABLE_INTERCOM=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

read -r -a SSH_OPTS <<< "${SSH_OPTS:-}"
SSH=(ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"})
SCP=(scp ${SSH_OPTS[@]+"${SSH_OPTS[@]}"})

echo "==> staging the system layer on $HOST"
"${SSH[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
"${SCP[@]}" -r "$ROOT/home" "$ROOT/intercom" "$ROOT/airplay" "$ROOT/usb" "$ROOT/netease" \
    "$ROOT/kiosk" "$ROOT/screen" "$ROOT/xiaozhi" "$ROOT/bluetooth" "$ROOT/zram" \
    "$ROOT/update" "$ROOT/image" "$ROOT/VERSION" "$HOST:$STAGE/"

echo "==> /opt/thinksmart-addons venv (aiohttp + lunar_python)"
"${SSH[@]}" "$HOST" "sudo python3 -m venv $VENV && sudo $VENV/bin/pip install --quiet aiohttp lunar_python"

echo "==> home service dependencies (ffmpeg for voice messages)"
"${SSH[@]}" "$HOST" "command -v ffmpeg >/dev/null || sudo apt-get install -y --no-install-recommends ffmpeg >/dev/null"

echo "==> system-apply (files land per image/system-files.txt; changed units restart)"
"${SSH[@]}" "$HOST" "cd $STAGE && sudo /usr/local/sbin/thinksmart-update system-apply ."

echo "==> enabling the home page"
"${SSH[@]}" "$HOST" "sudo systemctl enable --now thinksmart-home.service && sudo systemctl restart thinksmart-home.service && systemctl is-active thinksmart-home.service"

echo "==> enabling the NetEase music app (:8091)"
"${SSH[@]}" "$HOST" "sudo runuser -u ubuntu -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now thinksmart-music.service && sudo runuser -u ubuntu -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart thinksmart-music.service"

if [ "$ENABLE_AIRPLAY" = 1 ]; then
    echo "==> AirPlay (uxplay + its GStreamer decoders + avahi publishing)"
    "${SSH[@]}" "$HOST" "sudo apt-get install -y --no-install-recommends uxplay \
        gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-pulseaudio >/dev/null; \
        sudo sed -i 's/^disable-publishing=yes/disable-publishing=no/' /etc/avahi/avahi-daemon.conf; \
        sudo systemctl unmask avahi-daemon.service avahi-daemon.socket >/dev/null 2>&1 || true; \
        sudo systemctl enable --now avahi-daemon.service; \
        sudo runuser -u ubuntu -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now thinksmart-airplay.service"
fi

if [ "$ENABLE_INTERCOM" = 1 ]; then
    echo "==> 可视对讲 (camera service as root; the tunnel needs /etc/thinksmart-intercom + INTERCOM_REMOTE_HOST)"
    "${SSH[@]}" "$HOST" "sudo systemctl enable --now thinksmart-intercom.service"
    "${SSH[@]}" "$HOST" "if [ -d /etc/thinksmart-intercom ]; then sudo systemctl enable --now thinksmart-intercom-tunnel.service; else echo '  no /etc/thinksmart-intercom keys: tunnel left disabled'; fi"
fi

"${SSH[@]}" "$HOST" "rm -rf $STAGE"
echo
echo "Home page:    http://127.0.0.1:8095/  (kiosk overlay shows it over :8090 after a kiosk restart)"
echo "Music bridge: first run ./netease/deploy.sh so the hub serves /kiosk/netease/music/*"
echo "Weather:      needs plugins.get_weather credentials in the hub's data/.config.yaml"
