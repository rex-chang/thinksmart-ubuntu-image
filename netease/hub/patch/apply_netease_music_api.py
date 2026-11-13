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

    async def _netease_music_body(self, request, allowed=()):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        raw = await request.content.read(1025)
        if len(raw) > 1024:
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


def _backup(path: Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def _once(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count != 1:
        raise ValueError(f"{label} anchor count is {count}, expected 1")
    return text.replace(anchor, replacement, 1)


def apply(root: Path) -> str:
    path = root / "core/admin/admin_handler.py"
    if not path.is_file():
        raise ValueError(f"missing Hub handler: {path}")
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return "already"
    import_line = IMPORT if "import json\n" not in text else ""
    updated = _once(text, IMPORT_ANCHOR, IMPORT_ANCHOR + import_line, "import")
    updated = _once(updated, ROUTE_ANCHOR, ROUTES + ROUTE_ANCHOR, "route")
    updated = _once(updated, METHOD_ANCHOR, METHODS + METHOD_ANCHOR, "method")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _backup(path, stamp)
    path.write_text(updated, encoding="utf-8")
    return "patched"


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
