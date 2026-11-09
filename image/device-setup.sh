#!/bin/sh
# Turn a running ThinkSmart (the old Plasma-based image plus our services) into the release layout.
# Run on the device as root from a copy of this repository:
#   sudo sh image/device-setup.sh files            system-layer files from this tree (image/system-files.txt:
#                                                  kiosk, screen, mic, zram, Bluetooth speaker, updater)
#   sudo sh image/device-setup.sh update CLIENT    updater; CLIENT (a client tree with .venv) becomes
#                                                  /opt/thinksmart/client/<version>
#   sudo sh image/device-setup.sh kiosk            boot into the kiosk through thinksmart-kiosk.service, not SDDM
#   sudo sh image/device-setup.sh purge [-n]       remove Plasma/KWin/SDDM/Qt and other unused packages
#                                                  (-n: only show what apt would do)
#   sudo sh image/device-setup.sh tidy             persistent journal, time zone, leftovers of earlier experiments
#   sudo sh image/device-setup.sh codec            the DMIC clock fix: codec-dmic/out/ (codec-dmic/build.sh)
#                                                  module into /lib/modules and DTB into /boot (writes the boot
#                                                  partition: ask the owner first), checked against expected.md5
#   sudo sh image/device-setup.sh hold             keep the packages in image/held-packages.txt at their tested
#                                                  versions (apt-mark hold); the kernel isn't a package
#   sudo sh image/device-setup.sh alsa-plugin DEB  PipeWire's ALSA plugin taken out of Ubuntu's pipewire-alsa
#                                                  1.0.5 .deb (apt can't install it next to neon's pipewire 1.2)
# Each step can be run again. Reboot after kiosk, purge and codec and check the screen and sound.
set -e
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
U=ubuntu
H=/home/$U
UCONF=$H/.config/systemd/user

user_systemctl() { runuser -u $U -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user "$@"; }

files() {
    # The system layer: image/system-files.txt says what goes where, image/system-post.sh does the
    # rest (service links, BlueZ settings). The online update installs the same table, so both
    # ways end up with the same files; the version (VERSION) is recorded for the updater.
    python3 update/thinksmart-update system-apply .
}

update() {
    [ -n "$1" ] || { echo "usage: device-setup.sh update CLIENT_DIR"; exit 1; }
    sh update/install.sh "$1"
}

kiosk() {
    install -m 644 kiosk/thinksmart-kiosk.service /etc/systemd/system/
    install -m 644 kiosk/thinksmart-kiosk.pam /etc/pam.d/thinksmart-kiosk
    rm -f /usr/share/wayland-sessions/thinksmart-kiosk.desktop /etc/sddm.conf.d/99-thinksmart-autologin.conf
    systemctl daemon-reload
    systemctl disable sddm.service 2>/dev/null || true
    rm -f /etc/systemd/system/display-manager.service
    systemctl enable thinksmart-kiosk.service
    systemctl set-default graphical.target
    # rotation-daemon only drives kscreen-doctor (Plasma); the kiosk takes ROTATION from its config
    systemctl --global disable rotation-daemon.service 2>/dev/null || true
    rm -f /etc/xdg/systemd/user/rotation-daemon.service /usr/local/bin/rotation-daemon.py
}

# Desktop, toolkits and helpers nothing of ours uses (TODO-remove-desktop.md). The browser is
# WebKitGTK 4.1 on GTK 3 under Weston; audio is PipeWire; fonts and the patched Mesa stay.
PURGE="sddm sddm-theme-* plasma-* kwin-* kwin-common libkwin* kglobalacceld kio-extras kde-* kf6-* libkf5* libkf6*
 qt6-* libqt6* qml6-* qml-module-* breeze* maliit-* qmlkonsole systemsettings xdg-desktop-portal-kde drkonqi
 gnome-terminal* baloo* milou polkit-kde-agent-1 powerdevil* bluedevil kscreen libkscreen* kwayland* layer-shell-qt
 pocketsphinx* speech-dispatcher* fonts-noto-cjk-extra fonts-mplus anthy* libanthy* samba-libs libsmbclient0
 libqt5* qt5-* libllvm18 libclang1-18 libclang-cpp18 modemmanager"
# Kept on purpose even if the purge would orphan them
KEEP="weston gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0 gir1.2-gtk-3.0 python3-gi
 pipewire pipewire-pulse wireplumber libspa-0.2-modules libspa-0.2-bluetooth pulseaudio-utils alsa-utils
 libegl-mesa0 libgbm1 libgl1-mesa-dri libglx-mesa0 mesa-libgallium mesa-va-drivers mesa-vdpau-drivers mesa-vulkan-drivers
 fonts-noto-cjk fonts-noto-core fonts-noto-ui-core fonts-noto-mono fonts-noto-color-emoji fonts-dejavu-core fonts-dejavu-mono
 locales tzdata network-manager wpasupplicant bluez openssh-server sudo git curl ffmpeg
 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav python3-smbus2 python3-dbus i2c-tools"

