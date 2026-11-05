# 网易云语音点歌实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ThinkSmart View 的小麦语音助手里，用本机 mpv 播放网易云音乐，支持点歌、每日推荐、我的歌单、收藏和播放控制。

**Architecture:** hub 内嵌一个纯 Python 的 `plugins_func/netease/` 包（EAPI 加密、Web API、mpv JSON IPC、队伍），`plugins_func/functions/play_netease.py` 只做意图注册；mpv 以 `--ao=pulse` 常驻，走 PipeWire。

**Tech Stack:** Python 3.10（hub venv）、`cryptography`、`requests`、mpv 0.37、qrencode 4.1.1、systemd user unit。

## Global Constraints

- 设备：`10.9.1.60`，aarch64，2GB；hub 目录 `/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server`。
- **mpv 必须 `--ao=pulse`**，禁止 ALSA（绕过 PipeWire 碰 `/dev/snd` 会把 q6asm DSP 弄挂）。
- 会话文件 `data/netease-session.json`、设备号 `data/netease-device-id` 权限 `0600`。
- 不新增 pip 包；二维码用 `qrencode`。
- 队列默认播完即停；只有用户要求才 `repeat`。
- 日志不出现 `MUSIC_U`、Cookie、完整播放 URL。
- 意图白名单在 `config.yaml` 的 `Intent.function_call.functions` 和 `intent_llm.functions` 两处都要加。

---

## 文件结构

```
netease/
  hub/plugins_func/netease/__init__.py
  hub/plugins_func/netease/session.py     # 会话与 device-id
  hub/plugins_func/netease/eapi.py        # 加密传输 + Cookie
  hub/plugins_func/netease/api.py         # 搜索/每日/歌单/取流/收藏
  hub/plugins_func/netease/player.py      # mpv IPC + 队列
  hub/plugins_func/functions/play_netease.py  # 两个意图
  hub/scripts/netease-login.py            # 扫码登录
  hub/scripts/netease-selftest.py         # 端到端自检
  hub/patch/abort_handle.py.patch         # 唤醒暂停钩子
  tests/test_eapi.py
  tests/test_api.py
  tests/test_player.py
  deploy.sh
```

---

### Task 1: 会话与设备号

**Files:**
- Create: `netease/hub/plugins_func/netease/__init__.py`
- Create: `netease/hub/plugins_func/netease/session.py`
- Test: `netease/tests/test_session.py`

**Interfaces:**
- Produces: `load() -> dict|None`、`save(music_u, csrf, uid, nickname)`、`clear()`、`device_id() -> str`、`SESSION_FILE`、`DEVICE_FILE`。

- [ ] **Step 1: 写失败测试**

```python
import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from plugins_func.netease import session

class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.p = [mock.patch.object(session, "DATA", self.dir)]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()
        self.tmp.cleanup()

    def test_save_load_roundtrip_and_mode(self):
        session.save("mu", "csrf", 42, "小明")
        got = session.load()
        self.assertEqual(got["music_u"], "mu")
        self.assertEqual(got["uid"], 42)
        self.assertEqual(oct(session.SESSION_FILE.stat().st_mode & 0o777), "0o600")

    def test_clear(self):
        session.save("mu", "csrf", 42, "小明")
        session.clear()
        self.assertIsNone(session.load())

    def test_device_id_is_52_hex_and_stable(self):
        d1 = session.device_id()
        self.assertRegex(d1, r"^[0-9A-F]{52}$")
        self.assertEqual(d1, session.device_id())
        self.assertEqual(oct(session.DEVICE_FILE.stat().st_mode & 0o777), "0o600")
```

- [ ] **Step 2: 跑测试确认失败**

`cd netease && python3 -m unittest tests.test_session -v` → `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`session.py`：

```python
import json, os, secrets
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
SESSION_FILE = DATA / "netease-session.json"
DEVICE_FILE = DATA / "netease-device-id"


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load():
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return None


