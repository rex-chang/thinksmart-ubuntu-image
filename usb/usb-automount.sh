#!/bin/bash
# Called by thinksmart-usb-mount@.service, outside udev's mount namespace.
set -euo pipefail
ACTION="${1:-}"
DEVNAME="${2:-}"
[[ "$DEVNAME" =~ ^sd[a-z]+[0-9]*$ ]] || exit 2
DEV="/dev/$DEVNAME"
MNT="/mnt/usb"

exec 9>/run/lock/thinksmart-usb.lock
flock -x 9
mkdir -p "$MNT"
CURRENT=$(findmnt -rn -M "$MNT" -o SOURCE | head -n1 || true)

case "$ACTION" in
  add)
    [[ -b "$DEV" ]] || exit 1
    if [[ -n "$CURRENT" ]]; then
      if [[ -b "${CURRENT%%\\*}" ]]; then
        if [[ "$(readlink -m "${CURRENT%%\\*}")" != "$(readlink -m "$DEV")" ]]; then
          echo "[usb] $MNT already mounted from $CURRENT; leaving it in place"
          exit 0
        fi
        if timeout 2 stat "$MNT/." >/dev/null 2>&1; then
          echo "[usb] $DEV already mounted at $MNT; keeping in place"
          exit 0
        fi
      fi
      umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
    fi
    FSTYPE=$(blkid -o value -s TYPE "$DEV" || true)
    OPTIONS="rw,noatime,nodev,nosuid,noexec"
    case "$FSTYPE" in
      vfat) OPTIONS+=",uid=1000,gid=1000,umask=022,utf8" ;;
      exfat|ntfs) OPTIONS+=",uid=1000,gid=1000,umask=022" ;;
      ext2|ext3|ext4) ;;
      *) echo "[usb] Ignoring unsupported filesystem on $DEV: $FSTYPE"; exit 0 ;;
    esac
    if ! mount -t "$FSTYPE" -o "$OPTIONS" "$DEV" "$MNT" 2>/dev/null; then
      mount -t "$FSTYPE" -o "${OPTIONS/#rw,/ro,}" "$DEV" "$MNT"
    fi
    echo "[usb] Mounted $DEV ($FSTYPE) at $MNT"
    ;;
  remove)
    # An unrelated disk/partition removal must never unmount the current drive.
    if [[ -n "$CURRENT" && "$(readlink -m "${CURRENT%%\\*}")" == "$(readlink -m "$DEV")" ]]; then
      umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
      echo "[usb] Unmounted $DEV from $MNT"
    fi
    ;;
  *) exit 2 ;;
esac
