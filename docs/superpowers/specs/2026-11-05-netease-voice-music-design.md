# 网易云语音点歌设计（Hub 内嵌 + 本机 mpv）

- 日期：2026-11-05
- 状态：待评审
- 目标设备：`10.9.1.60`（Lenovo ThinkSmart View，aarch64，2GB，Ubuntu 24.04 + Weston kiosk）
- 参考实现：[`rex-chang/mygo-cloudmusic`](https://github.com/rex-chang/mygo-cloudmusic) 的 `internal/netease`（EAPI 加密、扫码登录、取流地址）

## 1. 目标

在现有“小麦”语音助手里支持网易云点歌：说歌名/歌手就播放，能控制暂停、切歌、音量、进度、收藏，能放每日推荐和我的歌单。音乐由本机 `mpv` 直接走 PipeWire 播放，音质不受语音通道限制。

## 2. 范围

**v1 包含**

- 点歌：`song`（歌名或“歌手 歌名”）、`artist`（歌手）
- 队列：每日推荐、我的歌单（含“我喜欢的音乐”）
- 控制：播放、暂停/继续、上一首/下一首、停止、音量、seek、收藏当前歌曲、循环（off/all/one）、状态查询
- 扫码登录，会话持久化
- 唤醒词打断时暂停音乐（不自动恢复）

**v1 不包含**

- 屏幕播放 UI、封面、歌词、进度条
- 歌单增删改、评论、播客、离线缓存
- 多房间/多设备同步、定时关闭

## 3. 架构

```
语音 → ASR → LLM function_call
                ├─ play_netease(query, kind)
                └─ control_netease(action, value)
                          ↓
              plugins_func/netease/
                eapi.py      AES-128-ECB + MD5、Cookie、device-id
                api.py       搜索/取流/每日推荐/歌单/收藏
                player.py    mpv JSON IPC、队列、事件
                session.py   0600 会话文件
                          ↓
                mpv --idle --no-video --ao=pulse
                          ↓
                PipeWire → TAS5782M → 喇叭
```

源码放在本仓库 `netease/`，部署到设备的 hub：

| 源码 | 设备路径 |
|---|---|
| `netease/hub/plugins_func/netease/` | `/home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server/plugins_func/netease/` |
| `netease/hub/plugins_func/functions/play_netease.py` | 同目录 `plugins_func/functions/` |
| `netease/hub/scripts/netease-login.py` | `.../xiaozhi-server/scripts/` |
| `netease/hub/scripts/netease-selftest.py` | 同上 |
| `netease/deploy.sh` | 不部署，部署时在本机执行 |

`plugins_func` 目前是 namespace package（没有 `__init__.py`），新增的 `plugins_func/netease/` 自带 `__init__.py` 即可被插件加载器正常 import。

## 4. 组件

### 4.1 `eapi.py`：传输层

两个通道，共用一份 Cookie 存储（`MUSIC_U`、`__csrf`、`NMTID`）：

**Web API**：`GET https://music.163.com<path>`，UA 用 Safari 桌面串，带 Cookie。用于搜索、每日推荐、歌单、账号。

**EAPI**：`POST https://interfacepc.music.163.com/eapi/<path 去掉 /api/>`，`Content-Type: application/x-www-form-urlencoded`，body 为 `params=<hex>`。

加密（照参考实现）：

```
body    = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
digest  = md5("nobody" + path + "use" + body + "md5forencrypt").hexdigest()
plain   = path + "-36cd479b6b5-" + body + "-36cd479b6b5-" + digest
params  = AES-128-ECB(key=b"e82ckenh8dichen8").encrypt(plain).hex()
```

请求头（放进 Cookie 头，和参考实现一致）：

```
os=pc, appver=3.1.17.204416,
osver=Microsoft-Windows-10-Professional-build-19045-64bit,
deviceId=<52 位大写十六进制>, requestId=<毫秒_三位>,
buildver=<unix 秒>, resolution=1920x1080, channel=netease,
versioncode=140, mobilename=, __csrf=<会话里的值>
User-Agent: NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)
```

- `device-id` 首次生成后存 `data/netease-device-id`（0600），之后复用。
- 响应里的 `Set-Cookie` 更新本地 Cookie 存储；不要用 `requests.Session` 的自动 jar，避免把站点 Cookie 混进 EAPI 请求。
- 异常：网络错误重试 1 次；业务码 301/302 或 HTTP 401 抛 `LoginRequired`；其余抛 `NeteaseError(code, msg)`。
- 日志只记 path、业务码、track id，不记 Cookie、不记完整播放 URL。

### 4.2 `api.py`：领域接口

| 用途 | 通道 | 路径与参数 |
|---|---|---|
| 搜索歌曲 | web | `GET /api/cloudsearch/pc?s=<q>&type=1&limit=<n>&offset=0&total=true` |
| 每日推荐 | web | `GET /api/v3/discovery/recommend/songs` |
| 我的歌单 | web | `GET /api/user/playlist?uid=<uid>&limit=200&offset=0` |
| 歌单详情 | web | `GET /api/v6/playlist/detail?id=<pid>&n=1000` |
| 账号 | web | `GET /api/nuser/account/get` |
| 歌曲详情/privilege | eapi | `/api/v3/song/detail`，`{"c":"[{\"id\":<id>}]"}` |
| 播放地址 | eapi | `/api/song/enhance/player/url/v1`，`{"ids":"[<id>]","level":<level>,"encodeType":"mp3\|flac"}` |
| 收藏/取消 | eapi | `/api/radio/like`，`{"alg":"itembased","trackId":"<id>","like":"true\|false","time":"3"}` |
| 二维码 key | eapi | `/api/login/qrcode/unikey`，`{"type":3}` |
| 扫码轮询 | eapi | `/api/login/qrcode/client/login`，`{"key":"<key>","type":3}` |

数据类：

```python
@dataclass
class Track:
    id: int
    name: str
    artists: list[str]
    album: str
    duration_ms: int
    fee: int                 # 1/4 = VIP/付费
    privilege: Privilege | None

@dataclass
class Stream:
    url: str
    type: str                # mp3 / flac
    level: str
    size: int
```

- `search_songs(q, limit=10)` 返回 `Track` 列表；`privilege` 缺省时由 `song_details(ids)` 补。
- `playable(track)`：`privilege.st >= 0 and privilege.pl > 0`；没有 privilege 时 `fee not in (1, 4)`。
- `song_url(id, level)`：按 `hires → lossless → exhigh → higher → standard` 逐级尝试，直到拿到非空 URL；`freeTrialInfo` 非空视为不可播；返回的 level 低于请求也接受。

### 4.3 `player.py`：mpv 与队列

单例，常驻一个 mpv：

```
mpv --idle=yes --no-video --audio-display=no --really-quiet \
    --ao=pulse --volume=<默认 70> \
    --input-ipc-server=/run/user/1000/thinksmart-mpv.sock
```

**硬性要求：`--ao=pulse`，禁止 ALSA。** 这台设备的喇叭 PCM 由 q6asm DSP 管理，绕过 PipeWire 直接碰 `/dev/snd` 会把 DSP 弄挂到重启（kiosk 单元里也写了这条）。

- 通过 Unix socket 发 JSON IPC：`loadfile <url> replace`、`set_property pause`、`seek <sec> absolute`、`set_property volume`、`stop`、`quit`。
- 事件线程收 `end-file`：`reason=eof` 下一首；`reason=error` 跳过（连续失败最多 3 首）；`reason=stop` 是用户停止，不自动下一首。
- 队列只存 `Track`，**轮到哪首才取 URL**（网易云地址会过期）；当前曲目的 URL 不缓存复用。
- 单例用 `RLock` 保护；hub 重启时先探测 socket，有残留 mpv 就 `quit` 再重拉。
- mpv 以 `ubuntu` 身份运行，继承 hub 的 `XDG_RUNTIME_DIR=/run/user/1000` 和 `PULSE_SERVER=unix:/run/user/1000/pulse/native`；不设置独立的 systemd 单元。
- 对外方法：

```python
play_queue(tracks: list[Track], start: int = 0)
play_track(track: Track, queue: list[Track] | None = None)
pause() / resume() / toggle()
next() / prev() / stop()
seek(seconds: float)
set_volume(0..100)
set_repeat("off" | "all" | "one")
status() -> {track, position, paused, volume, repeat}
pause_for_abort()          # 唤醒打断时调用
current() -> Track | None
```

### 4.4 `session.py`

- 文件：`data/netease-session.json`，`0600`，字段 `music_u`、`csrf`、`uid`、`nickname`、`saved_at`。
- `load()` / `save()` / `clear()`；原子写（临时文件 + `os.replace`）。
- `device_id()`：`data/netease-device-id`，`0600`，52 位大写十六进制。

### 4.5 插件 `play_netease.py`

注册两个函数（`ToolType.SYSTEM_CTL`，需要 `conn`）：

```python
@register_function("play_netease", play_netease_desc, ToolType.SYSTEM_CTL)
async def play_netease(conn, query: str, kind: str = "song"):
    ...

@register_function("control_netease", control_netease_desc, ToolType.SYSTEM_CTL)
async def control_netease(conn, action: str, value: str = ""):
    ...
```

返回值用 `ActionResponse(Action.RESPONSE, result=..., response="...")`，由 hub 的 TTS 播报。

插件从 `conn.config["plugins"]["play_netease"]` 读配置，缺省值如下：

```yaml
plugins:
  play_netease:
    level: exhigh          # standard/higher/exhigh/lossless/hires
    volume: 70
    mpv_path: /usr/bin/mpv
    socket: /run/user/1000/thinksmart-mpv.sock
```

库的导入方式：`from plugins_func.netease import api, player, session`（`plugins_func` 是 namespace package，hub 的工作目录就是 server 目录，直接可 import）。

### 4.6 脚本

- `scripts/netease-login.py`：`qrencode -t ANSIUTF8 "<login url>"` 在终端出二维码；同时写 `/tmp/netease-login.png` 备用。每 2 秒轮询，打印“待扫码 / 已扫码，请在手机上确认 / 成功：昵称 / 已过期”。成功后写 session。
- `scripts/netease-selftest.py`：读 session → `account()` → 搜《稻香》→ 取 `exhigh` URL → mpv 播 5 秒 → `status` → `seek 5` → `pause`/`resume` → `stop`，逐项打印 PASS/FAIL。

依赖：`apt install --no-install-recommends mpv qrencode`（mpv 0.37、qrencode 4.1.1，均在 Ubuntu 24.04 源里）。不新增 pip 包，`cryptography`、`requests` 已在 hub venv。

## 5. 登录与会话

1. 在设备上运行：
   `cd /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server && .venv/bin/python scripts/netease-login.py`
2. 手机网易云 App 扫码，确认登录。
3. 脚本把 `MUSIC_U`、`__csrf`、`uid`、`nickname` 写入 `data/netease-session.json`（0600）。
4. 插件启动时 `session.load()`；没有 session 时，点歌类请求回复“还没有登录网易云，先运行 scripts/netease-login.py 扫码”。
5. 运行中出现 `LoginRequired`：清空内存态，回复“登录过期了，重新扫码”；每个设备连接（`conn`）只提示一次，避免刷屏。

## 6. 播放行为

- 点歌：搜索前 10 条 → 优先标题完全匹配、其次标题包含、再看歌手匹配 → 取前 5 条补 `privilege` → 选第一条 `playable`。找不到时回“没找到《X》，换个说法？”。
- 歌手：`s=<歌手>` 搜索，过滤 `artists` 含该歌手（不区分大小写），取第一条可播。
- 每日推荐/歌单：整队入列，从第 0 首开始；歌单名在“我的歌单”里做模糊匹配（`difflib`，阈值 0.4），支持“我喜欢的音乐”（`specialType=5`）。
- 播放中回复：“正在播放《稻香》- 周杰伦”；队列开始回“放每日推荐，共 N 首，第一首《X》”。
- 队列结束默认**停**；用户说“循环播放”才设置 `repeat=all`，“单曲循环”设 `repeat=one`，“取消循环”设 `off`。
- 唤醒打断：`handleAbortMessage` 里 `drop_audio=True` 时调用 `pause_for_abort()`；不自动恢复。
- 音量默认 70，运行期修改只在内存，不写配置。

## 7. 语音意图与路由

hub 当前 `selected_module.Intent = function_call`，可用的函数是 `Intent.function_call.functions` 白名单。**两个新函数必须加进两个列表**（`function_call` 和 `intent_llm`），否则 LLM 看不到：

```yaml
Intent:
  intent_llm:
    functions: [..., play_music, play_netease, control_netease]
  function_call:
    functions: [..., play_music, play_netease, control_netease]
```

函数描述要写清分工：

- `play_netease`：“用网易云音乐播放歌曲/歌手/每日推荐/我的歌单时调用；`kind` 取 song/artist/daily/playlist。”
- `control_netease`：“控制正在播放的网易云音乐：pause/resume/next/prev/stop/volume/seek/like/loop/status。”
- `play_music` 的描述补一句“只播本机 music 目录里的音乐”。

如果实测 LLM 仍把“播放某歌”路由到 `play_music`，在 hub 的 agent prompt 里加一行“在线点歌优先 `play_netease`”，并保留本地音乐作为兜底。改动范围仅限 `config.yaml` 和 prompt 文件，不动核心代码。

`abortHandle.py` 的钩子：

```python
if drop_audio:
    try:
        from plugins_func.netease.player import pause_for_abort
        pause_for_abort()
    except Exception:
        pass
```

放在 `conn.client_abort = True` 之后、`clear_queues()` 之前。

## 8. 错误处理

| 情况 | 行为 |
|---|---|
| 未登录 / 会话过期 | 语音提示扫码；不重试 |
| 搜不到 | “没找到《X》，换个说法？” |
| VIP/无版权/试听片段 | 明确点歌：告知不可播；队列：跳过，连跳 3 首后停并提示 |
| 音质不满足 | 逐级降级，接受服务端返回的 level |
| 网络超时 | 重试 1 次，再失败提示“网络不太好” |
| mpv 启动失败 | 检查 `mpv` 是否存在，提示“播放器没装”；存在则重启 1 次 |
| mpv 播放出错 | 跳过当前，连续 3 首失败停止 |
| 唤醒词打断 | 暂停，不自动恢复 |
| hub 重启 | 清掉残留 mpv，避免双进程 |

## 9. 测试

**单元（stdlib `unittest`，不新增 pytest）**

- `test_eapi.py`：加密明文结构、MD5 摘要、AES 解密回环；Cookie 头拼装；`freeTrialInfo` 判定；level 降级（monkeypatch 传输层）。
- `test_queue.py`：`next/prev/repeat` 边界；`end-file` 的 eof/error/stop 分支（用假 IPC 传输）。

**集成**

- `scripts/netease-selftest.py`（需要真实 session + mpv）。
- 语音验收 10 条：
  1. “放一首稻香”
  2. “播放周杰伦”
  3. “放每日推荐”
  4. “放我的歌单 通勤”
  5. “暂停” / “继续”
  6. “下一首” / “上一首”
  7. “音量调到 40”
  8. “跳到 1 分 30 秒”
  9. “收藏这首歌”
  10. “循环播放” / “停止”
- 回归：本地 `play_music` 仍可播；唤醒时音乐暂停；hub 重启后 `pgrep -af thinksmart-mpv` 只有一个。

## 10. 部署与回滚

**部署**

1. `cp -a /home/ubuntu/xiaozhi-hub-server /home/ubuntu/xiaozhi-hub-server.bak-$(date +%F)`
2. `sudo apt-get install -y --no-install-recommends mpv qrencode`
3. 拷贝 `plugins_func/netease/`、`play_netease.py`、两个脚本。
4. 改 `config.yaml`（当前 600，用 sudo 编辑，改完 `python -c "import yaml;yaml.safe_load(open(...))"` 校验）：两个函数白名单 + `plugins.play_netease`（`level: exhigh`、`volume: 70`、`mpv_path: /usr/bin/mpv`、`socket: /run/user/1000/thinksmart-mpv.sock`）；保持 600。
5. 打 `abortHandle.py` 钩子（幂等 patch 脚本，重复执行不重复插入）。
6. `systemctl --user restart xiaozhi-hub.service`（`XDG_RUNTIME_DIR=/run/user/1000`）。
7. 跑 `netease-login.py` 扫码，再跑 `netease-selftest.py`。
8. 语音验收。

**回滚**

- 恢复 hub 备份，重启 `xiaozhi-hub`；mpv/qrencode 留着不影响。
- 单独回滚钩子：还原 `abortHandle.py`。

## 11. 风险

| 风险 | 缓解 |
|---|---|
| EAPI 是逆向协议，key/appver 可能失效 | 集中放在 `eapi.py` 常量；失效时只改这一处；错误提示明确 |
| 第三方接口/客户端违反网易云 ToS，账号可能被限流 | 默认单账号、低频率；不并发刷接口；文档写明风险 |
| 会话过期 | 明确提示重扫；session 0600 |
| mpv 用错音频后端会挂 DSP | 固定 `--ao=pulse`；selftest 检查 `status` 的 audio chain |
| hub 手动更新覆盖 `config.yaml`/`abortHandle.py` | 部署脚本幂等；备份 + 一键重放 |
| 2GB 内存 | mpv 约 40MB，插件在 hub 进程内，不新增常驻服务 |
| 网易云 URL 过期 | 每首开始时取 URL，不缓存复用 |

## 12. 验收标准

- [ ] `scripts/netease-selftest.py` 全部 PASS
- [ ] 10 条语音指令全部按预期
- [ ] 本地 `play_music` 仍正常
- [ ] 唤醒时音乐暂停，且不会自动恢复
- [ ] hub 重启后只有一个 mpv 进程
- [ ] `journalctl` 里没有 Cookie、`MUSIC_U`、完整播放 URL
- [ ] `data/netease-session.json` 与 `netease-device-id` 权限为 600
