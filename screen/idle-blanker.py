#!/usr/bin/env python3
"""
Input-idle screen blanker + ALS auto-brightness for CD-18781Y.
- Blanks after IDLE_TIMEOUT seconds of no input (writes brightness=BLANK_BRIGHTNESS)
- Suspends WebKitWebProcess CPU rendering while the screen is blanked (dropping idle CPU
  from ~90% to ~0%) and immediately resumes it on any wake event (touch, motion, proximity,
  voice wake word, AirPlay mirroring, Intercom video call, or service shutdown).
- While screen is on: adjusts brightness from ALS every ALS_INTERVAL seconds
- 2026-11-12: lit in the night (00:00-06:30) or on the sleep night view -> the lowest level; 温柔叫醒: from the
  screen's own level up to 50%, back to normal afterwards; a touch stops it (and the birds)
"""

import errno
import os
import sys
import time
import glob
import math
import select
import signal
import logging
import subprocess
import json
import statistics
from pathlib import Path
from thinksmart_display import MODE_PATH, PREVIEW_SOCKET, automatic_for, read_json, ambient, BrightnessPreview

IDLE_TIMEOUT     = 1800     # seconds before blanking
POLL_INTERVAL    = 0.2      # a wake word must light the screen before the chime ends
RUNTIME_DIR      = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
HINT_PATH        = RUNTIME_DIR / "thinksmart-screen.json"
MIRROR_FLAG_PATH = RUNTIME_DIR / "uxplay-mirroring-active"
INTERCOM_FLAG_PATH = RUNTIME_DIR / "intercom-call-active"
USER_BRIGHTNESS_PATH = RUNTIME_DIR / "user_brightness"
LEGACY_USER_BRIGHTNESS_PATH = Path("/tmp/user_brightness")
BACKLIGHT_PATH   = "/sys/class/backlight/backlight/brightness"
MAX_BRIGHTNESS   = 4095
MIN_BRIGHTNESS   = 2000     # readable automatic floor; WLED goes dark below ~1200
BLANK_BRIGHTNESS = 10
DIM_BRIGHTNESS   = 1600     # sleep hours (WLED goes dark below ~1200)
MANUAL_MIN       = 1400     # the user's 0% (hint "brightness"); thinksmart-client mirrors it
NIGHT_FROM, NIGHT_TO = (0, 0), (6, 30)   # Product spec 11-12: the night, every day
NIGHT_LEVEL      = MANUAL_MIN   # a screen lit in the night / on the sleep night view starts here
NIGHT_VIEW_PATH  = RUNTIME_DIR / "thinksmart-night-view"     # {"until": epoch}: the music service's night view is up
SUNRISE_PATH     = RUNTIME_DIR / "thinksmart-sunrise.json"   # {"start", "ramp", "hold"}: 温柔叫醒
FLAG_RECHECK     = 1.0      # seconds between looks at those two files
NIGHT_TEST       = False    # the regression suites can switch the night rules on with their own paths
ALS_PATH         = "/tmp/als_lux"
ALS_INTERVAL     = 3.0     # seconds between ALS adjustments
ALS_WAKE_COOL    = 30.0     # seconds after wake before ALS kicks in
RENDER_RECHECK   = 2.0      # catch a renderer replaced while the screen is blank
PROC_ROOT       = Path("/proc")
ALS_DEADBAND     = 65      # only adjust if target differs by this much
# Automatic brightness as (sensor lux, panel %) anchors, interpolated on a log scale; the panel %
# maps to the backlight exactly like the user's manual level (0% = 1400 ... 100% = 4095).
# The VCNL4200 sits behind the dark bezel and reads far below true room light: on 2026-11-04
# 15:17 ordinary daytime indoor light read ~2.4 lux and the user said 100% felt right there;
# on 2026-11-05 the user lowered the dark-room floor to the manual slider's 0%.
AUTO_CURVE       = ((0.1, 0), (0.3, 40), (0.8, 65), (1.5, 85), (2.0, 100))
_AUTO_CURVE_LOG  = tuple(
    (x0, y0, x1, y1, math.log10(x0), math.log10(x1) - math.log10(x0))
    for (x0, y0), (x1, y1) in zip(AUTO_CURVE, AUTO_CURVE[1:])
)
ALS_STEP_UP      = 500     # brighten fast when the lights come on
ALS_STEP_DOWN    = 160     # dim gently when it gets dark
ALS_HOLD         = 60      # smoothed-target changes smaller than this (~2%) are ignored
ALS_SNAP         = 150     # within this distance, go straight to the target
ALS_MEDIAN       = 3       # a change must last through this many readings (3 s apart)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def read_user_brightness() -> int:
    for p in (USER_BRIGHTNESS_PATH, LEGACY_USER_BRIGHTNESS_PATH):
        try:
            val = int(p.read_text().strip())
            if BLANK_BRIGHTNESS < val <= MAX_BRIGHTNESS:
                return val
        except Exception:
            pass
    return MAX_BRIGHTNESS