def save(music_u: str, csrf: str = "", uid: int = 0, nickname: str = ""):
    import time
    _atomic_write(SESSION_FILE, json.dumps({
        "music_u": music_u, "csrf": csrf, "uid": uid,
        "nickname": nickname, "saved_at": int(time.time()),
    }, ensure_ascii=False))


def clear():
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def device_id() -> str:
    try:
        d = DEVICE_FILE.read_text().strip()
        if len(d) == 52:
            return d
    except OSError:
        pass
    d = "".join(secrets.choice("0123456789ABCDEF") for _ in range(52))
    _atomic_write(DEVICE_FILE, d)
    return d
```

`__init__.py` 空文件。

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: Commit**

---

### Task 2: EAPI 加密传输

**Files:**
- Create: `netease/hub/plugins_func/netease/eapi.py`
- Test: `netease/tests/test_eapi.py`

**Interfaces:**
- Produces: `eapi_encrypt(path, data) -> str`、`class Client`（`web_get`、`eapi_post`、`account`、`cookie_header`）、`LoginRequired`、`NeteaseError`。

- [ ] **Step 1: 写失败测试**

```python
import hashlib, json, unittest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from plugins_func.netease.eapi import EAPI_KEY, eapi_encrypt

SEP = "-36cd479b6b5-"

def decrypt(hexstr):
    d = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).decryptor()
    return (d.update(bytes.fromhex(hexstr)) + d.finalize()).decode()

class TestEncrypt(unittest.TestCase):
    def test_layout_and_digest(self):
        path = "/api/song/enhance/player/url/v1"
        data = {"ids": "[1]", "level": "exhigh", "header": {"os": "pc"}, "e_r": False}
        plain = decrypt(eapi_encrypt(path, data))
        self.assertTrue(plain.startswith(path + SEP))
        body = plain.split(SEP)[1]
        digest = plain.split(SEP)[2]
        self.assertEqual(json.loads(body), data)
        self.assertEqual(digest, hashlib.md5(
            ("nobody" + path + "use" + body + "md5forencrypt").encode()).hexdigest())
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**

```python
import hashlib, json, random, time
from urllib.parse import quote
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from . import session

WEB_BASE = "https://music.163.com"
EAPI_BASE = "https://interfacepc.music.163.com"
EAPI_KEY = b"e82ckenh8dichen8"
UA_WEB = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
UA_EAPI = "NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)"

class NeteaseError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"netease code {code}: {msg}")
        self.code = code

class LoginRequired(NeteaseError):
    pass


def eapi_encrypt(path: str, data: dict) -> str:
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(
        ("nobody" + path + "use" + body + "md5forencrypt").encode()).hexdigest()
    plain = f"{path}-36cd479b6b5-{body}-36cd479b6b5-{digest}".encode()
    enc = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    return (enc.update(plain) + enc.finalize()).hex()


class Client:
    def __init__(self):
        s = session.load() or {}
        self.music_u = s.get("music_u", "")
        self.csrf = s.get("csrf", "")
        self.nmtid = s.get("nmtid", "")
        self.device = session.device_id()

    def _header(self):
        h = {
            "osver": "Microsoft-Windows-10-Professional-build-19045-64bit",
            "deviceId": self.device, "os": "pc", "appver": "3.1.17.204416",
            "versioncode": "140", "mobilename": "", "buildver": str(int(time.time())),
            "resolution": "1920x1080", "channel": "netease",
            "requestId": f"{int(time.time() * 1000)}_{random.randrange(1000):04d}",
            "__csrf": self.csrf,
        }
        if self.music_u: h["MUSIC_U"] = self.music_u
        if self.nmtid: h["NMTID"] = self.nmtid
        return h

    def cookie_header(self):
        return "; ".join(f"{k}={quote(str(v), safe='')}" for k, v in self._header().items())

    def _absorb(self, resp):
        for c in resp.cookies:
            if c.name == "MUSIC_U": self.music_u = c.value
            elif c.name == "__csrf": self.csrf = c.value
            elif c.name == "NMTID": self.nmtid = c.value

    def _json(self, resp):
        if resp.status_code == 401:
            raise LoginRequired(401)
        try:
            data = resp.json()
        except ValueError:
            raise NeteaseError(resp.status_code, resp.text[:80])
        code = data.get("code")
        if code in (301, 302):
            raise LoginRequired(code, data.get("message") or data.get("msg") or "")
        if code not in (None, 200):
            raise NeteaseError(code, data.get("message") or data.get("msg") or "")
        return data

    def web_get(self, path, params=None):
        r = requests.get(WEB_BASE + path, params=params, timeout=15,
                         headers={"User-Agent": UA_WEB, "Cookie": self.cookie_header()})
        self._absorb(r)
        return self._json(r)

    def eapi_post(self, path, data):
        header = self._header()
        payload = dict(data, header=header, e_r=False)
        r = requests.post(EAPI_BASE + "/eapi/" + path[len("/api/"):],
                          data={"params": eapi_encrypt(path, payload)},
                          timeout=15, headers={"User-Agent": UA_EAPI,
                                               "Content-Type": "application/x-www-form-urlencoded",
                                               "Cookie": self.cookie_header()})
        self._absorb(r)
        return self._json(r)

    def account(self):
        return self.web_get("/api/nuser/account/get").get("profile")
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: Commit**

---

### Task 3: 搜索、歌单、取流、收藏

**Files:**
- Create: `netease/hub/plugins_func/netease/api.py`
- Test: `netease/tests/test_api.py`

**Interfaces:**
- Produces: `Track`、`Stream`、`search_songs(client, q, limit=10)`、`daily_songs(client)`、`user_playlists(client, uid)`、`playlist_tracks(client, pid)`、`song_url(client, id, level)`、`like(client, id, like=True)`、`playable(track)`、`LEVELS`。

- [ ] **Step 1: 写失败测试**（假 client，不联网）

```python
import unittest
from plugins_func.netease import api

