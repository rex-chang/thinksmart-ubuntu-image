import json
import os
import socket
import subprocess
import threading
import time

SOCKET_PATH = "/run/user/1000/thinksmart-mpv.sock"


class Mpv:
    """One long-lived mpv process, driven over its JSON IPC socket."""

    def __init__(self, path="/usr/bin/mpv", socket_path=SOCKET_PATH, volume=70):
        self.socket_path = socket_path
        self.on_event = None
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._rid = 0
        self._lock = threading.Lock()

        self._stop_existing()
        env = os.environ.copy()
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
        self._proc = subprocess.Popen(
            [
                path,
                "--idle=yes",
                "--no-video",
                "--audio-display=no",
                "--really-quiet",
                "--ao=pulse",
                f"--volume={volume}",
                f"--input-ipc-server={socket_path}",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._sock = self._connect()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _stop_existing(self):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(1)
            s.connect(self.socket_path)
            s.sendall(b'{"command":["quit"]}\n')
            s.close()
            time.sleep(0.3)
        except OSError:
            pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _connect(self):
        for _ in range(50):
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.socket_path)
                return s
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("mpv did not start")

    def command(self, *args):
        with self._lock:
            self._sock.sendall(json.dumps({"command": list(args)}).encode() + b"\n")

    def command_wait(self, *args, timeout=1.0):
        with self._pending_lock:
            self._rid += 1
            rid = self._rid
            ev = threading.Event()
            self._pending[rid] = [ev, None]
        with self._lock:
            self._sock.sendall(
                json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n"
            )
        ev.wait(timeout)
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        return slot[1] if slot else None

    def get_property(self, name):
        return self.command_wait("get_property", name)

    def _read_loop(self):
        buf = b""
        while True:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg:
                    with self._pending_lock:
                        slot = self._pending.get(msg["request_id"])
                    if slot:
                        slot[1] = msg.get("data")
                        slot[0].set()
                if msg.get("event") and self.on_event:
                    try:
                        self.on_event(msg["event"], msg)
                    except Exception:
                        pass

    def quit(self):
        try:
            self.command("quit")
        except OSError:
            pass


class Player:
    def __init__(self, mpv, resolver, level="exhigh", volume=70):
        self.mpv = mpv
        self.resolver = resolver
        self.level = level
        self.volume = volume
        self.paused = False
        self.repeat = "off"
        self.queue = []
        self.index = -1
        self.current = None
        self._failures = 0
        mpv.on_event = self.on_event

    def play_queue(self, tracks, start=0):
        self.queue = list(tracks)
        if not self.queue:
            self.stop()
            return
        self.index = max(0, min(start, len(self.queue) - 1))
        self._failures = 0
        self._load()

    def _load(self):
        while self.queue and 0 <= self.index < len(self.queue):
            t = self.queue[self.index]
            try:
                stream = self.resolver(t.id, self.level)
            except Exception:
                self._failures += 1
                if self._failures >= 3:
                    self.stop()
                    return
                self.index += 1
                continue
            self._failures = 0
            self.current = t
            self.mpv.command("loadfile", stream.url, "replace")
            self.mpv.command("set_property", "pause", False)
            self.paused = False
            return
        self.stop()

    def on_event(self, name, data):
        if name != "end-file":
            return
        reason = data.get("reason")
        if reason == "eof":
            self.next(auto=True)
        elif reason in ("error", "aborted"):
            self.next(auto=False)

    def next(self, auto=False):
        if not self.queue:
            return
        if self.repeat == "one" and auto:
            self._load()
            return
        if self.index + 1 < len(self.queue):
            self.index += 1
            self._load()
            return
        if self.repeat == "all":
            self.index = 0
            self._load()
            return
        self.stop()

    def prev(self):
        if not self.queue:
            return
        self.index = max(0, self.index - 1)
        self._load()

    def stop(self):
        self.current = None
        self.queue = []
        self.index = -1
        self.paused = False
        self._failures = 0
        self.mpv.command("stop")

    def pause(self):
        self.mpv.command("set_property", "pause", True)
        self.paused = True

    def resume(self):
        self.mpv.command("set_property", "pause", False)
        self.paused = False

    def toggle(self):
        self.resume() if self.paused else self.pause()

    def pause_for_abort(self):
        if self.current and not self.paused:
            self.pause()

    def seek(self, seconds):
        self.mpv.command("seek", float(seconds), "absolute")

    def set_volume(self, value):
        self.volume = max(0, min(100, int(value)))
        self.mpv.command("set_property", "volume", self.volume)

    def set_repeat(self, mode):
        self.repeat = mode if mode in ("off", "all", "one") else "off"

    def status(self):
        if not self.current:
            return None
        position = None
        getter = getattr(self.mpv, "get_property", None)
        if getter:
            try:
                position = getter("time-pos")
            except Exception:
                position = None
        return {
            "id": self.current.id,
            "name": self.current.name,
            "artists": self.current.artists,
            "position": position,
            "paused": self.paused,
            "volume": self.volume,
            "repeat": self.repeat,
        }


_PLAYER = None


def get_player(config, resolver):
    global _PLAYER
    if _PLAYER is None:
        cfg = config or {}
        _PLAYER = Player(
            Mpv(
                cfg.get("mpv_path", "/usr/bin/mpv"),
                cfg.get("socket", SOCKET_PATH),
                cfg.get("volume", 70),
            ),
            resolver,
            cfg.get("level", "exhigh"),
            cfg.get("volume", 70),
        )
    return _PLAYER


def pause_for_abort():
    if _PLAYER:
        _PLAYER.pause_for_abort()