def write_user_brightness(val: int):
    if USER_BRIGHTNESS_PATH == RUNTIME_DIR / "user_brightness" and HINT_PATH.parent != RUNTIME_DIR:
        return
    try:
        tmp = USER_BRIGHTNESS_PATH.with_suffix(".tmp")
        tmp.write_text(f"{int(val)}\n")
        tmp.replace(USER_BRIGHTNESS_PATH)
    except Exception:
        pass


def set_blank_render_paused(paused: bool):
    """Pause WebKitWebProcess when screen is dark; resume when waking (unless AirPlay/Intercom owns screen)."""
    if not paused and (MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists()):
        return
    sig = signal.SIGSTOP if paused else signal.SIGCONT
    my_pid = os.getpid()
    my_uid = os.getuid()
    signaled = []
    try:
        it = os.scandir(PROC_ROOT)
    except OSError:
        return
    with it:
        for entry in it:
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if pid == my_pid or entry.stat(follow_symlinks=False).st_uid != my_uid:
                    continue
                with open(os.path.join(entry.path, "cmdline"), "rb") as f:
                    argv0 = f.read(512).partition(b"\x00")[0]
                if argv0 != b"WebKitWebProcess" and not argv0.endswith(b"/WebKitWebProcess"):
                    continue
                with open(os.path.join(entry.path, "stat"), "r", encoding="utf-8", errors="ignore") as f:
                    state = f.read(256).rsplit(")", 1)[1].split(None, 1)[0]
                # No repeated signals/logs for renderers already in the desired state.
                if paused and state in ("T", "t", "Z", "X"):
                    continue
                if not paused and state not in ("T", "t"):
                    continue
                os.kill(pid, sig)
                signaled.append(pid)
            except (OSError, ValueError, IndexError):
                pass
    if signaled:
        logger.info(f"WebKitWebProcess {signaled} -> {'PAUSED (blank)' if paused else 'RESUMED (awake)'}")


def _night_rules():
    return HINT_PATH.parent == RUNTIME_DIR or NIGHT_TEST


def in_night(now=None):
    """00:00-06:30 (NIGHT_FROM-NIGHT_TO), every day."""
    if not _night_rules():
        return False
    lt = time.localtime(time.time() if now is None else now)
    hm = (lt.tm_hour, lt.tm_min)
    return NIGHT_FROM <= hm < NIGHT_TO if NIGHT_FROM <= NIGHT_TO else (hm >= NIGHT_FROM or hm < NIGHT_TO)


def _read_flag_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _num(v):
    return type(v) in (int, float) and math.isfinite(v)


def read_night_view(now=None):
    """The sleep page's night view is up (an old flag past its "until" counts as gone)."""
    if not _night_rules():
        return False
    data = _read_flag_json(NIGHT_VIEW_PATH)
    until = data.get("until") if data else None
    return _num(until) and (time.time() if now is None else now) < until


def read_sunrise(now=None):
    """温柔叫醒 while it is due: {"start", "ramp", "hold", "max", "stopped"} (epoch seconds / seconds / panel %), else None."""
    if not _night_rules():
        return None
    data = _read_flag_json(SUNRISE_PATH)
    if not data:
        return None
    start, ramp, hold = data.get("start"), data.get("ramp"), data.get("hold")
    if not (_num(start) and _num(ramp) and _num(hold)) or not (0 < ramp <= 3600 and 0 <= hold <= 7200):
        return None
    mx = data.get("max")
    mx = float(mx) if _num(mx) and 0 <= mx <= 100 else 50.0
    now = time.time() if now is None else now
    if not (start <= now < start + ramp + hold):
        return None
    return {"start": float(start), "ramp": float(ramp), "hold": float(hold), "max": mx, "stopped": "stopped" in data}