class Fake:
    def __init__(self, web=None, eapi=None):
        self.web, self.eapi = web or {}, eapi or []
    def web_get(self, path, params=None): return self.web[path]
    def eapi_post(self, path, data):
        if path == "/api/song/enhance/player/url/v1":
            return self.eapi.pop(0)
        return {"code": 200}

SONG = {"id": 1, "name": "稻香", "ar": [{"name": "周杰伦"}], "al": {"name": "魔杰座"},
        "dt": 223000, "fee": 0, "privilege": {"st": 0, "pl": 320000}}

class TestApi(unittest.TestCase):
    def test_search_maps_track(self):
        c = Fake(web={"/api/cloudsearch/pc": {"result": {"songs": [SONG]}}})
        t = api.search_songs(c, "稻香")[0]
        self.assertEqual((t.id, t.name, t.artists, t.album), (1, "稻香", ["周杰伦"], "魔杰座"))
        self.assertTrue(api.playable(t))

    def test_song_url_falls_back_then_rejects_preview(self):
        c = Fake(eapi=[{"data": [{"url": "", "freeTrialInfo": None}]},
                       {"data": [{"url": "http://x/1.mp3", "type": "mp3", "level": "exhigh",
                                  "size": 10, "freeTrialInfo": None}]}])
        s = api.song_url(c, 1, "lossless")
        self.assertEqual(s.url, "https://x/1.mp3")
        self.assertEqual(s.level, "exhigh")
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（关键：`song_url` 的降级链与试听判定）

