import asyncio
import difflib

from plugins_func.register import register_function, ToolType, ActionResponse, Action

from plugins_func.netease import api, eapi, player, session

TAG = __name__

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
            tracks = api.search_songs(client, query, 10)
            label = ""

        if not tracks:
            return f"没找到《{query}》，换个说法？"

        if kind in ("daily", "playlist"):
            chosen, queue = tracks[0], tracks
        else:
            chosen = _pick(tracks, query)
            if not chosen:
                return f"《{query}》要会员或没有版权，换一首？"
            queue = [chosen]

        p = player.get_player(_cfg(conn), _resolver)
        p.play_queue(queue)
        who = "、".join(chosen.artists) or "未知歌手"
        if kind in ("daily", "playlist"):
            return f"开始播放{label}，共 {len(queue)} 首，第一首《{chosen.name}》- {who}"
        return f"正在播放《{chosen.name}》- {who}"
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
            if not p.queue:
                return "队列是空的"
            p.next()
            return "下一首"
        if action == "prev":
            if not p.queue:
                return "队列是空的"
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


@register_function("play_netease", PLAY_DESC, ToolType.SYSTEM_CTL)
async def play_netease(conn, query: str = "", kind: str = "song"):
    if kind not in ("song", "artist", "daily", "playlist"):
        kind = "song"
    return _say(await asyncio.to_thread(_play_sync, conn, query, kind))


@register_function("control_netease", CTL_DESC, ToolType.SYSTEM_CTL)
async def control_netease(conn, action: str, value: str = ""):
    return _say(await asyncio.to_thread(_control_sync, conn, action, value))
