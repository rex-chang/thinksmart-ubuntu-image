import asyncio
import difflib
import json
import time

from plugins_func.register import register_function, ToolType, ActionResponse, Action

from plugins_func.netease import api, eapi, login, player, session

TAG = __name__

LOGIN_DESC = {
    "type": "function",
    "function": {
        "name": "login_netease",
        "description": "用户要登录网易云音乐、扫码登录网易云或重新登录网易云时调用；在设备屏幕当前对话里显示二维码。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CANCEL_LOGIN_DESC = {
    "type": "function",
    "function": {
        "name": "cancel_netease_login",
        "description": "用户要取消网易云扫码登录时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_login_watchers = {}

PLAY_DESC = {
    "type": "function",
    "function": {
        "name": "play_netease",
        "description": (
            "用网易云音乐在线播放歌曲、歌手、每日推荐或我的歌单时调用；"
            "本机 music 目录里的音乐用 play_music。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌名、歌手名或歌单名；每日推荐可以留空",
                },
                "kind": {
                    "type": "string",
                    "enum": ["song", "artist", "daily", "playlist"],
                    "description": "song=歌名，artist=歌手，daily=每日推荐，playlist=我的歌单",
                },
            },
            "required": [],
        },
    },
}

CTL_DESC = {
    "type": "function",
    "function": {
        "name": "control_netease",
        "description": (
            "控制正在播放的网易云音乐：暂停/继续/上一首/下一首/停止/"
            "音量/进度/收藏/循环/状态。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "pause", "resume", "next", "prev", "stop",
                        "volume", "seek", "like", "loop", "status",
                    ],
                },
                "value": {
                    "type": "string",
                    "description": "音量 0-100；进度秒数或 mm:ss；循环 off/all/one",
                },
            },
            "required": ["action"],
        },
    },
}


def _client():
    if not session.load():
        raise eapi.LoginRequired(301, "not logged in")
    return eapi.Client()


def _cfg(conn):
    return (conn.config.get("plugins") or {}).get("play_netease") or {}


def _resolver(track_id, level):
    return api.song_url(eapi.Client(), track_id, level)


def _say(text):
    return ActionResponse(Action.RESPONSE, result=text, response=text)


def _login_public(result):
    task = result.get("task") or {}
    account = result.get("account") or {}
    allowed_task = {
        key: task[key]
        for key in ("id", "state", "created_at", "expires_at", "qr_url", "qr_data_url", "error")
        if key in task
    }
    allowed_account = {
        key: account[key]
        for key in ("logged_in", "uid", "nickname", "saved_at")
        if key in account
    }
    return {"type": "netease_login", "task": allowed_task, "account": allowed_account}


async def _send_login(conn, result):
    ws = getattr(conn, "websocket", None) if conn is not None else None
    if ws is None:
        return
    try:
        await ws.send(json.dumps(_login_public(result), ensure_ascii=False))
    except Exception as e:
        try:
            conn.logger.bind(tag=TAG).debug(f"login event failed: {e}")
        except Exception:
            pass


async def _watch_login(conn, task_id):
    key = id(conn)
    try:
        while True:
            result = await asyncio.to_thread(login.manager().status)
            await _send_login(conn, result)
            state = (result.get("task") or {}).get("state")
            if state in ("succeeded", "expired", "cancelled", "failed"):
                return
            await asyncio.sleep(2)
    finally:
        if _login_watchers.get(key) is asyncio.current_task():
            _login_watchers.pop(key, None)


def _restart_login_watcher(conn, task_id):
    key = id(conn)
    previous = _login_watchers.get(key)
    if previous is not None:
        previous.cancel()
    _login_watchers[key] = asyncio.create_task(_watch_login(conn, task_id))


@register_function("login_netease", LOGIN_DESC, ToolType.SYSTEM_CTL)
async def login_netease(conn):
    result = await asyncio.to_thread(login.manager().start)
    await _send_login(conn, result)
    task_id = (result.get("task") or {}).get("id")
    if task_id:
        _restart_login_watcher(conn, task_id)
    return _say("请看屏幕上的二维码扫码登录网易云")