```python
from dataclasses import dataclass, field
import json

LEVELS = ["standard", "higher", "exhigh", "lossless", "hires"]

@dataclass
class Privilege:
    st: int = 0
    pl: int = 0

@dataclass
class Track:
    id: int
    name: str
    artists: list = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    fee: int = 0
    privilege: Privilege | None = None

@dataclass
class Stream:
    url: str
    type: str = "mp3"
    level: str = "standard"
    size: int = 0

def _track(raw):
    p = raw.get("privilege")
    return Track(raw.get("id", 0), raw.get("name", ""),
                 [a.get("name", "") for a in raw.get("ar") or []],
                 (raw.get("al") or {}).get("name", ""), raw.get("dt", 0), raw.get("fee", 0),
                 Privilege(p.get("st", 0), p.get("pl", 0)) if p else None)

def playable(t):
    if t.privilege: return t.privilege.st >= 0 and t.privilege.pl > 0
    return t.fee not in (1, 4)

def search_songs(client, q, limit=10):
    res = client.web_get("/api/cloudsearch/pc",
                         {"s": q, "type": 1, "limit": limit, "offset": 0, "total": "true"})
    return [_track(s) for s in ((res.get("result") or {}).get("songs") or [])]

def daily_songs(client):
    res = client.web_get("/api/v3/discovery/recommend/songs")
    return [_track(s) for s in ((res.get("data") or {}).get("dailySongs") or [])]

def user_playlists(client, uid):
    res = client.web_get("/api/user/playlist", {"uid": uid, "limit": 200, "offset": 0})
    return res.get("playlist") or []

def playlist_tracks(client, pid):
    res = client.web_get("/api/v6/playlist/detail", {"id": pid, "n": 1000})
    return [_track(s) for s in ((res.get("playlist") or {}).get("tracks") or [])]

def _lower(level):
    i = LEVELS.index(level) if level in LEVELS else 2
    return LEVELS[:i + 1][::-1]

def song_url(client, track_id, level="exhigh"):
    for lv in _lower(level):
        res = client.eapi_post("/api/song/enhance/player/url/v1",
                               {"ids": f"[{track_id}]", "level": lv,
                                "encodeType": "flac" if lv in ("lossless", "hires") else "mp3"})
        item = (res.get("data") or [{}])[0]
        if item.get("freeTrialInfo"):
            raise LookupError("unavailable")
        if item.get("url"):
            return Stream(item["url"].replace("http://", "https://", 1),
                          (item.get("type") or "mp3").lower(), item.get("level") or lv,
                          item.get("size") or 0)
    raise LookupError("unavailable")

def like(client, track_id, like=True):
    client.eapi_post("/api/radio/like", {"alg": "itembased", "trackId": str(track_id),
                                         "like": "true" if like else "false", "time": "3"})
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: Commit**

---

### Task 4: mpv 与队列

**Files:**
- Create: `netease/hub/plugins_func/netease/player.py`
- Test: `netease/tests/test_player.py`

**Interfaces:**
- Produces: `class Player`（`play_queue`、`pause/resume/toggle`、`next/prev/stop`、`seek`、`set_volume`、`set_repeat`、`status`、`pause_for_abort`）、`get_player`、`class Mpv`。

- [ ] **Step 1: 写失败测试**（假 mpv，不拉起进程）

```python
import unittest
from plugins_func.netease.player import Player

class FakeMpv:
    def __init__(self): self.cmds, self.on_event = [], None
    def command(self, *a): self.cmds.append(a)

def fake_resolver(tid, level):
    class S: url = f"http://cdn/{tid}.mp3"
    return S()

def track(i): return type("T", (), {"id": i, "name": f"s{i}", "artists": ["a"]})()

class TestQueue(unittest.TestCase):
    def make(self):
        m = FakeMpv(); p = Player(m, fake_resolver); return m, p

    def test_eof_advances_and_stops_at_end(self):
        m, p = self.make(); p.play_queue([track(1), track(2)])
        p.on_event("end-file", {"reason": "eof"})
        self.assertEqual(p.current.id, 2)
        p.on_event("end-file", {"reason": "eof"})
        self.assertIsNone(p.current)

    def test_repeat_all_wraps(self):
        m, p = self.make(); p.play_queue([track(1), track(2)]); p.set_repeat("all")
        p.on_event("end-file", {"reason": "eof"}); p.on_event("end-file", {"reason": "eof"})
        self.assertEqual(p.current.id, 1)

    def test_abort_pauses_only_once(self):
        m, p = self.make(); p.play_queue([track(1)])
        p.pause_for_abort()
        self.assertEqual(m.cmds[-1], ("set_property", "pause", True))
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（`Mpv` 负责进程/socket/读线程；`Player` 只做队列，测试注入假 mpv）

