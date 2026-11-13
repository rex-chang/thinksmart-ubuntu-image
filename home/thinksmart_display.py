"""Shared display preferences; the existing blanker remains the sole backlight owner."""
import asyncio
import json
import math
import os
import socket
import stat
import threading
import time
from pathlib import Path

# Written by thinksmart-home's DisplayControl (StateDirectory of thinksmart-home, owned by
# ubuntu) and read by the idle blanker, which runs as the same user.
MODE_PATH = Path('/var/lib/thinksmart-home/display.json')
HINT_PATH = Path('/run/user/1000/thinksmart-screen.json')
ALS_PATH = Path('/run/user/1000/thinksmart-als.json')
BACKLIGHT_PATH = Path('/sys/class/backlight/backlight/brightness')
MINIMUM, MAXIMUM = 1400, 4095
PREVIEW_SOCKET = Path('/run/user/1000/thinksmart-display.sock')
PREVIEW_TTL = 2.0


def valid_level(level):
    return type(level) is int and 0 <= level <= 100


def send_preview(body, path=PREVIEW_SOCKET):
    """Wake the backlight owner immediately, with no config writes or polling delay."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.setblocking(False)
        channel.sendto(json.dumps(body).encode(), str(path))


class BrightnessPreview:
    """Ephemeral drag state. The existing blanker remains the sole sysfs writer."""
    def __init__(self, path=PREVIEW_SOCKET):
        self.path = Path(path)
        self.channel = None
        self.level = None
        self.received = None

    def open(self):
        if os.path.lexists(self.path):
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('Unexpected preview socket owner/type')
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.bind(str(self.path))
            self.path.chmod(0o600)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        self.channel = sock
        return self

    def receive(self):
        if self.channel is None:
            return
        for _ in range(32):
            try:
                raw = self.channel.recv(512)
            except BlockingIOError:
                break
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and set(data) == {'level'} and valid_level(data['level']):
                    self.level, self.received = data['level'], time.monotonic()
                elif isinstance(data, dict) and data == {'clear': True}:
                    self.level, self.received = None, None
            except (ValueError, TypeError, UnicodeError):
                pass

    def current(self):
        return self.level if self.level is not None and self.received is not None and time.monotonic()-self.received < PREVIEW_TTL else None

    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel = None
            self.path.unlink(missing_ok=True)


def read_json(path):
    try:
        value = path.read_text() if isinstance(path, Path) else Path(path).read_text()
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def automatic_for(hint, mode):
    # A subsequent voice/original-settings change takes priority over automatic mode.
    level = hint.get('brightness', 100)
    anchor = mode.get('anchor')
    try:
        return (type(level) in (int, float) and math.isfinite(level) and 0 <= level <= 100
                and mode.get('auto') is True and type(anchor) in (int, float) and math.isfinite(anchor) and anchor == level)
    except OverflowError:
        return False


def ambient(path=ALS_PATH):
    data = read_json(path)
    if type(data.get('lux')) is bool or type(data.get('at')) is bool:
        return None
    try:
        lux, age = float(data['lux']), time.time() - float(data['at'])
        return lux if math.isfinite(lux) and 0 <= lux <= 2000 and 0 <= age < 15 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class DisplayControl:
    def __init__(self, mode=MODE_PATH, hint=HINT_PATH, als=ALS_PATH,
                 backlight=BACKLIGHT_PATH, core_url='http://127.0.0.1:8090', preview_socket=PREVIEW_SOCKET):
        self.mode, self.hint, self.als, self.backlight = map(Path, (mode, hint, als, backlight))
        self.core_url = core_url
        self.preview_socket = Path(preview_socket)
        self.lock = asyncio.Lock()

    def snapshot(self):
        hint, mode = read_json(self.hint), read_json(self.mode)
        b = hint.get('brightness', 100)
        fallback = round(max(0, min(100, b))) if type(b) in (int, float) and math.isfinite(b) else 100
        try:
            raw = int(self.backlight.read_text())
            level = round(max(0, min(100, (raw - MINIMUM) * 100 / (MAXIMUM - MINIMUM)))) if raw > 10 else fallback
        except (OSError, ValueError):
            raw, level = None, fallback
        lux = ambient(self.als)
        return {'auto': automatic_for(hint, mode), 'level': level,
                'lux': round(lux, 2) if lux is not None else None,
                'sensor_ok': lux is not None, 'dimmed': bool(hint.get('dim')),
                'screen_off': not (type(hint.get('idle')) is int and hint.get('idle') == 0), 'screen_on': raw is not None and raw > 10}

    def save(self, mode):
        self.mode.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(mode)
        temporary = self.mode.parent / f'.{self.mode.name}.{os.getpid()}.{threading.get_ident()}.tmp'
        created = False
        try:
            if self.mode.is_symlink():
                raise OSError('Refusing symlink mode path')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(payload)
            if self.mode.is_symlink():
                raise OSError('Refusing symlink mode path')
            os.replace(temporary, self.mode)
        finally:
            if created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    async def change(self, body):
        if not isinstance(body, dict) or len(body) != 1 or not set(body) <= {'auto', 'level'}:
            raise ValueError('请只指定自动模式或亮度')
        saved_level = None
        async with self.lock:
            if 'auto' in body:
                if type(body['auto']) is not bool:
                    raise ValueError('自动模式格式不正确')
                if body['auto']:
                    if ambient(self.als) is None:
                        raise ValueError('暂时没有光线读数，请先手动调整')
                    hint = read_json(self.hint)
                    b = hint.get('brightness', 100)
                    if type(b) not in (int, float) or not math.isfinite(b) or not 0 <= b <= 100:
                        raise ValueError('暂时无法读取屏幕设置')
                    self.save({'auto': True, 'anchor': b})
                    self.clear_preview()
                else:
                    # Freeze the current automatic level when switching to manual.
                    saved_level = self.snapshot()['level']
                    await self.manual(saved_level)
            else:
                level = body['level']
                if not valid_level(level):
                    raise ValueError('亮度需为 0 到 100 的整数')
                saved_level = level
                await self.manual(level)
        value = self.snapshot()
        if saved_level is not None:
            # The sysfs owner may still be consuming the hint; acknowledge the saved
            # target, never return an old physical value that pulls the thumb back.
            value.update(level=saved_level, auto=False)
        return value

    async def preview(self, body):
        if not isinstance(body, dict) or set(body) != {'level'} or not valid_level(body['level']):
            raise ValueError('亮度需为 0 到 100 的整数')
        async with self.lock:
            send_preview(body, self.preview_socket)
        return {'level': body['level'], 'auto': False}

    def clear_preview(self):
        try:
            send_preview({'clear': True}, self.preview_socket)
        except OSError:
            pass  # Old/missing blanker still supports the durable manual API.

    async def manual(self, level):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as session:
            async with session.post(self.core_url + '/api/brightness', json={'level': level}) as response:
                try:
                    result = await response.json(content_type=None)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError('亮度没有保存，请重试') from exc
                if response.status != 200 or not isinstance(result, dict) or result.get('ok') is not True:
                    raise RuntimeError('亮度没有保存，请重试')
        self.save({'auto': False})
        self.clear_preview()
