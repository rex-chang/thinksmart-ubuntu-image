import json
import os
import secrets
import time
from pathlib import Path

# <server>/plugins_func/netease/session.py -> <server>/data
DATA = Path(__file__).resolve().parents[2] / "data"
SESSION_FILE = DATA / "netease-session.json"
DEVICE_FILE = DATA / "netease-device-id"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load():
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(music_u: str, csrf: str = "", uid: int = 0, nickname: str = "") -> None:
    _atomic_write(
        SESSION_FILE,
        json.dumps(
            {
                "music_u": music_u,
                "csrf": csrf,
                "uid": uid,
                "nickname": nickname,
                "saved_at": int(time.time()),
            },
            ensure_ascii=False,
        ),
    )


def clear() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def device_id() -> str:
    try:
        d = DEVICE_FILE.read_text(encoding="utf-8").strip()
        if len(d) == 52:
            return d
    except OSError:
        pass
    d = "".join(secrets.choice("0123456789ABCDEF") for _ in range(52))
    _atomic_write(DEVICE_FILE, d)
    return d