@register_function("cancel_netease_login", CANCEL_LOGIN_DESC, ToolType.SYSTEM_CTL)
async def cancel_netease_login(conn):
    current = await asyncio.to_thread(login.manager().status)
    task_id = (current.get("task") or {}).get("id")
    if not task_id:
        return _say("现在没有正在进行的网易云登录")
    result = await asyncio.to_thread(login.manager().cancel, task_id)
    await _send_login(conn, result)
    watcher = _login_watchers.pop(id(conn), None)
    if watcher is not None:
        watcher.cancel()
    return _say("已取消网易云登录")

def _queue_view(p, limit=60):
    return [{"id": t.id, "name": t.name, "artists": t.artists}
            for t in (p.queue or [])[:limit]]


async def _send(conn, payload):
    """Send one extra music event (lyrics/playlists/...) to the kiosk."""
    ws = getattr(conn, "websocket", None) if conn is not None else None
    if ws is None:
        return
    try:
        await ws.send(json.dumps({"type": "music", **payload}, ensure_ascii=False))
    except Exception as e:
        try:
            conn.logger.bind(tag=TAG).debug(f"music event failed: {e}")
        except Exception:
            pass


async def _publish(conn):
    """Tell the kiosk what is playing so it can show the now-playing bar."""
    ws = getattr(conn, "websocket", None) if conn is not None else None
    if ws is None:
        return
    try:
        p = player.get_player(_cfg(conn), _resolver)
    except Exception:
        return
    cur = p.current
    msg = {"type": "music",
           "state": "stopped" if not cur else ("paused" if p.paused else "playing")}
    if cur:
        msg.update({
            "id": cur.id, "title": cur.name, "artists": cur.artists,
            "album": cur.album, "cover": getattr(cur, "cover", ""),
            "duration": int((cur.duration_ms or 0) / 1000),
            "paused": p.paused, "queue": len(p.queue), "index": p.index,
            "queue_items": _queue_view(p),
        })
    try:
        await ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception as e:
        try:
            conn.logger.bind(tag=TAG).debug(f"music event failed: {e}")
        except Exception:
            pass


def _score(track, q):
    name = track.name.lower()
    artists = " ".join(track.artists).lower()
    if name == q:
        return 0
    if q in name or q in artists:
        return 1
    tokens = [x for x in q.replace("的", " ").split() if x]
    if tokens and all(x in name or x in artists for x in tokens):
        return 2
    return 3


def _pick(tracks, query):
    q = query.strip().lower()
    for t in sorted(tracks, key=lambda t: _score(t, q)):
        if api.playable(t):
            return t
    return None


def _same_artist(track, artist):
    artist = (artist or "").strip().lower()
    return bool(artist) and any(artist in a.lower() or a.lower() in artist for a in track.artists)


def _match_playlist(playlists, name):
    best, score = None, 0.0
    for p in playlists:
        r = difflib.SequenceMatcher(None, name, p.get("name", "")).ratio()
        if r > score:
            best, score = p, r
    return best if score >= 0.4 else None


