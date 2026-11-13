#!/usr/bin/env python3
"""Install the local NetEase music control API into a xiaozhi-server checkout.

Loopback-only routes on the hub admin server for thinksmart-home (127.0.0.1:8095):
  GET  /kiosk/netease/music/status    what is playing (the home music card's one poll)
  GET  /kiosk/netease/music/lyrics    current track lyrics, parsed from LRC
  POST /kiosk/netease/music/seek      {"sec": float}
  POST /kiosk/netease/music/volume    {"volume": 0-100}
  POST /kiosk/netease/music/control   {"action": pause|resume|toggle|next|prev|stop|loop|single|mode}

The player is the same mpv singleton the voice tools use (plugins_func/netease/player.py),
so the kiosk now-playing bar, voice commands and the home card all move the same queue.
Shuffle is not implemented by the player and reports unsupported. Ambient / sleep features
of the original QQ-music service do not exist here; thinksmart-home hides them.
"""

import shutil
import sys
import time
from pathlib import Path

MARKER = "# NETEASE_MUSIC_API_PATCH_V1"
MARKER2 = "# NETEASE_MUSIC_API_PATCH_V2"
IMPORT_ANCHOR = "from aiohttp import web\n"
ROUTE_ANCHOR = '                web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),\n'
METHOD_ANCHOR = "    async def handle_kiosk_llm(self, request: web.Request):\n"

IMPORT = "import json\n"
ROUTES = """                web.get("/kiosk/netease/music/status", self.handle_kiosk_netease_music_status),
                web.get("/kiosk/netease/music/lyrics", self.handle_kiosk_netease_music_lyrics),
                web.post("/kiosk/netease/music/seek", self.handle_kiosk_netease_music_seek),
                web.post("/kiosk/netease/music/volume", self.handle_kiosk_netease_music_volume),
                web.post("/kiosk/netease/music/control", self.handle_kiosk_netease_music_control),
"""
METHODS = '''    # NETEASE_MUSIC_API_PATCH_V1
    @staticmethod
    def _netease_music_player():
        from plugins_func.netease.player import get_player

        def resolver(track_id, level):
            from plugins_func.netease import api, eapi

            return api.song_url(eapi.Client(), track_id, level)

        return get_player({}, resolver)

    @staticmethod
    def _netease_music_public(st):
        """Player.status() -> the shape thinksmart-home's music card expects."""
        if not st:
            return {"playing": False, "paused": False, "elapsed": 0, "duration": 0, "volume": 70,
                    "play_mode": "loop", "lyrics_seq": 0, "ambient": None, "sleep": None,
                    "sleep_session": None, "sleep_preview": None, "track": None}
        return {"playing": not st.get("paused"), "paused": bool(st.get("paused")),
                "elapsed": st.get("position") or 0, "duration": st.get("duration") or 0,
                "volume": st.get("volume", 70),
                "play_mode": "single" if st.get("repeat") == "one" else "loop",
                "lyrics_seq": st.get("id") or 0, "ambient": None, "sleep": None,
                "sleep_session": None, "sleep_preview": None,
                "track": {"title": st.get("name") or "", "artist": ", ".join(st.get("artists") or []),
                          "cover": st.get("cover") or "", "mid": str(st.get("id") or "")}}

    @staticmethod
    def _netease_lrc_rows(lrc, tlrc, limit=300):
        import re

        def parse(text):
            rows = {}
            for line in (text or "").splitlines():
                stamps = []
                rest = line
                while rest.startswith("["):
                    close = rest.find("]")
                    if close < 0:
                        break
                    tag = rest[1:close]
                    m = re.match(r"^(\\d+):(\\d+(?:\\.\\d+)?)$", tag)
                    if not m:
                        break
                    stamps.append(int(m.group(1)) * 60 + float(m.group(2)))
                    rest = rest[close + 1:]
                txt = rest.strip()
                if txt and stamps:
                    for t in stamps:
                        rows[round(t, 2)] = txt
            return rows

        base, trans = parse(lrc), parse(tlrc)
        merged = []
        for t in sorted(base):
            text = base[t] + (f"（{trans[t]}）" if trans.get(t) and trans[t] != base[t] else "")
            merged.append({"time": t, "text": text[:160]})
        return merged[:limit]

    async def _netease_music_body(self, request, allowed=(), max_bytes=1024):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        raw = await request.content.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None, self._error("请求太大", 413)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        unknown = set(body) - set(allowed)
        if unknown:
            return None, self._error("不支持请求参数", 400)
        return body, None

    async def handle_kiosk_netease_music_status(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        try:
            st = await self._blocking(self._netease_music_player().status)
        except Exception:
            st = None
        return self._json(self._netease_music_public(st))

    async def handle_kiosk_netease_music_lyrics(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        player = self._netease_music_player()
        try:
            st = await self._blocking(player.status)
        except Exception:
            st = None
        if not st:
            return self._json({"lyrics": [], "seq": 0, "mid": ""})

        def fetch():
            from plugins_func.netease import api, eapi

            return api.lyrics(eapi.Client(), st["id"])

        try:
            lrc, tlrc = await self._blocking(fetch)
        except Exception:
            return self._json({"lyrics": [], "seq": 0, "mid": ""})
        rows = self._netease_lrc_rows(lrc, tlrc)
        return self._json({"lyrics": rows, "seq": st.get("id") or 0,
                           "mid": str(st.get("id") or "")})

    async def handle_kiosk_netease_music_seek(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("sec",))
        if error:
            return error
        try:
            sec = max(0.0, min(86400.0, float(body.get("sec"))))
        except (TypeError, ValueError):
            return self._error("sec 格式错误", 400)
        player = self._netease_music_player()
        if not await self._blocking(player.status):
            return self._error("现在没有在播放", 409)
        await self._blocking(player.seek, sec)
        return self._json({"ok": True, "elapsed": round(sec, 2)})

    async def handle_kiosk_netease_music_volume(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("volume",))
        if error:
            return error
        try:
            volume = max(0, min(100, int(round(float(body.get("volume"))))))
        except (TypeError, ValueError):
            return self._error("volume 格式错误", 400)
        await self._blocking(self._netease_music_player().set_volume, volume)
        return self._json({"ok": True, "volume": volume})

    async def handle_kiosk_netease_music_control(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("action",))
        if error:
            return error
        action = str(body.get("action") or "").strip().lower()
        if action not in ("pause", "resume", "toggle", "next", "prev", "stop", "loop", "single", "shuffle", "mode"):
            return self._error("不支持的动作", 400)
        if action == "shuffle":
            return self._error("暂不支持随机播放", 400)
        player = self._netease_music_player()
        if action == "loop":
            await self._blocking(player.set_repeat, "all")
        elif action == "single":
            await self._blocking(player.set_repeat, "one")
        elif action == "mode":
            st = await self._blocking(player.status)
            await self._blocking(player.set_repeat, "all" if (st or {}).get("repeat") == "one" else "one")
        elif action == "toggle":
            st = await self._blocking(player.status)
            if not st:
                return self._error("现在没有在播放", 409)
            await self._blocking(player.resume if st.get("paused") else player.pause)
        else:
            await self._blocking(getattr(player, action))
        st = await self._blocking(player.status)
        pub = self._netease_music_public(st)
        return self._json({"ok": True, "play_mode": pub["play_mode"]})

'''