```python
import json, os, socket, subprocess, threading, time

class Mpv:
    def __init__(self, path="/usr/bin/mpv", socket_path="/run/user/1000/thinksmart-mpv.sock", volume=70):
        self.socket_path, self.on_event = socket_path, None
        self._quit_existing()
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
        self._proc = subprocess.Popen(
            [path, "--idle=yes", "--no-video", "--audio-display=no", "--really-quiet",
             "--ao=pulse", f"--volume={volume}", f"--input-ipc-server={socket_path}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._sock = self._connect()
        self._lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _quit_existing(self):
        try:
            s = socket.socket(socket.AF_UNIX); s.connect(self.socket_path)
            s.sendall(b'{"command":["quit"]}\n'); s.close(); time.sleep(0.2)
        except OSError:
            pass
        try: os.unlink(self.socket_path)
        except OSError: pass

    def _connect(self):
        for _ in range(50):
            try:
                s = socket.socket(socket.AF_UNIX); s.connect(self.socket_path); return s
            except OSError: time.sleep(0.1)
        raise RuntimeError("mpv did not start")

    def command(self, *args):
        with self._lock:
            self._sock.sendall(json.dumps({"command": list(args)}).encode() + b"\n")

    def _read_loop(self):
        buf = b""
        while True:
            try: buf += self._sock.recv(65536)
            except OSError: return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try: msg = json.loads(line)
                except ValueError: continue
                if msg.get("event") and self.on_event:
                    try: self.on_event(msg["event"], msg)
                    except Exception: pass

    def quit(self):
        try: self.command("quit")
        except OSError: pass


class Player:
    def __init__(self, mpv, resolver, level="exhigh", volume=70):
        self.mpv, self.resolver, self.level = mpv, resolver, level
        self.volume, self.paused, self.repeat = volume, False, "off"
        self.queue, self.index, self.current = [], -1, None
        mpv.on_event = self.on_event

    def play_queue(self, tracks, start=0):
        self.queue = list(tracks); self.index = start; self._load()

    def _load(self):
        t = self.queue[self.index]
        stream = self.resolver(t.id, self.level)
        self.current = t
        self.mpv.command("loadfile", stream.url, "replace")
        self.mpv.command("set_property", "pause", False)
        self.paused = False

    def on_event(self, name, data):
        if name == "end-file":
            reason = data.get("reason")
            if reason == "eof": self.next(auto=True)
            elif reason in ("error", "aborted"): self._skip()

    def next(self, auto=False):
        if self.repeat == "one" and auto: return self._load()
        if self.index + 1 < len(self.queue):
            self.index += 1; return self._load()
        if self.repeat == "all":
            self.index = 0; return self._load()
        self.stop()

    def prev(self):
        if not self.queue: return
        self.index = max(0, self.index - 1); self._load()

    def _skip(self):
        self.next()

    def stop(self):
        self.current = None; self.queue = []; self.index = -1
        self.mpv.command("stop"); self.paused = False

    def pause(self): self.mpv.command("set_property", "pause", True); self.paused = True
    def resume(self): self.mpv.command("set_property", "pause", False); self.paused = False
    def toggle(self): self.resume() if self.paused else self.pause()
    def pause_for_abort(self):
        if self.current and not self.paused: self.pause()
    def seek(self, sec): self.mpv.command("seek", float(sec), "absolute")
    def set_volume(self, v): self.volume = max(0, min(100, int(v))); self.mpv.command("set_property", "volume", self.volume)
    def set_repeat(self, mode): self.repeat = mode if mode in ("off", "all", "one") else "off"

    def status(self):
        if not self.current: return None
        return {"name": self.current.name, "artists": self.current.artists,
                "paused": self.paused, "volume": self.volume, "repeat": self.repeat}
```

`get_player(config, resolver)` 单例：

```python
_PLAYER = None
def get_player(config, resolver):
    global _PLAYER
    if _PLAYER is None:
        _PLAYER = Player(Mpv(config.get("mpv_path", "/usr/bin/mpv"),
                             config.get("socket", "/run/user/1000/thinksmart-mpv.sock"),
                             config.get("volume", 70)),
                         resolver, config.get("level", "exhigh"), config.get("volume", 70))
    return _PLAYER

def pause_for_abort():
    if _PLAYER: _PLAYER.pause_for_abort()
```

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: Commit**

