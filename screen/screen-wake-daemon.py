#!/usr/bin/env python3
import errno
import glob
import json
import math
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

ACCEL_DIR = Path('/sys/bus/iio/devices/iio:device0')
ACCEL_X = ACCEL_DIR / 'in_accel_x_raw'
ACCEL_Y = ACCEL_DIR / 'in_accel_y_raw'
ACCEL_Z = ACCEL_DIR / 'in_accel_z_raw'
TOUCH_EVENT = '/dev/input/event1'
ACCEL_DELTA_THRESHOLD = 40
POLL_SECONDS = 0.2
IDLE_ON_SECONDS = 0.5
WAKE_COOLDOWN_SECONDS = 2.0
# idle-blanker blanks to 10; anything at or below this counts as a blanked screen
BLANKED_BRIGHTNESS_MAX = 10
MAX_BRIGHTNESS = 4095
MIRROR_FLAG_PATH = Path('/run/user/1000/uxplay-mirroring-active')
INTERCOM_FLAG_PATH = Path('/run/user/1000/intercom-call-active')
USER_BRIGHTNESS_PATH = Path('/run/user/1000/user_brightness')
LEGACY_USER_BRIGHTNESS_PATH = Path('/tmp/user_brightness')
HINT_PATH = Path('/run/user/1000/thinksmart-screen.json')
MODE_PATH = Path('/var/lib/thinksmart-monitor/display.json')
SLEEP_DIM_BRIGHTNESS = 1600
PROC_ROOT = Path('/proc')
BACKLIGHT_SYSFS = Path('/sys/class/backlight/backlight/brightness')
DRM_DPMS_PATH = Path('/sys/class/drm/card0-DSI-1/dpms')

last_wake = None
last_accel = None


def log(msg: str) -> None:
    print(f'[screen-wake] {msg}', flush=True)


def read_wake_brightness() -> int:
    hint, mode = {}, {}
    try:
        h = json.loads(HINT_PATH.read_text())
        if isinstance(h, dict):
            hint = h
    except Exception:
        pass
    try:
        m = json.loads(MODE_PATH.read_text())
        if isinstance(m, dict):
            mode = m
    except Exception:
        pass
    b = hint.get('brightness')
    valid_b = type(b) in (int, float) and math.isfinite(b) and 0 <= b <= 100
    anchor_b = b if valid_b else 100
    anchor = mode.get('anchor')
    is_auto = mode.get('auto') is True and type(anchor) in (int, float) and anchor == anchor_b
    val = None
    if not is_auto and valid_b:
        val = 1400 + round((MAX_BRIGHTNESS - 1400) * max(0, min(100, b)) / 100)
    if val is None:
        for p in (USER_BRIGHTNESS_PATH, LEGACY_USER_BRIGHTNESS_PATH):
            try:
                v = int(p.read_text().strip())
                if BLANKED_BRIGHTNESS_MAX < v <= MAX_BRIGHTNESS:
                    val = v
                    break
            except Exception:
                pass
    if val is None:
        val = MAX_BRIGHTNESS
    if hint.get('dim'):
        val = min(SLEEP_DIM_BRIGHTNESS, val)
    return val


def resume_webkit_if_idle() -> None:
    if MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists():
        return
    try:
        entries = os.scandir(PROC_ROOT)
    except Exception:
        return
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if entry.stat(follow_symlinks=False).st_uid != 1000:
                    continue
                with open(os.path.join(entry.path, 'cmdline'), 'rb') as f:
                    argv0 = f.read(512).partition(b'\x00')[0]
                if argv0 != b'WebKitWebProcess' and not argv0.endswith(b'/WebKitWebProcess'):
                    continue
                with open(os.path.join(entry.path, 'stat'), 'r', encoding='utf-8', errors='ignore') as f:
                    state = f.read(256).rsplit(')', 1)[1].split(None, 1)[0]
                if state in ('T', 't'):
                    os.kill(pid, signal.SIGCONT)
            except Exception:
                pass