ROUTES2 = """                web.get("/kiosk/netease/music/search", self.handle_kiosk_netease_music_search),
                web.get("/kiosk/netease/music/playlists", self.handle_kiosk_netease_music_playlists),
                web.get("/kiosk/netease/music/playlist_tracks", self.handle_kiosk_netease_music_playlist_tracks),
                web.get("/kiosk/netease/music/queue", self.handle_kiosk_netease_music_queue),
                web.post("/kiosk/netease/music/play_queue", self.handle_kiosk_netease_music_play_queue),
                web.post("/kiosk/netease/music/play_index", self.handle_kiosk_netease_music_play_index),
                web.post("/kiosk/netease/music/like", self.handle_kiosk_netease_music_like),
"""
METHODS2 = '''    # NETEASE_MUSIC_API_PATCH_V2
    @staticmethod
    def _netease_music_track(t):
        return {"id": t.id, "name": t.name, "artists": list(t.artists or []),
                "album": t.album or "", "duration_ms": t.duration_ms or 0,
                "cover": getattr(t, "cover", "") or ""}

    @staticmethod
    def _netease_music_client():
        from plugins_func.netease import eapi, session

        if not session.load():
            raise PermissionError("还没有登录网易云")
        return eapi.Client()

    async def _netease_music_list(self, request, fn):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        try:
            items = await self._blocking(fn)
        except PermissionError as exc:
            return self._error(str(exc), 401)
        except Exception as exc:
            return self._error(f"请求失败：{type(exc).__name__}", 502)
        return self._json({"ok": True, "items": [self._netease_music_track(t) for t in items]})

    async def handle_kiosk_netease_music_search(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        q = str(request.query.get("q") or "").strip()[:60]
        if not q:
            return self._error("缺少搜索词", 400)
        from plugins_func.netease import api
        return await self._netease_music_list(request, lambda: api.search_songs(self._netease_music_client(), q, 20))

    async def handle_kiosk_netease_music_playlists(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)

        def fetch():
            from plugins_func.netease import api
            client = self._netease_music_client()
            me = client.account()
            if not me:
                raise PermissionError("还没有登录网易云")
            return api.user_playlists(client, me["userId"])

        try:
            pls = await self._blocking(fetch)
        except PermissionError as exc:
            return self._error(str(exc), 401)
        except Exception as exc:
            return self._error(f"请求失败：{type(exc).__name__}", 502)
        items = [{"id": p.get("id"), "name": p.get("name", ""), "cover": p.get("coverImgUrl", ""),
                  "count": p.get("trackCount", 0)} for p in pls]
        return self._json({"ok": True, "items": items})

    async def handle_kiosk_netease_music_playlist_tracks(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        try:
            pid = int(request.query.get("id") or 0)
        except ValueError:
            pid = 0
        if not pid:
            return self._error("缺少歌单 id", 400)
        from plugins_func.netease import api
        return await self._netease_music_list(request, lambda: api.playlist_tracks(self._netease_music_client(), pid))

    async def handle_kiosk_netease_music_queue(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        player = self._netease_music_player()
        items = [{"id": t.id, "name": t.name, "artists": list(t.artists or []),
                  "album": t.album or "", "duration_ms": t.duration_ms or 0,
                  "cover": getattr(t, "cover", "") or ""} for t in (player.queue or [])[:100]]
        return self._json({"ok": True, "items": items, "index": player.index})

    async def handle_kiosk_netease_music_play_queue(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("tracks", "start"), max_bytes=262144)
        if error:
            return error
        from plugins_func.netease.api import Track
        raw = body.get("tracks")
        if not isinstance(raw, list) or not raw or len(raw) > 200:
            return self._error("tracks 格式错误", 400)
        tracks = []
        for t in raw:
            if not isinstance(t, dict) or type(t.get("id")) is not int or not t.get("name"):
                return self._error("track 格式错误", 400)
            artists = t.get("artists")
            tracks.append(Track(id=t["id"], name=str(t["name"])[:200],
                                artists=[str(a)[:80] for a in artists][:10] if isinstance(artists, list) else [],
                                album=str(t.get("album") or "")[:200],
                                duration_ms=int(t.get("duration_ms") or 0),
                                cover=str(t.get("cover") or "")[:500]))
        try:
            start = max(0, min(int(body.get("start") or 0), len(tracks) - 1))
        except (TypeError, ValueError):
            start = 0
        player = self._netease_music_player()
        try:
            await self._blocking(player.play_queue, tracks, start)
        except Exception as exc:
            from plugins_func.netease import eapi
            if isinstance(exc, eapi.NeteaseError):
                msg = ("网易云检测到当前网络环境存在风险，暂时无法播放，请稍后再试"
                       if exc.code == -460 else f"网易云接口出错了（{exc.code}），请稍后再试")
                return self._error(msg, 502)
            return self._error(f"播放失败：{type(exc).__name__}: {exc}"[:300], 500)
        return self._json({"ok": True, "count": len(tracks)})

    async def handle_kiosk_netease_music_play_index(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("index",))
        if error:
            return error
        if type(body.get("index")) is not int or body["index"] < 0:
            return self._error("index 格式错误", 400)
        player = self._netease_music_player()
        try:
            ok = await self._blocking(player.play_at, body["index"])
        except Exception as exc:
            from plugins_func.netease import eapi
            if isinstance(exc, eapi.NeteaseError):
                msg = ("网易云检测到当前网络环境存在风险，暂时无法播放，请稍后再试"
                       if exc.code == -460 else f"网易云接口出错了（{exc.code}），请稍后再试")
                return self._error(msg, 502)
            return self._error(f"播放失败：{type(exc).__name__}: {exc}"[:300], 500)
        return self._json({"ok": bool(ok)})

    async def handle_kiosk_netease_music_like(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_music_body(request, ("id", "like"))
        if error:
            return error
        if type(body.get("id")) is not int or type(body.get("like")) is not bool:
            return self._error("参数格式错误", 400)
        from plugins_func.netease import api
        try:
            ok = await self._blocking(api.like, self._netease_music_client(), body["id"], body["like"])
        except PermissionError as exc:
            return self._error(str(exc), 401)
        except Exception as exc:
            return self._error(f"操作失败：{type(exc).__name__}", 502)
        return self._json({"ok": bool(ok)})

'''


