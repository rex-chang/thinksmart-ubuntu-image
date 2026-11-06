#!/usr/bin/env python3
"""Install the hub's direct music-control message handler (idempotent).

The kiosk now-playing bar sends {"type": "music", "action": "pause"}; this
handler runs control_netease directly, without a model turn or TTS.

Usage: python3 apply_music_handler.py /path/to/xiaozhi-server
"""
import shutil
import sys
import time
from pathlib import Path

HANDLER = '''from typing import Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from plugins_func.functions.play_netease import music_command


class MusicTextMessageHandler(TextMessageHandler):
    """Direct player commands from the kiosk: no model, no TTS."""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MUSIC

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        action = (msg_json.get("action") or "").strip()
        value = msg_json.get("value", "")
        if action not in ("pause", "resume", "next", "prev", "stop", "seek",
                          "status", "lyrics", "playlists", "play_playlist", "playlist_tracks", "play_playlist_track", "play_index", "queue"):
            return
        await music_command(conn, action, value)
'''


def _backup(path: Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def patch_type(root: Path, stamp: str) -> str:
    path = root / "core/handle/textMessageType.py"
    text = path.read_text(encoding="utf-8")
    if 'MUSIC = "music"' in text:
        return "already"
    needle = '    PING = "ping"\n'
    if needle not in text:
        return "needle not found"
    _backup(path, stamp)
    path.write_text(text.replace(needle, needle + '    MUSIC = "music"\n', 1), encoding="utf-8")
    return "patched"


def patch_registry(root: Path, stamp: str) -> str:
    path = root / "core/handle/textMessageHandlerRegistry.py"
    text = path.read_text(encoding="utf-8")
    if "MusicTextMessageHandler" in text:
        return "already"
    imp = "from core.handle.textHandler.pingMessageHandler import PingMessageHandler\n"
    entry = "            PingMessageHandler(),\n"
    if imp not in text or entry not in text:
        return "needle not found"
    _backup(path, stamp)
    text = text.replace(
        imp,
        imp + "from core.handle.textHandler.musicMessageHandler import MusicTextMessageHandler\n",
        1,
    )
    text = text.replace(entry, entry + "            MusicTextMessageHandler(),\n", 1)
    path.write_text(text, encoding="utf-8")
    return "patched"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: apply_music_handler.py <xiaozhi-server>")
        return 2
    root = Path(sys.argv[1]).resolve()
    if not (root / "core/handle").is_dir():
        print(f"not a xiaozhi-server root: {root}")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")

    handler = root / "core/handle/textHandler/musicMessageHandler.py"
    handler.parent.mkdir(parents=True, exist_ok=True)
    handler.write_text(HANDLER, encoding="utf-8")

    print("textMessageType:", patch_type(root, stamp))
    print("registry:", patch_registry(root, stamp))
    print("handler:", handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