purge() {
    # set -f: the patterns are for dpkg-query, not for the shell
    installed() ( set -f; dpkg-query -W -f='${Package} ${Status}\n' $1 2>/dev/null | awk '/ installed$/{print $1}' )
    keep=$(installed "$KEEP")
    # shellcheck disable=SC2086
    apt-mark manual $keep >/dev/null
    pkgs=$(installed "$PURGE" | grep -v -x -F "$keep" || true)
    if [ "$1" = -n ]; then
        # shellcheck disable=SC2086
        apt-get -s purge --autoremove $pkgs | grep -E '^(Purg|Remv|Inst)' | sort
        return
    fi
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get -y purge --autoremove $pkgs
    # KDE neon's repository only served the desktop
    rm -f /etc/apt/sources.list.d/plasmon.sources /usr/share/keyrings/kde-neon-archive-keyring.gpg
    apt-get clean
    rm -rf /var/lib/apt/lists/* $H/.cache/plasma* $H/.cache/k* $H/.cache/icon-cache.kcache $H/.local/share/baloo
}

tidy() {
    # Journal on disk so a crash can be read after the reboot, capped for the 8 GB eMMC
    install -d /etc/systemd/journald.conf.d
    printf '[Journal]\nStorage=persistent\nSystemMaxUse=64M\nSystemKeepFree=300M\nMaxRetentionSec=1month\n' \
        > /etc/systemd/journald.conf.d/thinksmart.conf
    install -d -g systemd-journal -m 2755 /var/log/journal
    systemctl restart systemd-journald
    timedatectl set-timezone Asia/Shanghai
    # rsyslog wrote every journal line again into /var/log, outside the journal's cap (169 MB there)
    systemctl disable --now rsyslog.service 2>/dev/null || true
    rm -f /var/log/syslog* /var/log/auth.log* /var/log/kern.log* /var/log/user.log* /var/log/messages*
    echo Asia/Shanghai > /etc/timezone
    # Earlier experiments: py-xiaozhi and the first chat page (replaced by thinksmart-client), Plasma-era helpers
    systemctl disable --now py-xiaozhi.service 2>/dev/null || true
    rm -f /etc/systemd/system/py-xiaozhi.service /usr/local/bin/thinksmart-chat.py /usr/local/bin/screen-wake-daemon.py.july
    user_systemctl disable thinksmart-chat.service accel-wake.service kde-baloo.service 2>/dev/null || true
    rm -f $UCONF/thinksmart-chat.service $UCONF/accel-wake.service $UCONF/kde-baloo.service \
        $UCONF/tas5782m-preinit.service.bak-* $UCONF/plasma-*.service
    rm -rf $UCONF/plasma-core.target.wants $UCONF/plasma-plasmashell.service.d
    rm -rf $H/py-xiaozhi $H/.local/share/py-xiaozhi $H/.local/bin/accel-wake.py $H/.local/bin/wake-daemon.py \
        $H/.local/bin/fix-rotation.sh $H/current
    # The client runs from /opt/thinksmart/client now (update step); the old checkout is unused
    rm -rf $H/thinksmart-client
    rm -rf /etc/sddm.conf /etc/sddm.conf.d /etc/sddm.conf.d.bak /usr/share/sddm /usr/share/plasma
    # a stray file next to ModemManager.service's mask (the purge left it)
    [ -L /etc/systemd/system/ModemManager.service ] && rm -f /etc/systemd/system/ModemManager
    dpkg -l | awk '/^rc/{print $2}' | xargs -r dpkg --purge
    # The screen daemons log to the journal now (their own files grew without bound)
    rm -f $H/.local/share/idle-blanker.log $H/.local/share/proximity-daemon.log $H/.local/share/rotation-daemon.log
    systemctl daemon-reload
}

codec() {
    (cd codec-dmic/out && md5sum -c --quiet ../expected.md5) \
        || { echo "codec-dmic/out/ missing or not the expected build: run codec-dmic/build.sh"; exit 1; }
    mod=/lib/modules/$(uname -r)/kernel/sound/soc/codecs/snd-soc-msm8916-digital.ko
    install -m 644 codec-dmic/out/snd-soc-msm8916-digital.ko "$mod"
    install -m 644 codec-dmic/out/apq8053-lenovo-cd-18781y.dtb /boot/apq8053-lenovo-cd-18781y.dtb
    depmod -a
    sync
    echo "installed; reboot to load them"
}

alsa_plugin() {
    [ -f "${1:-}" ] || { echo "usage: device-setup.sh alsa-plugin pipewire-alsa_1.0.5-*_arm64.deb"; exit 1; }
    t=$(mktemp -d)
    dpkg-deb -x "$1" "$t"
    install -m 644 "$t"/usr/lib/aarch64-linux-gnu/alsa-lib/libasound_module_pcm_pipewire.so \
        "$t"/usr/lib/aarch64-linux-gnu/alsa-lib/libasound_module_ctl_pipewire.so /usr/lib/aarch64-linux-gnu/alsa-lib/
    install -m 644 "$t"/usr/share/alsa/alsa.conf.d/50-pipewire.conf /usr/share/alsa/alsa.conf.d/
    ln -sf /usr/share/alsa/alsa.conf.d/50-pipewire.conf /etc/alsa/conf.d/50-pipewire.conf
    rm -rf "$t"
}

hold() {
    # shellcheck disable=SC2046
    apt-mark hold $(grep -v '^#' image/held-packages.txt) >/dev/null
    apt-mark showhold
}

case "$1" in
    files|kiosk|tidy|codec|hold) "$1" ;;
    update|purge) cmd=$1; shift; "$cmd" "$@" ;;
    alsa-plugin) shift; alsa_plugin "$@" ;;
    *) sed -n '2,18p' "$0"; exit 1 ;;
esac