def sunrise_mark_stopped(start):
    """A touch: the music service stops the birds when it sees this."""
    data = _read_flag_json(SUNRISE_PATH)
    if not data or data.get("start") != start or "stopped" in data:
        return
    data["stopped"] = int(time.time())
    try:
        tmp = SUNRISE_PATH.with_name("." + SUNRISE_PATH.name + ".blk")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, SUNRISE_PATH)
    except OSError:
        pass


def sunrise_level(sr, now=None):
    """From the screen's own level ("from") up to "to" over the ramp, slow at first like the dawn."""
    now = time.time() if now is None else now
    x = max(0.0, min(1.0, (now - sr["start"]) / sr["ramp"]))
    return round(sr["from"] + (sr["to"] - sr["from"]) * x * x)


def lux_to_level(lux):
    """Panel percentage for a sensor reading (AUTO_CURVE, log-interpolated)."""
    lux = max(0.0, float(lux))
    if lux <= AUTO_CURVE[0][0]:
        return float(AUTO_CURVE[0][1])
    if lux >= AUTO_CURVE[-1][0]:
        return float(AUTO_CURVE[-1][1])
    for x0, y0, x1, y1, log_x0, log_span in _AUTO_CURVE_LOG:
        if x0 <= lux <= x1:
            t = (math.log10(lux) - log_x0) / log_span
            return y0 + (y1 - y0) * t
    return float(AUTO_CURVE[-1][1])


def lux_to_brightness(lux):
    return manual_brightness(lux_to_level(lux))


def next_brightness(current, target):
    """One adjustment step toward target: fast up, gentle down, nothing inside the deadband."""
    diff = target - current
    if abs(diff) < ALS_HOLD:
        return current
    if abs(diff) <= ALS_SNAP:
        return target
    step = min(ALS_STEP_UP, diff * 0.5) if diff > 0 else max(-ALS_STEP_DOWN, diff * 0.4)
    return round(current + step)


class AmbientFilter:
    """Median of the last ALS_MEDIAN readings (a passing shadow or flash is ignored), then a
    log-domain average that follows brighter light quickly and darker light slowly."""
    def __init__(self):
        self.samples, self.value, self.at = [], None, None

    def add(self, lux, now):
        if self.at is None or now - self.at > 30:     # stale: was manual / screen off; start over
            self.samples, self.value = [], None
        self.at = now
        self.samples = (self.samples + [max(0.0, float(lux))])[-ALS_MEDIAN:]
        m = math.log10(statistics.median(self.samples) + 0.02)
        if self.value is None:
            self.value = m
        else:
            self.value += (0.6 if m > self.value else 0.3) * (m - self.value)
        return max(0.0, 10 ** self.value - 0.02)


def manual_brightness(percent):
    """The user's 0-100 as a backlight level, or None (automatic) for anything else."""
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
        return None
    return MANUAL_MIN + round((MAX_BRIGHTNESS - MANUAL_MIN) * max(0, min(100, percent)) / 100)


def read_als():
    return ambient()