---

### Task 5: 语音意图

**Files:**
- Create: `netease/hub/plugins_func/functions/play_netease.py`

**Interfaces:**
- Consumes: `session`、`eapi.Client`、`api`、`player.get_player`。
- Produces: 两个注册函数 `play_netease(query, kind)`、`control_netease(action, value)`。

- [ ] **Step 1: 写实现**

```python
import difflib
import time
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from plugins_func.netease import api, eapi, player, session

TAG = __name__

PLAY_DESC = {"type": "function", "function": {
    "name": "play_netease",
    "description": "用网易云音乐在线播放歌曲、歌手、每日推荐或我的歌单时调用；本地 music 目录用 play_music。",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "歌名、歌手名或歌单名；每日推荐留空"},
        "kind": {"type": "string", "enum": ["song", "artist", "daily", "playlist"],
                 "description": "song=歌名，artist=歌手，daily=每日推荐，playlist=我的歌单"}},
        "required": []}}}

CTL_DESC = {"type": "function", "function": {
    "name": "control_netease",
    "description": "控制正在播放的网易云音乐：暂停/继续/上一首/下一首/停止/音量/进度/收藏/循环/状态。",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["pause", "resume", "next", "prev", "stop",
                                               "volume", "seek", "like", "loop", "status"]},
        "value": {"type": "string", "description": "音量 0-100；进度秒数或 mm:ss；循环 off/all/one"}},
        "required": ["action"]}}}


def _client():
    if not session.load():
        raise eapi.LoginRequired(301, "not logged in")
    return eapi.Client()


def _cfg(conn):
    return (conn.config.get("plugins") or {}).get("play_netease") or {}


def _say(text):
    return ActionResponse(Action.RESPONSE, result=text, response=text)


def _pick(tracks, query):
    q = query.strip().lower()
    ranked = sorted(tracks, key=lambda t: (
        0 if t.name.lower() == q else 1 if q in t.name.lower() else 2,
        0 if any(q in a.lower() for a in t.artists) else 1))
    return next((t for t in ranked if api.playable(t)), None)


def _match_playlist(playlists, name):
    best, score = None, 0.0
    for p in playlists:
        r = difflib.SequenceMatcher(None, name, p.get("name", "")).ratio()
        if r > score: best, score = p, r
    return best if score >= 0.4 else None


@register_function("play_netease", PLAY_DESC, ToolType.SYSTEM_CTL)
async def play_netease(conn, query: str = "", kind: str = "song"):
    try:
        client = _client()
        if kind == "daily":
            tracks = api.daily_songs(client)
        elif kind == "playlist":
            me = client.account()
            pls = api.user_playlists(client, me["userId"]) if me else []
            pl = _match_playlist(pls, query)
            if not pl:
                return _say(f"没找到歌单「{query}」")
            tracks = api.playlist_tracks(client, pl["id"])
            query = pl["name"]
        else:
            tracks = api.search_songs(client, query, 10)
        if not tracks:
            return _say(f"没找到《{query}》，换个说法？")
        if kind in ("daily", "playlist"):
            chosen, queue = tracks[0], tracks
        else:
            chosen = _pick(tracks, query)
            if not chosen:
                return _say(f"《{query}》要会员或没有版权，换一首？")
            queue = [chosen]
        p = player.get_player(_cfg(conn), lambda tid, lv: api.song_url(client, tid, lv))
        p.play_queue(queue)
        who = "、".join(chosen.artists)
        if kind in ("daily", "playlist"):
            return _say(f"开始播放{query}，共 {len(queue)} 首，第一首《{chosen.name}》- {who}")
        return _say(f"正在播放《{chosen.name}》- {who}")
    except eapi.LoginRequired:
        conn.logger.bind(tag=TAG).warning("netease login required")
        return _say("还没有登录网易云，先运行 scripts/netease-login.py 扫码登录")
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"play_netease failed: {e}")
        return _say("网易云播放出错了，稍后再试")


@register_function("control_netease", CTL_DESC, ToolType.SYSTEM_CTL)
async def control_netease(conn, action: str, value: str = ""):
    try:
        p = player.get_player(_cfg(conn), lambda tid, lv: api.song_url(_client(), tid, lv))
        if action == "pause": p.pause(); return _say("好，暂停了")
        if action == "resume": p.resume(); return _say("继续播放")
        if action == "next": p.next(); return _say("下一首")
        if action == "prev": p.prev(); return _say("上一首")
        if action == "stop": p.stop(); return _say("停了")
        if action == "volume": p.set_volume(int(value)); return _say(f"音量调到 {p.volume}")
        if action == "seek":
            if ":" in value:
                m, s = value.split(":", 1); sec = int(m) * 60 + int(s)
            else:
                sec = int(float(value))
            p.seek(sec); return _say(f"跳到 {sec // 60} 分 {sec % 60} 秒")
        if action == "loop":
            mode = value if value in ("off", "all", "one") else "off"
            p.set_repeat(mode)
            return _say({"off": "取消循环", "all": "列表循环", "one": "单曲循环"}[mode])
        if action == "like":
            cur = p.current
            if not cur: return _say("现在没有在放的歌")
            api.like(_client(), cur.id, True); return _say(f"收藏了《{cur.name}》")
        if action == "status":
            st = p.status()
            if not st: return _say("现在没有在放歌")
            return _say(f"正在播放《{st['name']}》- {'、'.join(st['artists'])}"
                        + ("，已暂停" if st["paused"] else ""))
        return _say("不支持这个操作")
    except eapi.LoginRequired:
        return _say("还没有登录网易云，先扫码登录")
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"control_netease failed: {e}")
        return _say("操作失败了")
```