def _play_sync(conn, query, kind):
    query = (query or "").strip()
    try:
        client = _client()
        if kind == "daily":
            tracks = api.daily_songs(client)
            label = "每日推荐"
        elif kind == "playlist":
            name = query or "我喜欢的音乐"
            me = client.account()
            if not me:
                raise eapi.LoginRequired(301, "no profile")
            pl = _match_playlist(api.user_playlists(client, me["userId"]), name)
            if not pl:
                return f"没找到歌单「{name}」"
            tracks = api.playlist_tracks(client, pl["id"])
            label = f"歌单「{pl.get('name', name)}」"
        else:
            if not query:
                return "想听什么歌？说个歌名或歌手。"
            tracks = api.search_songs(client, query, 30 if kind == "artist" else 10)
            label = ""

        if not tracks:
            return f"没找到《{query}》，换个说法？"

        if kind in ("daily", "playlist"):
            chosen, queue = tracks[0], tracks
        else:
            chosen = _pick(tracks, query)
            if not chosen:
                return f"《{query}》要会员或没有版权，换一首？"
            if kind == "artist":
                related = [t for t in tracks
                           if t.id != chosen.id and _same_artist(t, query) and api.playable(t)]
                queue = [chosen] + related[:20]
            else:
                queue = [chosen]

        p = player.get_player(_cfg(conn), _resolver)
        p.play_queue(queue)
        if not p.current:
            conn.logger.bind(tag=TAG).warning(f"no stream for {chosen.name!r}")
            return f"《{chosen.name}》暂时放不了（版权或网络），换一首？"
        # loadfile is asynchronous: wait until mpv actually holds a file, otherwise a
        # resolver/HTTP failure would still be reported as "正在播放".
        deadline = time.time() + 2.5
        started = False
        while time.time() < deadline:
            idle = p.mpv.get_property("idle-active")
            if idle is False:
                started = True
                break
            time.sleep(0.1)
        if not started or not p.current:
            conn.logger.bind(tag=TAG).warning(f"mpv did not start {chosen.name!r}")
            p.stop()
            return f"《{chosen.name}》暂时放不了（版权或网络），换一首？"
        actual = p.current
        who = "、".join(actual.artists) or "未知歌手"
        if kind in ("daily", "playlist"):
            return f"开始播放{label}，共 {len(queue)} 首，第一首《{actual.name}》- {who}"
        if kind == "artist" and len(queue) > 1:
            return f"开始播放{query}，共 {len(queue)} 首，第一首《{actual.name}》- {who}"
        return f"正在播放《{actual.name}》- {who}"
    except eapi.LoginRequired:
        return "还没有登录网易云，先运行 scripts/netease-login.py 扫码登录"
    except eapi.NeteaseError as e:
        conn.logger.bind(tag=TAG).warning(f"netease api error: {e}")
        return "网易云接口出错了，稍后再试"
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"play_netease failed: {e}")
        return "网易云播放出错了，稍后再试"


def _control_sync(conn, action, value):
    try:
        p = player.get_player(_cfg(conn), _resolver)
        if action == "pause":
            if not p.current:
                return "现在没有在放歌"
            p.pause()
            return "好，暂停了"
        if action == "resume":
            if not p.current:
                return "现在没有在放歌"
            p.resume()
            return "继续播放"
        if action == "next":
            if not p.current:
                return "现在没有在放歌"
            if not p.queue:
                return "队列是空的"
            if p.repeat != "all" and p.index + 1 >= len(p.queue):
                return "已经是最后一首了"
            p.next()
            return "下一首"
        if action == "prev":
            if not p.current:
                return "现在没有在放歌"
            if not p.queue:
                return "队列是空的"
            if p.index <= 0:
                return "已经是第一首了"
            p.prev()
            return "上一首"
        if action == "stop":
            p.stop()
            return "停了"
        if action == "volume":
            p.set_volume(int(float(value)))
            return f"音量调到 {p.volume}"
        if action == "seek":
            if ":" in value:
                minutes, seconds = value.split(":", 1)
                target = int(minutes) * 60 + int(seconds)
            else:
                target = int(float(value))
            if not p.current:
                return "现在没有在放歌"
            p.seek(target)
            return f"跳到 {target // 60} 分 {target % 60} 秒"
        if action == "like":
            if not p.current:
                return "现在没有在放歌"
            api.like(_client(), p.current.id, True)
            return f"收藏了《{p.current.name}》"
        if action == "loop":
            mode = value if value in ("off", "all", "one") else "off"
            p.set_repeat(mode)
            return {"off": "取消循环", "all": "列表循环", "one": "单曲循环"}[mode]
        if action == "status":
            st = p.status()
            if not st:
                return "现在没有在放歌"
            text = f"正在播放《{st['name']}》- {'、'.join(st['artists']) or '未知歌手'}"
            if st.get("position") is not None:
                pos = int(st["position"])
                text += f"，已播 {pos // 60} 分 {pos % 60} 秒"
            if st["paused"]:
                text += "，已暂停"
            return text
        return "不支持这个操作"
    except eapi.LoginRequired:
        return "还没有登录网易云，先扫码登录"
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"control_netease failed: {e}")
        return "操作失败了"


