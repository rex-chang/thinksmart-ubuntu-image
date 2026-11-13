import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PATCH = Path(__file__).parents[1] / "hub" / "patch" / "apply_netease_music_api.py"

HANDLER = '''from aiohttp import web

class AdminHandler:
    def register(self, app):
        app.add_routes([
                web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),
        ])

    async def handle_kiosk_llm(self, request: web.Request):
        return None

    async def handle_kiosk_chat_clear(self, request):
        return None
'''


class HandlerHarness:
    """Loads the patched admin-handler source with fakes for its hub helpers."""

    def __init__(self, player_status=None, player=None):
        self.calls = []
        self.responses = {}
        self.player = player or FakePlayer(player_status)

    def load(self, handler_text):
        path = Path(tempfile.mkdtemp()) / "admin_handler.py"
        path.write_text(handler_text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"admin_handler_{id(self)}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        handler = module.AdminHandler()
        handler._netease_music_player = lambda: self.player
        handler._from_this_machine = lambda request: True
        handler._error = self._error
        handler._json = lambda payload: {"json": payload}
        handler._blocking = self._blocking
        return handler

    def _error(self, message, status=400):
        return {"error": message, "status": status}

    async def _blocking(self, fn, *args):
        self.calls.append((getattr(fn, "__name__", repr(fn)), args))
        result = fn(*args)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class FakePlayer:
    def __init__(self, status=None):
        self._base = dict(status) if status else None
        self.repeat = (status or {}).get("repeat", "off")

    def status(self):
        if self._base is None:
            return None
        return {**self._base, "repeat": self.repeat}

    def pause(self):
        pass

    def resume(self):
        pass

    def next(self):
        pass

    def prev(self):
        pass

    def stop(self):
        pass

    def seek(self, sec):
        pass

    def set_volume(self, v):
        pass

    def set_repeat(self, mode):
        self.repeat = mode


class _FakeContent:
    def __init__(self, raw):
        self._raw = raw

    async def read(self, n=-1):
        return self._raw[:n] if n and n > 0 else self._raw


class FakeRequest:
    def __init__(self, body=None, content_type="application/json"):
        self.body = body if body is not None else {}
        self.content_type = content_type
        self.content = _FakeContent(json.dumps(self.body).encode())


def status_playing():
    return {"id": 347230, "name": "富士山下", "artists": ["陈奕迅"], "album": "认了吧",
            "cover": "https://p3.music.126.net/x.jpg", "position": 42.5, "duration": 247,
            "paused": False, "volume": 60, "repeat": "one"}


class TestMusicPatch(unittest.TestCase):
    def make_tree(self, handler=HANDLER):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = root / "core/admin/admin_handler.py"
        path.parent.mkdir(parents=True)
        path.write_text(handler, encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return root, path

    def run_patch(self, root):
        return subprocess.run([sys.executable, str(PATCH), str(root)],
                              capture_output=True, text=True)

    def test_patch_is_idempotent(self):
        root, path = self.make_tree()
        first = self.run_patch(root)
        self.assertEqual(first.returncode, 0, first.stderr)
        patched = path.read_bytes()
        self.assertIn(b"NETEASE_MUSIC_API_PATCH_V1", patched)
        second = self.run_patch(root)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already", second.stdout)
        self.assertEqual(path.read_bytes(), patched)

    def test_missing_anchor_does_not_modify(self):
        root, path = self.make_tree(HANDLER.replace('/kiosk/chat/clear', '/kiosk/other'))
        before = path.read_bytes()
        result = self.run_patch(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.bak-*")), [])

    def test_coexists_with_login_patch(self):
        root, path = self.make_tree()
        login = Path(__file__).parents[1] / "hub" / "patch" / "apply_netease_login_api.py"
        r1 = subprocess.run([sys.executable, str(login), str(root)], capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.run_patch(root)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("patched", r2.stdout)
        text = path.read_text()
        self.assertIn("NETEASE_LOGIN_API_PATCH_V1", text)
        self.assertIn("NETEASE_MUSIC_API_PATCH_V1", text)


class TestMusicHandlers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "core/admin/admin_handler.py"
        path.parent.mkdir(parents=True)
        path.write_text(HANDLER, encoding="utf-8")
        result = subprocess.run([sys.executable, str(PATCH), str(root)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        cls.source = path.read_text(encoding="utf-8")

    def harness(self, status=None):
        h = HandlerHarness(player_status=status)
        return h, h.load(self.source)

    def test_status_maps_player_fields(self):
        h, handler = self.harness(status_playing())
        out = asyncio.run(handler.handle_kiosk_netease_music_status(FakeRequest()))
        payload = out["json"]
        self.assertTrue(payload["playing"])
        self.assertEqual(payload["track"]["title"], "富士山下")
        self.assertEqual(payload["track"]["artist"], "陈奕迅")
        self.assertEqual(payload["track"]["mid"], "347230")
        self.assertEqual(payload["play_mode"], "single")
        self.assertEqual(payload["elapsed"], 42.5)
        self.assertEqual(payload["duration"], 247)
        self.assertIsNone(payload["ambient"])

    def test_status_idle_shape(self):
        h, handler = self.harness(status=None)
        out = asyncio.run(handler.handle_kiosk_netease_music_status(FakeRequest()))
        payload = out["json"]
        self.assertFalse(payload["playing"])
        self.assertIsNone(payload["track"])
        self.assertEqual(payload["play_mode"], "loop")

    def test_control_actions_and_mode_cycle(self):
        h, handler = self.harness(status_playing())
        out = asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "pause"})))
        self.assertTrue(out["json"]["ok"])
        self.assertIn(("pause", ()), h.calls)
        # mode flips one -> all (single -> loop)
        out = asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "mode"})))
        self.assertEqual(out["json"]["play_mode"], "loop")
        self.assertEqual(h.player.repeat, "all")
        out = asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "mode"})))
        self.assertEqual(out["json"]["play_mode"], "single")
        self.assertEqual(h.player.repeat, "one")

    def test_control_shuffle_unsupported(self):
        h, handler = self.harness(status_playing())
        out = asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "shuffle"})))
        self.assertEqual(out["status"], 400)

    def test_control_rejects_unknown_action(self):
        h, handler = self.harness(status_playing())
        out = asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "eject"})))
        self.assertEqual(out["status"], 400)

    def test_seek_and_volume(self):
        h, handler = self.harness(status_playing())
        out = asyncio.run(handler.handle_kiosk_netease_music_seek(FakeRequest({"sec": 90})))
        self.assertTrue(out["json"]["ok"])
        self.assertIn(("seek", (90.0,)), h.calls)
        out = asyncio.run(handler.handle_kiosk_netease_music_volume(FakeRequest({"volume": 35})))
        self.assertEqual(out["json"]["volume"], 35)
        self.assertIn(("set_volume", (35,)), h.calls)

    def test_seek_without_track_is_409(self):
        h, handler = self.harness(status=None)
        out = asyncio.run(handler.handle_kiosk_netease_music_seek(FakeRequest({"sec": 10})))
        self.assertEqual(out["status"], 409)

    def test_toggle_pauses_playing_track(self):
        h, handler = self.harness(status_playing())
        asyncio.run(handler.handle_kiosk_netease_music_control(FakeRequest({"action": "toggle"})))
        self.assertIn(("pause", ()), h.calls)


class TestLrcParser(unittest.TestCase):
    def test_rows(self):
        h = HandlerHarness()
        handler = h.load(TestMusicHandlers.source)
        rows = handler._netease_lrc_rows(
            "[ti:富士山下]\n[00:01.00]第一句\n[00:05.50]第二句\n",
            "[00:01.00]First line\n")
        self.assertEqual(rows[0], {"time": 1.0, "text": "第一句（First line）"})
        self.assertEqual(rows[1], {"time": 5.5, "text": "第二句"})
        # metadata line without timestamp is dropped
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
