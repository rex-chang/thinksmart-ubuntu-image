import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from plugins_func.netease import login, session


class FakeClient:
    responses = []
    keys = []
    profile = {"userId": 42, "nickname": "小明"}

    def __init__(self):
        self.music_u = "new-secret"
        self.csrf = "new-csrf"
        self.calls = []

    def eapi_post(self, path, data):
        self.calls.append((path, data))
        if path.endswith("/unikey"):
            return {"unikey": self.keys.pop(0)}
        return self.responses.pop(0)

    def account(self):
        return self.profile


class TestLoginManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(session, "DATA", data),
            mock.patch.object(session, "SESSION_FILE", data / "netease-session.json"),
            mock.patch.object(session, "DEVICE_FILE", data / "netease-device-id"),
        ]
        for patcher in self.patches:
            patcher.start()
        self.clients = []

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.tmp.cleanup()

    def manager(self, responses, encoder=None):
        FakeClient.responses = list(responses)
        FakeClient.keys = ["key-1", "key-2"]
        def factory():
            client = FakeClient()
            self.clients.append(client)
            return client

        return login.LoginManager(
            client_factory=factory,
            qr_encoder=encoder or (lambda _url: b"PNG"),
            sleep=lambda _seconds: None,
            poll_seconds=0,
        )

    def test_qr_states_and_successful_session(self):
        manager = self.manager([{"code": 801}, {"code": 802}, {"code": 803}])
        task = manager.start()
        result = manager.wait(task["task"]["id"], timeout=1)

        self.assertEqual(result["task"]["state"], "succeeded")
        self.assertEqual(result["account"]["nickname"], "小明")
        self.assertEqual(result["account"]["uid"], 42)
        self.assertNotIn("music_u", str(result))
        self.assertNotIn("csrf", str(result))
        self.assertEqual(session.load()["music_u"], "new-secret")

    def test_expired_qr_can_be_refreshed_once(self):
        manager = self.manager([{"code": 800}, {"code": 803}])
        first = manager.start()
        expired = manager.wait(first["task"]["id"], timeout=1)
        self.assertEqual(expired["task"]["state"], "expired")

        refreshed = manager.refresh(first["task"]["id"])
        result = manager.wait(refreshed["task"]["id"], timeout=1)
        self.assertEqual(result["task"]["state"], "succeeded")
        self.assertNotEqual(first["task"]["qr_url"], refreshed["task"]["qr_url"])

    def test_start_reuses_active_task_and_cancel_is_idempotent(self):
        gate = []
        manager = self.manager([{"code": 801}])
        manager._sleep = lambda _seconds: gate.append(True)
        first = manager.start()
        second = manager.start()
        self.assertEqual(first["task"]["id"], second["task"]["id"])

        cancelled = manager.cancel(first["task"]["id"])
        self.assertEqual(cancelled["task"]["state"], "cancelled")
        self.assertEqual(manager.cancel(first["task"]["id"])["task"]["state"], "cancelled")


    def test_qr_renderer_failure_is_terminal_without_credentials(self):
        manager = self.manager([{"code": 803}], encoder=lambda _url: (_ for _ in ()).throw(login.QrRenderError()))
        result = manager.wait(manager.start()["task"]["id"], timeout=1)
        self.assertEqual(result["task"]["state"], "failed")
        self.assertEqual(result["task"]["error"], "qr_render_failed")
        self.assertIsNone(session.load())


class TestPublicSummary(unittest.TestCase):
    def test_public_summary_filters_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw)
            with mock.patch.object(session, "SESSION_FILE", data / "session.json"):
                session.save("secret", "csrf", 7, "name")
                summary = session.public_summary()
        self.assertEqual(summary["logged_in"], True)
        self.assertEqual(summary["uid"], 7)
        self.assertEqual(summary["nickname"], "name")
        self.assertNotIn("music_u", summary)
        self.assertNotIn("csrf", summary)
        self.assertNotIn("nmtid", summary)


if __name__ == "__main__":
    unittest.main()