def write_brightness(val: int) -> bool:
    # Write brightness directly from root -- fast path, no process spawn.
    try:
        if BACKLIGHT_SYSFS.exists():
            BACKLIGHT_SYSFS.write_text(f'{val}\n')
            return True
        wrote = False
        for p in Path('/sys/class/backlight').glob('*/brightness'):
            p.write_text(f'{val}\n')
            wrote = True
        return wrote
    except Exception:
        try:
            r = subprocess.run(
                ['tee', '/sys/class/backlight/backlight/brightness'],
                input=f'{val}\n', text=True,
                capture_output=True, timeout=2
            )
            return r.returncode == 0
        except Exception:
            return False


def drm_state() -> str:
    try:
        return DRM_DPMS_PATH.read_text().strip()
    except Exception:
        return 'unknown'


def read_brightness():
    try:
        return int(BACKLIGHT_SYSFS.read_text().strip())
    except Exception:
        return None


def screen_is_off() -> bool:
    brightness = read_brightness()
    if brightness is not None and brightness <= BLANKED_BRIGHTNESS_MAX:
        return True
    return drm_state() == 'Off'


def wake_display(reason: str) -> bool:
    global last_wake, last_accel
    if not screen_is_off():
        return False
    now = time.monotonic()
    if last_wake is not None and now - last_wake < WAKE_COOLDOWN_SECONDS:
        return False

    resume_webkit_if_idle()
    wake_val = read_wake_brightness()
    ok_bl = write_brightness(wake_val)
    if ok_bl:
        last_wake = now
        last_accel = None
    log(f'wake ({reason}): bl={"ok" if ok_bl else "fail"} ({wake_val}) drm={drm_state()}')
    return ok_bl


def read_accel():
    try:
        x = int(ACCEL_X.read_text().strip())
        y = int(ACCEL_Y.read_text().strip())
        z = int(ACCEL_Z.read_text().strip())
        return (x, y, z)
    except Exception:
        return None


def motion_detected(curr, prev):
    dx = abs(curr[0] - prev[0])
    dy = abs(curr[1] - prev[1])
    dz = abs(curr[2] - prev[2])
    return max(dx, dy, dz) >= ACCEL_DELTA_THRESHOLD, (dx, dy, dz)


def main() -> int:
    if not ACCEL_DIR.exists():
        log('accelerometer path missing')
        return 1

    log('starting as root service (zero I2C poll while screen is on)')

    stop_event = threading.Event()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, lambda *_: stop_event.set())

    touch_fd = None
    last_touch_open = None

    global last_accel
    last_accel = None

    try:
        while not stop_event.is_set():
            if touch_fd is None and (last_touch_open is None or time.monotonic() - last_touch_open >= 5.0):
                last_touch_open = time.monotonic()
                try:
                    touch_fd = os.open(TOUCH_EVENT, os.O_RDONLY | os.O_NONBLOCK)
                    log('touch input opened')
                except Exception as e:
                    log(f'cannot open touch device: {e}')
                    touch_fd = None

            off = screen_is_off()
            had_touch = False
            if touch_fd is not None:
                try:
                    ready, _, _ = select.select([touch_fd], [], [], POLL_SECONDS if off else 0)
                    if ready:
                        got_touch = False
                        while True:
                            try:
                                if not os.read(touch_fd, 4096):
                                    raise OSError(errno.ENODEV, 'touch device EOF')
                                got_touch = True
                            except BlockingIOError:
                                break
                            except OSError as e:
                                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                                    raise
                                break
                        had_touch = True
                        if off and got_touch and wake_display('touch'):
                            continue
                except Exception as e:
                    log(f'touch read error: {e}')
                    try:
                        os.close(touch_fd)
                    except Exception:
                        pass
                    touch_fd = None

            if not off:
                last_accel = None
                stop_event.wait(IDLE_ON_SECONDS)
                continue

            curr = read_accel()
            if curr is not None and last_accel is not None:
                moved, deltas = motion_detected(curr, last_accel)
                if moved:
                    wake_display(f'accel d={deltas}')
            if curr is not None:
                last_accel = curr

            if touch_fd is None or had_touch:
                stop_event.wait(POLL_SECONDS)
    finally:
        if touch_fd is not None:
            try:
                os.close(touch_fd)
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
