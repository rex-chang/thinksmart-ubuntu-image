import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PATCH = Path(__file__).parents[1] / "hub" / "patch" / "apply_netease_login_api.py"


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


class TestLoginPatch(unittest.TestCase):
    def make_tree(self, handler=HANDLER):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = root / "core/admin/admin_handler.py"
        path.parent.mkdir(parents=True)
        path.write_text(handler, encoding="utf-8")
        self.addCleanup(tmp.cleanup)
        return root, path

    def run_patch(self, root):
        return subprocess.run(
            [sys.executable, str(PATCH), str(root)],
            capture_output=True,
            text=True,
        )

    def test_patch_is_idempotent(self):
        root, path = self.make_tree()
        first = self.run_patch(root)
        self.assertEqual(first.returncode, 0, first.stderr)
        patched = path.read_bytes()
        self.assertIn(b"NETEASE_LOGIN_API_PATCH_V1", patched)
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

    def test_ambiguous_anchor_does_not_modify(self):
        root, path = self.make_tree(HANDLER + HANDLER)
        before = path.read_bytes()
        result = self.run_patch(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.bak-*")), [])


if __name__ == "__main__":
    unittest.main()