- [ ] **Step 2: 语法检查** `python3 -m py_compile ...`
- [ ] **Step 3: Commit**

---

### Task 6: 扫码登录脚本

**Files:**
- Create: `netease/hub/scripts/netease-login.py`

- [ ] **Step 1: 实现**

```python
#!/usr/bin/env python3
import subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins_func.netease import eapi, session

def show(url):
    try: subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
    except FileNotFoundError: print("(qrencode 未安装，用链接手动生成二维码)")
    subprocess.run(["qrencode", "-o", "/tmp/netease-login.png", url], check=False)
    print(url)
    print("二维码也写在 /tmp/netease-login.png")

def main():
    c = eapi.Client()
    res = c.eapi_post("/api/login/qrcode/unikey", {"type": 3})
    key = res.get("unikey")
    if not key:
        print("拿不到二维码 key:", res); return 1
    url = "https://music.163.com/login?codekey=" + key
    show(url)
    print("等待扫码…")
    last = None
    for _ in range(150):
        r = c.eapi_post("/api/login/qrcode/client/login", {"key": key, "type": 3})
        code = r.get("code")
        if code == 800: print("二维码过期，重新运行"); return 1
        if code == 802 and last != 802: print("已扫码，请在手机上确认")
        if code == 803:
            me = c.account() or {}
            session.save(c.music_u, c.csrf, me.get("userId", 0), me.get("nickname", ""))
            print(f"登录成功：{me.get('nickname', '')} (uid={me.get('userId', '')})")
            return 0
        last = code
        time.sleep(2)
    print("超时，重新运行"); return 1

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 语法检查**
- [ ] **Step 3: Commit**

---

### Task 7: 端到端自检脚本

**Files:**
- Create: `netease/hub/scripts/netease-selftest.py`

- [ ] **Step 1: 实现**

```python
#!/usr/bin/env python3
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins_func.netease import api, eapi, player, session

