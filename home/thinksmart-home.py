#!/opt/thinksmart-addons/venv/bin/python
"""ThinkSmart View home page service (merged from fubin0478/thinksmart-view, 2026-11).

Serves our own home page (clock / weather / lunar calendar / music card / app tiles) on
http://127.0.0.1:8095/. The kiosk keeps showing XiaoZhi's page (127.0.0.1:8090, untouched,
it owns Wi-Fi setup and the alarm screen); /usr/local/share/thinksmart-home/home_overlay.js
lays this page over it as a full-screen frame and hides it again for 「小智」.

Music is NetEase Cloud Music, played hub-side by plugins_func/netease (mpv): this service
proxies the hub's loopback kiosk API (/kiosk/netease/music/* on the hub admin port) and never
talks to the network itself. Ambient mixes / sleep playlists / sleep sessions from the original
QQ-music service are not available and report {"unsupported": true}.

The study companion service is not deployed; the pomodoro (专注计时) runs locally here and
the study tile/switch are hidden. Also serves the brightness panel (/display) that used to
live in the monitor app, backed by thinksmart_display.DisplayControl.

Also the phone -> screen message board: the intercom phone page (VPN only) posts messages here,
the kiosk overlay shows unread ones until 「收到」 is tapped, then the text is read out once
through the voice channel (STUDY_API, optional).

API (loopback only, Host must be 127.0.0.1:8095 / localhost:8095, foreign Origins refused):
  GET  /                      home page              GET /apps.json   app tiles (config driven)
  GET  /api/home/state        music / focus / messages summary (one poll for the page)
  GET  /api/home/weather      cached QWeather (key read from the hub's private config)
  GET  /api/home/calendar?ym=YYYY-MM   month grid + today (lunar, solar terms, festivals, holidays)
  POST /api/home/music {action}  /focus {...}
  GET  /api/home/inbox        unread messages (kiosk overlay)   POST /api/home/inbox/ack {id}
  GET  /api/home/messages     recent messages; POST /api/home/messages {text} needs X-TS-Internal: 1
                              and no Origin (only the intercom relay can add messages)
  GET  /display               brightness panel       GET|POST /api/display[/preview]
  2026-11-12: messages carry who (FAMILY), voice transcripts, tag 'home' (到家了) / 'remind' (due time);
  POST /api/home/messages/quick {quick} (internal) sets the screen's quick replies, /messages/cancel {id} drops a
  pending reminder, /messages/tool (internal) is 小麦's: send / read / remind / reminders / cancel
"""
import array
import asyncio
import base64
import collections
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

sys.path.insert(0, '/opt/thinksmart-addons/vendor')
try:
    from lunar_python import Solar
    from lunar_python.util import HolidayUtil
except Exception:  # pragma: no cover - the page then shows the solar calendar only
    Solar = None
    HolidayUtil = None
try:
    from thinksmart_display import send_preview, DisplayControl  # /usr/local/bin, next to this script
except Exception:  # pragma: no cover
    send_preview = None
    DisplayControl = None

_IO_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='home-io')

LOG = logging.getLogger('home')
PORT = 8095
SHARE = Path(os.environ.get('HOME_SHARE', '/usr/local/share/thinksmart-home'))
STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/thinksmart-home'))
CONFIG_PATH = STATE / 'config.json'
MSG_PATH = STATE / 'messages.json'
HUB_CONFIG = Path('/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/data/.config.yaml')
# NetEase music runs inside the hub (plugins_func/netease + mpv); the hub's loopback admin API
# exposes status/lyrics/commands for us. Ducking, ambient mixes and sleep sessions are not
# available there and report unsupported.
MUSIC = os.environ.get('MUSIC_API', 'http://127.0.0.1:16688/kiosk/netease/music')
MUSIC_AMBIENT = False   # the QQ-music service's ambient/sleep features are not deployed
# Optional study companion service (voice read-out / camera posture checks). Not deployed:
# the pomodoro runs locally, and TTS read-outs are skipped.
STUDY = os.environ.get('STUDY_API') or None
LOCAL_TZ = ZoneInfo('Asia/Shanghai')
DEFAULT_CITY = os.environ.get('WEATHER_DEFAULT_CITY', '北京')
ALLOWED_HOSTS = {'127.0.0.1:8095', 'localhost:8095'}
ALLOWED_ORIGINS = {f'http://{h}:{p}' for h in ('127.0.0.1', 'localhost') for p in (8090, 8095)}
MAX_MSG_CHARS = 200
MAX_MSGS = 20                    # Product spec 11-12: the latest 20 (reminders still to come are kept apart, see _trim_items)
MAX_PENDING = 20
MIN_VOICE_SEC = 1.0              # Product spec 11-12: shorter recordings are not sent
RECALL_SEC = 120                 # a message can be taken back within 2 minutes
WEEKDAY_CN = '一二三四五六日'
MSG_RATE = (20, 3600)            # at most 20 new messages per hour
MAX_VOICE_WAV = 2 * 1024 * 1024  # 2026-11-12: voice up to 60 s (16 kHz mono WAV is about 1.9 MB)
MAX_VOICE_MP3 = 384 * 1024
MAX_VOICE_SEC = 60.0
# 2026-11-12 (Claude): who a message is from (or, for a reminder, who it is for), the screen's quick replies,
# voice transcripts, timed reminders
FAMILY = ('爸爸', '妈妈', '姐姐', '妹妹')
QUICK_DEFAULT = ('👌 收到啦', '📚 正在学习', '🍚 马上来', '📞 有空打给我', '🍽️ 吃过饭了', '✍️ 作业写完了', '😴 准备睡觉了')
MAX_QUICK, MAX_QUICK_CHARS = 8, 14
MAX_TRANSCRIPT = 400
REMIND_AHEAD_MAX = 8 * 86400
CALL_FLAG = Path('/run/user/1000/intercom-call-active')
MIRROR_FLAG = Path('/run/user/1000/uxplay-mirroring-active')
AUDIO_MIRROR_FLAG = Path('/run/user/1000/uxplay-audio-active')


def _media_busy() -> bool:
    return CALL_FLAG.exists() or MIRROR_FLAG.exists() or AUDIO_MIRROR_FLAG.exists()


def _read_boot_id() -> str:
    with contextlib.suppress(OSError):
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()[:64]
    return ''
WEATHER_TTL = 900
WEATHER_RETRY = 30             # a failed refresh (e.g. network still settling after boot) retries soon
QW_HOST_RE = re.compile(r'^(?:[a-z0-9-]+\.)*(?:qweatherapi\.com|qweather\.com)$')
SOLAR_FEST_SKIP = {'万圣节', '万圣节前夜', '愚人节', '感恩节', '消费者权益日', '平安夜'}
FEST_SHORT = {'元旦节': '元旦', '国庆节': '国庆', '劳动节': '劳动节'}
WEEK_CN = '一二三四五六日'
SESSION = web.AppKey('session', aiohttp.ClientSession)


# ---- small file helpers ---------------------------------------------------------------
def _read_regular_bytes(path, max_bytes, private=False):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError('not a bounded regular file')
        if private and (st.st_mode & 0o077) != 0 and st.st_uid == os.getuid():
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        chunks, rem = [], max_bytes + 1
        while rem > 0:
            b = os.read(fd, min(65536, rem))
            if not b:
                break
            chunks.append(b)
            rem -= len(b)
        raw = b''.join(chunks)
        if len(raw) > max_bytes:
            raise OSError('file too large')
        return raw
    finally:
        os.close(fd)


def _harden_private_dir(d):
    with contextlib.suppress(OSError):
        st = os.lstat(d)
        if stat.S_ISDIR(st.st_mode) and (st.st_mode & 0o077) != 0 and st.st_uid == os.getuid():
            os.chmod(d, 0o700)


def _write_private_json(path, obj):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _harden_private_dir(path.parent)
    tmp = path.parent / f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    created = False
    try:
        if path.is_symlink():
            raise OSError('refusing symlink')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        created = True
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(obj, ensure_ascii=False))
        os.replace(tmp, path)
        created = False
    finally:
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def load_config():
    cfg = {'city': DEFAULT_CITY}
    with contextlib.suppress(OSError, ValueError, UnicodeDecodeError):
        raw = json.loads(_read_regular_bytes(CONFIG_PATH, 65536, private=True).decode('utf-8'))
        if isinstance(raw, dict) and isinstance(raw.get('city'), str) and 0 < len(raw['city'].strip()) <= 20:
            cfg['city'] = raw['city'].strip()
    return cfg


# ---- weather (QWeather, same key as the hub's get_weather plugin) -----------------------
def asr_endpoint(path=HUB_CONFIG):
    """2026-11-12: the hub's MacASR entry (the Mac recognizer XiaoZhi uses): {url, model, language, prompt, key}."""
    try:
        text = _read_regular_bytes(path, 1024 * 1024).decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    out, base = {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if base is None:
            if re.match(r'^MacASR\s*:\s*(#.*)?$', stripped):
                base = indent
            continue
        if indent <= base:
            break
        m = re.match(r'^(base_url|model_name|language|prompt|api_key)\s*:\s*(?:"([^"]*)"|\'([^\']*)\'|([^#\s]+))', stripped)
        if m:
            out[m.group(1)] = next((g for g in m.groups()[1:] if g is not None), '')
    url = out.get('base_url') or ''
    if not re.match(r'^https?://[^\s]+/audio/transcriptions$', url):
        return None
    return {'url': url, 'model': out.get('model_name') or '', 'language': out.get('language') or '',
            'prompt': out.get('prompt') or '', 'key': out.get('api_key') or ''}


def _mp3_to_wav16k(mp3_path) -> bytes:
    proc = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', str(mp3_path),
                           '-ar', '16000', '-ac', '1', '-f', 'wav', 'pipe:1'],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    if proc.returncode != 0 or len(proc.stdout) < 1000:
        raise OSError('decode failed')
    return proc.stdout


