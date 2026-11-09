import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "hub" / "scripts" / "netease-login.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("netease_login_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeManager:
    def __init__(self, states):
        self.states = list(states)

    def start(self):
        return {"ok": True, "task": {"id": "task-1", "state": "waiting_scan", "qr_url": "https://example/qr"}}

    def status(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {
            "ok": True,
            "account": {"nickname": "小明", "uid": 42},
            "task": {"id": "task-1", "state": state, "error": "" if state == "succeeded" else state},
        }


class TestCli(unittest.TestCase):
    def run_cli(self, manager, sleep=None):
        cli = load_cli()
        out = io.StringIO()
        patches = [
            mock.patch.object(cli.login, "manager", lambda: manager),
            mock.patch.object(cli.subprocess, "run", lambda *a, **k: None),
            mock.patch.object(cli.time, "sleep", sleep or (lambda _s: None)),
        ]
        for patcher in patches:
            patcher.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = cli.main()
        finally:
            for patcher in patches:
                patcher.stop()
        return code, out.getvalue()

    def test_success_prints_qr_and_returns_zero(self):
        code, output = self.run_cli(FakeManager(["waiting_scan", "scanned", "succeeded"]))
        self.assertEqual(code, 0)
        self.assertIn("https://example/qr", output)
        self.assertIn("已扫码", output)
        self.assertIn("登录成功", output)
        self.assertIn("小明", output)

    def test_failure_returns_non_zero(self):
        code, output = self.run_cli(FakeManager(["waiting_scan", "expired"]))
        self.assertEqual(code, 1)
        self.assertIn("登录失败", output)

    def test_cli_does_not_poll_net_ease_itself(self):
        cli = load_cli()
        self.assertFalse(hasattr(cli, "eapi"), "the CLI must reuse login.manager, not call eapi")


if __name__ == "__main__":
    unittest.main()