def get_brightness():
    try:
        with open(BACKLIGHT_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return None


def set_brightness(val):
    if BACKLIGHT_PATH == "/sys/class/backlight/backlight/brightness" and HINT_PATH.parent != RUNTIME_DIR:
        return True
    try:
        Path(BACKLIGHT_PATH).write_text(str(val) + "\n")
        return True
    except PermissionError:
        r = subprocess.run(["sudo", "tee", BACKLIGHT_PATH],
                           input=str(val) + "\n", text=True,
                           capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception as e:
        logger.warning(f"set_brightness({val}) failed: {e}")
        return False


def read_hint(last_sig):
    """The voice client's hint, if it changed since last_sig: (sig, hint dict or None)."""
    try:
        st = HINT_PATH.stat()
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None, None
    except OSError:
        return last_sig, None
    if sig == last_sig:
        return last_sig, None
    try:
        data = json.loads(HINT_PATH.read_text())
        return sig, (data if isinstance(data, dict) else None)
    except Exception:
        return sig, None


def read_mode(last_sig, last_mode):
    """Cached read of MODE_PATH; avoids opening/parsing eMMC JSON on every 200ms poll tick."""
    try:
        st = MODE_PATH.stat()
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None, {}
    except OSError:
        return last_sig, last_mode
    if sig == last_sig:
        return last_sig, last_mode
    return sig, read_json(MODE_PATH)


def open_input_devices():
    fds = []
    for path in glob.glob("/dev/input/event*"):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fds.append(fd)
        except Exception:
            pass
    return fds


def drain(fds, timeout=0, preview=None):
    wake_fd = preview.channel.fileno() if (preview is not None and preview.channel is not None) else None
    watched = fds if wake_fd is None else fds + [wake_fd]
    if not watched:
        time.sleep(timeout)
        return False
    ready, _, _ = select.select(watched, [], [], timeout)
    had_ev = False
    drain.dev_event = False
    for fd in ready:
        if fd == wake_fd:
            preview.receive()
            had_ev = True
        else:
            while True:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        if fd in fds:
                            fds.remove(fd)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        break
                    had_ev = True
                    drain.dev_event = True
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                        if fd in fds:
                            fds.remove(fd)
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    break
    return had_ev


def _run(preview=None):
    logger.info(f"Idle blanker started -- timeout={IDLE_TIMEOUT}s, ALS interval={ALS_INTERVAL}s")
    fds = open_input_devices()
    logger.info(f"Watching {len(fds)} input device(s)")
    try:
        _run_loop(fds, preview)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _run_loop(fds, preview=None):

    cur_init_bl = get_brightness()
    last_input    = time.monotonic()
    last_render_check = None
    last_logged_als = None
    last_als      = None
    last_wake     = None
    blanked       = bool(cur_init_bl is not None and cur_init_bl <= BLANK_BRIGHTNESS)
    target_brightness = read_user_brightness()
    last_written_ub = None
    als_filter    = AmbientFilter()
    hint_mtime    = None
    hint_seq      = None
    mode_sig      = None
    mode_data     = {}
    hold          = False          # a voice conversation is going: never blank
    dim           = False          # the voice client's sleep hours
    preview_level = None
    client_level  = 100
    manual        = None           # the user's backlight level (hint "brightness"), None: auto
    idle_timeout  = IDLE_TIMEOUT   # the user's screen-off (hint "idle"), 0: never
    timeout       = IDLE_TIMEOUT   # shortened after a conversation, back to normal on touch
    night_view    = read_night_view()   # the sleep page's night view is up
    night_hold    = False          # lit in the night / on the night view: the lowest level until someone sets one
    sunrise       = None           # 温柔叫醒 in progress
    sunrise_seen  = None           # start of the last one handled
    sunrise_lvl   = None
    last_flags    = None
    back          = None           # after the wake-up: a short fade back to the user's own level

    if blanked and not (MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists()):
        set_blank_render_paused(True)
    elif not blanked:
        set_blank_render_paused(False)

    def awake_brightness():
        return NIGHT_LEVEL if night_hold else normal_brightness()

    def normal_brightness():
        if manual is not None:
            level = manual
        else:
            level = read_user_brightness()
        return min(DIM_BRIGHTNESS, level) if dim and preview_level is None else level

    def handle_signal(sig, frame):
        logger.info("Signal -- restoring brightness and exiting")
        set_blank_render_paused(False)
        if blanked:
            set_brightness(awake_brightness())
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    switched_to_auto = False

    def wake(why):
        nonlocal blanked, last_wake, target_brightness, night_hold
        night_hold = bool(night_view or in_night())
        logger.info(f"{why} -- restoring brightness" + (" (night: lowest)" if night_hold else ""))
        last_wake = None if switched_to_auto else time.monotonic()
        set_blank_render_paused(False)
        awake_bl = awake_brightness()
        set_brightness(awake_bl)
        target_brightness = awake_bl
        blanked = False

    while True:
        switched_to_auto = False
        had_preview = preview_level is not None
        had_input = drain(fds, POLL_INTERVAL, preview)
        preview_level = preview.current() if preview is not None else None

        mode_sig, mode_data = read_mode(mode_sig, mode_data)
        hint_mtime, hint = read_hint(hint_mtime)
        if hint is not None:
            b = hint.get("brightness", 100)
            client_level = b if (not isinstance(b, bool) and isinstance(b, (int, float)) and math.isfinite(b) and 0 <= b <= 100) else 100
            new_dim = bool(hint.get("dim"))
            new_manual = (manual_brightness(preview_level) if preview_level is not None else
                          None if automatic_for({'brightness': client_level}, mode_data) else manual_brightness(client_level))
            if (new_dim, new_manual) != (dim, manual):
                night_hold = False
                dim_changed = new_dim != dim
                dim, manual = new_dim, new_manual
                logger.info(f"Voice client: dim={dim} brightness={manual or 'auto'}")
                if manual is None:
                    last_als = None    # back to auto: follow the light sensor right away
                    last_wake = None
                    switched_to_auto = True
                if not blanked and (manual is not None or (dim_changed and (dim or read_als() is None))):
                    set_brightness(awake_brightness())
            new_idle = hint.get("idle", IDLE_TIMEOUT)
            if isinstance(new_idle, bool) or not isinstance(new_idle, int) or new_idle < 0:
                new_idle = IDLE_TIMEOUT
            if new_idle != idle_timeout:
                idle_timeout = timeout = new_idle
                logger.info(f"Voice client: screen-off after {idle_timeout}s" if idle_timeout else "Voice client: screen-off never")
            if hint.get("seq") != hint_seq:
                hint_seq = hint.get("seq")
                last_input = time.monotonic()
                hold = bool(hint.get("hold"))
                after = hint.get("after")
                timeout = (idle_timeout if hold or not idle_timeout or isinstance(after, bool)
                           or not isinstance(after, int) or after <= 0 else after)
                logger.info(f"Voice client: hold={hold} timeout={timeout}s")
                if blanked:
                    wake("Voice client")

        # The separate brightness panel can change mode without touching voice hints.
        auto_mode = automatic_for({'brightness': client_level}, mode_data)
        desired_manual = (manual_brightness(preview_level) if preview_level is not None else
                          None if auto_mode else manual_brightness(client_level))
        if desired_manual != manual or (had_preview and preview_level is None) or (dim and not had_preview and preview_level is not None):
            night_hold = False
            manual = desired_manual
            last_als = None
            if manual is None:
                last_wake = None
                switched_to_auto = True
            if (manual is not None or had_preview or dim) and not blanked:
                set_brightness(awake_brightness())

        now = time.monotonic()
        if last_flags is None or now - last_flags >= FLAG_RECHECK:
            last_flags = now
            nv = read_night_view()
            if nv != night_view:
                night_view = nv
                logger.info("Night view " + ("up: lowest brightness" if nv else "gone"))
                if not blanked and sunrise is None:
                    night_hold = bool(nv or (night_hold and in_night()))
                    target_brightness = awake_brightness()
                    set_brightness(target_brightness)
            sr = read_sunrise()
            if sr is not None and sr["start"] != sunrise_seen and not sr["stopped"]:
                cur = get_brightness()
                start_lvl = cur if (not blanked and cur is not None and cur > BLANK_BRIGHTNESS) else normal_brightness()
                sr["from"] = start_lvl
                sr["to"] = max(start_lvl, manual_brightness(sr["max"]))
                sunrise_seen, sunrise, sunrise_lvl, back = sr["start"], sr, None, None
                logger.info(f"Sunrise (温柔叫醒): {start_lvl} -> {sr['to']} over {int(sr['ramp'])}s")
                night_hold = False
                if blanked:   # wake at the screen's own level, then brighten
                    set_blank_render_paused(False)
                    sunrise_lvl = target_brightness = start_lvl
                    set_brightness(sunrise_lvl)
                    blanked = False
                    last_wake = now
                last_input = now
                timeout = idle_timeout
            elif sunrise is not None and sr is not None and sr["start"] == sunrise["start"] and sr["stopped"]:
                sunrise["stopped"] = True   # the music service ended it (the switch, music, an alarm)
        if had_input:
            last_input = now
            timeout = idle_timeout
            if blanked:
                wake("Input detected")
                now = time.monotonic()
        if sunrise is not None:
            wall = time.time()
            touched = had_input and sunrise_lvl is not None and getattr(drain, "dev_event", True)
            if touched or sunrise.get("stopped") or wall >= sunrise["start"] + sunrise["ramp"] + sunrise["hold"]:
                if touched:
                    sunrise_mark_stopped(sunrise["start"])   # the birds stop too
                cur = get_brightness()
                if manual is not None and cur is not None and cur > BLANK_BRIGHTNESS and not blanked:
                    back = {"from": cur, "to": normal_brightness(), "at": now}   # the user's own level again
                elif cur is not None and cur > BLANK_BRIGHTNESS:
                    target_brightness = cur   # automatic: the light sensor takes over after its usual pause
                last_wake = now
                logger.info("Sunrise over (" + ("touch" if touched else ("stopped" if sunrise.get("stopped") else "time")) + ")")
                sunrise = None
            elif not blanked:
                lvl = sunrise_level(sunrise, wall)
                if sunrise_lvl is None or abs(lvl - sunrise_lvl) >= 12:
                    set_brightness(lvl)
                    sunrise_lvl = lvl
                last_input = now   # no idle blank while it brightens

        idle_secs  = now - last_input
        brightness = get_brightness()

        # External restore (e.g. proximity / motion daemon)
        if blanked and brightness is not None and brightness > BLANK_BRIGHTNESS:
            logger.info("Screen restored externally -- resetting idle timer")
            set_blank_render_paused(False)
            now        = time.monotonic()
            last_input = now
            last_wake  = None if switched_to_auto else now
            timeout    = idle_timeout
            night_hold = bool(night_view or in_night())
            awake_bl   = awake_brightness()
            set_brightness(awake_bl)
            target_brightness = awake_bl
            brightness = awake_bl
            idle_secs = 0
            blanked   = False

        if back is not None:
            if blanked or sunrise is not None:
                back = None
            else:
                x = min(1.0, (now - back["at"]) / 3.0)
                lvl = round(back["from"] + (back["to"] - back["from"]) * x)
                set_brightness(lvl)
                target_brightness = lvl
                if x >= 1.0:
                    back = None

        overlay_active = MIRROR_FLAG_PATH.exists() or INTERCOM_FLAG_PATH.exists()
        if overlay_active:
            last_input = now
            timeout    = idle_timeout
            idle_secs = 0
            if blanked:
                wake("AirPlay/Intercom Active")
                brightness = target_brightness
                now = time.monotonic()

        if not blanked and not hold and not overlay_active and brightness is not None and brightness <= BLANK_BRIGHTNESS:
            logger.info("Screen blanked externally -- pausing renderer")
            blanked = True
            set_blank_render_paused(True)

        if blanked and not overlay_active:
            if last_render_check is None or now - last_render_check >= RENDER_RECHECK:
                last_render_check = now
                set_blank_render_paused(True)

        if not blanked:
            # ALS auto-brightness while screen is on (not in the sleep hours or at the user's level)
            if (not dim and manual is None and sunrise is None and not night_view
                    and (last_als is None or now - last_als >= ALS_INTERVAL) and (last_wake is None or now - last_wake >= ALS_WAKE_COOL)):
                last_als = now
                lux = read_als()
                if lux is not None:
                    smooth = als_filter.add(lux, now)
                    lvl = lux_to_level(smooth)
                    new_target = manual_brightness(lvl)
                    current = brightness if (brightness is not None and brightness > BLANK_BRIGHTNESS) else target_brightness
                    stepped = next_brightness(current, new_target)
                    if stepped != current:
                        if stepped > NIGHT_LEVEL + ALS_HOLD:
                            night_hold = False
                        target_brightness = stepped
                        if target_brightness == new_target and (last_logged_als is None or abs(target_brightness - last_logged_als) >= 80):
                            last_logged_als = target_brightness
                            logger.info(f"ALS: {lux:.2f} lux (smoothed {smooth:.2f}) -> {lvl:.0f}% "
                                        f"(target {new_target}, now {target_brightness})")
                        if brightness is not None and brightness > BLANK_BRIGHTNESS:
                            set_brightness(target_brightness)
                            write_user_brightness(target_brightness)
                            last_written_ub = target_brightness
                    elif brightness is not None and brightness > BLANK_BRIGHTNESS and last_written_ub != current:
                        target_brightness = current
                        write_user_brightness(current)
                        last_written_ub = current

            # Blank on idle (never during a voice conversation, AirPlay mirroring, Intercom call, or screen-off off)
            if not hold and not overlay_active and idle_timeout and idle_secs >= timeout:
                logger.info(f"Idle for {idle_secs:.1f}s -- blanking screen")
                if set_brightness(BLANK_BRIGHTNESS):
                    logger.info("Screen blanked")
                    blanked = True
                    set_blank_render_paused(True)


def run():
    sock_path = PREVIEW_SOCKET if HINT_PATH.parent == RUNTIME_DIR else (HINT_PATH.parent / "thinksmart-display.sock")
    preview = BrightnessPreview(sock_path)
    try:
        preview.open()
    except OSError as exc:
        logger.warning("Brightness preview unavailable: %s", type(exc).__name__)
        preview.close()
        preview = None
    try:
        _run(preview)
    finally:
        if preview is not None:
            preview.close()


if __name__ == "__main__":
    run()