def _wav_pieces(wav16k: bytes, max_sec=20.0):
    """A 16 kHz mono WAV in pieces of at most max_sec, each cut at the quietest 0.1 s of the 2 s before the limit
    (smaller uploads through the home gateway, and words are not cut in half)."""
    import io as _io
    import wave as _wave
    with _wave.open(_io.BytesIO(wav16k)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            return [wav16k]
        pcm = array.array('h')
        pcm.frombytes(w.readframes(w.getnframes()))
    step, out, start = int(16000 * max_sec), [], 0
    while len(pcm) - start > step:
        best, cut = None, start + step
        for p in range(start + step - 32000, start + step - 1600, 800):
            e = sum(abs(x) for x in pcm[p:p + 1600:4])
            if best is None or e < best:
                best, cut = e, p + 800
        out.append(pcm[start:cut])
        start = cut
    out.append(pcm[start:])
    pieces = []
    for part in out:
        if len(part) < 4800:   # under 0.3 s: nothing to recognise
            continue
        buf = _io.BytesIO()
        with _wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(part.tobytes())
        pieces.append(buf.getvalue())
    return pieces


def _trim_items(items):
    """The latest MAX_MSGS messages, plus the reminders still to come (up to MAX_PENDING), in their order."""
    pend = [m for m in items if m.get('due') and not m.get('fired')]
    rest = [m for m in items if not (m.get('due') and not m.get('fired'))]
    keep = {m['id'] for m in rest[-MAX_MSGS:]} | {m['id'] for m in pend[-MAX_PENDING:]}
    return [m for m in items if m['id'] in keep]


def parse_days(v):
    """Weekdays 1 (Monday) .. 7 (Sunday) from [1, 3, 5] / '1,3,5' / '一三五' / '周一到周五' style text."""
    out = set()
    if isinstance(v, (list, tuple)):
        for x in v:
            if type(x) is int and 1 <= x <= 7:
                out.add(x)
            elif isinstance(x, str):
                out |= set(parse_days(x))
    elif isinstance(v, str):
        t = v.replace('星期', '周').replace('礼拜', '周')
        m = re.search(r'周?([一二三四五六日天])\s*[到至~-]\s*周?([一二三四五六日天])', t)
        if m:
            a, b = ('一二三四五六日'.index(c) + 1 if c != '天' else 7 for c in m.groups())
            out |= set(range(a, b + 1)) if a <= b else set()
        for c in t:
            if c in '1234567':
                out.add(int(c))
            elif c in '一二三四五六':
                out.add('一二三四五六'.index(c) + 1)
            elif c in '日天':
                out.add(7)
    return sorted(out)


def parse_repeat(repeat, days=None):
    """('daily' | 'workdays' | 'weekly', [weekdays]) or (None, None) for a one-off reminder."""
    r = str(repeat or '').strip().lower()
    if r in ('daily', 'everyday', 'every_day', '每天', '天天'):
        return 'daily', None
    if r in ('workdays', 'workday', 'weekdays', '工作日', '周一到周五'):
        return 'workdays', None
    if r in ('weekly', 'every_week', '每周', '每星期'):
        return 'weekly', parse_days(days)
    return None, None


def _repeat_hits(d, repeat, days):
    wd = d.isoweekday()
    return repeat == 'daily' or (repeat == 'workdays' and wd <= 5) or (repeat == 'weekly' and wd in (days or ()))


def remind_next(due, repeat, days=None):
    """The next time a repeating reminder comes up after `due`, at the same clock time (None if it never does)."""
    d = dt.datetime.fromtimestamp(due)
    for i in range(1, 8):
        n = d + dt.timedelta(days=i)
        if _repeat_hits(n, repeat, days):
            return n.timestamp()
    return None


def repeat_label(m):
    r = m.get('repeat') if isinstance(m, dict) else None
    if r == 'daily':
        return '每天'
    if r == 'workdays':
        return '工作日'
    if r == 'weekly':
        return '每周' + ''.join(WEEKDAY_CN[d - 1] for d in (m.get('days') or []) if type(d) is int and 1 <= d <= 7)
    return ''


def remind_due(at=None, day=None, time_s=None, in_minutes=None, now=None):
    """When a reminder is due (epoch seconds) from 'YYYY-MM-DD HH:MM' / day ('today'|'tomorrow'|'后天'|YYYY-MM-DD)
    + 'HH:MM' / minutes from now. A bare HH:MM that has passed today means tomorrow. None if invalid."""
    now = time.time() if now is None else now
    if in_minutes is not None and not isinstance(in_minutes, bool):
        try:
            mins = float(in_minutes)
        except (TypeError, ValueError, OverflowError):
            return None
        return now + mins * 60 if math.isfinite(mins) and 1 <= mins <= REMIND_AHEAD_MAX / 60 else None
    if isinstance(at, str) and at.strip():
        m = re.match(r'^\s*(\d{4}-\d{1,2}-\d{1,2})[ T](\d{1,2})[:：](\d{2})', at)
        if not m:
            m2 = re.match(r'^\s*(\d{1,2})[:：](\d{2})\s*$', at)
            if not m2:
                return None
            time_s, day = f'{m2.group(1)}:{m2.group(2)}', day
        else:
            day, time_s = m.group(1), f'{m.group(2)}:{m.group(3)}'
    m = re.match(r'^\s*(\d{1,2})[:：点](\d{1,2})?\s*$', str(time_s or ''))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    base = dt.datetime.fromtimestamp(now)
    d = str(day or '').strip().lower()
    if d in ('', 'today', '今天'):
        target = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if not d and target.timestamp() <= now + 30:
            target += dt.timedelta(days=1)
    elif d in ('tomorrow', '明天', '明早', '明晚'):
        target = (base + dt.timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
    elif d in ('后天',):
        target = (base + dt.timedelta(days=2)).replace(hour=h, minute=mi, second=0, microsecond=0)
    else:
        try:
            y, mo, dd = (int(x) for x in d.split('-'))
            target = dt.datetime(y, mo, dd, h, mi)
        except (TypeError, ValueError):
            return None
    t = target.timestamp()
    return t if now + 30 <= t <= now + REMIND_AHEAD_MAX else None


def weather_credentials(path=HUB_CONFIG):
    """api_host / api_key of plugins.get_weather in the hub's private YAML (no YAML lib needed)."""
    try:
        text = _read_regular_bytes(path, 1024 * 1024).decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None, None
    host = key = None
    base = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if base is None:
            if re.match(r'^get_weather\s*:\s*(#.*)?$', stripped):
                base = indent
            continue
        if indent <= base:
            break
        m = re.match(r'^(api_host|api_key)\s*:\s*["\']?([^"\'#\s]+)', stripped)
        if m:
            if m.group(1) == 'api_host':
                host = m.group(2)
            else:
                key = m.group(2)
    if host:
        host = host.lower().removeprefix('https://').removeprefix('http://').strip('/')
        if not QW_HOST_RE.fullmatch(host):
            host = None
    if key and not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', key):
        key = None
    return host, key


_WEATHER_UNAVAILABLE = {'ok': False, 'error': '天气暂时不可用'}


EXTRA_WEATHER_CITIES = tuple(c.strip() for c in os.environ.get('WEATHER_EXTRA_CITIES', '上海,广州,深圳').split(',') if c.strip())

_POLLUTANT_LABELS = {
    'pm2p5': 'PM2.5', 'pm25': 'PM2.5', 'pm10': 'PM10',
    'no2': 'NO₂', 'o3': 'O₃', 'so2': 'SO₂', 'co': 'CO',
}


def weather_cities():
    home = load_config()['city']
    out = [home]
    for c in EXTRA_WEATHER_CITIES:
        if c not in out:
            out.append(c)
    return out


class Weather:
    def __init__(self):
        self.data = None
        self.fetched = None
        self.failed = None
        self.detail = None
        self.detail_fetched = None
        self.detail_failed = None
        self.loc = None
        self.locs = {}
        self.detail_map = {}
        self.detail_locks = {}
        self.seq = 0
        self.detail_seq = 0
        self.last_force = 0.0
        self.lock = asyncio.Lock()

    async def _get(self, session, host, key, path, params=None):
        async with session.get(f'https://{host}{path}', params=params or {}, headers={'X-QW-Api-Key': key},
                               timeout=aiohttp.ClientTimeout(total=12, sock_connect=6), allow_redirects=False) as r:
            raw = await r.content.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024 or r.status != 200:
                raise ConnectionError(f'weather HTTP {r.status}')
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict) or ('code' in data and str(data['code']) != '200'):
                raise ConnectionError(f"weather code {data.get('code') if isinstance(data, dict) else '?'}")
            return data

    async def _ensure_loc(self, session, host, key, city):
        cached = self.locs.get(city)
        if not cached and self.loc and self.loc.get('query') == city:
            cached = self.loc
        if not cached:
            geo = await self._get(session, host, key, '/geo/v2/city/lookup', {'location': city, 'number': 1, 'lang': 'zh'})
            first = (geo.get('location') or [None])[0]
            if not isinstance(first, dict) or not first.get('id'):
                raise RuntimeError('city not found')
            cached = {
                'query': city,
                'id': str(first['id']),
                'name': str(first.get('name') or city)[:20],
                'adm1': str(first.get('adm1') or '')[:20],
                'adm2': str(first.get('adm2') or '')[:20],
                'lat': first.get('lat'),
                'lon': first.get('lon'),
            }
            if len(self.locs) >= 12:
                self.locs.clear()
            self.locs[city] = cached
        if city == load_config()['city']:
            self.loc = cached
        return cached

    async def get_with_seq(self, session, force=False):
        now = time.monotonic()
        city = load_config()['city']
        same_city = bool(self.loc and self.loc.get('query') == city)
        allow_force = bool(force and (self.last_force == 0.0 or now - self.last_force >= 20.0))
        d, s = self.data, self.seq
        if not allow_force:
            if same_city and d and self.fetched is not None and now - self.fetched < WEATHER_TTL:
                return d, s
            if same_city and self.failed is not None and now - self.failed < WEATHER_RETRY:
                return (d, s) if d else (_WEATHER_UNAVAILABLE, 0)
        async with self.lock:
            now = time.monotonic()
            city = load_config()['city']
            same_city = bool(self.loc and self.loc.get('query') == city)
            allow_force = bool(force and (self.last_force == 0.0 or now - self.last_force >= 20.0))
            if not allow_force:
                if same_city and self.data and self.fetched is not None and now - self.fetched < WEATHER_TTL:
                    return self.data, self.seq
                if same_city and self.failed is not None and now - self.failed < WEATHER_RETRY:
                    return (self.data, self.seq) if self.data else (_WEATHER_UNAVAILABLE, 0)
            else:
                self.last_force = now
            try:
                self.data = await self._fetch(session)
                self.fetched = time.monotonic()
                self.failed = None
                self.seq += 1
            except Exception as exc:
                self.failed = time.monotonic()
                LOG.warning('weather refresh failed: %s', type(exc).__name__)
            return (self.data, self.seq) if self.data else (_WEATHER_UNAVAILABLE, 0)

    async def get(self, session):
        d, _ = await self.get_with_seq(session)
        return d

    def _slot(self, city):
        slot = self.detail_map.get(city)
        if slot is None:
            if len(self.detail_map) >= 12:
                self.detail_map.clear()
                self.detail_locks.clear()
            slot = {'data': None, 'fetched': None, 'failed': None, 'seq': 0, 'last_force': 0.0}
            self.detail_map[city] = slot
        return slot

    async def get_detail_with_seq(self, session, force=False, city=None):
        home_city = load_config()['city']
        target = (city or home_city).strip() or home_city
        is_home = (target == home_city)
        now = time.monotonic()
        slot = self._slot(target)
        lf = self.last_force if is_home else slot['last_force']
        allow_force = bool(force and (lf == 0.0 or now - lf >= 20.0))
        if not allow_force:
            if slot['data'] and slot['fetched'] is not None and now - slot['fetched'] < WEATHER_TTL:
                return slot['data'], slot['seq']
            if slot['failed'] is not None and now - slot['failed'] < WEATHER_RETRY:
                return (slot['data'], slot['seq']) if slot['data'] else (_WEATHER_UNAVAILABLE, 0)
        lk = self.lock if is_home else self.detail_locks.setdefault(target, asyncio.Lock())
        async with lk:
            now = time.monotonic()
            slot = self._slot(target)
            lf = self.last_force if is_home else slot['last_force']
            allow_force = bool(force and (lf == 0.0 or now - lf >= 20.0))
            if not allow_force:
                if slot['data'] and slot['fetched'] is not None and now - slot['fetched'] < WEATHER_TTL:
                    return slot['data'], slot['seq']
                if slot['failed'] is not None and now - slot['failed'] < WEATHER_RETRY:
                    return (slot['data'], slot['seq']) if slot['data'] else (_WEATHER_UNAVAILABLE, 0)
            else:
                slot['last_force'] = now
                if is_home:
                    self.last_force = now
            try:
                base, det = await self._fetch_detail(session, city=target)
                t_mono = time.monotonic()
                slot['data'] = det
                slot['fetched'] = t_mono
                slot['failed'] = None
                slot['seq'] += 1
                if is_home:
                    self.data = base
                    self.fetched = t_mono
                    self.failed = None
                    self.seq += 1
                    self.detail = det
                    self.detail_fetched = t_mono
                    self.detail_failed = None
                    self.detail_seq = slot['seq']
            except Exception as exc:
                slot['failed'] = time.monotonic()
                if is_home:
                    self.detail_failed = slot['failed']
                LOG.warning('weather detail refresh failed (%s): %s', target, type(exc).__name__)
            return (slot['data'], slot['seq']) if slot['data'] else (_WEATHER_UNAVAILABLE, 0)

    async def _fetch(self, session):
        host, key = weather_credentials()
        if not host or not key:
            raise RuntimeError('weather not configured')
        city = load_config()['city']
        loc = await self._ensure_loc(session, host, key, city)
        now_r, d3_r = await asyncio.gather(
            self._get(session, host, key, '/v7/weather/now', {'location': loc['id']}),
            self._get(session, host, key, '/v7/weather/3d', {'location': loc['id']}))
        n = now_r.get('now') or {}
        s = lambda v, lim=12: str(v)[:lim] if v is not None else ''
        daily = []
        for d in (d3_r.get('daily') or [])[:3]:
            if isinstance(d, dict):
                daily.append({'date': s(d.get('fxDate')), 'max': s(d.get('tempMax'), 4), 'min': s(d.get('tempMin'), 4),
                              'text': s(d.get('textDay')), 'icon': s(d.get('iconDay'), 4),
                              'text_night': s(d.get('textNight')), 'icon_night': s(d.get('iconNight'), 4),
                              # 2026-11-12 Claude: sunrise / sunset for the home page's sun path (light theme)
                              'sunrise': s(d.get('sunrise'), 5), 'sunset': s(d.get('sunset'), 5)})
        air = None
        with contextlib.suppress(Exception):
            lat, lon = float(loc['lat']), float(loc['lon'])
            aq = await self._get(session, host, key, f'/airquality/v1/current/{lat:.2f}/{lon:.2f}')
            idx = [i for i in (aq.get('indexes') or []) if isinstance(i, dict)]
            pick = next((i for i in idx if i.get('code') == 'cn-mee'), idx[0] if idx else None)
            if pick and pick.get('aqi') is not None:
                air = {'aqi': s(pick.get('aqiDisplay') or pick.get('aqi'), 5), 'category': s(pick.get('category'), 8)}
        if air is None:
            with contextlib.suppress(Exception):
                aq = await self._get(session, host, key, '/v7/air/now', {'location': loc['id']})
                a = aq.get('now') or {}
                if a.get('aqi'):
                    air = {'aqi': s(a.get('aqi'), 5), 'category': s(a.get('category'), 8)}
        return {'ok': True, 'city': loc['name'], 'updated': int(time.time()),
                'now': {'temp': s(n.get('temp'), 4), 'feels': s(n.get('feelsLike'), 4), 'text': s(n.get('text')),
                        'icon': s(n.get('icon'), 4), 'humidity': s(n.get('humidity'), 4),
                        'wind_dir': s(n.get('windDir')), 'wind_scale': s(n.get('windScale'), 6)},
                'daily': daily, 'air': air}

    async def _fetch_detail(self, session, city=None):
        host, key = weather_credentials()
        if not host or not key:
            raise RuntimeError('weather not configured')
        city = (city or load_config()['city']).strip()
        loc = await self._ensure_loc(session, host, key, city)
        s = lambda v, lim=16: str(v)[:lim] if v is not None else ''
        tasks = [
            self._get(session, host, key, '/v7/weather/now', {'location': loc['id']}),
            self._get(session, host, key, '/v7/weather/7d', {'location': loc['id']}),
            self._get(session, host, key, '/v7/weather/24h', {'location': loc['id']}),
            self._get(session, host, key, '/v7/indices/1d', {'location': loc['id'], 'type': '1,2,3,5,8,9,15,16'}),
        ]
        has_coord = False
        lat = lon = 0.0
        with contextlib.suppress(Exception):
            lat, lon = float(loc['lat']), float(loc['lon'])
            has_coord = True
        if has_coord:
            tasks.append(self._get(session, host, key, f'/airquality/v1/current/{lat:.2f}/{lon:.2f}'))
            tasks.append(self._get(session, host, key, f'/weatheralert/v1/current/{lat:.2f}/{lon:.2f}'))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        now_r = res[0]
        if isinstance(now_r, Exception) or not isinstance(now_r, dict):
            raise now_r if isinstance(now_r, Exception) else RuntimeError('weather now failed')
        d7_r = res[1] if not isinstance(res[1], Exception) and isinstance(res[1], dict) else None
        if d7_r is None:
            d7_r = await self._get(session, host, key, '/v7/weather/3d', {'location': loc['id']})
        h24_r = res[2] if not isinstance(res[2], Exception) and isinstance(res[2], dict) else {}
        idx_r = res[3] if not isinstance(res[3], Exception) and isinstance(res[3], dict) else {}
        aq_r = res[4] if has_coord and not isinstance(res[4], Exception) and isinstance(res[4], dict) else None
        al_r = res[5] if has_coord and not isinstance(res[5], Exception) and isinstance(res[5], dict) else {}

        n = now_r.get('now') or {}
        daily7 = []
        for d in (d7_r.get('daily') or [])[:7]:
            if isinstance(d, dict):
                daily7.append({
                    'date': s(d.get('fxDate'), 12),
                    'max': s(d.get('tempMax'), 5),
                    'min': s(d.get('tempMin'), 5),
                    'text': s(d.get('textDay'), 14),
                    'icon': s(d.get('iconDay'), 4),
                    'text_night': s(d.get('textNight'), 14),
                    'icon_night': s(d.get('iconNight'), 4),
                    'sunrise': s(d.get('sunrise'), 5),
                    'sunset': s(d.get('sunset'), 5),
                    'moonrise': s(d.get('moonrise'), 5),
                    'moonset': s(d.get('moonset'), 5),
                    'moon_phase': s(d.get('moonPhase'), 12),
                    'uv': s(d.get('uvIndex'), 4),
                    'humidity': s(d.get('humidity'), 4),
                    'vis': s(d.get('vis'), 6),
                    'precip': s(d.get('precip'), 6),
                    'wind_dir': s(d.get('windDirDay'), 10),
                    'wind_scale': s(d.get('windScaleDay'), 8),
                    'wind_dir_night': s(d.get('windDirNight'), 10),
                    'wind_scale_night': s(d.get('windScaleNight'), 8),
                })

        hourly = []
        for h in (h24_r.get('hourly') or [])[:24]:
            if isinstance(h, dict):
                fxt = s(h.get('fxTime'), 25)
                hm_m = re.search(r'T(\d{2}:\d{2})', fxt)
                hourly.append({
                    'time': hm_m.group(1) if hm_m else fxt[:5],
                    'temp': s(h.get('temp'), 5),
                    'icon': s(h.get('icon'), 4),
                    'text': s(h.get('text'), 12),
                    'pop': s(h.get('pop'), 4),
                    'wind_dir': s(h.get('windDir'), 10),
                    'wind_scale': s(h.get('windScale'), 8),
                })

        air_short = None
        air_detail = None
        if aq_r:
            idx_list = [i for i in (aq_r.get('indexes') or []) if isinstance(i, dict)]
            pick = next((i for i in idx_list if i.get('code') == 'cn-mee'), idx_list[0] if idx_list else None)
            pols = []
            for p_it in (aq_r.get('pollutants') or [])[:8]:
                if not isinstance(p_it, dict):
                    continue
                code = str(p_it.get('code') or '').lower()
                conc = p_it.get('concentration') if isinstance(p_it.get('concentration'), dict) else {}
                val = conc.get('value')
                if isinstance(val, (int, float)):
                    v_str = f'{val:.1f}' if code == 'co' else (str(int(round(val))) if abs(val - round(val)) < 0.05 else f'{val:.1f}')
                else:
                    v_str = s(val, 8)
                pols.append({
                    'code': code[:10],
                    'name': _POLLUTANT_LABELS.get(code, s(p_it.get('name'), 10)),
                    'value': v_str,
                    'unit': s(conc.get('unit'), 10),
                })
            if pick and pick.get('aqi') is not None:
                aqi_s = s(pick.get('aqiDisplay') or pick.get('aqi'), 5)
                cat_s = s(pick.get('category'), 8)
                air_short = {'aqi': aqi_s, 'category': cat_s}
                pri = pick.get('primaryPollutant') if isinstance(pick.get('primaryPollutant'), dict) else {}
                pri_code = str(pri.get('code') or '').lower()
                hlth = pick.get('health') if isinstance(pick.get('health'), dict) else {}
                adv = hlth.get('advice') if isinstance(hlth.get('advice'), dict) else {}
                adv_txt = ' '.join(x for x in (s(adv.get('generalPopulation'), 80), s(adv.get('sensitivePopulation'), 80)) if x)
                air_detail = {
                    'aqi': aqi_s,
                    'category': cat_s,
                    'primary': _POLLUTANT_LABELS.get(pri_code, s(pri.get('name'), 12)),
                    'effect': s(hlth.get('effect'), 120),
                    'advice': adv_txt[:160],
                    'pollutants': pols,
                }
        if air_short is None:
            with contextlib.suppress(Exception):
                aq2 = await self._get(session, host, key, '/v7/air/now', {'location': loc['id']})
                a = aq2.get('now') or {}
                if a.get('aqi'):
                    air_short = {'aqi': s(a.get('aqi'), 5), 'category': s(a.get('category'), 8)}
                    pols = []
                    for k_code, lbl, unit in (('pm2p5', 'PM2.5', 'μg/m³'), ('pm10', 'PM10', 'μg/m³'),
                                              ('no2', 'NO₂', 'μg/m³'), ('o3', 'O₃', 'μg/m³'),
                                              ('so2', 'SO₂', 'μg/m³'), ('co', 'CO', 'mg/m³')):
                        if a.get(k_code) is not None:
                            pols.append({'code': k_code, 'name': lbl, 'value': s(a.get(k_code), 8), 'unit': unit})
                    air_detail = {'aqi': air_short['aqi'], 'category': air_short['category'],
                                  'primary': s(a.get('primary'), 12), 'effect': '', 'advice': '', 'pollutants': pols}

        indices = []
        want_order = {'3': 0, '9': 1, '1': 2, '5': 3, '8': 4, '2': 5, '16': 6, '15': 7}
        raw_idx = [x for x in (idx_r.get('daily') or []) if isinstance(x, dict)]
        raw_idx.sort(key=lambda x: want_order.get(str(x.get('type') or ''), 99))
        for x in raw_idx[:8]:
            nm = s(x.get('name'), 12).replace('指数', '')
            indices.append({
                'type': s(x.get('type'), 4),
                'name': nm or s(x.get('name'), 12),
                'category': s(x.get('category'), 12),
                'text': s(x.get('text'), 180),
            })

        alerts = []
        for al in (al_r.get('alerts') or [])[:4]:
            if not isinstance(al, dict):
                continue
            ev = al.get('eventType')
            ev_name = s(ev.get('name') if isinstance(ev, dict) else ev, 20)
            col = al.get('color')
            col_code = s(col.get('code') if isinstance(col, dict) else al.get('severity'), 12).lower()
            title = s(al.get('headline') or al.get('title') or ev_name, 60)
            if title:
                alerts.append({
                    'title': title,
                    'type': ev_name,
                    'color': col_code,
                    'text': s(al.get('description') or al.get('text'), 500),
                })

        ts = int(time.time())
        obs_m = re.search(r'T(\d{2}:\d{2})', s(n.get('obsTime'), 28))
        base = {
            'ok': True,
            'city': loc['name'],
            'updated': ts,
            'now': {
                'temp': s(n.get('temp'), 4),
                'feels': s(n.get('feelsLike'), 4),
                'text': s(n.get('text'), 12),
                'icon': s(n.get('icon'), 4),
                'humidity': s(n.get('humidity'), 4),
                'wind_dir': s(n.get('windDir'), 12),
                'wind_scale': s(n.get('windScale'), 6),
            },
            'daily': [
                {
                    'date': d['date'], 'max': d['max'][:4], 'min': d['min'][:4],
                    'text': d['text'][:12], 'icon': d['icon'],
                    'text_night': d['text_night'][:12], 'icon_night': d['icon_night'],
                    'sunrise': d['sunrise'], 'sunset': d['sunset'],
                } for d in daily7[:3]
            ],
            'air': air_short,
        }
        det = {
            'ok': True,
            'city': loc['name'],
            'cities': weather_cities(),
            'adm1': loc.get('adm1', ''),
            'adm2': loc.get('adm2', ''),
            'lat': f"{float(loc['lat']):.2f}" if has_coord else '',
            'lon': f"{float(loc['lon']):.2f}" if has_coord else '',
            'updated': ts,
            'obs_hm': obs_m.group(1) if obs_m else '',
            'now': {
                'temp': s(n.get('temp'), 5),
                'feels': s(n.get('feelsLike'), 5),
                'text': s(n.get('text'), 14),
                'icon': s(n.get('icon'), 4),
                'humidity': s(n.get('humidity'), 4),
                'wind_dir': s(n.get('windDir'), 12),
                'wind_scale': s(n.get('windScale'), 8),
                'wind_speed': s(n.get('windSpeed'), 6),
                'pressure': s(n.get('pressure'), 6),
                'vis': s(n.get('vis'), 6),
                'precip': s(n.get('precip'), 6),
                'cloud': s(n.get('cloud'), 4),
                'dew': s(n.get('dew'), 5),
            },
            'hourly': hourly,
            'daily': daily7,
            'air': air_detail,
            'indices': indices,
            'alerts': alerts,
        }
        return base, det


# ---- calendar (lunar_python, MIT, vendored in /opt/thinksmart-addons/vendor) ------------
def _short(name):
    return FEST_SHORT.get(name, name)[:3]


def day_info(day):
    info = {'d': day.day, 'label': '', 'kind': '', 'hol': None}
    if Solar is None:
        return info
    s = Solar.fromYmd(day.year, day.month, day.day)
    lu = s.getLunar()
    fest = list(lu.getFestivals()) + [f for f in s.getFestivals() if f not in SOLAR_FEST_SKIP]
    jq = lu.getJieQi()
    if fest:
        info.update(label=_short(fest[0]), kind='fest')
    elif jq:
        info.update(label=jq, kind='jq')
    elif lu.getDay() == 1:
        info.update(label=lu.getMonthInChinese() + '月', kind='m1')
    else:
        info['label'] = lu.getDayInChinese()
    if HolidayUtil is not None:
        h = HolidayUtil.getHoliday(day.year, day.month, day.day)
        if h is not None:
            info['hol'] = 'work' if h.isWork() else 'off'
            info['hol_name'] = h.getName()
    return info


_MONTH_CACHE = {}
_TODAY_CACHE = {}


def month_grid(year, month):
    key = (year, month, dt.datetime.now(LOCAL_TZ).date().isoformat())
    if key in _MONTH_CACHE:
        return _MONTH_CACHE[key]
    today = dt.datetime.now(LOCAL_TZ).date()
    first = dt.date(year, month, 1)
    start = first - dt.timedelta(days=first.weekday())
    days = []
    for i in range(42):
        d = start + dt.timedelta(days=i)
        info = day_info(d)
        info.update(inMonth=d.month == month, today=d == today, weekend=d.weekday() >= 5, date=d.isoformat())
        days.append(info)
    res = {'year': year, 'month': month, 'days': days}
    if len(_MONTH_CACHE) > 24:
        _MONTH_CACHE.clear()
    _MONTH_CACHE[key] = res
    return res


def today_info():
    now = dt.datetime.now(LOCAL_TZ)
    today = now.date()
    tkey = today.isoformat()
    if tkey in _TODAY_CACHE:
        return _TODAY_CACHE[tkey]
    res = {'date': tkey, 'weekday': '星期' + WEEK_CN[today.weekday()], 'lunar': '', 'year': '', 'tags': []}
    if Solar is None:
        return res
    s = Solar.fromYmd(today.year, today.month, today.day)
    lu = s.getLunar()
    res['lunar'] = f'农历{lu.getMonthInChinese()}月{lu.getDayInChinese()}'
    res['year'] = f'{lu.getYearInGanZhi()}{lu.getYearShengXiao()}年'
    tags = []
    for f in list(lu.getFestivals()) + [f for f in s.getFestivals() if f not in SOLAR_FEST_SKIP]:
        tags.append({'text': f, 'kind': 'fest'})
    jq = lu.getJieQi()
    if jq:
        tags.append({'text': f'今日{jq}', 'kind': 'jq'})
    else:
        with contextlib.suppress(Exception):
            nxt = lu.getNextJieQi()
            ns = nxt.getSolar()
            days = (dt.date(ns.getYear(), ns.getMonth(), ns.getDay()) - today).days
            if days > 0:
                tags.append({'text': f'{days}天后{nxt.getName()}', 'kind': 'jq-next'})
    if HolidayUtil is not None:
        h = HolidayUtil.getHoliday(today.year, today.month, today.day)
        if h is not None:
            tags.append({'text': f'{h.getName()}调休上班' if h.isWork() else f'{h.getName()}假期', 'kind': 'work' if h.isWork() else 'off'})
    res['tags'] = tags[:3]
    if len(_TODAY_CACHE) > 4:
        _TODAY_CACHE.clear()
    _TODAY_CACHE[tkey] = res
    return res


# ---- message board (two-way text & voice: phone <-> screen) --------------------------------
def _write_private_bytes(path, raw_bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _harden_private_dir(path.parent)
    tmp = path.parent / f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    created = False
    try:
        if path.is_symlink():
            raise OSError('refusing symlink')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        created = True
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(raw_bytes)
        os.replace(tmp, path)
        created = False
    finally:
        if created:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _transcode_to_voice_mp3(raw_bytes: bytes, is_raw_pcm16: bool = False) -> tuple[bytes, float]:
    """Convert 16kHz s16le raw PCM or browser WAV into normalized 24kHz 32kbps mono MP3."""
    if is_raw_pcm16:
        if len(raw_bytes) < int(32000 * MIN_VOICE_SEC):
            raise ValueError('说话时间太短，请再录一次')
        pcm_even = raw_bytes[:(len(raw_bytes) // 2) * 2]
        samples = array.array('h')
        samples.frombytes(pcm_even)
        if sys.byteorder != 'little':
            samples.byteswap()
        peak = max((abs(x) for x in samples), default=0)
        if peak < 35:
            raise ValueError('没有录到声音，请检查机身顶部麦克风开关是否已打开')
        dur = min(MAX_VOICE_SEC, round(len(pcm_even) / 32000.0, 1))
        cmd = [
            'ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
            '-f', 's16le', '-ar', '16000', '-ac', '1', '-i', 'pipe:0',
            '-t', '60',
            '-af', 'highpass=f=90,volume=5.2,alimiter=limit=0.95',
            '-ar', '24000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '32k', '-f', 'mp3', 'pipe:1',
        ]
    else:
        if len(raw_bytes) < 400 or len(raw_bytes) > MAX_VOICE_WAV or raw_bytes[:4] != b'RIFF' or raw_bytes[8:12] != b'WAVE':
            raise ValueError('语音格式无效')
        # Parse WAV header for accurate duration when PCM
        dur = 0.0
        try:
            pos = 12
            byte_rate = 32000
            data_sz = max(0, len(raw_bytes) - 44)
            chunks_seen = 0
            while chunks_seen < 32 and pos + 8 <= len(raw_bytes):
                chunks_seen += 1
                cid = raw_bytes[pos:pos + 4]
                csz = struct.unpack_from('<I', raw_bytes, pos + 4)[0]
                if cid == b'fmt ' and csz >= 16 and pos + 24 <= len(raw_bytes):
                    _, nch, srate, brate, _, bps = struct.unpack_from('<HHIIHH', raw_bytes, pos + 8)
                    if srate < 8000 or srate > 96000 or nch < 1 or nch > 2 or bps not in (8, 16, 24, 32):
                        raise ValueError('语音采样参数无效')
                    if brate > 0:
                        byte_rate = brate
                elif cid == b'data':
                    data_sz = min(csz, len(raw_bytes) - (pos + 8))
                    break
                step = 8 + csz + (csz & 1)
                if step <= 0:
                    break
                pos += step
            dur = round(data_sz / float(max(8000, byte_rate)), 1)
        except ValueError:
            raise
        except Exception:
            dur = 0.0
        if 0.0 < dur < MIN_VOICE_SEC:
            raise ValueError('说话时间太短，请再录一次')
        cmd = [
            'ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1',
            '-f', 'wav', '-i', 'pipe:0',
            '-t', '60',
            '-af', 'highpass=f=80,volume=1.8,alimiter=limit=0.95',
            '-ar', '24000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '32k', '-f', 'mp3', 'pipe:1',
        ]
    try:
        proc = subprocess.run(cmd, input=raw_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25)
    except subprocess.TimeoutExpired:
        raise OSError('voice transcode timed out') from None
    mp3 = proc.stdout or b''
    if proc.returncode != 0 or len(mp3) < 200 or len(mp3) > MAX_VOICE_MP3:
        raise ValueError('语音编码失败，请重试')
    if dur <= 0.0:
        dur = max(1.0, min(MAX_VOICE_SEC, round(len(mp3) * 8.0 / 32000.0, 1)))
    return mp3, max(0.5, min(MAX_VOICE_SEC, dur))


class Inbox:
    def __init__(self, path=MSG_PATH):
        self.path = path
        self.voice_dir = path.parent / 'voice'
        self.lock = threading.Lock()
        self.recent_adds = collections.deque()
        self.version = 0
        self.items = self._load()
        self.quick_path = path.parent / 'quick_replies.json'
        self.quick = self._load_quick()
        with self.lock:
            self._prune_voices_locked(self.items)

    def _load(self):
        _harden_private_dir(self.path.parent)
        try:
            raw = json.loads(_read_regular_bytes(self.path, 512 * 1024, private=True).decode('utf-8'))
        except (OSError, ValueError, UnicodeDecodeError):
            return []
        out = []
        for m in raw if isinstance(raw, list) else []:
            if (isinstance(m, dict) and isinstance(m.get('id'), str) and re.fullmatch(r'[0-9a-f]{12}', m['id'])
                    and isinstance(m.get('text'), str) and type(m.get('at')) in (int, float)):
                read_at = m.get('read_at')
                src = m.get('source') if m.get('source') in ('phone', 'screen') else 'phone'
                kind = m.get('kind') if m.get('kind') in ('text', 'voice') else 'text'
                raw_dur = m.get('duration')
                dur = round(float(raw_dur), 1) if (kind == 'voice' and type(raw_dur) in (int, float) and 0.3 <= float(raw_dur) <= 60.0) else 0.0
                tag = m.get('tag') if m.get('tag') in ('home', 'remind') else ''
                due = m.get('due') if (tag == 'remind' and type(m.get('due')) in (int, float)) else None
                tr = m.get('transcript') if (kind == 'voice' and isinstance(m.get('transcript'), str)) else ''
                item = {'id': m['id'], 'text': m['text'][:MAX_MSG_CHARS], 'at': m['at'],
                        'read_at': read_at if type(read_at) in (int, float) else None,
                        'source': src, 'kind': kind, 'duration': dur,
                        'who': m.get('who') if m.get('who') in FAMILY else '', 'transcript': tr[:MAX_TRANSCRIPT],
                        'tag': 'remind' if due else ('home' if tag == 'home' else ''), 'due': due,
                        'fired': bool(m.get('fired')) if due else False}
                rp, days = parse_repeat(m.get('repeat'), m.get('days')) if due else (None, None)
                if rp and (rp != 'weekly' or days):
                    item['repeat'] = rp
                    if rp == 'weekly':
                        item['days'] = days
                if kind == 'voice' and m.get('no_speech') is True and not tr:
                    item['no_speech'] = True
                out.append(item)
        return _trim_items(out)

    def _prune_voices_locked(self, keep_items):
        if not self.voice_dir.exists() or self.voice_dir.is_symlink():
            return
        keep_ids = {m['id'] for m in keep_items if m.get('kind') == 'voice'}
        with contextlib.suppress(OSError):
            for entry in os.scandir(self.voice_dir):
                if entry.name.endswith('.mp3') and entry.name[:-4] not in keep_ids:
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)

    def _save(self):
        _write_private_json(self.path, self.items)

    def _check_rate_locked(self, mono):
        while self.recent_adds and mono - self.recent_adds[0] > MSG_RATE[1]:
            self.recent_adds.popleft()
        if len(self.recent_adds) >= MSG_RATE[0]:
            raise OverflowError('留言太频繁，请稍后再发')

    def check_rate(self):
        with self.lock:
            self._check_rate_locked(time.monotonic())

    def add(self, text, source='phone', who='', tag='', due=None, repeat=None, days=None):
        if source not in ('phone', 'screen'):
            source = 'phone'
        lines = [' '.join(part.split()) for part in str(text).splitlines()]
        text = '\n'.join(part for part in lines if part)[:MAX_MSG_CHARS + 1]
        if not text or len(text) > MAX_MSG_CHARS:
            raise ValueError(f'留言需为 1-{MAX_MSG_CHARS} 个字')
        now = time.time()
        rp, rdays = parse_repeat(repeat, days) if due is not None else (None, None)
        if due is not None:
            if isinstance(due, bool) or type(due) not in (int, float) or not (now + 20 <= due <= now + REMIND_AHEAD_MAX):
                raise ValueError('提醒时间要在 8 天以内的将来')
            tag = 'remind'
            if rp == 'weekly' and not rdays:
                rdays = [dt.datetime.fromtimestamp(due).isoweekday()]
            if rp and not _repeat_hits(dt.datetime.fromtimestamp(due), rp, rdays):
                due = remind_next(due, rp, rdays)   # the first day that fits the rule, same time
        elif tag not in ('home',):
            tag = ''
        mono = time.monotonic()
        with self.lock:
            self._check_rate_locked(mono)
            item = {'id': secrets.token_hex(6), 'text': text, 'at': now, 'read_at': None,
                    'source': source, 'kind': 'text', 'duration': 0.0,
                    'who': who if who in FAMILY else '', 'transcript': '', 'tag': tag,
                    'due': float(due) if due is not None else None, 'fired': False}
            if rp:
                item['repeat'] = rp
                if rp == 'weekly':
                    item['days'] = rdays
            next_items = _trim_items(self.items + [item])
            _write_private_json(self.path, next_items)
            self._prune_voices_locked(next_items)
            self.recent_adds.append(mono)
            self.items = next_items
            self.version += 1
            return dict(item)

    def add_voice(self, mp3_bytes: bytes, duration: float, source='phone', who=''):
        if source not in ('phone', 'screen'):
            source = 'phone'
        if not isinstance(mp3_bytes, (bytes, bytearray)) or not (100 <= len(mp3_bytes) <= MAX_VOICE_MP3):
            raise ValueError('语音数据大小无效')
        dur = max(0.5, min(MAX_VOICE_SEC, round(float(duration), 1)))
        sec_lbl = max(1, int(round(dur)))
        label = f'🎙️ {"语音留言" if source == "phone" else "屏幕语音回复"} ({sec_lbl}″)'
        now = time.time()
        mono = time.monotonic()
        msg_id = secrets.token_hex(6)
        with self.lock:
            self._check_rate_locked(mono)
            vpath = self.voice_dir / f'{msg_id}.mp3'
            _write_private_bytes(vpath, bytes(mp3_bytes))
            item = {'id': msg_id, 'text': label, 'at': now, 'read_at': None,
                    'source': source, 'kind': 'voice', 'duration': dur,
                    'who': who if who in FAMILY else '', 'transcript': '', 'tag': '', 'due': None, 'fired': False}
            next_items = _trim_items(self.items + [item])
            try:
                _write_private_json(self.path, next_items)
            except BaseException:
                with contextlib.suppress(OSError):
                    vpath.unlink(missing_ok=True)
                raise
            self._prune_voices_locked(next_items)
            self.recent_adds.append(mono)
            self.items = next_items
            self.version += 1
            return dict(item)

    def read_voice_bytes(self, msg_id: str) -> bytes:
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise FileNotFoundError()
        with self.lock:
            found = next((m for m in self.items if m['id'] == msg_id and m.get('kind') == 'voice'), None)
        if not found:
            raise FileNotFoundError()
        vpath = self.voice_dir / f'{msg_id}.mp3'
        try:
            return _read_regular_bytes(vpath, MAX_VOICE_MP3, private=True)
        except OSError:
            raise FileNotFoundError() from None

    def ack(self, msg_id, expected_source=None):
        with self.lock:
            for idx, m in enumerate(self.items):
                if m['id'] == msg_id:
                    if expected_source is not None and m.get('source', 'phone') != expected_source:
                        return None, False
                    first = m['read_at'] is None
                    if first:
                        updated = dict(m, read_at=time.time())
                        next_items = list(self.items)
                        next_items[idx] = updated
                        _write_private_json(self.path, next_items)
                        self.items = next_items
                        self.version += 1
                        return dict(updated), True
                    return dict(m), False
        return None, False

    def recent(self, n=10):
        """The latest n messages, plus every reminder still to come, newest first."""
        with self.lock:
            rest = [m for m in self.items if not (m.get('due') and not m.get('fired'))]
            keep = {m['id'] for m in rest[-n:]} | {m['id'] for m in self.items if m.get('due') and not m.get('fired')}
            return [dict(m) for m in reversed(self.items) if m['id'] in keep]

    def remove(self, msg_id, source=None, max_age=None):
        """Take a message back / delete it (and its voice file). LookupError: not there; ValueError: not allowed."""
        with self.lock:
            m = next((x for x in self.items if x['id'] == msg_id), None)
            if m is None:
                raise LookupError(msg_id)
            if source is not None and m.get('source', 'phone') != source:
                raise ValueError('只能撤回自己发的留言')
            if max_age is not None and time.time() - float(m['at']) > max_age:
                raise ValueError('超过 2 分钟了，不能撤回')
            keep = [x for x in self.items if x['id'] != msg_id]
            _write_private_json(self.path, keep)
            self.items = keep
            self.version += 1
            self._prune_voices_locked(keep)
            return dict(m)

    def unread(self):
        """What the screen shows until 「收到」: phone messages, and reminders once their time has come."""
        with self.lock:
            return [dict(m) for m in self.items if m['read_at'] is None and (
                (m.get('due') and m.get('fired')) or (not m.get('due') and m.get('source', 'phone') == 'phone'))]

    def _replace_locked(self, idx, updated):
        next_items = list(self.items)
        next_items[idx] = updated
        _write_private_json(self.path, next_items)
        self.items = next_items
        self.version += 1

    def set_transcript(self, msg_id, text):
        """The words of a voice message; none recognised: no_speech (shown as 「没听清」)."""
        text = ' '.join(str(text or '').split())[:MAX_TRANSCRIPT]
        with self.lock:
            for idx, m in enumerate(self.items):
                if m['id'] == msg_id and m.get('kind') == 'voice':
                    if text and m.get('transcript') != text:
                        upd = dict(m, transcript=text)
                        upd.pop('no_speech', None)
                        self._replace_locked(idx, upd)
                    elif not text and not m.get('transcript') and not m.get('no_speech'):
                        self._replace_locked(idx, dict(m, no_speech=True))
                    return True
        return False

    def pending(self):
        with self.lock:
            return sorted((dict(m) for m in self.items if m.get('due') and not m.get('fired')), key=lambda m: m['due'])

    def fire_due(self, now=None):
        """Reminders whose time has come: shown (unread) from now on. Returns them."""
        now = time.time() if now is None else now
        fired = []
        with self.lock:
            if not any(m.get('due') and not m.get('fired') and m['due'] <= now for m in self.items):
                return fired
            next_items, later = [], []
            for m in self.items:
                if m.get('due') and not m.get('fired') and m['due'] <= now:
                    m = dict(m, fired=True, read_at=None)
                    fired.append(dict(m))
                    if m.get('repeat'):   # the next one of a repeating reminder (skipping any missed while off)
                        nxt = remind_next(m['due'], m['repeat'], m.get('days'))
                        while nxt is not None and nxt <= now:
                            nxt = remind_next(nxt, m['repeat'], m.get('days'))
                        if nxt is not None:
                            later.append(dict(m, id=secrets.token_hex(6), at=now, due=nxt, fired=False, read_at=None))
                next_items.append(m)
            next_items = _trim_items(next_items + later)
            _write_private_json(self.path, next_items)
            self.items = next_items
            self.version += 1
        return fired

    def cancel_reminder(self, msg_id):
        with self.lock:
            keep = [m for m in self.items if not (m['id'] == msg_id and m.get('due') and not m.get('fired'))]
            if len(keep) == len(self.items):
                return False
            _write_private_json(self.path, keep)
            self.items = keep
            self.version += 1
            return True

    def _load_quick(self):
        try:
            raw = json.loads(_read_regular_bytes(self.quick_path, 16384, private=True).decode('utf-8'))
            if isinstance(raw, list):
                return _clean_quick(raw) or list(QUICK_DEFAULT)
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return list(QUICK_DEFAULT)

    def set_quick(self, items):
        clean = _clean_quick(items)
        if not clean:
            raise ValueError('至少留一条快捷回复')
        with self.lock:
            _write_private_json(self.quick_path, clean)
            self.quick = clean
            self.version += 1
        return list(clean)


def _clean_quick(items):
    out = []
    for q in items if isinstance(items, list) else []:
        q = ' '.join(str(q).split()) if isinstance(q, str) else ''
        if q and len(q) <= MAX_QUICK_CHARS and q not in out:
            out.append(q)
    return out[:MAX_QUICK]


def _pulse_env():
    return {**os.environ, 'XDG_RUNTIME_DIR': '/run/user/1000', 'PULSE_SERVER': 'unix:/run/user/1000/pulse/native'}


class SpeakerClipPlayer:
    """Plays voice messages or gentle timer chimes on PipeWire without overlapping calls/AirPlay."""

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = ()
        self.gen = 0
        self._active = False

    @property
    def is_playing(self) -> bool:
        with self.lock:
            return self._active

    def stop(self):
        with self.lock:
            self.gen += 1
            self._active = False
            old = self.procs
            self.procs = ()
        for p in old:
            if p is not None:
                with contextlib.suppress(Exception):
                    p.kill()
                with contextlib.suppress(Exception):
                    p.poll()

    def play_mp3_file(self, mp3_path: Path, on_done=None):
        if CALL_FLAG.exists() or MIRROR_FLAG.exists():
            return False
        try:
            st = mp3_path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_VOICE_MP3:
                return False
        except OSError:
            return False
        self.stop()
        with self.lock:
            self.gen += 1
            my_gen = self.gen
            self._active = True

        def _worker():
            p_dec = p_out = None
            try:
                env = _pulse_env()
                p_dec = subprocess.Popen(
                    ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-filter_threads', '1', '-i', str(mp3_path),
                     '-af', 'volume=1.85,alimiter=limit=0.96', '-f', 'f32le', '-ar', '48000', '-ac', '2', 'pipe:1'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                )
                p_out = subprocess.Popen(
                    ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                    stdin=p_dec.stdout, stderr=subprocess.DEVNULL, env=env,
                )
                if p_dec.stdout:
                    p_dec.stdout.close()
                with self.lock:
                    if self.gen != my_gen:
                        return
                    self.procs = (p_dec, p_out)
                deadline = time.monotonic() + 62.0
                while p_out.poll() is None and time.monotonic() < deadline:
                    if CALL_FLAG.exists() or MIRROR_FLAG.exists():
                        break
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        p_out.wait(timeout=0.2)
                if not (CALL_FLAG.exists() or MIRROR_FLAG.exists()):
                    p_dec.wait(timeout=2)
            except Exception:
                pass
            finally:
                if p_dec is not None and p_dec.stdout:
                    with contextlib.suppress(Exception):
                        p_dec.stdout.close()
                for p in (p_dec, p_out):
                    if p is not None:
                        with contextlib.suppress(Exception):
                            p.kill()
                        with contextlib.suppress(Exception):
                            p.wait(timeout=1.0)
                with self.lock:
                    if self.procs == (p_dec, p_out):
                        self.procs = ()
                    if self.gen == my_gen:
                        self._active = False
                if on_done is not None:
                    with contextlib.suppress(Exception):
                        on_done()

        threading.Thread(target=_worker, daemon=True).start()
        return True

    _chime_raw = None

    @classmethod
    def _get_chime_raw(cls) -> bytes:
        if cls._chime_raw is None:
            rate = 48000
            notes = ((523.25, 0.0, 0.45), (659.25, 0.16, 0.48), (783.99, 0.32, 0.55), (1046.50, 0.50, 0.85))
            total = int(rate * 1.45)
            buf = array.array('f', [0.0] * (total * 2))
            for hz, start_s, dur_s in notes:
                i0 = int(start_s * rate)
                ln = min(int(dur_s * rate), total - i0)
                w = 2.0 * math.pi * hz / rate
                for i in range(ln):
                    env_a = min(1.0, i / (0.008 * rate)) * math.exp(-3.8 * i / ln)
                    s = 0.22 * env_a * (math.sin(w * i) + 0.22 * math.sin(2.0 * w * i))
                    idx = (i0 + i) * 2
                    buf[idx] += s
                    buf[idx + 1] += s
            cls._chime_raw = buf.tobytes()
        return cls._chime_raw

    def play_timer_chime(self, on_done=None):
        if CALL_FLAG.exists() or MIRROR_FLAG.exists():
            return False
        self.stop()
        with self.lock:
            self.gen += 1
            my_gen = self.gen
            self._active = True

        def _chime():
            p_out = None
            try:
                raw = self._get_chime_raw()
                with self.lock:
                    if self.gen != my_gen:
                        return
                p_out = subprocess.Popen(
                    ['pacat', '--rate=48000', '--channels=2', '--format=float32le', '--latency-msec=60'],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_pulse_env(),
                )
                with self.lock:
                    if self.gen != my_gen:
                        return
                    self.procs = (p_out,)
                if p_out.stdin:
                    mv = memoryview(raw)
                    off = 0
                    aborted = False
                    while off < len(mv):
                        if self.gen != my_gen or CALL_FLAG.exists() or MIRROR_FLAG.exists():
                            aborted = True
                            break
                        p_out.stdin.write(mv[off:off + 16384])
                        off += 16384
                    if aborted:
                        return
                    p_out.stdin.close()
                deadline = time.monotonic() + 4.0
                while p_out.poll() is None and time.monotonic() < deadline:
                    if CALL_FLAG.exists() or MIRROR_FLAG.exists():
                        break
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        p_out.wait(timeout=0.2)
            except Exception:
                pass
            finally:
                if p_out is not None:
                    with contextlib.suppress(Exception):
                        p_out.kill()
                    if p_out.stdin:
                        with contextlib.suppress(Exception):
                            p_out.stdin.close()
                    with contextlib.suppress(Exception):
                        p_out.wait(timeout=1.0)
                with self.lock:
                    if self.procs == (p_out,):
                        self.procs = ()
                    if self.gen == my_gen:
                        self._active = False
                if on_done is not None:
                    with contextlib.suppress(Exception):
                        on_done()

        threading.Thread(target=_chime, daemon=True).start()
        return True


class ScreenVoiceRecorder:
    """Captures up to 60.5 s (since 11-12; was 15.5 s) of 16kHz mono PCM from thinksmart_mic on screen demand (0% idle CPU)."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.proc = None
        self.buf = bytearray()
        self.reader_task = None
        self.started_mono = 0.0
        self.finished_mono = 0.0
        self.aborted_reason = ''
        self.gen = 0
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def start(self, on_done=None):
        async with self.lock:
            if CALL_FLAG.exists() or MIRROR_FLAG.exists():
                raise RuntimeError('当前正在视频通话或投屏，暂时无法录语音留言')
            await self._stop_locked()
            self.gen += 1
            my_gen = self.gen
            self.buf = bytearray()
            self.finished_mono = 0.0
            self.aborted_reason = ''
            loop = asyncio.get_running_loop()
            self.proc = await asyncio.create_subprocess_exec(
                '/usr/bin/parec',
                '--device=thinksmart_mic',
                '--format=s16le',
                '--rate=16000',
                '--channels=1',
                '--latency-msec=40',
                env=_pulse_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.started_mono = time.monotonic()
            self._recording = True
            proc = self.proc

            async def _drain():
                max_bytes = int(60.6 * 32000)
                try:
                    while proc.stdout and len(self.buf) < max_bytes:
                        if _media_busy():
                            self.aborted_reason = '通话或投屏中已自动取消录音'
                            break
                        chunk = await proc.stdout.read(4096)
                        if not chunk:
                            break
                        if _media_busy():
                            self.aborted_reason = '通话或投屏中已自动取消录音'
                            break
                        take = min(len(chunk), max_bytes - len(self.buf))
                        self.buf.extend(chunk[:take])
                        if len(self.buf) >= max_bytes:
                            break
                except Exception:
                    pass
                finally:
                    if proc.returncode is None:
                        with contextlib.suppress(Exception):
                            proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=1.0)
                        except Exception:
                            with contextlib.suppress(Exception):
                                proc.kill()
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(proc.wait(), timeout=1.0)
                    if self.gen == my_gen:
                        self._recording = False
                        self.finished_mono = time.monotonic()

                        def _expire_stale_buf():
                            if self.gen == my_gen and not self._recording and self.buf:
                                self.buf = bytearray()
                                self.aborted_reason = ''

                        with contextlib.suppress(RuntimeError):
                            loop.call_later(15.0, _expire_stale_buf)
                        if on_done is not None:
                            with contextlib.suppress(Exception):
                                on_done()

            self.reader_task = asyncio.create_task(_drain())
            return {'ok': True, 'max_seconds': 60}

    async def _stop_locked(self) -> bytes:
        self.gen += 1
        self.finished_mono = 0.0
        proc, rtask = self.proc, self.reader_task
        self.proc, self.reader_task = None, None
        self._recording = False
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.2)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
        if rtask is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(rtask, timeout=1.0)
        raw = bytes(self.buf)
        self.buf = bytearray()
        return raw

    async def cancel(self):
        async with self.lock:
            self.aborted_reason = ''
            await self._stop_locked()
        return {'ok': True}

    async def finish_to_mp3(self) -> tuple[bytes, float]:
        async with self.lock:
            was_rec = self._recording
            fin_mono = self.finished_mono
            aborted = self.aborted_reason
            self.aborted_reason = ''
            raw = await self._stop_locked()
        if aborted:
            raise ValueError(aborted)
        if not was_rec and (fin_mono <= 0.0 or time.monotonic() - fin_mono > 15.0):
            raise ValueError('录音未开启或已超时，请重录')
        if not raw:
            raise ValueError('没有录到声音，请重试')
        return await asyncio.get_running_loop().run_in_executor(_IO_POOL, _transcode_to_voice_mp3, raw, True)


class DeskTimer:
    """Lightweight event-loop timer supporting both countdown (chimes on completion) and stopwatch (count-up + laps)."""

    def __init__(self, on_change, on_alarm, timer_path=None):
        self.mode = 'countdown'  # 'countdown' | 'stopwatch'
        self.phase = None        # None | 'running' | 'paused' | 'done'
        self.total = 0
        self.deadline = 0.0
        self.paused_rem = 0.0
        self.started_at = 0.0
        self.elapsed_acc = 0.0
        self.laps = []
        self.done_until = 0.0
        self.label = ''
        self.seq = 0
        self.timer_path = Path(timer_path) if timer_path is not None else None
        self._handle = None
        self._clear_handle = None
        self._on_change = on_change
        self._on_alarm = on_alarm

    def _save_state(self):
        tp = self.timer_path
        if tp is None:
            return
        with contextlib.suppress(OSError):
            if self.phase not in ('running', 'paused'):
                if not tp.is_symlink():
                    tp.unlink(missing_ok=True)
                return
            now_m = time.monotonic()
            now_w = time.time()
            doc = {
                'mode': self.mode,
                'phase': self.phase,
                'total': int(self.total or 0),
                'label': self.label,
                'laps': list(self.laps[:20]),
                'boot_id': _read_boot_id(),
                'saved_mono': round(now_m, 3),
                'saved_wall': round(now_w, 3),
            }
            if self.mode == 'stopwatch':
                el = self.elapsed_acc + (max(0.0, now_m - self.started_at) if self.phase == 'running' else 0.0)
                doc['elapsed'] = round(min(86400.0, el), 3)
            else:
                rem = max(0.5, self.deadline - now_m) if self.phase == 'running' else max(0.5, self.paused_rem)
                doc['rem'] = round(min(10800.0, rem), 3)
            _write_private_bytes(tp, json.dumps(doc, ensure_ascii=False).encode('utf-8'))

    def restore_on_start(self):
        tp = self.timer_path
        if tp is None or not tp.exists() or self.phase is not None:
            return
        try:
            raw = json.loads(_read_regular_bytes(tp, 32768, private=True).decode('utf-8'))
            if not isinstance(raw, dict):
                return
            mode = raw.get('mode')
            phase = raw.get('phase')
            if mode not in ('countdown', 'stopwatch') or phase not in ('running', 'paused'):
                return
            now_m = time.monotonic()
            now_w = time.time()
            saved_mono = float(raw.get('saved_mono') or 0.0)
            saved_wall = float(raw.get('saved_wall') or 0.0)
            cur_boot = _read_boot_id()
            if cur_boot and raw.get('boot_id') == cur_boot and now_m >= saved_mono > 0.0:
                dt_sec = max(0.0, now_m - saved_mono)
            elif saved_wall > 1700000000.0 and now_w >= saved_wall:
                dt_sec = max(0.0, now_w - saved_wall)
            else:
                dt_sec = 0.0
            self.mode = mode
            self.label = str(raw.get('label') or '').strip()[:20]
            laps_in = raw.get('laps') if isinstance(raw.get('laps'), list) else []
            clean_laps = []
            for lp in laps_in[:20]:
                if isinstance(lp, dict) and type(lp.get('idx')) is int and isinstance(lp.get('split'), (int, float)) and isinstance(lp.get('total'), (int, float)):
                    clean_laps.append({'idx': lp['idx'], 'split': round(float(lp['split']), 1), 'total': round(float(lp['total']), 1)})
            self.laps = clean_laps
            if mode == 'stopwatch':
                self.total = 0
                saved_el = max(0.0, float(raw.get('elapsed') or 0.0))
                if phase == 'paused':
                    self.elapsed_acc = min(86400.0, saved_el)
                    self.phase = 'paused'
                else:
                    cur_el = saved_el + dt_sec
                    if cur_el >= 86400.0:
                        self.elapsed_acc = 86400.0
                        self.phase = 'paused'
                    else:
                        self.elapsed_acc = cur_el
                        self.started_at = now_m
                        self.phase = 'running'
                        self._arm(max(1.0, 86400.0 - cur_el))
            else:
                self.total = max(5, min(10800, int(raw.get('total') or 900)))
                saved_rem = max(0.5, min(10800.0, float(raw.get('rem') or self.total)))
                if phase == 'paused':
                    self.paused_rem = saved_rem
                    self.phase = 'paused'
                else:
                    rem = saved_rem - dt_sec
                    if rem > 0.5:
                        self.paused_rem = rem
                        self.deadline = now_m + rem
                        self.phase = 'running'
                        self._arm(rem)
                    elif rem > -120.0:
                        self.paused_rem = 0.05
                        self.deadline = now_m + 0.05
                        self.phase = 'running'
                        self._arm(0.05)
                    else:
                        self.phase = None
                        self._save_state()
                        return
            self.seq += 1
            self._save_state()
            self._on_change()
        except Exception:
            with contextlib.suppress(OSError):
                if not tp.is_symlink():
                    tp.unlink(missing_ok=True)

    def shutdown(self):
        if self.phase in ('running', 'paused'):
            self._save_state()
        self._cancel_handles()

    def _cancel_handles(self):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._clear_handle is not None:
            self._clear_handle.cancel()
            self._clear_handle = None

    def _arm(self, delay: float):
        self._cancel_handles()
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(max(0.05, delay), self._finish)

    def _finish(self):
        self._handle = None
        if self.phase != 'running':
            return
        if self.mode == 'stopwatch':
            self.elapsed_acc = 86400.0
            self.phase = 'paused'
            self.seq += 1
            self._save_state()
            self._on_change()
            return
        self.phase = 'done'
        self.paused_rem = 0.0
        self.done_until = time.monotonic() + 90.0
        self.seq += 1
        alarm_seq = self.seq
        self._save_state()
        self._on_change()
        loop = asyncio.get_running_loop()
        self._clear_handle = loop.call_later(90.0, self._auto_clear_done)
        try:
            coro = self._on_alarm(self.label, self.total, alarm_seq)
        except TypeError:
            coro = self._on_alarm(self.label, self.total)
        asyncio.create_task(coro)

    def _auto_clear_done(self):
        self._clear_handle = None
        if self.phase == 'done':
            self.phase = None
            self.seq += 1
            self._save_state()
            self._on_change()

    def start(self, seconds: int, label: str = '') -> dict:
        sec = max(5, min(10800, int(seconds)))
        self.mode = 'countdown'
        self.total = sec
        self.label = str(label or '').strip()[:20]
        self.phase = 'running'
        self.deadline = time.monotonic() + sec
        self.paused_rem = float(sec)
        self.laps = []
        self.seq += 1
        self._arm(float(sec))
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public(), 'message': f'已开启 {max(1, round(sec / 60))} 分钟倒计时'}

    def start_stopwatch(self, label: str = '') -> dict:
        self._cancel_handles()
        self.mode = 'stopwatch'
        self.total = 0
        self.label = str(label or '').strip()[:20]
        self.phase = 'running'
        self.started_at = time.monotonic()
        self.elapsed_acc = 0.0
        self.laps = []
        self.seq += 1
        self._arm(86400.0)
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public(), 'message': '已开启正向计时'}

    def set_label(self, label: str) -> dict:
        if self.phase not in ('running', 'paused', 'done'):
            return {'ok': False, 'error': '当前未在计时'}
        self.label = str(label or '').strip()[:20]
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public()}

    def lap(self) -> dict:
        if self.mode != 'stopwatch' or self.phase not in ('running', 'paused'):
            return {'ok': False, 'error': '当前未在正向计时'}
        now = time.monotonic()
        cur = self.elapsed_acc + (max(0.0, now - self.started_at) if self.phase == 'running' else 0.0)
        tot = round(cur, 1)
        prev_tot = self.laps[0]['total'] if self.laps else 0.0
        if self.laps and tot - prev_tot < 0.08:
            return {'ok': True, 'timer': self.public()}
        split = round(max(0.1, tot - prev_tot), 1)
        idx = (self.laps[0]['idx'] + 1) if self.laps else 1
        self.laps = [{'idx': idx, 'split': split, 'total': tot}] + self.laps[:19]
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public()}

    def pause(self) -> dict:
        if self.phase != 'running':
            return {'ok': False, 'error': '计时器未在运行'}
        now = time.monotonic()
        if self.mode == 'stopwatch':
            self.elapsed_acc += max(0.0, now - self.started_at)
        else:
            self.paused_rem = max(0.5, self.deadline - now)
        self.phase = 'paused'
        self._cancel_handles()
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public()}

    def resume(self) -> dict:
        if self.phase != 'paused':
            return {'ok': False, 'error': '计时器未处于暂停态'}
        now = time.monotonic()
        self.phase = 'running'
        if self.mode == 'stopwatch':
            self.started_at = now
            self._arm(max(1.0, 86400.0 - self.elapsed_acc))
        else:
            rem = max(0.5, self.paused_rem)
            self.deadline = now + rem
            self._arm(rem)
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public()}

    def add_seconds(self, delta: int) -> dict:
        if self.mode == 'stopwatch':
            return self.lap()
        if self.phase not in ('running', 'paused', 'done'):
            return {'ok': False, 'error': '当前没有倒计时'}
        d = max(-3600, min(3600, int(delta)))
        if self.phase == 'done':
            if d <= 0:
                return self.stop()
            return self.start(max(30, d), self.label)
        if self.phase == 'running':
            rem = max(5.0, min(10800.0, (self.deadline - time.monotonic()) + d))
            self.deadline = time.monotonic() + rem
            self.total = min(10800, max(int(math.ceil(rem)), self.total + max(0, d)))
            self._arm(rem)
        else:
            self.paused_rem = max(5.0, min(10800.0, self.paused_rem + d))
            self.total = min(10800, max(int(math.ceil(self.paused_rem)), self.total + max(0, d)))
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': self.public()}

    def stop(self) -> dict:
        self._cancel_handles()
        self.phase = None
        self.total = 0
        self.paused_rem = 0.0
        self.elapsed_acc = 0.0
        self.laps = []
        self.seq += 1
        self._save_state()
        self._on_change()
        return {'ok': True, 'timer': None, 'message': '已结束计时'}

    def public(self):
        if self.phase is None:
            return None
        now = time.monotonic()
        if self.mode == 'stopwatch':
            el = self.elapsed_acc + (max(0.0, now - self.started_at) if self.phase == 'running' else 0.0)
            return {
                'mode': 'stopwatch',
                'phase': self.phase,
                'elapsed': round(el, 1),
                'laps': list(self.laps[:8]),
                'label': self.label,
                'seq': self.seq,
            }
        if self.phase == 'done' and now >= self.done_until:
            self._cancel_handles()
            self._auto_clear_done()
            return None
        if self.phase == 'running':
            rem = max(0.0, self.deadline - now)
        elif self.phase == 'paused':
            rem = max(0.0, self.paused_rem)
        else:
            rem = 0.0
        return {
            'mode': 'countdown',
            'phase': self.phase,
            'total': self.total,
            'remaining': round(rem, 1),
            'label': self.label,
            'seq': self.seq,
        }


class FocusTimer:
    """Local pomodoro (the study service's focus without the camera): focus -> break -> done.

    The UI reads the same fields the study service used to publish: phase ('focus'|'break'),
    remaining (seconds), minutes, break_minutes, with_study (recorded, never acted on here).
    """

    def __init__(self, on_change, on_chime, stats_path=None):
        self.phase = None        # None | 'focus' | 'break' | 'paused'
        self.minutes = 0
        self.break_minutes = 0
        self.with_study = False
        self.label = ''
        self.deadline = 0.0
        self.paused_rem = 0.0
        self.paused_phase = None  # 'focus' | 'break' while phase == 'paused'
        self.seq = 0
        self.stats_path = Path(stats_path) if stats_path is not None else None
        self._handle = None
        self._on_change = on_change
        self._on_chime = on_chime

    # ---- today's stats (sessions / minutes) ---------------------------------------------
    def _read_stats(self):
        tp = self.stats_path
        if tp is None:
            return {'date': '', 'sessions': 0, 'minutes': 0}
        try:
            data = json.loads(_read_regular_bytes(tp, 1024, private=True).decode('utf-8'))
            if isinstance(data, dict):
                return {'date': str(data.get('date') or ''), 'sessions': int(data.get('sessions') or 0),
                        'minutes': int(data.get('minutes') or 0)}
        except Exception:
            pass
        return {'date': '', 'sessions': 0, 'minutes': 0}

    def _bump_stats(self, minutes):
        st = self._read_stats()
        today = dt.datetime.now(LOCAL_TZ).date().isoformat()
        if st.get('date') != today:
            st = {'date': today, 'sessions': 0, 'minutes': 0}
        st['sessions'] = int(st.get('sessions') or 0) + 1
        st['minutes'] = int(st.get('minutes') or 0) + max(1, int(minutes or 0))
        tp = self.stats_path
        if tp is not None:
            _write_private_json(tp, st)

    def today(self):
        st = self._read_stats()
        today = dt.datetime.now(LOCAL_TZ).date().isoformat()
        return {'sessions': st['sessions'], 'minutes': st['minutes']} if st.get('date') == today else {'sessions': 0, 'minutes': 0}

    # ---- state machine --------------------------------------------------------------------
    def _cancel_handle(self):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _arm(self, delay):
        self._cancel_handle()
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(max(0.05, delay), self._finish)

    def _finish(self):
        self._handle = None
        if self.phase not in ('focus', 'break'):
            return
        done_focus = self.phase == 'focus'
        if done_focus:
            self._bump_stats(self.minutes)
            if self.break_minutes > 0:
                self.phase = 'break'
                self.deadline = time.monotonic() + self.break_minutes * 60
                self.paused_rem = float(self.break_minutes * 60)
                self._arm(self.break_minutes * 60)
                self._chime()
                self._changed()
                return
        self.phase = None
        self._chime()
        self._changed()

    def _chime(self):
        try:
            self._on_chime()
        except Exception:
            pass

    def _changed(self):
        self._on_change()

    def start(self, minutes, break_minutes=0, with_study=False, label='') -> dict:
        mins = max(1, min(180, int(minutes or 25)))
        brk = max(0, min(60, int(break_minutes or 0)))
        self.minutes = mins
        self.break_minutes = brk
        self.with_study = bool(with_study)
        self.label = str(label or '').strip()[:20]
        self.phase = 'focus'
        self.paused_phase = None
        self.deadline = time.monotonic() + mins * 60
        self.paused_rem = float(mins * 60)
        self.seq += 1
        self._arm(mins * 60)
        self._changed()
        return self.public()

    def pause(self) -> dict:
        if self.phase not in ('focus', 'break'):
            return {'ok': False, 'error': '当前没有专注计时'}
        self.paused_rem = max(0.5, self.deadline - time.monotonic())
        self.paused_phase = self.phase
        self.phase = 'paused'
        self._cancel_handle()
        self.seq += 1
        self._changed()
        return {'ok': True, **self.public()}

    def resume(self) -> dict:
        if self.phase != 'paused':
            return {'ok': False, 'error': '专注计时未处于暂停态'}
        self.phase = self.paused_phase or 'focus'
        self.paused_phase = None
        rem = max(0.5, self.paused_rem)
        self.deadline = time.monotonic() + rem
        self.seq += 1
        self._arm(rem)
        self._changed()
        return {'ok': True, **self.public()}

    def stop(self) -> dict:
        self._cancel_handle()
        self.phase = None
        self.paused_phase = None
        self.seq += 1
        self._changed()
        return {'ok': True, 'focus': None, 'message': '已结束专注计时'}

    def shutdown(self):
        self._cancel_handle()

    def public(self):
        if self.phase is None:
            return None
        if self.phase == 'paused':
            rem = max(0.0, self.paused_rem)
        else:
            rem = max(0.0, self.deadline - time.monotonic())
        return {'phase': self.phase, 'remaining': round(rem, 1), 'minutes': self.minutes,
                'break_minutes': self.break_minutes, 'with_study': self.with_study,
                'label': self.label, 'seq': self.seq}


# ---- HTTP ---------------------------------------------------------------------------------
DESK_TIMER = web.AppKey('desk_timer', DeskTimer)
FIRE_REMINDERS = web.AppKey('fire_reminders', object)   # 2026-11-12: the reminder step, for the tests
async def _get_json(session, url, timeout=2.0, max_bytes=262144, etag_box=None):
    hdrs = {'If-None-Match': etag_box['etag']} if etag_box and etag_box.get('etag') else None
    try:
        async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 304 and etag_box and isinstance(etag_box.get('data'), dict):
                return etag_box['data']
            raw = await r.content.read(max_bytes + 1)
            if r.status != 200 or len(raw) > max_bytes:
                if etag_box is not None:
                    etag_box['etag'], etag_box['data'] = '', None
                return None
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict):
                if etag_box is not None:
                    etag_box['etag'], etag_box['data'] = '', None
                return None
            if etag_box is not None:
                etag_box['etag'] = r.headers.get('ETag') or ''
                etag_box['data'] = data
            return data
    except Exception:
        if etag_box is not None:
            etag_box['etag'], etag_box['data'] = '', None
        return None


async def _post_json(session, url, body, timeout=5.0, headers=None):
    try:
        async with session.post(url, json=body, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            raw = await r.content.read(65537)
            try:
                data = json.loads(raw.decode('utf-8')) if raw and len(raw) <= 65536 else None
            except ValueError:
                data = None
            return r.status, data if isinstance(data, dict) else None
    except Exception:
        return 0, None


def _amb_public(amb):
    if not amb:
        return None
    raw_lg = amb.get('layer_gain')
    lg = round(max(0.1, min(1.0, float(raw_lg))), 2) if (type(raw_lg) in (int, float) and math.isfinite(raw_lg)) else 0.45
    out = {k: str(amb.get(k) or '')[:80] for k in ('preset', 'name', 'primary_name', 'icon', 'desc', 'layer', 'layer_name', 'layer_icon')}
    out['layer_gain'] = lg
    if type(amb.get('seq')) is int:
        out['seq'] = amb['seq']
    return out


def _music_public(st):
    if not st:
        return None
    t = st.get('track') if isinstance(st.get('track'), dict) else None
    vol = st.get('volume')
    pm = st.get('play_mode')
    lseq = st.get('lyrics_seq')
    amb = st.get('ambient') if isinstance(st.get('ambient'), dict) else None
    return {'playing': bool(st.get('playing')), 'paused': bool(st.get('paused')),
            'elapsed': st.get('elapsed') if type(st.get('elapsed')) in (int, float) else 0,
            'duration': st.get('duration') if type(st.get('duration')) in (int, float) else 0,
            'volume': int(vol) if type(vol) in (int, float) else 55,
            'play_mode': pm if pm in ('loop', 'shuffle', 'single') else 'loop',
            'lyrics_seq': int(lseq) if type(lseq) is int else 0,
            'ambient': _amb_public(amb),
            'sleep': st.get('sleep') if isinstance(st.get('sleep'), dict) else None,
            'sleep_session': st.get('sleep_session') if isinstance(st.get('sleep_session'), dict) else None,
            'sleep_preview': st.get('sleep_preview') if isinstance(st.get('sleep_preview'), dict) else None,
            'track': {k: str(t.get(k) or '')[:300] for k in ('title', 'artist', 'cover', 'mid')} if t else None}


def make_app(inbox=None):
    inbox = inbox or Inbox()
    weather = Weather()
    clip_player = SpeakerClipPlayer()
    voice_recorder = ScreenVoiceRecorder()
    state_cache = {'at': 0.0, 'value': None}
    lyrics_etag_box = {'etag': '', 'data': None}
    state_lock = asyncio.Lock()
    state_gen = 0

    def _etag_of(raw_bytes):
        return f'"{hashlib.blake2b(raw_bytes, digest_size=8).hexdigest()}"'

    def _json_etag_resp(request, payload, sem=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        etag = _etag_of(body if sem is None else json.dumps(sem, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        return web.Response(body=body, content_type='application/json', charset='utf-8', headers={'ETag': etag})

    def _memo_resp(request, memo, key, build_fn, max_len=8):
        hit = memo.get(key)
        if hit is None:
            body = json.dumps(build_fn(), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            hit = (_etag_of(body), body)
            if len(memo) >= max_len:
                memo.clear()
            memo[key] = hit
        etag, body = hit
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        return web.Response(body=body, content_type='application/json', charset='utf-8', headers={'ETag': etag})

    def _apply_common_headers(resp, origin):
        if origin is not None:
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers['Vary'] = 'Origin'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, If-None-Match'
            resp.headers['Access-Control-Expose-Headers'] = 'ETag'
            resp.headers['Access-Control-Max-Age'] = '600'
        resp.headers.setdefault('Cache-Control', 'no-store')
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp

    @web.middleware
    async def guard(request, handler):
        if request.remote not in ('127.0.0.1', '::1'):
            raise web.HTTPForbidden()
        if (request.headers.get('Host') or '').lower() not in ALLOWED_HOSTS:
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        if origin is not None and origin not in ALLOWED_ORIGINS:
            raise web.HTTPForbidden()
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            if not (
                (origin is not None and origin in ALLOWED_ORIGINS)
                or (origin is None and request.method in ('GET', 'HEAD') and request.headers.get('Sec-Fetch-Dest') == 'iframe')
            ):
                raise web.HTTPForbidden()
        try:
            if request.method != 'OPTIONS' and request.method == 'POST':
                if request.path == '/api/home/messages/voice':
                    if request.content_type not in ('audio/wav', 'audio/x-wav', 'application/octet-stream', 'application/json'):
                        raise web.HTTPUnsupportedMediaType()
                elif request.content_type != 'application/json':
                    raise web.HTTPUnsupportedMediaType()
            resp = await handler(request)
        except web.HTTPException as exc:
            if exc.status != 403:
                _apply_common_headers(exc, origin)
            raise
        return _apply_common_headers(resp, origin)

    app = web.Application(client_max_size=768 * 1024, middlewares=[guard])

    def internal(request):
        if request.headers.get('Origin') is not None or request.headers.get('X-TS-Internal') != '1':
            raise web.HTTPForbidden()

    async def body_of(request):
        raw = await request.content.read(16385)
        if len(raw) > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
        try:
            body = json.loads(raw.decode('utf-8')) if raw else None
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        return body

    static_cache = {}

    def static_file(name, ctype, extra=None):
        async def handler(request):
            path = SHARE / name
            try:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
                    raise OSError()
                key = (st.st_ino, st.st_mtime_ns, st.st_size)
                cached = static_cache.get(name)
                if cached and cached[0] == key:
                    data, etag = cached[1], cached[2]
                else:
                    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                    try:
                        fst = os.fstat(fd)
                        if not stat.S_ISREG(fst.st_mode) or fst.st_size > 1024 * 1024:
                            raise OSError()
                        chunks, rem = [], 1024 * 1024 + 1
                        while rem > 0:
                            b = os.read(fd, min(65536, rem))
                            if not b:
                                break
                            chunks.append(b)
                            rem -= len(b)
                        data = b''.join(chunks)
                        if len(data) > 1024 * 1024:
                            raise OSError()
                        key = (fst.st_ino, fst.st_mtime_ns, fst.st_size)
                        etag = f'"{fst.st_ino:x}-{fst.st_mtime_ns:x}-{fst.st_size:x}"'
                    finally:
                        os.close(fd)
                    static_cache[name] = (key, data, etag)
            except OSError:
                raise web.HTTPNotFound()
            headers = {'Cache-Control': 'no-cache', 'ETag': etag}
            for k, v in (extra or {}).items():
                headers[k] = v
            if request.headers.get('If-None-Match') == etag:
                return web.Response(status=304, headers=headers)
            return web.Response(body=data, content_type=ctype, charset='utf-8', headers=headers)
        return handler

    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self'; "
           "img-src 'self' data: https://*.gtimg.cn http://*.gtimg.cn https://gtimg.cn https://*.qq.com http://*.qq.com https://qq.com https://*.qlogo.cn http://*.qlogo.cn https://*.qpic.cn http://*.qpic.cn; connect-src 'self'; "
           "frame-ancestors 'self' http://127.0.0.1:8090 http://localhost:8090")

    state_etag_memo = {'sem_key': None, 'etag': ''}
    inbox_state_memo = {'ver': -1, 'msgs': None}
    weather_memo = {}
    weather_detail_memo = {}
    cal_memo = {}
    inbox_memo = {}
    msgs_memo = {}

    VALID_AMBIENTS = frozenset({
        'rain', 'waves', 'forest', 'stream', 'night', 'sleep_piano',
        'fireplace', 'library', 'singing_bowl',
        'white_noise', 'pink_noise', 'brown_noise',
        'deep_study', 'cozy_read', 'deep_sleep',
    })
    ambient_owner = {'focus': False, 'timer': False}
    ambient_seen = [False]
    manual_amb_guard = [False]
    owned_amb_seq = [None]
    amb_op_gen = [0]
    focus_amb_handle = [None]
    focus_amb_retries = [0]
    focus_phase = [None]
    home_audio_ducked = [False]
    home_duck_hold = [0]
    timer_duck_count = [0]
    timer_say_sent = [False]
    voice_reply_lock = asyncio.Lock()
    app_loop = [None]

    def _clear_duck_flag_if_idle():
        if not voice_recorder.is_recording and not clip_player.is_playing and home_duck_hold[0] == 0:
            home_audio_ducked[0] = False

    def _note_owned_amb(data):
        amb_d = data.get('ambient') if isinstance(data, dict) else None
        owned_amb_seq[0] = amb_d.get('seq') if (isinstance(amb_d, dict) and type(amb_d.get('seq')) is int) else None
        if isinstance(amb_d, dict):
            ambient_seen[0] = True

    async def _duck_home_audio(seconds: float = 24.0):
        sess = app.get(SESSION)
        if sess is not None and not sess.closed:
            st_code, _ = await _post_json(sess, f'{MUSIC}/control', {'action': 'duck', 'seconds': seconds, 'source': 'home'}, 1.5)
            if st_code == 200:
                home_audio_ducked[0] = True

    async def _unduck_home_audio_if_idle():
        if not home_audio_ducked[0]:
            return
        if home_duck_hold[0] > 0 or clip_player.is_playing or voice_recorder.is_recording:
            return
        home_audio_ducked[0] = False
        sess = app.get(SESSION)
        if sess is not None and not sess.closed:
            with contextlib.suppress(Exception):
                await _post_json(sess, f'{MUSIC}/control', {'action': 'unduck', 'source': 'home'}, 1.5)

    async def _unduck_timer_study_if_idle():
        if timer_duck_count[0] > 0:
            return
        sess = app.get(SESSION)
        if sess is not None and not sess.closed:
            with contextlib.suppress(Exception):
                await _post_json(sess, f'{MUSIC}/control', {'action': 'unduck', 'source': 'study'}, 1.5)

    async def _cancel_timer_voice_say():
        if not timer_say_sent[0] or STUDY is None:
            return
        timer_say_sent[0] = False
        sess = app.get(SESSION)
        if sess is not None and not sess.closed:
            with contextlib.suppress(Exception):
                await _post_json(sess, f'{STUDY}/api/voice/say', {'cancel_tag': 'timer_alarm'}, 1.5, {'X-TS-Internal': '1'})

    def _schedule_unduck_check():
        lp = app_loop[0]
        if lp is not None and not lp.is_closed():
            with contextlib.suppress(RuntimeError):
                lp.call_soon_threadsafe(lambda: asyncio.create_task(_unduck_home_audio_if_idle()))

    def _clear_all_amb_owners():
        amb_op_gen[0] += 1
        ambient_seen[0] = False
        owned_amb_seq[0] = None
        ambient_owner['focus'] = False
        ambient_owner['timer'] = False
        _cancel_focus_amb()

    def _forget_state():
        nonlocal state_gen
        state_gen += 1
        state_cache['value'] = None
        state_etag_memo['sem_key'] = None

    def _cancel_focus_amb():
        focus_amb_retries[0] = 0
        if focus_amb_handle[0] is not None:
            focus_amb_handle[0].cancel()
            focus_amb_handle[0] = None

    async def _stop_owned_ambient(kind: str) -> bool:
        was = ambient_owner.get(kind, False)
        ambient_owner[kind] = False
        if kind == 'focus':
            _cancel_focus_amb()
        sent_off = False
        if not any(ambient_owner.values()):
            amb_op_gen[0] += 1
            my_amb_gen = amb_op_gen[0]
            exp_seq = owned_amb_seq[0]
            owned_amb_seq[0] = None
            ambient_seen[0] = False
            if was:
                sess = app.get(SESSION)
                if sess is not None and not sess.closed:
                    should_stop = True
                    if exp_seq is not None:
                        cur_st = await _get_json(sess, f'{MUSIC}/status?lite=1', 1.2)
                        if isinstance(cur_st, dict):
                            cur_amb = cur_st.get('ambient')
                            if not isinstance(cur_amb, dict) or (type(cur_amb.get('seq')) is int and cur_amb['seq'] != exp_seq):
                                should_stop = False
                    if amb_op_gen[0] != my_amb_gen or any(ambient_owner.values()) or owned_amb_seq[0] is not None:
                        should_stop = False
                    if should_stop:
                        await _post_json(sess, f'{MUSIC}/ambient', {'preset': 'off'}, 3.0)
                        sent_off = True
                    _forget_state()
        return sent_off

    async def _ensure_ambient_off(sess):
        manual_amb_guard[0] = False
        _clear_all_amb_owners()
        if sess is not None and not sess.closed:
            cur_st = await _get_json(sess, f'{MUSIC}/status?lite=1', 1.2)
            if isinstance(cur_st, dict) and cur_st.get('ambient'):
                await _post_json(sess, f'{MUSIC}/ambient', {'preset': 'off'}, 3.0)
                _forget_state()

    def _arm_focus_amb_check(delay_sec: float):
        if focus_amb_handle[0] is not None:
            focus_amb_handle[0].cancel()
            focus_amb_handle[0] = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _check():
            if not ambient_owner['focus'] or STUDY is None:
                return
            sess = app.get(SESSION)
            if sess is not None and not sess.closed:
                st = await _get_json(sess, f'{STUDY}/api/study', 1.5)
                if st is None:
                    focus_amb_retries[0] += 1
                    if focus_amb_retries[0] <= 6:
                        _arm_focus_amb_check(5.0)
                        return
                else:
                    focus_amb_retries[0] = 0
                    foc = st.get('focus') if isinstance(st.get('focus'), dict) else {}
                    if foc.get('phase') == 'focus':
                        rem = max(2.0, float(foc.get('remaining') or 2.0) + 1.0)
                        _arm_focus_amb_check(rem)
                        return
            await _stop_owned_ambient('focus')

        focus_amb_handle[0] = loop.call_later(max(1.0, delay_sec), lambda: asyncio.create_task(_check()))

    alarm_cancel_ev = asyncio.Event()

    def _alarm_stale(alarm_seq) -> bool:
        return desk_timer.phase != 'done' or (alarm_seq is not None and desk_timer.seq != alarm_seq)

    def _poke_home_display():
        if send_preview is None or inbox.path != Path('/var/lib/thinksmart-home/messages.json'):
            return
        if CALL_FLAG.exists() or MIRROR_FLAG.exists():
            return
        with contextlib.suppress(OSError):
            send_preview({'clear': True})

    async def _on_timer_alarm(label, total_sec, alarm_seq=None):
        if _alarm_stale(alarm_seq):
            return
        alarm_cancel_ev.clear()
        await _stop_owned_ambient('timer')
        if alarm_cancel_ev.is_set() or _alarm_stale(alarm_seq) or _media_busy():
            return
        if send_preview is not None:
            with contextlib.suppress(OSError):
                send_preview({'clear': True})
        sess = app.get(SESSION)
        ducked = False
        if sess is not None and not sess.closed:
            m_st = await _get_json(sess, f'{MUSIC}/status?lite=1', 1.2)
            if alarm_cancel_ev.is_set() or _alarm_stale(alarm_seq):
                return
            if isinstance(m_st, dict) and (m_st.get('playing') or m_st.get('ambient')):
                st_code, _ = await _post_json(sess, f'{MUSIC}/control', {'action': 'duck', 'source': 'study'}, 1.5)
                ducked = st_code == 200
        if alarm_cancel_ev.is_set() or _alarm_stale(alarm_seq):
            if ducked:
                await _unduck_timer_study_if_idle()
            return
        if ducked:
            timer_duck_count[0] += 1
        clip_player.play_timer_chime()
        handed_to_study = False
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(alarm_cancel_ev.wait(), 1.15)
            if alarm_cancel_ev.is_set() or _alarm_stale(alarm_seq):
                clip_player.stop()
                return
            if STUDY is not None and sess is not None and not sess.closed:
                mins = max(1, round(total_sec / 60))
                txt = f'{label}倒计时时间到啦' if label else f'{mins}分钟倒计时时间到啦'
                say_body = {'text': txt[:60], 'tag': 'timer_alarm'}
                if ducked:
                    say_body['from_duck'] = True
                timer_say_sent[0] = True
                st_code, _ = await _post_json(sess, f'{STUDY}/api/voice/say', say_body, 3.0, {'X-TS-Internal': '1'})
                if st_code != 200:
                    timer_say_sent[0] = False
                else:
                    handed_to_study = ducked
                    if alarm_cancel_ev.is_set() or _alarm_stale(alarm_seq):
                        timer_say_sent[0] = False
                        await _post_json(sess, f'{STUDY}/api/voice/say', {'cancel_tag': 'timer_alarm'}, 1.5, {'X-TS-Internal': '1'})
        finally:
            if ducked:
                timer_duck_count[0] = max(0, timer_duck_count[0] - 1)
                if not handed_to_study:
                    await _unduck_timer_study_if_idle()

    desk_timer = DeskTimer(_forget_state, _on_timer_alarm, timer_path=(inbox.path.parent / 'timer.json'))
    app[DESK_TIMER] = desk_timer

    def _on_focus_chime():
        clip_player.play_timer_chime()

    focus_timer = FocusTimer(_forget_state, _on_focus_chime, stats_path=(inbox.path.parent / 'focus.json'))
    display = DisplayControl() if DisplayControl is not None else None

    async def get_state(request):
        session = request.app[SESSION]
        fresh_ok = True
        async with state_lock:
            if state_cache['value'] is not None and time.monotonic() - state_cache['at'] < 0.8:
                value = state_cache['value']
            else:
                g = state_gen
                music = await _get_json(session, f'{MUSIC}/status?lite=1', 1.5)
                value = {'now': int(time.time()),
                         'music': _music_public(music),
                         'study': {'enabled': False, 'available': False,
                                   'focus': focus_timer.public() or {'phase': None},
                                   'today_focus': focus_timer.today()}}
                if state_gen == g:
                    state_cache.update(at=time.monotonic(), value=value)
                else:
                    fresh_ok = False
        if fresh_ok:
            focus_phase[0] = (value['study']['focus'] or {}).get('phase')
            if ambient_owner['focus'] and focus_phase[0] != 'focus':
                asyncio.create_task(_stop_owned_ambient('focus'))
            if ambient_owner['timer'] and desk_timer.phase not in ('running', 'paused'):
                asyncio.create_task(_stop_owned_ambient('timer'))
            m_pub = value.get('music')
            if m_pub is not None:
                amb_now = m_pub.get('ambient')
                if amb_now:
                    if owned_amb_seq[0] is not None and type(amb_now.get('seq')) is int and amb_now['seq'] != owned_amb_seq[0]:
                        _clear_all_amb_owners()
                        if desk_timer.phase not in ('running', 'paused') and focus_phase[0] != 'focus':
                            manual_amb_guard[0] = True
                    else:
                        ambient_seen[0] = True
                        if not any(ambient_owner.values()) and desk_timer.phase not in ('running', 'paused') and focus_phase[0] != 'focus':
                            manual_amb_guard[0] = True
                elif m_pub.get('playing') or ambient_seen[0]:
                    manual_amb_guard[0] = False
                    _clear_all_amb_owners()
        ver = inbox.version
        if inbox_state_memo['ver'] == ver and inbox_state_memo['msgs'] is not None:
            msgs = inbox_state_memo['msgs']
        else:
            msgs = {'recent': inbox.recent(3), 'unread': len(inbox.unread())}
            inbox_state_memo.update(ver=ver, msgs=msgs)
        tmr = desk_timer.public()
        amb_owned = bool(any(ambient_owner.values()))
        sem_key = (value['music'], value['study'], tmr, ver, amb_owned)
        if state_etag_memo['sem_key'] == sem_key:
            etag = state_etag_memo['etag']
        else:
            sem_bytes = json.dumps({'music': value['music'], 'study': value['study'], 'timer': tmr, 'messages': msgs, 'ambient_owned': amb_owned},
                                   ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            etag = _etag_of(sem_bytes)
            state_etag_memo.update(sem_key=sem_key, etag=etag)
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers={'ETag': etag})
        out_val = dict(value, messages=msgs, timer=tmr, ambient_owned=amb_owned)
        body = json.dumps(out_val, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return web.Response(body=body, content_type='application/json', charset='utf-8', headers={'ETag': etag})

    async def get_weather(request):
        w, w_key = await weather.get_with_seq(request.app[SESSION], force=(request.query.get('force') == '1'))
        return _memo_resp(request, weather_memo, w_key, lambda: w, 4)

    async def get_weather_detail(request):
        raw_city = (request.query.get('city') or '').strip()
        allowed = weather_cities()
        if raw_city and raw_city not in allowed:
            raise web.HTTPBadRequest()
        target = raw_city or allowed[0]
        w, w_seq = await weather.get_detail_with_seq(request.app[SESSION], force=(request.query.get('force') == '1'), city=target)
        return _memo_resp(request, weather_detail_memo, (target, w_seq), lambda: w, 12)

    async def get_calendar(request):
        ym = request.query.get('ym') or dt.datetime.now(LOCAL_TZ).strftime('%Y-%m')
        m = re.fullmatch(r'(\d{4})-(\d{2})', ym)
        if not m or not 1 <= int(m.group(2)) <= 12 or not 1950 <= int(m.group(1)) <= 2099:
            raise web.HTTPBadRequest()
        y, mo = int(m.group(1)), int(m.group(2))
        today_iso = dt.datetime.now(LOCAL_TZ).date().isoformat()
        grid = _MONTH_CACHE.get((y, mo, today_iso))
        today = _TODAY_CACHE.get(today_iso)
        if grid is None or today is None:
            grid, today = await asyncio.get_running_loop().run_in_executor(_IO_POOL, lambda: (month_grid(y, mo), today_info()))
        return _memo_resp(request, cal_memo, (y, mo, today_iso), lambda: {'month': grid, 'today': today}, 8)

    async def get_music_lyrics(request):
        data = await _get_json(request.app[SESSION], f'{MUSIC}/lyrics', 2.0, max_bytes=262144, etag_box=lyrics_etag_box)
        if not isinstance(data, dict):
            return web.json_response({'ok': False, 'seq': 0, 'lyrics': []})
        raw_lrc = data.get('lyrics') if isinstance(data.get('lyrics'), list) else []
        clean_lrc = []
        for row in raw_lrc[:300]:
            if isinstance(row, dict) and type(row.get('time')) in (int, float) and isinstance(row.get('text'), str):
                txt = row['text'].strip()[:160]
                if txt:
                    clean_lrc.append([round(float(row['time']), 2), txt])
        payload = {'ok': True, 'seq': int(data.get('seq') or 0), 'mid': str(data.get('mid') or '')[:64], 'lyrics': clean_lrc}
        return _json_etag_resp(request, payload, sem=(payload['seq'], payload['mid'], len(clean_lrc)))

    async def post_music(request):
        body = await body_of(request)
        action = body.get('action')
        session = request.app[SESSION]
        if action == 'seek':
            raw_sec = body.get('sec')
            if isinstance(raw_sec, bool) or not isinstance(raw_sec, (int, float)):
                raise web.HTTPBadRequest()
            sec_f = float(raw_sec)
            if not math.isfinite(sec_f) or sec_f < 0:
                raise web.HTTPBadRequest()
            status, data = await _post_json(session, f'{MUSIC}/seek', {'sec': round(min(sec_f, 86400.0), 2)}, 6.0)
            _forget_state()
            ok = status == 200 and bool(data and data.get('ok'))
            return web.json_response(
                {'ok': ok, 'elapsed': data.get('elapsed') if isinstance(data, dict) else None},
                status=200 if ok else 400,
            )
        if action == 'volume':
            raw_v = body.get('volume')
            if isinstance(raw_v, bool) or not isinstance(raw_v, (int, float)):
                raise web.HTTPBadRequest()
            vf = float(raw_v)
            if not math.isfinite(vf):
                raise web.HTTPBadRequest()
            v = max(0, min(100, int(round(vf))))
            status, data = await _post_json(session, f'{MUSIC}/volume', {'volume': v}, 4.0)
            _forget_state()
            ok = status == 200 and bool(data and data.get('ok'))
            return web.json_response({'ok': ok, 'volume': data.get('volume', v) if isinstance(data, dict) else v}, status=200 if ok else 400)
        if action == 'ambient':
            if not MUSIC_AMBIENT:   # ambient mixes were the QQ-music service's; NetEase has none
                return web.json_response({'ok': False, 'unsupported': True, 'message': '当前音乐服务不支持环境音'},
                                         status=400)
            preset = body.get('preset')
            if not isinstance(preset, str) or not preset.strip() or len(preset) > 24:
                raise web.HTTPBadRequest()
            p_clean = preset.strip()
            p_low = p_clean.lower()
            is_off = p_low in ('off', 'none', 'stop', '0', '关闭', '停止', '静音')
            rebind_owner = body.get('rebind_owner') is True
            if is_off:
                manual_amb_guard[0] = False
                _clear_all_amb_owners()
                _clear_duck_flag_if_idle()
            else:
                _clear_all_amb_owners()
                clip_player.stop()
                _clear_duck_flag_if_idle()
            my_amb_gen = amb_op_gen[0]
            fwd = {'preset': p_clean}
            if 'layer' in body and body.get('layer') is not None:
                rl = body.get('layer')
                if not isinstance(rl, str) or len(rl) > 24:
                    raise web.HTTPBadRequest()
                fwd['layer'] = rl.strip()
            if 'layer_gain' in body and body.get('layer_gain') is not None:
                rlg = body.get('layer_gain')
                if isinstance(rlg, bool) or not isinstance(rlg, (int, float)) or not math.isfinite(float(rlg)):
                    raise web.HTTPBadRequest()
                lg_val = float(rlg)
                if lg_val > 1.001:
                    lg_val = lg_val / 100.0
                fwd['layer_gain'] = round(max(0.10, min(1.00, lg_val)), 2)
            if 'volume' in body:
                rv = body.get('volume')
                if isinstance(rv, bool) or not isinstance(rv, (int, float)) or not math.isfinite(float(rv)):
                    raise web.HTTPBadRequest()
                fwd['volume'] = max(0, min(100, int(round(float(rv)))))
            status, data = await _post_json(session, f'{MUSIC}/ambient', fwd, 5.0)
            ok = status == 200 and bool(data and data.get('ok'))
            if ok and not is_off and amb_op_gen[0] == my_amb_gen and (p_low in VALID_AMBIENTS or (isinstance(data, dict) and data.get('ambient'))):
                ambient_seen[0] = False
                allow_bind = rebind_owner or not manual_amb_guard[0]
                has_owner = False
                if allow_bind and desk_timer.phase in ('running', 'paused'):
                    ambient_owner['timer'] = True
                    has_owner = True
                elif allow_bind and focus_phase[0] == 'focus':
                    ambient_owner['focus'] = True
                    has_owner = True
                    if focus_amb_handle[0] is None:
                        _arm_focus_amb_check(15.0)
                if has_owner:
                    manual_amb_guard[0] = False
                    _note_owned_amb(data)
                else:
                    manual_amb_guard[0] = True
                    ambient_seen[0] = True
            _forget_state()
            return web.json_response(data or {'ok': False}, status=200 if ok else 400)
        if action == 'toggle':
            st = await _get_json(session, f'{MUSIC}/status?lite=1', 1.5)
            if st and st.get('ambient'):
                manual_amb_guard[0] = False
                _clear_all_amb_owners()
                _clear_duck_flag_if_idle()
                status, data = await _post_json(session, f'{MUSIC}/ambient', {'preset': 'off'}, 4.0)
                _forget_state()
                return web.json_response({'ok': status == 200 and bool(data and data.get('ok'))})
            action = 'pause' if st and st.get('playing') else 'resume'
        if action not in ('pause', 'resume', 'next', 'prev', 'mode', 'loop', 'shuffle', 'single'):
            raise web.HTTPBadRequest()
        if action in ('pause', 'resume', 'next', 'prev'):
            manual_amb_guard[0] = False
            _clear_all_amb_owners()
            if action != 'pause':
                clip_player.stop()
            _clear_duck_flag_if_idle()
        status, data = await _post_json(session, f'{MUSIC}/control', {'action': action}, 8.0)
        _forget_state()
        ok = status == 200 and bool(data and data.get('ok'))
        return web.json_response({
            'ok': ok,
            'open': not ok and action == 'resume',
            'play_mode': data.get('play_mode') if isinstance(data, dict) else None,
            'message': data.get('message') if isinstance(data, dict) else None,
        })

    async def post_sleep(request):
        body = await body_of(request)
        fwd = {k: body[k] for k in ('minutes', 'tracks', 'cancel') if k in body}
        if len(fwd) != 1:
            raise web.HTTPBadRequest()
        if not MUSIC_AMBIENT:   # sleep timers belonged to the QQ-music service's sleep feature
            return web.json_response({'ok': False, 'unsupported': True, 'message': '当前音乐服务不支持睡眠定时'}, status=400)
        status, data = await _post_json(request.app[SESSION], f'{MUSIC}/sleep', fwd)
        _forget_state()
        return web.json_response(data or {'ok': False}, status=200 if status == 200 else 400)

    async def sleepmode_api(request):
        # 2026-11-12 (Claude): sleep sessions live in the music service; this only forwards checked requests.
        session = request.app[SESSION]
        if not MUSIC_AMBIENT:   # sleep sessions (sounds + own volume + night view) are QQ-music only
            return web.json_response({'ok': False, 'unsupported': True, 'message': '当前音乐服务不支持睡眠模式'}, status=400)
        if request.method == 'GET':
            data = await _get_json(session, f'{MUSIC}/sleep_session', 4.0)
            return web.json_response(data or {'ok': False}, status=200 if data else 502)
        body = await body_of(request)
        act = body.get('action')
        if act not in ('start', 'stop', 'extend', 'screen_off', 'preview', 'preview_stop', 'night_view', 'wake'):
            raise web.HTTPBadRequest()
        if act in ('night_view', 'wake'):   # the night view is up / gone; the 温柔叫醒 switch and its time
            fwd = {'action': act}
            if isinstance(body.get('on'), bool):
                fwd['on'] = body['on']
            if act == 'wake' and isinstance(body.get('time'), str) and len(body['time']) <= 5:
                fwd['time'] = body['time']
            if len(fwd) == 1 or (act == 'night_view' and 'on' not in fwd):
                raise web.HTTPBadRequest()
            status, data = await _post_json(session, f'{MUSIC}/sleep_session', fwd, 5.0)
            if act == 'wake':
                _forget_state()
            return web.json_response(data or {'ok': False}, status=200 if status == 200 else 400)

        def num(v):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise web.HTTPBadRequest()
            return v

        fwd = {'action': act}
        if act in ('start', 'preview'):
            snd = body.get('sound')
            if isinstance(snd, str):
                if len(snd) > 24:
                    raise web.HTTPBadRequest()
                fwd['sound'] = snd
            elif isinstance(snd, dict):
                clean = {}
                for k in ('kind', 'preset', 'name', 'id', 'layer'):
                    v = snd.get(k)
                    if v is not None:
                        if not isinstance(v, str) or len(v) > 24:
                            raise web.HTTPBadRequest()
                        clean[k] = v
                if snd.get('layer_gain') is not None:
                    clean['layer_gain'] = num(snd['layer_gain'])
                fwd['sound'] = clean
            elif snd is not None:
                raise web.HTTPBadRequest()
            for k in ('minutes', 'fade', 'volume', 'seconds'):
                if body.get(k) is not None:
                    fwd[k] = num(body[k])
            if body.get('until') is not None:
                if not isinstance(body['until'], str) or len(body['until']) > 8:
                    raise web.HTTPBadRequest()
                fwd['until'] = body['until']
            if body.get('night') is True:
                fwd['night'] = True
            # the sound belongs to the sleep session (or its preview) now, not to a focus or a desk timer
            manual_amb_guard[0] = False
            _clear_all_amb_owners()
            clip_player.stop()
            _clear_duck_flag_if_idle()
        elif act == 'extend':
            fwd['minutes'] = num(body.get('minutes', 30))
        status, data = await _post_json(session, f'{MUSIC}/sleep_session', fwd, 20.0)
        _forget_state()
        return web.json_response(data or {'ok': False}, status=200 if status == 200 else 400)

    async def post_study(request):
        body = await body_of(request)
        if type(body.get('enabled')) is not bool:
            raise web.HTTPBadRequest()
        # The study companion service is not deployed (the pomodoro runs locally, see post_focus).
        if STUDY is not None:
            status, data = await _post_json(request.app[SESSION], f'{STUDY}/api/study/toggle', {'enabled': body['enabled']})
            return web.json_response({'ok': status == 200 and bool(data and data.get('ok')),
                                      'enabled': bool(data and data.get('enabled'))})
        return web.json_response({'ok': False, 'unsupported': True, 'enabled': False,
                                  'message': '学习陪伴未部署'})

    def _clean_amb_layer_args(src: dict) -> dict:
        extra = {}
        rl = src.get('layer')
        if isinstance(rl, str) and 0 < len(rl.strip()) <= 24:
            extra['layer'] = rl.strip()
        rlg = src.get('layer_gain')
        if not isinstance(rlg, bool) and isinstance(rlg, (int, float)) and math.isfinite(float(rlg)):
            lg_val = float(rlg)
            if lg_val > 1.001:
                lg_val = lg_val / 100.0
            extra['layer_gain'] = round(max(0.10, min(1.00, lg_val)), 2)
        return extra

    async def post_focus(request):
        body = await body_of(request)
        act = str(body.get('action') or '').strip().lower()
        if act == 'start':
            raw_m = body.get('minutes', 25)
            raw_b = body.get('break_minutes', 0)
            for v in (raw_m, raw_b):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    raise web.HTTPBadRequest()
            alarm_cancel_ev.set()
            desk_timer.stop()
            await _cancel_timer_voice_say()
            focus_timer.start(int(raw_m), int(raw_b), bool(body.get('with_study')), str(body.get('label') or ''))
            focus_phase[0] = 'focus'
            _forget_state()
            pub = focus_timer.public() or {}
            return web.json_response({'ok': True, **pub, 'today_focus': focus_timer.today()})
        if act == 'pause':
            res = focus_timer.pause()
            _forget_state()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'resume':
            res = focus_timer.resume()
            if res.get('ok'):
                focus_phase[0] = res.get('phase')
            _forget_state()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'stop':
            focus_phase[0] = None
            res = focus_timer.stop()
            await _stop_owned_ambient('focus')
            _forget_state()
            return web.json_response(res)
        raise web.HTTPBadRequest()

    async def timer_api(request):
        if request.method == 'GET':
            return web.json_response({'ok': True, 'timer': desk_timer.public()})
        body = await body_of(request)
        act = str(body.get('action') or '').strip().lower()
        if act == 'start':
            lbl = body.get('label', '')
            if not isinstance(lbl, str):
                raise web.HTTPBadRequest()
            amb = str(body.get('ambient') or '').strip().lower()
            is_sw = str(body.get('mode') or '').strip().lower() == 'stopwatch'
            sec = 0
            if not is_sw:
                raw_s = body.get('seconds')
                if isinstance(raw_s, bool) or not isinstance(raw_s, (int, float)) or not math.isfinite(float(raw_s)):
                    raise web.HTTPBadRequest()
                sec = int(round(float(raw_s)))
                if not (5 <= sec <= 10800):
                    raise web.HTTPBadRequest()
            alarm_cancel_ev.set()
            res = desk_timer.start_stopwatch(lbl) if is_sw else desk_timer.start(sec, lbl)
            _poke_home_display()
            await _cancel_timer_voice_say()
            cached_foc = ((state_cache.get('value') or {}).get('study') or {}).get('focus', {}).get('phase')
            need_stop_foc = bool(focus_phase[0] or cached_foc or (focus_timer.public() or {}).get('phase'))
            if need_stop_foc:
                focus_phase[0] = None
                focus_timer.stop()
            if amb in VALID_AMBIENTS:
                manual_amb_guard[0] = False
                clip_player.stop()
                _clear_duck_flag_if_idle()
                _clear_all_amb_owners()
                my_amb_gen = amb_op_gen[0]
                amb_fwd = {'preset': amb, **_clean_amb_layer_args(body)}
                amb_st, amb_data = await _post_json(request.app[SESSION], f'{MUSIC}/ambient', amb_fwd, 4.0)
                if amb_st == 200 and isinstance(amb_data, dict) and amb_data.get('ok'):
                    if amb_op_gen[0] == my_amb_gen and desk_timer.phase in ('running', 'paused'):
                        ambient_owner['timer'] = True
                        _note_owned_amb(amb_data)
                    elif amb_op_gen[0] == my_amb_gen and desk_timer.phase not in ('running', 'paused'):
                        await _post_json(request.app[SESSION], f'{MUSIC}/ambient', {'preset': 'off'}, 3.0)
            else:
                s_f = await _stop_owned_ambient('focus')
                s_t = await _stop_owned_ambient('timer')
                if amb in ('off', 'none', 'stop', '0', '关闭', '停止', '静音') and not (s_f or s_t):
                    await _ensure_ambient_off(request.app[SESSION])
                elif amb in ('off', 'none', 'stop', '0', '关闭', '停止', '静音'):
                    manual_amb_guard[0] = False
            _forget_state()
            return web.json_response(res)
        if act == 'lap':
            res = desk_timer.lap()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'label':
            lbl = body.get('label', '')
            if not isinstance(lbl, str):
                raise web.HTTPBadRequest()
            res = desk_timer.set_label(lbl)
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'pause':
            res = desk_timer.pause()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'resume':
            res = desk_timer.resume()
            if res.get('ok'):
                _poke_home_display()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act == 'add':
            raw_d = body.get('seconds', 60)
            if isinstance(raw_d, bool) or not isinstance(raw_d, (int, float)) or not math.isfinite(float(raw_d)):
                raise web.HTTPBadRequest()
            was_done = desk_timer.phase == 'done'
            if was_done:
                alarm_cancel_ev.set()
                clip_player.stop()
            res = desk_timer.add_seconds(int(round(float(raw_d))))
            if was_done:
                await _cancel_timer_voice_say()
            return web.json_response(res, status=200 if res.get('ok') else 400)
        if act in ('stop', 'cancel', 'ack'):
            alarm_cancel_ev.set()
            clip_player.stop()
            res = desk_timer.stop()
            await _cancel_timer_voice_say()
            await _stop_owned_ambient('timer')
            return web.json_response(res)
        raise web.HTTPBadRequest()

    async def get_inbox(request):
        return _memo_resp(request, inbox_memo, inbox.version,
                          lambda: (lambda u: {'count': len(u), 'unread': u[:5]})(inbox.unread()), 2)

    async def post_ack(request):
        body = await body_of(request)
        msg_id = body.get('id')
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        with inbox.lock:
            cur_m = next((m for m in inbox.items if m['id'] == msg_id
                          and (m.get('source', 'phone') == 'phone' or (m.get('due') and m.get('fired')))), None)
        if cur_m is None:
            raise web.HTTPNotFound()
        if cur_m.get('kind') == 'voice':
            if voice_recorder.is_recording:
                return web.json_response({'ok': False, 'error': '正在录音，请先发送或取消'}, status=409)
            if _media_busy():
                return web.json_response({'ok': False, 'error': '当前正在视频通话或投屏，暂时无法播放语音'}, status=400)
        try:
            item, first = inbox.ack(msg_id, expected_source=None if cur_m.get('due') else 'phone')
        except OSError:
            return web.json_response({'ok': False, 'error': '留言状态保存失败，请稍后重试'}, status=503)
        if item is None:
            raise web.HTTPNotFound()
        if not voice_recorder.is_recording:
            if item.get('kind') == 'voice':
                duck_sec = max(24.0, min(65.0, float(item.get('duration') or 20.0) + 4.0))
                home_duck_hold[0] += 1
                played_ok = False
                try:
                    clip_player.stop()
                    await _duck_home_audio(duck_sec)
                    played_ok = clip_player.play_mp3_file(inbox.voice_dir / f'{msg_id}.mp3', on_done=_schedule_unduck_check)
                finally:
                    home_duck_hold[0] = max(0, home_duck_hold[0] - 1)
                if not played_ok:
                    await _unduck_home_audio_if_idle()
            elif first and not item.get('due'):  # a reminder was already read out when it came up
                clip_player.stop()
                await _unduck_home_audio_if_idle()
                # read the message out once (shared voice channel: never over XiaoZhi / a call / AirPlay)
                if STUDY is not None:
                    status, _ = await _post_json(request.app[SESSION], f'{STUDY}/api/voice/say',
                                                 {'text': f'{item.get("who") or ""}留言：{item["text"]}'[:240]}, 3.0, {'X-TS-Internal': '1'})
                    if status != 200:
                        LOG.warning('message read-out not queued (HTTP %s)', status)
        _forget_state()
        return web.json_response({'ok': True, 'message': item})

    async def get_messages(request):
        try:
            n = max(1, min(MAX_MSGS, int(request.query.get('limit', 20))))
        except ValueError:
            raise web.HTTPBadRequest()
        return _memo_resp(request, msgs_memo, (inbox.version, n),
                          lambda: {'messages': inbox.recent(n), 'quick': list(inbox.quick), 'family': list(FAMILY)}, 6)

    def _who_of(v):
        return v if isinstance(v, str) and v in FAMILY else ''

    async def _sleep_on():
        sess = app.get(SESSION)
        if sess is None or sess.closed:
            return False
        st = await _get_json(sess, f'{MUSIC}/status?lite=1', 1.2)
        return isinstance(st, dict) and isinstance(st.get('sleep_session'), dict)

    async def _wake_for_message():
        """A new phone message lights the screen, except while a sleep session is on (Product spec 11-12)."""
        if send_preview is None:
            return
        if await _sleep_on():
            LOG.info('new message during sleep mode: the screen stays dark')
            return
        with contextlib.suppress(OSError):
            send_preview({'clear': True})   # wake the screen (same channel as the brightness panel)

    async def post_message(request):
        internal(request)
        body = await body_of(request)
        text = body.get('text')
        if not isinstance(text, str):
            raise web.HTTPBadRequest()
        due = body.get('due')
        if due is not None and (isinstance(due, bool) or not isinstance(due, (int, float))):
            raise web.HTTPBadRequest()
        rp = body.get('repeat')
        if rp is not None and (not isinstance(rp, str) or len(rp) > 12):
            raise web.HTTPBadRequest()
        days = body.get('days')
        if days is not None and not (isinstance(days, list) and len(days) <= 7):
            raise web.HTTPBadRequest()
        try:
            item = inbox.add(text, 'phone', _who_of(body.get('who')), '', due, rp, days)
        except OverflowError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=429)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except OSError:
            return web.json_response({'ok': False, 'error': '留言保存失败，请稍后重试'}, status=503)
        _forget_state()
        LOG.info('new message (%d chars%s)', len(item['text']), (', reminder ' + (item.get('repeat') or 'once')) if item.get('due') else '')
        if not item.get('due'):
            await _wake_for_message()
        return web.json_response({'ok': True, 'message': item})

    async def post_message_voice(request):
        internal(request)
        if request.content_type == 'application/json':
            body = await body_of(request)
            b64 = body.get('wav_b64')
            if not isinstance(b64, str) or len(b64) > 920000:
                raise web.HTTPBadRequest()
            try:
                raw_wav = base64.b64decode(b64, validate=True)
            except Exception:
                raise web.HTTPBadRequest()
        else:
            buf = bytearray()
            async for part in request.content.iter_chunked(65536):
                buf.extend(part)
                if len(buf) > MAX_VOICE_WAV:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_VOICE_WAV, actual_size=len(buf))
            raw_wav = bytes(buf)
        try:
            inbox.check_rate()
            mp3_bytes, dur = await asyncio.get_running_loop().run_in_executor(_IO_POOL, _transcode_to_voice_mp3, raw_wav, False)
            item = inbox.add_voice(mp3_bytes, dur, 'phone', _who_of(request.query.get('who')))
        except OverflowError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=429)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except OSError:
            return web.json_response({'ok': False, 'error': '语音保存失败，请稍后重试'}, status=503)
        _forget_state()
        LOG.info('new phone voice message (%.1fs, %dB)', dur, len(mp3_bytes))
        _start_transcript(item['id'])
        await _wake_for_message()
        return web.json_response({'ok': True, 'message': item})

    async def get_message_audio(request):
        msg_id = request.match_info.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPNotFound()
        etag = f'"v-{msg_id}"'
        if request.headers.get('If-None-Match') == etag:
            with inbox.lock:
                has_msg = any(m.get('id') == msg_id and m.get('kind') == 'voice' for m in inbox.items)
            if not has_msg:
                raise web.HTTPNotFound()
            return web.Response(status=304, headers={'ETag': etag, 'Cache-Control': 'private, max-age=3600'})
        try:
            mp3 = await asyncio.get_running_loop().run_in_executor(_IO_POOL, inbox.read_voice_bytes, msg_id)
        except FileNotFoundError:
            raise web.HTTPNotFound()
        return web.Response(
            body=mp3,
            content_type='audio/mpeg',
            headers={'ETag': etag, 'Cache-Control': 'private, max-age=3600', 'Content-Disposition': 'inline'},
        )

    async def post_message_play(request):
        body = await body_of(request)
        msg_id = body.get('id')
        if body.get('stop') is True:
            clip_player.stop()
            await _unduck_home_audio_if_idle()
            return web.json_response({'ok': True, 'playing': False})
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        if voice_recorder.is_recording:
            return web.json_response({'ok': False, 'error': '正在录音，请先发送或取消'}, status=409)
        if _media_busy():
            return web.json_response({'ok': False, 'error': '当前正在视频通话或投屏，暂时无法播放语音'}, status=400)
        try:
            _ = await asyncio.get_running_loop().run_in_executor(_IO_POOL, inbox.read_voice_bytes, msg_id)
        except FileNotFoundError:
            raise web.HTTPNotFound()
        cur = None
        with inbox.lock:
            cur = next((m for m in inbox.items if m['id'] == msg_id), None)
        duck_sec = max(24.0, min(65.0, float((cur or {}).get('duration') or 20.0) + 4.0))
        home_duck_hold[0] += 1
        ok = False
        try:
            clip_player.stop()
            await _duck_home_audio(duck_sec)
            ok = clip_player.play_mp3_file(inbox.voice_dir / f'{msg_id}.mp3', on_done=_schedule_unduck_check)
        finally:
            home_duck_hold[0] = max(0, home_duck_hold[0] - 1)
        item = None
        if ok and cur and cur.get('source', 'phone') == 'phone' and cur.get('read_at') is None:
            with contextlib.suppress(OSError):
                item, _ = inbox.ack(msg_id, expected_source='phone')
                _forget_state()
        if not ok:
            await _unduck_home_audio_if_idle()
        return web.json_response({'ok': ok, 'message': item})

    async def post_message_ack_phone(request):
        internal(request)
        body = await body_of(request)
        msg_id = body.get('id')
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        try:
            item, _ = inbox.ack(msg_id, expected_source='screen')
        except OSError:
            return web.json_response({'ok': False, 'error': '状态保存失败'}, status=503)
        if item is None:
            raise web.HTTPNotFound()
        _forget_state()
        return web.json_response({'ok': True, 'message': item})

    async def post_screen_reply(request):
        body = await body_of(request)
        text = body.get('text')
        if not isinstance(text, str):
            raise web.HTTPBadRequest()
        try:
            item = inbox.add(text, 'screen', _who_of(body.get('who')), 'home' if body.get('tag') == 'home' else '')
        except OverflowError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=429)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except OSError:
            return web.json_response({'ok': False, 'error': '回复保存失败，请稍后重试'}, status=503)
        _forget_state()
        LOG.info('new screen text reply (%d chars)', len(item['text']))
        return web.json_response({'ok': True, 'message': item})

    async def post_voice_reply(request):
        body = await body_of(request)
        act = str(body.get('action') or '').strip().lower()
        if act not in ('start', 'cancel', 'stop'):
            raise web.HTTPBadRequest()
        async with voice_reply_lock:
            if act == 'start':
                if _media_busy():
                    return web.json_response({'ok': False, 'error': '当前正在视频通话或投屏，暂时无法录语音留言'}, status=400)
                try:
                    inbox.check_rate()
                except OverflowError as exc:
                    return web.json_response({'ok': False, 'error': str(exc)}, status=429)
                home_duck_hold[0] += 1
                started_ok = False
                try:
                    clip_player.stop()
                    await _duck_home_audio(66.0)
                    res = await voice_recorder.start(on_done=_schedule_unduck_check)
                    started_ok = True
                    return web.json_response(res)
                except Exception as exc:
                    return web.json_response({'ok': False, 'error': str(exc) or '麦克风暂时不可用'}, status=400)
                finally:
                    home_duck_hold[0] = max(0, home_duck_hold[0] - 1)
                    if not started_ok:
                        if voice_recorder.is_recording:
                            with contextlib.suppress(Exception):
                                await asyncio.shield(voice_recorder.cancel())
                        with contextlib.suppress(Exception):
                            await asyncio.shield(_unduck_home_audio_if_idle())
            if act == 'cancel':
                res = await voice_recorder.cancel()
                await _unduck_home_audio_if_idle()
                return web.json_response(res)
            try:
                mp3_bytes, dur = await voice_recorder.finish_to_mp3()
                item = inbox.add_voice(mp3_bytes, dur, 'screen', _who_of(body.get('who')))
            except OverflowError as exc:
                return web.json_response({'ok': False, 'error': str(exc)}, status=429)
            except ValueError as exc:
                return web.json_response({'ok': False, 'error': str(exc)}, status=400)
            except OSError:
                return web.json_response({'ok': False, 'error': '语音回复保存失败'}, status=503)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.shield(_unduck_home_audio_if_idle())
            _forget_state()
            LOG.info('new screen voice reply (%.1fs, %dB)', dur, len(mp3_bytes))
            _start_transcript(item['id'])
            return web.json_response({'ok': True, 'message': item})

    # ---- 2026-11-12 (Claude): transcripts, quick replies, reminders, 小麦's tool ----
    transcribe_tasks = set()

    async def _transcribe(msg_id):
        cfg = asr_endpoint()
        if not cfg:
            return
        for attempt in range(2):
            try:
                loop = asyncio.get_running_loop()
                wav = await loop.run_in_executor(_IO_POOL, _mp3_to_wav16k, inbox.voice_dir / f'{msg_id}.mp3')
                pieces = await loop.run_in_executor(_IO_POOL, _wav_pieces, wav)
                texts = []
                for piece in pieces:
                    form = aiohttp.FormData()
                    form.add_field('file', piece, filename='audio.wav', content_type='audio/wav')
                    for k, v in (('model', cfg['model']), ('language', cfg['language']), ('prompt', cfg['prompt'])):
                        if v:
                            form.add_field(k, v)
                    hdrs = {'Authorization': f'Bearer {cfg["key"]}'} if cfg['key'] else {}
                    async with app[SESSION].post(cfg['url'], data=form, headers=hdrs, timeout=aiohttp.ClientTimeout(total=40)) as r:
                        raw = await r.content.read(65536)
                        if r.status != 200:
                            raise OSError(f'HTTP {r.status}')
                    payload = json.loads(raw.decode('utf-8'))
                    texts.append(str(payload.get('text') or '').strip() if isinstance(payload, dict) else '')
                # each piece ends in 。 even when it was cut mid-sentence: join inner pieces with ，
                text = ''.join((t[:-1] + '，') if (i < len(texts) - 1 and t.endswith('。')) else t for i, t in enumerate(texts)).strip()
                if inbox.set_transcript(msg_id, text):
                    _forget_state()
                    LOG.info('voice message transcribed (%d chars)' if text else 'voice message: no words recognised (%d)', len(text))
                return
            except Exception as exc:
                LOG.warning('voice transcript failed (%s)%s', type(exc).__name__, '; retrying' if attempt == 0 else '')
                if attempt == 0:
                    await asyncio.sleep(20)

    def _start_transcript(msg_id):
        t = asyncio.create_task(_transcribe(msg_id))
        transcribe_tasks.add(t)
        t.add_done_callback(transcribe_tasks.discard)

    async def _fire_reminders():
        for m in inbox.fire_due():
            _forget_state()
            if await _sleep_on():   # Product spec 11-12: sleep mode is on: it waits on the screen, dark and silent
                LOG.info('reminder shown quietly (sleep mode, %d chars)', len(m['text']))
                continue
            if send_preview is not None:
                with contextlib.suppress(OSError):
                    send_preview({'clear': True})
            if time.time() - (m.get('due') or 0) <= 3600:   # read it out once (not when it is hours late)
                who = m.get('who') or ''
                say = f'{who}，提醒你：{m["text"]}' if who else f'提醒：{m["text"]}'
                if STUDY is not None:
                    status, _ = await _post_json(app[SESSION], f'{STUDY}/api/voice/say', {'text': say[:240]}, 3.0, {'X-TS-Internal': '1'})
                    if status != 200:
                        LOG.warning('reminder read-out not queued (HTTP %s)', status)
            LOG.info('reminder shown (%d chars)', len(m['text']))

    app[FIRE_REMINDERS] = _fire_reminders

    async def post_message_quick(request):
        internal(request)
        body = await body_of(request)
        try:
            quick = inbox.set_quick(body.get('quick'))
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except OSError:
            return web.json_response({'ok': False, 'error': '保存失败，请稍后重试'}, status=503)
        _forget_state()
        return web.json_response({'ok': True, 'quick': quick})

    async def post_message_recall(request):
        """Take a message back within 2 minutes: screen replies from the screen, phone messages from the phone."""
        body = await body_of(request)
        msg_id = body.get('id')
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        with inbox.lock:
            cur = next((m for m in inbox.items if m['id'] == msg_id), None)
        if cur is None:
            return web.json_response({'ok': False, 'error': '这条留言已经不在了'}, status=404)
        if cur.get('due'):
            return web.json_response({'ok': False, 'error': '提醒请点「取消」'}, status=400)
        if cur.get('source') == 'phone':
            internal(request)   # the phone (through the intercom relay) takes back its own
        try:
            inbox.remove(msg_id, source=cur.get('source'), max_age=RECALL_SEC)
        except LookupError:
            return web.json_response({'ok': False, 'error': '这条留言已经不在了'}, status=404)
        except ValueError as exc:
            return web.json_response({'ok': False, 'error': str(exc)}, status=400)
        except OSError:
            return web.json_response({'ok': False, 'error': '保存失败，请稍后重试'}, status=503)
        _forget_state()
        LOG.info('message taken back (%s)', cur.get('source'))
        return web.json_response({'ok': True})

    async def post_message_delete(request):
        """The phone deletes a message (any age; a reminder still to come is cancelled with it)."""
        internal(request)
        body = await body_of(request)
        msg_id = body.get('id')
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        try:
            inbox.remove(msg_id)
        except LookupError:
            return web.json_response({'ok': False, 'error': '这条留言已经不在了'}, status=404)
        except OSError:
            return web.json_response({'ok': False, 'error': '保存失败，请稍后重试'}, status=503)
        _forget_state()
        LOG.info('message deleted from the phone')
        return web.json_response({'ok': True})

    async def post_message_cancel(request):
        body = await body_of(request)
        msg_id = body.get('id')
        if not isinstance(msg_id, str) or not re.fullmatch(r'[0-9a-f]{12}', msg_id):
            raise web.HTTPBadRequest()
        try:
            ok = inbox.cancel_reminder(msg_id)
        except OSError:
            return web.json_response({'ok': False, 'error': '保存失败，请稍后重试'}, status=503)
        _forget_state()
        return web.json_response({'ok': ok}, status=200 if ok else 404)

    def _when_txt(t, m=None):
        d, now = dt.datetime.fromtimestamp(t), dt.datetime.now()
        if m is not None and repeat_label(m):
            return f'{repeat_label(m)} {d.hour}:{d.minute:02d}'
        day = '今天' if d.date() == now.date() else ('明天' if d.date() == (now + dt.timedelta(days=1)).date() else f'{d.month}月{d.day}日')
        return f'{day} {d.hour}:{d.minute:02d}'

    async def post_message_tool(request):
        """小麦 (voice bridge): send a message to the phones, read unread ones (no extra read-out), reminders."""
        internal(request)
        body = await body_of(request)
        act = body.get('action')
        try:
            if act == 'send':
                item = inbox.add(str(body.get('text') or ''), 'screen', _who_of(body.get('from')))
                _forget_state()
                return web.json_response({'ok': True, 'text': '好的，已经留言到手机上了'})
            if act == 'read':
                unread = inbox.unread()
                if not unread:
                    return web.json_response({'ok': True, 'text': '没有新留言'})
                parts = []
                for m in unread[:5]:
                    who = m.get('who') or ('提醒' if m.get('due') else '手机')
                    if m.get('kind') == 'voice':
                        body_t = m.get('transcript') or f'一段 {int(round(m.get("duration") or 0))} 秒的语音，内容还没转出来，可以在屏幕上点播放'
                    else:
                        body_t = m['text']
                    parts.append(f'{who}（{_when_txt(m["at"] if not m.get("due") else m["due"])}）：{body_t}')
                    if m.get('kind') != 'voice':   # voice ones stay unread until someone plays them
                        with contextlib.suppress(OSError):
                            inbox.ack(m['id'])
                _forget_state()
                more = f'；还有 {len(unread) - 5} 条在屏幕上' if len(unread) > 5 else ''
                return web.json_response({'ok': True, 'text': f'有 {len(unread)} 条新留言：' + '；'.join(parts) + more})
            if act == 'remind':
                due = remind_due(body.get('at'), body.get('day'), body.get('time'), body.get('in_minutes'))
                if due is None:
                    return web.json_response({'ok': False, 'text': '提醒时间没听清，要说清楚几点（8 天以内）'})
                rp = None if body.get('in_minutes') is not None else body.get('repeat')
                item = inbox.add(str(body.get('text') or ''), 'screen', _who_of(body.get('who')), '', due,
                                 rp if isinstance(rp, str) else None, body.get('weekdays'))
                _forget_state()
                who = item.get('who')
                return web.json_response({'ok': True, 'text': f'好的，{_when_txt(item["due"], item)}' + (f'提醒{who}' if who else '提醒') + f'：{item["text"]}'})
            if act == 'reminders':
                pend = inbox.pending()
                if not pend:
                    return web.json_response({'ok': True, 'text': '现在没有待提醒的事'})
                return web.json_response({'ok': True, 'text': '；'.join(
                    f'{_when_txt(m["due"], m)}' + (f'提醒{m["who"]}' if m.get('who') else '') + f'：{m["text"]}' for m in pend[:6])})
            if act == 'cancel':
                kw = ' '.join(str(body.get('keyword') or '').split())
                pend = inbox.pending()
                hits = [m for m in pend if kw and kw in m['text']] if kw else pend[:1] if len(pend) == 1 else []
                if len(hits) != 1:
                    return web.json_response({'ok': False, 'text': '要取消哪一个提醒？' + ('；'.join(m['text'] for m in pend[:6]) if pend else '现在没有提醒')})
                inbox.cancel_reminder(hits[0]['id'])
                _forget_state()
                return web.json_response({'ok': True, 'text': f'已取消提醒：{hits[0]["text"]}'})
        except OverflowError as exc:
            return web.json_response({'ok': False, 'text': str(exc)})
        except ValueError as exc:
            return web.json_response({'ok': False, 'text': str(exc)})
        except OSError:
            return web.json_response({'ok': False, 'text': '留言保存失败，请稍后再试'})
        raise web.HTTPBadRequest()

    async def _keepalive_304(_req, resp):
        if resp.status == 304:
            resp.headers['Content-Length'] = '0'

    async def lifecycle(app_):
        app_loop[0] = asyncio.get_running_loop()
        app_[SESSION] = aiohttp.ClientSession(trust_env=False)
        desk_timer.restore_on_start()
        keepalive_task = None
        if inbox.path == Path('/var/lib/thinksmart-home/messages.json'):
            async def _timer_screen_keepalive():
                while True:
                    await asyncio.sleep(15.0)
                    if desk_timer.phase in ('running', 'done'):
                        _poke_home_display()
            keepalive_task = asyncio.create_task(_timer_screen_keepalive())
        remind_task = None
        if inbox.path == Path('/var/lib/thinksmart-home/messages.json'):
            async def _reminder_loop():
                while True:
                    with contextlib.suppress(Exception):
                        await _fire_reminders()
                    await asyncio.sleep(10.0)
            remind_task = asyncio.create_task(_reminder_loop())
        try:
            yield
        finally:
            for t in [x for x in (keepalive_task, remind_task) if x is not None] + list(transcribe_tasks):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            _cancel_focus_amb()
            desk_timer.shutdown()
            focus_timer.shutdown()
            clip_player.stop()
            with contextlib.suppress(Exception):
                await voice_recorder.cancel()
            with contextlib.suppress(Exception):
                await _unduck_home_audio_if_idle()
            app_loop[0] = None
            await app_[SESSION].close()

    # ---- brightness panel (moved from the monitor app; backed by thinksmart_display) ----
    async def display_state(_request):
        if display is None:
            return web.json_response({'ok': False, 'error': '面板不可用'}, status=503, headers={'Cache-Control': 'no-store'})
        return web.json_response(display.snapshot(), headers={'Cache-Control': 'no-store'})

    async def display_change(request):
        # Only the local screen/forwarded preview can write display preferences.
        # No CORS: a different site cannot submit JSON to this loopback API.
        if display is None:
            return web.json_response({'ok': False, 'error': '面板不可用'}, status=503, headers={'Cache-Control': 'no-store'})
        try:
            req_url_host = request.url.host
            req_host = request.host
        except ValueError:
            raise web.HTTPForbidden()
        if req_url_host not in ('127.0.0.1', 'localhost') or request.headers.get('Origin') != f'{request.scheme}://{req_host}':
            raise web.HTTPForbidden()
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        try:
            body = await request.json()
            value = (await display.preview(body) if request.path.endswith('/preview')
                     else await display.change(body))
            return web.json_response(dict(ok=True, **value), headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError):
            return web.json_response({'ok': False, 'error': '设置无效或传感器暂不可用，请重试'}, status=400, headers={'Cache-Control': 'no-store'})
        except (RuntimeError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({'ok': False, 'error': '亮度没有保存，请重试'}, status=503, headers={'Cache-Control': 'no-store'})

    app.on_response_prepare.append(_keepalive_304)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', static_file('index.html', 'text/html', {'Content-Security-Policy': csp}))
    app.router.add_get('/apps.json', static_file('apps.json', 'application/json'))
    app.router.add_get('/display', static_file('display.html', 'text/html'))
    app.router.add_get('/api/display', display_state)
    app.router.add_post('/api/display', display_change)
    app.router.add_post('/api/display/preview', display_change)
    app.router.add_get('/api/home/state', get_state)
    app.router.add_get('/api/home/weather', get_weather)
    app.router.add_get('/api/home/weather/detail', get_weather_detail)
    app.router.add_get('/api/home/calendar', get_calendar)
    app.router.add_get('/api/home/music/lyrics', get_music_lyrics)
    app.router.add_post('/api/home/music', post_music)
    app.router.add_post('/api/home/sleep', post_sleep)
    app.router.add_get('/api/home/sleepmode', sleepmode_api)
    app.router.add_post('/api/home/sleepmode', sleepmode_api)
    app.router.add_post('/api/home/study', post_study)
    app.router.add_post('/api/home/focus', post_focus)
    app.router.add_get('/api/home/timer', timer_api)
    app.router.add_post('/api/home/timer', timer_api)
    app.router.add_get('/api/home/inbox', get_inbox)
    app.router.add_post('/api/home/inbox/ack', post_ack)
    app.router.add_get('/api/home/messages', get_messages)
    app.router.add_post('/api/home/messages', post_message)
    app.router.add_post('/api/home/messages/voice', post_message_voice)
    app.router.add_get('/api/home/messages/audio/{id}', get_message_audio)
    app.router.add_post('/api/home/messages/play', post_message_play)
    app.router.add_post('/api/home/messages/ack_phone', post_message_ack_phone)
    app.router.add_post('/api/home/messages/reply', post_screen_reply)
    app.router.add_post('/api/home/messages/quick', post_message_quick)
    app.router.add_post('/api/home/messages/cancel', post_message_cancel)
    app.router.add_post('/api/home/messages/recall', post_message_recall)
    app.router.add_post('/api/home/messages/delete', post_message_delete)
    app.router.add_post('/api/home/messages/tool', post_message_tool)
    app.router.add_post('/api/home/voice_reply', post_voice_reply)
    async def preflight(_request):
        return web.Response(status=204)

    for path in ('/api/home/inbox', '/api/home/inbox/ack'):
        app.router.add_route('OPTIONS', path, preflight)
    return app


if __name__ == '__main__':
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s home: %(message)s')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    web.run_app(make_app(), host='127.0.0.1', port=PORT, access_log=None, print=None)
