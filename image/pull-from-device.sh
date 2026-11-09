#!/bin/bash
# Capture the root and boot filesystems of a prepared device (image/device-setup.sh) as tarballs:
#   TS_HOST=<device IP> image/pull-from-device.sh WORK_DIR
# The hub and the client are stopped while the files are read and started again afterwards.
# The capture still holds the owner's keys and logs: keep WORK_DIR out of any repository;
# build-image.sh sanitizes a copy inside the container.
set -euo pipefail
OUT=${1:?work dir}
: "${TS_HOST:?set TS_HOST=<device IP>}"
TS="$(cd "$(dirname "$0")/.." && pwd)/ts.sh"
mkdir -p "$OUT"; chmod 700 "$OUT"

"$TS" 'systemctl --user stop xiaozhi-hub; sudo systemctl stop thinksmart-client; sync'
trap '"$TS" "sudo systemctl start thinksmart-client; systemctl --user start xiaozhi-hub"' EXIT

"$TS" 'cd /boot && sudo tar --numeric-owner -cpf - --exclude="./*.orig" --exclude=./lost+found . | zstd -3 -T4 -q' \
    > "$OUT/boot.tar.zst"
# --one-file-system: /boot, /proc, /run and the firmware mounts are separate filesystems
"$TS" 'cd / && sudo tar --numeric-owner --xattrs --xattrs-include="*" --acls --one-file-system -cpf - \
        --exclude=./tmp/* --exclude=./var/tmp/* --exclude=./var/lib/apt/lists/* \
        --exclude=./var/cache/apt/archives/*.deb --exclude=./var/log/journal/* \
        --exclude=./home/ubuntu/.cache --exclude=./lost+found . 2>/dev/null | zstd -3 -T4 -q' \
    > "$OUT/root.tar.zst"
# hub commit, client version, then the system layer's version and file-set id (system-verify fails if
# an installed system file was edited by hand since: the image would differ from the release)
"$TS" 'git -C ~/xiaozhi-hub-server rev-parse HEAD; readlink /opt/thinksmart/client/current;
       sudo /usr/local/sbin/thinksmart-update system-verify' > "$OUT/versions.txt"
ls -l "$OUT"
