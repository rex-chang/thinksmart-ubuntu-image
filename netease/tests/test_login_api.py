import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

from aiohttp import web

PATCH = Path(__file__).parents[1] / "hub" / "patch" / "apply_netease_login_api.py"


class LoginError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeContent:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def read(self, size=-1):
        return self.raw[:size] if size >= 0 else self.raw


class FakeRequest:
    def __init__(self, raw: bytes = b"{}", content_type="application/json", remote="127.0.0.1", headers=None):
        self.content = FakeContent(raw)
        self.content_type = content_type
        self.remote = remote
        self.headers = headers or {}


def load_injected_class():
    spec = importlib.util.spec_from_file_location("apply_netease_login_api", PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = {
        "web": web,
        "json": json,
        "LoginError": LoginError,
        "staticmethod": staticmethod,
    }
    exec("class Injected:\n" + module.METHODS, namespace)
    return namespace["Injected"]


class Harness:
    def _from_this_machine(self, request):
        if "Forwarded" in request.headers or "X-Forwarded-For" in request.headers:
            return False
        return request.remote in ("127.0.0.1", "::1")

    def _json(self, data, status=200):
        return web.json_response(data, status=status)

    def _error(self, message, status):
        return web.json_response({"ok": False, "error": message}, status=status)

    async def _blocking(self, func, *args):
        return func(*args)


class TestInjectedHandlers(unittest.TestCase):
    def setUp(self):
        self.handler_cls = type("Handler", (load_injected_class(), Harness), {})
        self.fake = types.SimpleNamespace(
            status=lambda: {"ok": True, "task": None},
            start=lambda: {"ok": True, "task": {"id": "t"}},
            refresh=lambda task_id: {"ok": True, "task": {"id": task_id}},
            cancel=lambda task_id: (_ for _ in ()).throw(LoginError("not_found")),
            logout=lambda: {"ok": True},
        )
        login_module = types.ModuleType("plugins_func.netease.login")
        login_module.manager = lambda: self.fake
        for name, module in (
            ("plugins_func", types.ModuleType("plugins_func")),
            ("plugins_func.netease", types.ModuleType("plugins_func.netease")),
        ):
            sys.modules.setdefault(name, module)
        sys.modules["plugins_func.netease.login"] = login_module

    def run_handler(self, name, request):
        return asyncio.run(getattr(self.handler_cls(), name)(request))

    def test_non_loopback_is_rejected(self):
        response = self.run_handler("handle_kiosk_netease_status", FakeRequest(remote="192.168.1.5"))
        self.assertEqual(response.status, 403)

    def test_forwarded_request_is_rejected(self):
        request = FakeRequest(headers={"Forwarded": "for=1.2.3.4"})
        self.assertEqual(self.run_handler("handle_kiosk_netease_logout", request).status, 403)

    def test_start_rejects_unknown_fields_and_large_bodies(self):
        self.assertEqual(self.run_handler("handle_kiosk_netease_start", FakeRequest(b'{"x":1}')).status, 400)
        huge = FakeRequest(b"{" + b" " * 2000 + b"}")
        self.assertEqual(self.run_handler("handle_kiosk_netease_start", huge).status, 413)

    def test_task_requests_require_task_id(self):
        self.assertEqual(self.run_handler("handle_kiosk_netease_refresh", FakeRequest(b"{}")).status, 400)
        not_found = self.run_handler("handle_kiosk_netease_cancel", FakeRequest(b'{"task_id":"t"}'))
        self.assertEqual(not_found.status, 404)

    def test_status_passes_manager_result_through(self):
        response = self.run_handler("handle_kiosk_netease_status", FakeRequest())
        self.assertEqual(json.loads(response.text), {"ok": True, "task": None})

    def test_start_returns_task(self):
        response = self.run_handler("handle_kiosk_netease_start", FakeRequest(b"{}"))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["task"]["id"], "t")

    def test_wrong_content_type_is_rejected(self):
        request = FakeRequest(b"{}", content_type="text/plain")
        with self.assertRaises(web.HTTPUnsupportedMediaType):
            self.run_handler("handle_kiosk_netease_start", request)


if __name__ == "__main__":
    unittest.main()
