import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugins_func.netease import session


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(session, "DATA", d),
            mock.patch.object(session, "SESSION_FILE", d / "netease-session.json"),
            mock.patch.object(session, "DEVICE_FILE", d / "netease-device-id"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_save_load_roundtrip(self):
        session.save("mu", "csrf", 42, "小明")
        got = session.load()
        self.assertEqual(got["music_u"], "mu")
        self.assertEqual(got["csrf"], "csrf")
        self.assertEqual(got["uid"], 42)
        self.assertEqual(got["nickname"], "小明")
        self.assertEqual(session.SESSION_FILE.stat().st_mode & 0o777, 0o600)

    def test_clear(self):
        session.save("mu", "csrf", 42, "小明")
        session.clear()
        self.assertIsNone(session.load())

    def test_device_id_is_52_hex_and_stable(self):
        d1 = session.device_id()
        self.assertRegex(d1, r"^[0-9A-F]{52}$")
        self.assertEqual(d1, session.device_id())
        self.assertEqual(session.DEVICE_FILE.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
