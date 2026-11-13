"""idle-blanker.py's main loop against a fake backlight, hint file and touch input (a pipe),
plus the night / sunrise rules (ported from thinksmart-view's round-2 regression suite).

  uv run --no-project --with pytest python -m pytest -q screen
"""
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

SCRIPT = Path(__file__).with_name('idle-blanker.py')
# idle-blanker imports thinksmart_display, which lives next to the home service
sys.path.insert(0, str(Path(__file__).parents[1] / 'home'))


@pytest.fixture
def blanker(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))      # its log file
    spec = importlib.util.spec_from_file_location(f'idle_blanker_{tmp_path.name}', SCRIPT)
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)
    backlight, hint = tmp_path / 'backlight', tmp_path / 'hint.json'
    backlight.write_text('4095\n')
    touch_r, touch_w = os.pipe()
    os.set_blocking(touch_r, False)   # real input devices are O_NONBLOCK; drain reads to EAGAIN
    ib.BACKLIGHT_PATH, ib.HINT_PATH = str(backlight), hint
    ib.USER_BRIGHTNESS_PATH = tmp_path / 'user_brightness'
    ib.LEGACY_USER_BRIGHTNESS_PATH = tmp_path / 'user_brightness_legacy'
    ib.MODE_PATH = tmp_path / 'display.json'
    ib.MIRROR_FLAG_PATH, ib.INTERCOM_FLAG_PATH = tmp_path / 'mirror', tmp_path / 'call'
    ib.NIGHT_VIEW_PATH, ib.SUNRISE_PATH = tmp_path / 'night-view', tmp_path / 'sunrise.json'
    ib.read_als = lambda: None                      # no sensor in here: manual levels only
    ib.open_input_devices = lambda: [touch_r]
    ib.signal.signal = lambda *a: None
    threading.Thread(target=ib.run, daemon=True).start()   # left running: its files are this test's

    class Blanker:
        seq = 0

        def hint(self, activity=True, **fields):
            Blanker.seq += activity
            hint.write_text(json.dumps({'seq': Blanker.seq, **fields}))
            time.sleep(0.4)

        def touch(self):
            os.write(touch_w, b'x')
            time.sleep(0.4)

        DIM = ib.DIM_BRIGHTNESS

        def level(self):
            return int(backlight.read_text())

        def blanked_within(self, seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if int(backlight.read_text()) == ib.BLANK_BRIGHTNESS:
                    return True
                time.sleep(0.1)
            return False

    return Blanker()


def test_the_users_screen_off_after_a_conversation_and_after_a_touch(blanker):
    blanker.hint(hold=False, idle=1, after=1)
    assert blanker.blanked_within(2)
    blanker.touch()                       # lights it again, and the user's time applies again
    assert not blanker.blanked_within(0.3)
    assert blanker.blanked_within(2)


def test_never_while_a_conversation_holds_the_screen(blanker):
    blanker.hint(hold=True, idle=1)
    assert not blanker.blanked_within(2)


def test_screen_off_turned_off_never_blanks(blanker):
    blanker.hint(hold=False, idle=0, after=1)   # "after" is for older blankers
    assert not blanker.blanked_within(2)
    blanker.touch()
    assert not blanker.blanked_within(1)


def test_changing_the_setting_alone_applies_it(blanker):
    blanker.hint(hold=False, idle=0)
    blanker.hint(activity=False, hold=False, idle=1, after=1)   # not activity: same seq
    assert blanker.blanked_within(2.5)


def test_an_older_client_keeps_after_and_the_long_touch_timeout(blanker):
    blanker.hint(hold=False, after=1)     # no "idle"
    assert blanker.blanked_within(2)
    blanker.touch()                       # back to IDLE_TIMEOUT (30 minutes)
    assert not blanker.blanked_within(2)


@pytest.mark.parametrize('bad', [True, -5, 'x', 1.5])
def test_a_bad_idle_is_the_normal_timeout(blanker, bad):
    blanker.hint(hold=False, idle=bad)
    blanker.touch()
    assert not blanker.blanked_within(1.5)


# The sleep hours ("dim") only lower the backlight while the screen is on; screen-off follows the
# user's setting there too (the user: 「我希望睡眠时段也遵循息屏设置」).

def test_sleep_hours_blank_after_the_users_time_and_wake_dimmed(blanker):
    blanker.hint(hold=False, dim=True, idle=1, after=1)
    assert blanker.level() == blanker.DIM
    assert blanker.blanked_within(2)
    blanker.touch()
    assert blanker.level() == blanker.DIM
    assert blanker.blanked_within(2)


def test_sleep_hours_with_screen_off_turned_off_stay_on_dimmed(blanker):
    blanker.hint(hold=False, dim=True, idle=0)
    blanker.touch()
    assert not blanker.blanked_within(2.5)
    assert blanker.level() == blanker.DIM


def test_sleep_hours_wait_for_a_longer_time(blanker):
    blanker.hint(hold=False, dim=True, idle=3, after=3)
    assert not blanker.blanked_within(2)
    assert blanker.blanked_within(2)


# ---- the night / sunrise rules (round 2, ported from thinksmart-view) ----------------------
def at(day, month, hour, minute):
    return dt.datetime(2026, month, day, hour, minute).timestamp()


def make_r2(tmp_path, hint='{"brightness":100,"idle":1800}', mode='{"auto": true, "anchor": 100}', stored='3459'):
    spec = importlib.util.spec_from_file_location(f'idle_r2_{tmp_path.name}', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = tmp_path / 'r2'
    d.mkdir(exist_ok=True)
    m.HINT_PATH = d / 'hint'
    m.HINT_PATH.write_text(hint)
    m.MODE_PATH = d / 'mode'
    m.MODE_PATH.write_text(mode)
    m.MIRROR_FLAG_PATH, m.INTERCOM_FLAG_PATH = d / 'mirror', d / 'call'
    m.USER_BRIGHTNESS_PATH = d / 'ub'
    m.LEGACY_USER_BRIGHTNESS_PATH = d / 'ub_legacy'
    m.USER_BRIGHTNESS_PATH.write_text(stored + '\n')
    m.NIGHT_VIEW_PATH, m.SUNRISE_PATH = d / 'night-view', d / 'sunrise.json'
    m.NIGHT_TEST = True
    m.read_als = lambda: None
    return m


class Done(Exception):
    pass


def drive(m, script, start_bl, wall):
    """Run the loop: script(m, bl) -> True for an input event, or (True, False) for a brightness-channel message.
    Returns the brightness writes."""
    bl = [start_bl]
    writes = []
    ticks = [0]
    clock = [1000.0]

    def drain(*a):
        ticks[0] += 1
        clock[0] += 1.0
        if ticks[0] > len(script):
            raise Done()
        r = script[ticks[0] - 1](m, bl)
        if isinstance(r, tuple):
            drain.dev_event = r[1]
            return r[0]
        drain.__dict__.pop('dev_event', None)
        return r

    def setb(v):
        writes.append(v)
        bl[0] = v
        return True

    with mock.patch.object(m, 'open_input_devices', return_value=[]), mock.patch.object(m, 'drain', drain), \
            mock.patch.object(m, 'get_brightness', side_effect=lambda: bl[0]), mock.patch.object(m, 'set_brightness', side_effect=setb), \
            mock.patch.object(m, 'set_blank_render_paused'), mock.patch.object(m.signal, 'signal'), \
            mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), \
            mock.patch.object(m.time, 'time', side_effect=lambda: wall[0] + clock[0] - 1000.0):
        try:
            m._run()
        except Done:
            pass
    return writes


IDLE = lambda m, bl: False
TOUCH = lambda m, bl: True
POKE = lambda m, bl: (True, False)      # the brightness channel (e.g. a new phone message), not a touch


def sunrise_file(m, start):
    m.SUNRISE_PATH.write_text(json.dumps({'start': start, 'ramp': 600, 'hold': 360, 'max': 50}))


def test_lit_in_the_night_is_lowest(tmp_path):
    m = make_r2(tmp_path)
    assert m.in_night(at(13, 11, 1, 0))
    assert not m.in_night(at(13, 11, 6, 30))
    night = drive(m, [IDLE, TOUCH, IDLE], 10, [at(13, 11, 1, 0)])
    assert night[0] == m.NIGHT_LEVEL
    day = drive(make_r2(tmp_path), [IDLE, TOUCH, IDLE], 10, [at(13, 11, 9, 0)])
    assert day[0] == 3459


def test_night_view_keeps_it_dim(tmp_path):
    m = make_r2(tmp_path)

    def up(m_, bl):
        m_.NIGHT_VIEW_PATH.write_text(json.dumps({'until': time.time() + 3600 * 24 * 400}))
        return False

    def gone(m_, bl):
        m_.NIGHT_VIEW_PATH.unlink()
        return False

    w = drive(m, [IDLE, up, IDLE, IDLE, gone, IDLE, IDLE], 3459, [at(12, 11, 21, 30)])
    assert w == [m.NIGHT_LEVEL, 3459]          # dim while it is up, back when it is gone
    m2 = make_r2(tmp_path)                                       # blanked by the night view, then a touch: lowest
    m2.NIGHT_VIEW_PATH.write_text(json.dumps({'until': time.time() + 3600 * 24 * 400}))
    w2 = drive(m2, [IDLE, TOUCH, IDLE], 10, [at(12, 11, 21, 30)])
    assert w2[0] == m2.NIGHT_LEVEL


def test_sunrise_from_its_own_level_up_to_half(tmp_path):
    m = make_r2(tmp_path, stored='1600')
    wall = [at(13, 11, 6, 30)]
    sunrise_file(m, wall[0])

    def later(m_, bl):
        wall[0] += 299
        return False

    w = drive(m, [IDLE, IDLE, later, IDLE, later, IDLE, IDLE], 10, wall)
    assert w[0] == 1600                         # where the screen would wake
    assert max(w) == m.manual_brightness(50)    # 50% and no more
    assert w == sorted(w)


def test_sunrise_on_a_lit_screen_starts_where_it_is(tmp_path):
    m = make_r2(tmp_path)
    wall = [at(13, 11, 6, 30)]

    def rise(m_, bl):
        sunrise_file(m_, wall[0])
        return False

    def later(m_, bl):
        wall[0] += 200
        return False

    w = drive(m, [IDLE, rise, IDLE, later, IDLE], 2000, wall)
    assert w and min(w) >= 2000 and w == sorted(w)   # never darker first


def test_by_hand_level_comes_back_and_a_touch_stops_the_birds(tmp_path):
    m = make_r2(tmp_path, hint='{"brightness":0,"idle":0}', mode='{"auto": false}')   # 0% by hand, never off (11-12)
    wall = [at(13, 11, 6, 30)]
    sunrise_file(m, wall[0])

    def later(m_, bl):
        wall[0] += 400
        return False

    w = drive(m, [IDLE, IDLE, later, IDLE, TOUCH] + [IDLE] * 6, 10, wall)
    assert w[0] == 1400                         # the user's own level
    assert max(w) > 2000
    assert w[-1] == 1400                         # back to it after the touch
    assert 'stopped' in json.loads(m.SUNRISE_PATH.read_text())   # so the birds stop too


def test_not_a_touch_and_stopped_by_the_music_service(tmp_path):
    m = make_r2(tmp_path, stored='1600')
    wall = [at(13, 11, 6, 30)]
    sunrise_file(m, wall[0])
    seen = {}

    def later(m_, bl):
        wall[0] += 100
        return False

    def stop(m_, bl):
        d = json.loads(m_.SUNRISE_PATH.read_text())
        seen['before'] = 'stopped' in d                  # the channel message did not stop it
        d['stopped'] = int(wall[0])
        m_.SUNRISE_PATH.write_text(json.dumps(d))
        return False

    w = drive(m, [IDLE, IDLE, later, POKE, IDLE, stop, IDLE, IDLE, later, IDLE], 10, wall)
    assert not seen['before']
    assert w == sorted(w)                       # automatic: no jump back, the light sensor takes over
