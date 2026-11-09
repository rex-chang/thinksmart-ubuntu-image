import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class TestVoiceQrContract(unittest.TestCase):
    def test_client_has_ephemeral_login_event_and_proxy(self):
        app = (ROOT / "kiosk" / "thinksmart_client" / "app.py").read_text()
        web = (ROOT / "kiosk" / "thinksmart_client" / "web.py").read_text()
        self.assertIn("netease_login", app)
        self.assertIn("/kiosk/netease/login/{action}", app)
        self.assertIn("/api/netease/login/cancel", web)
        self.assertIn("netease_login", app[app.index("def snapshot"):])

    def test_card_is_separate_from_thread_messages(self):
        html = (ROOT / "kiosk" / "web" / "v2" / "index.html").read_text()
        js = (ROOT / "kiosk" / "web" / "v2" / "app.js").read_text()
        self.assertIn('id="neteaseLoginCard"', html)
        self.assertIn("ui.neteaseLogin", js)
        self.assertIn("data:image/png;base64,", js)
        self.assertIn("/api/netease/login/${action}", js)
        self.assertIn("task_id", js)

    def test_hub_tools_are_registered(self):
        source = (ROOT / "hub" / "plugins_func" / "functions" / "play_netease.py").read_text()
        self.assertIn('@register_function("login_netease"', source)
        self.assertIn('@register_function("cancel_netease_login"', source)
        self.assertIn('"type": "netease_login"', source)


if __name__ == "__main__":
    unittest.main()