def main():
    if not session.load():
        print("FAIL: no session, run netease-login.py first"); return 1
    c = eapi.Client()
    me = c.account()
    print("account:", (me or {}).get("nickname"), "OK" if me else "FAIL")
    tracks = api.search_songs(c, "稻香", 3)
    print("search:", len(tracks), "OK" if tracks else "FAIL")
    if not tracks or not me: return 1
    p = player.get_player({"level": "exhigh"}, lambda tid, lv: api.song_url(c, tid, lv))
    p.play_queue([tracks[0]])
    print("play:", tracks[0].name, "-", "、".join(tracks[0].artists))
    time.sleep(5)
    print("status:", p.status())
    p.seek(5); print("seek 5s OK")
    p.pause(); print("paused:", p.status()["paused"])
    p.resume(); p.stop(); print("stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 语法检查**
- [ ] **Step 3: Commit**

---

### Task 8: 部署脚本与 hub 钩子

**Files:**
- Create: `netease/deploy.sh`
- Create: `netease/hub/patch/abort_handle.py.patch`

- [ ] **Step 1: 写钩子 patch（上下文是 `conn.client_abort = True` 之后）**

```python
# abortHandle.py, after: conn.client_abort = True
if drop_audio:
    try:
        from plugins_func.netease.player import pause_for_abort
        pause_for_abort()
    except Exception:
        pass
```

- [ ] **Step 2: 写 deploy.sh**

```bash
#!/bin/bash
# Run from the repo on the Mac: ./netease/deploy.sh
set -euo pipefail
HOST=${HOST:-ubuntu@10.9.1.60}
SRV=/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
DEST=netease/hub

ssh "$HOST" "cp -a /home/ubuntu/xiaozhi-hub-server /home/ubuntu/xiaozhi-hub-server.bak-\$(date +%F-%H%M)"
rsync -av "$DEST/plugins_func/netease/" "$HOST:$SRV/plugins_func/netease/"
rsync -av "$DEST/plugins_func/functions/play_netease.py" "$HOST:$SRV/plugins_func/functions/"
rsync -av "$DEST/scripts/" "$HOST:$SRV/scripts/"
ssh "$HOST" "sudo apt-get install -y --no-install-recommends mpv qrencode"
echo "files copied; now apply config + abort hook, then restart hub"
```

- [ ] **Step 3: 配置改动（手工，部署时执行）**

在 `config.yaml` 的 `Intent.intent_llm.functions` 和 `Intent.function_call.functions` 两个列表末尾加 `play_netease`、`control_netease`；加：

```yaml
plugins:
  play_netease:
    level: exhigh
    volume: 70
    mpv_path: /usr/bin/mpv
    socket: /run/user/1000/thinksmart-mpv.sock
```

- [ ] **Step 4: Commit**

---

### Task 9: 部署与现场验收

- [ ] **Step 1:** 跑 `./netease/deploy.sh`
- [ ] **Step 2:** 备份 `config.yaml`，用 Python 加白名单和配置，`yaml.safe_load` 校验，权限保持 600
- [ ] **Step 3:** 给 `abortHandle.py` 打钩子（幂等），`python3 -m py_compile` 检查
- [ ] **Step 4:** `systemctl --user restart xiaozhi-hub.service`（`XDG_RUNTIME_DIR=/run/user/1000`），确认 active
- [ ] **Step 5:** 后台跑 `netease-login.py`，把 `/tmp/netease-login.png` 拉到本机给用户扫
- [ ] **Step 6:** 扫码成功后跑 `netease-selftest.py`，全 PASS
- [ ] **Step 7:** 用户语音验收 10 条；`pgrep -af thinksmart-mpv` 只有一个；`journalctl` 无敏感信息
- [ ] **Step 8:** Commit 部署记录/README 更新（可选）

---

## Self-Review

- 规格覆盖：点歌/歌手/每日/歌单/收藏/暂停/切歌/音量/seek/循环/状态 → Task 3/5；扫码 → Task 6；mpv pulse → Task 4；唤醒暂停 → Task 8；意图白名单 → Task 8；session 0600 → Task 1；测试 → Task 1-4 + Task 7。
- 无 TBD/占位。
- 类型一致性：`Track.id/name/artists`、`Stream.url`、`Player.current/queue/index` 在 Task 3/4/5 中一致。
