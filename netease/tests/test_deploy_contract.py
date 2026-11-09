import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class TestDeployContract(unittest.TestCase):
    def test_deploy_applies_login_patch_and_checks_runtime(self):
        script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        for command in (
            "apply_netease_login_api.py",
            "$PYTHON -m py_compile",
            ".venv/bin/python3",
            "qrencode",
            "mpv",
            "netease-selftest.py --login-api",
            "systemctl --user restart xiaozhi-hub.service",
        ):
            self.assertIn(command, script)

    def test_docs_define_local_only_backend(self):
        docs = (ROOT / "kiosk" / "README.md").read_text(encoding="utf-8")
        self.assertIn("local-only", docs)
        self.assertIn("/kiosk/netease/login/status", docs)
        self.assertIn("no kiosk login UI", docs)


if __name__ == "__main__":
    unittest.main()