async def music_command(conn, action, value=""):
    """Direct commands from the kiosk player: no model turn, no TTS."""
    action = (action or "").strip()
    value = "" if value is None else str(value)
    if action == "status":
        await _publish(conn)
        return ""
    if action in ("pause", "resume", "next", "prev", "stop", "seek"):
        text = await asyncio.to_thread(_control_sync, conn, action, value)
        await _publish(conn)
        return text
    if action == "lyrics":
        try:
            song_id = int(value)
            lrc, tlrc = await asyncio.to_thread(api.lyrics, _client(), song_id)
            await _send(conn, {"subtype": "lyrics", "id": song_id,
                               "lrc": lrc, "tlyric": tlrc})
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"lyrics failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "歌词加载失败"})
        return ""
    if action == "playlists":
        try:
            client = _client()
            me = client.account()
            if not me:
                raise eapi.LoginRequired(301, "no profile")
            items = await asyncio.to_thread(api.user_playlists, client, me["userId"])
            await _send(conn, {"subtype": "playlists", "items": [
                {"id": p.get("id"), "name": p.get("name", ""),
                 "cover": p.get("coverImgUrl", ""), "count": p.get("trackCount", 0)}
                for p in items
            ]})
        except eapi.LoginRequired:
            await _send(conn, {"subtype": "error", "message": "还没有登录网易云"})
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"playlists failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "歌单加载失败"})
        return ""
    if action == "play_playlist":
        try:
            tracks = await asyncio.to_thread(api.playlist_tracks, _client(), int(value))
            if not tracks:
                raise ValueError("empty playlist")
            p = player.get_player(_cfg(conn), _resolver)
            p.play_queue(tracks)
            await _publish(conn)
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"play playlist failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "歌单播放失败"})
        return ""
    if action == "playlist_tracks":
        try:
            tracks = await asyncio.to_thread(api.playlist_tracks, _client(), int(value))
            await _send(conn, {"subtype": "playlist_tracks", "playlist": int(value), "items": [
                {"id": t.id, "name": t.name, "artists": t.artists,
                 "duration": int((t.duration_ms or 0) / 1000),
                 "cover": getattr(t, "cover", "")} for t in tracks
            ]})
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"playlist tracks failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "歌单加载失败"})
        return ""
    if action == "play_playlist_track":
        try:
            pid, idx = value.split(":", 1)
            tracks = await asyncio.to_thread(api.playlist_tracks, _client(), int(pid))
            if not tracks:
                raise ValueError("empty playlist")
            p = player.get_player(_cfg(conn), _resolver)
            p.play_queue(tracks)
            p.play_at(int(idx))
            await _publish(conn)
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"play playlist track failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "歌单播放失败"})
        return ""
    if action == "play_index":
        try:
            p = player.get_player(_cfg(conn), _resolver)
            if not p.queue:
                return ""
            p.play_at(int(value))
            await _publish(conn)
        except Exception as e:
            try:
                conn.logger.bind(tag=TAG).warning(f"play index failed: {e}")
            except Exception:
                pass
            await _send(conn, {"subtype": "error", "message": "切歌失败"})
        return ""
    if action == "queue":
        try:
            p = player.get_player(_cfg(conn), _resolver)
            await _send(conn, {"subtype": "queue", "items": _queue_view(p),
                               "index": p.index})
        except Exception:
            pass
        return ""
    return ""


@register_function("play_netease", PLAY_DESC, ToolType.SYSTEM_CTL)
async def play_netease(conn, query: str = "", kind: str = "song"):
    if kind not in ("song", "artist", "daily", "playlist"):
        kind = "song"
    text = await asyncio.to_thread(_play_sync, conn, query, kind)
    await _publish(conn)
    return _say(text)


@register_function("control_netease", CTL_DESC, ToolType.SYSTEM_CTL)
async def control_netease(conn, action: str, value: str = ""):
    text = await asyncio.to_thread(_control_sync, conn, action, value)
    await _publish(conn)
    return _say(text)
