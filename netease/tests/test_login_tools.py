import asyncio
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

register_module = types.ModuleType("plugins_func.register")
register_module.ToolType = SimpleNamespace(SYSTEM_CTL="system")
register_module.ActionResponse = lambda action, result=None, response=None: SimpleNamespace(
    action=action, result=result, response=response
)
register_module.Action = SimpleNamespace(RESPONSE="response")
register_module.register_function = lambda *_args, **_kwargs: (lambda fn: fn)
sys.modules["plugins_func.register"] = register_module

from plugins_func.functions import play_netease


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class FakeManager:
    def __init__(self):
        self.states = [
            {"ok": True, "account": {"logged_in": False},
             "task": {"id": "task-voice", "state": "waiting_scan", "qr_url": "https://music/qr",
                       "qr_data_url": "data:image/png;base64,AAA"}},
            {"ok": True, "account": {"logged_in": True, "uid": 7, "nickname": "小明"},
             "task": {"id": "task-voice", "state": "succeeded"}},
        ]

    def start(self):
        return self.states[0]

    def status(self):
        return self.states.pop(0) if self.states else {"ok": True, "account": {"logged_in": True},
                                                        "task": {"id": "task-voice", "state": "succeeded"}}

    def cancel(self, task_id):
        return {"ok": True, "account": {"logged_in": False},
                "task": {"id": task_id, "state": "cancelled", "error": "cancelled"}}


class TestLoginTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        play_netease._login_watchers.clear()
        self.manager = FakeManager()
        self.manager.states = [self.manager.states[-1]]
        self.ws = FakeWebSocket()
        self.conn = SimpleNamespace(
            websocket=self.ws,
            logger=SimpleNamespace(bind=lambda **_: SimpleNamespace(debug=lambda *_: None)),
            config={},
        )
        self.manager_patch = mock.patch.object(play_netease.login, "manager", return_value=self.manager)
        self.manager_patch.start()

    async def asyncTearDown(self):
        self.manager_patch.stop()
        for task in list(play_netease._login_watchers.values()):
            task.cancel()
        await asyncio.sleep(0)
        play_netease._login_watchers.clear()

    async def test_login_sends_initial_and_terminal_public_events(self):
        result = await play_netease.login_netease(self.conn)
        self.assertEqual(result.response, "请看屏幕上的二维码扫码登录网易云")
        await asyncio.sleep(0.01)
        self.assertEqual(self.ws.messages[0]["type"], "netease_login")
        self.assertEqual(self.ws.messages[-1]["task"]["state"], "succeeded")
        encoded = json.dumps(self.ws.messages)
        self.assertNotIn("music_u", encoded)
        self.assertNotIn("csrf", encoded)
        self.assertNotIn("nmtid", encoded)

    async def test_cancel_tool_cancels_active_task(self):
        self.manager.states = [{"ok": True, "account": {"logged_in": False},
                                "task": {"id": "task-voice", "state": "waiting_scan"}}]
        result = await play_netease.cancel_netease_login(self.conn)
        self.assertIn("已取消", result.response)
        self.assertEqual(self.ws.messages[-1]["task"]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