def _backup(path: Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def _once(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count != 1:
        raise ValueError(f"{label} anchor count is {count}, expected 1")
    return text.replace(anchor, replacement, 1)


# Targeted fixes for devices that already have V2 (a fresh V2 block above already contains
# them). Each replaces its old text once; a second run finds nothing and is a no-op.
V2_FIXES = [
    # play_queue carries whole playlists (tens of KB) — the 1 KB login-style cap rejected them.
    ("    async def _netease_music_body(self, request, allowed=()):",
     "    async def _netease_music_body(self, request, allowed=(), max_bytes=1024):"),
    ("        raw = await request.content.read(1025)\n        if len(raw) > 1024:",
     "        raw = await request.content.read(max_bytes + 1)\n        if len(raw) > max_bytes:"),
    ('        body, error = await self._netease_music_body(request, ("tracks", "start"))',
     '        body, error = await self._netease_music_body(request, ("tracks", "start"), max_bytes=262144)'),
    # Surface NetEase risk-control (-460) as a friendly message instead of a bare 500.
    ("        player = self._netease_music_player()\n        await self._blocking(player.play_queue, tracks, start)\n        return self._json({\"ok\": True, \"count\": len(tracks)})",
     "        player = self._netease_music_player()\n        try:\n            await self._blocking(player.play_queue, tracks, start)\n        except Exception as exc:\n            from plugins_func.netease import eapi\n            if isinstance(exc, eapi.NeteaseError):\n                msg = (\"网易云检测到当前网络环境存在风险，暂时无法播放，请稍后再试\"\n                       if exc.code == -460 else f\"网易云接口出错了（{exc.code}），请稍后再试\")\n                return self._error(msg, 502)\n            return self._error(f\"播放失败：{type(exc).__name__}: {exc}\"[:300], 500)\n        return self._json({\"ok\": True, \"count\": len(tracks)})"),
    ("        player = self._netease_music_player()\n        ok = await self._blocking(player.play_at, body[\"index\"])\n        return self._json({\"ok\": bool(ok)})",
     "        player = self._netease_music_player()\n        try:\n            ok = await self._blocking(player.play_at, body[\"index\"])\n        except Exception as exc:\n            from plugins_func.netease import eapi\n            if isinstance(exc, eapi.NeteaseError):\n                msg = (\"网易云检测到当前网络环境存在风险，暂时无法播放，请稍后再试\"\n                       if exc.code == -460 else f\"网易云接口出错了（{exc.code}），请稍后再试\")\n                return self._error(msg, 502)\n            return self._error(f\"播放失败：{type(exc).__name__}: {exc}\"[:300], 500)\n        return self._json({\"ok\": bool(ok)})"),
]


def apply(root: Path) -> str:
    path = root / "core/admin/admin_handler.py"
    if not path.is_file():
        raise ValueError(f"missing Hub handler: {path}")
    text = path.read_text(encoding="utf-8")
    changed = []
    if MARKER not in text:
        import_line = IMPORT if "import json\n" not in text else ""
        text = _once(text, IMPORT_ANCHOR, IMPORT_ANCHOR + import_line, "import")
        text = _once(text, ROUTE_ANCHOR, ROUTES + ROUTE_ANCHOR, "route")
        text = _once(text, METHOD_ANCHOR, METHODS + METHOD_ANCHOR, "method")
        changed.append("v1")
    if MARKER2 not in text:
        text = _once(text, ROUTE_ANCHOR, ROUTES2 + ROUTE_ANCHOR, "route2")
        text = _once(text, METHOD_ANCHOR, METHODS2 + METHOD_ANCHOR, "method2")
        changed.append("v2")
    for old, new in V2_FIXES:
        if old in text:
            text = text.replace(old, new, 1)
            if "fix" not in changed:
                changed.append("fix")
    if not changed:
        return "already"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _backup(path, stamp)
    path.write_text(text, encoding="utf-8")
    return "patched " + "+".join(changed)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: apply_netease_music_api.py <xiaozhi-server>")
        return 2
    try:
        result = apply(Path(sys.argv[1]).resolve())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"netease music API: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
