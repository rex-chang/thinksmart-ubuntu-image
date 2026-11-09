#!/bin/sh
# Run as root after the files of image/system-files.txt are in place (image/device-setup.sh files,
# and every online update of the system layer): the rest of the system layer, all of it safe to run
# again. Anything it edits in place is listed in the table with source '-' so an update backs it up.
#   sh image/system-post.sh REPO_DIR
set -e
cd "${1:-.}"
U=ubuntu
user_systemctl() { runuser -u $U -- env XDG_RUNTIME_DIR=/run/user/$(id -u $U) systemctl --user "$@"; }

# The proximity sensor is read straight from /dev/i2c-0, not through sudo i2cget once a second
usermod -aG i2c $U
# Bluetooth speaker: /etc/bluetooth/main.conf settings (no drop-in directory in BlueZ 5.72)
sh bluetooth/setup-bluez.sh
systemctl mask ModemManager.service avahi-daemon.service avahi-daemon.socket udisks2.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable thinksmart-gpu-watchdog.service screen-wake.service thinksmart-zram.service \
    thinksmart-update-check.timer thinksmart-update-request.path thinksmart-update-boot.service
# reenable: its WantedBy moved from multi-user.target to sysinit.target
systemctl reenable thinksmart-ssh-hostkeys.service
# reenable: thinksmart-mic gained WantedBy=pipewire-pulse.service, so it comes back whenever
# pipewire-pulse restarts; an existing install only has the default.target link
user_systemctl daemon-reload
user_systemctl reenable thinksmart-mic.service idle-blanker.service
# Only welcome.html, keyboard.js and qrcode.js are used here; the rest is from the first chat
# page and py-xiaozhi (vendor logos, an Edge TTS sample)
rm -rf /usr/local/share/thinksmart-kiosk/chat.html /usr/local/share/thinksmart-kiosk/logos \
    /usr/local/share/thinksmart-kiosk/wake.wav
