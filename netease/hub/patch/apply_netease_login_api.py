#!/usr/bin/env python3
"""Install the local NetEase QR-login API into a xiaozhi-server checkout."""

import shutil
import sys
import time
from pathlib import Path

MARKER = "# NETEASE_LOGIN_API_PATCH_V1"
IMPORT_ANCHOR = "from aiohttp import web\n"
ROUTE_ANCHOR = '                web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),\n'
METHOD_ANCHOR = "    async def handle_kiosk_llm(self, request: web.Request):\n"

IMPORT = "import json\nfrom plugins_func.netease.login import LoginError\n"
ROUTES = """                web.get("/kiosk/netease/login/status", self.handle_kiosk_netease_status),
                web.post("/kiosk/netease/login/start", self.handle_kiosk_netease_start),
                web.post("/kiosk/netease/login/refresh", self.handle_kiosk_netease_refresh),
                web.post("/kiosk/netease/login/cancel", self.handle_kiosk_netease_cancel),
                web.post("/kiosk/netease/logout", self.handle_kiosk_netease_logout),
"""
METHODS = '''    # NETEASE_LOGIN_API_PATCH_V1
    @staticmethod
    def _netease_manager():
        from plugins_func.netease.login import manager
        return manager()

    async def _netease_body(self, request, allowed=()):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        raw = await request.content.read(1025)
        if len(raw) > 1024:
            return None, self._error("请求太大", 413)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        unknown = set(body) - set(allowed)
        if unknown:
            return None, self._error("不支持请求参数", 400)
        return body, None

    async def handle_kiosk_netease_status(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        return self._json(await self._blocking(self._netease_manager().status))

    async def handle_kiosk_netease_start(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_body(request)
        if error:
            return error
        return self._json(await self._blocking(self._netease_manager().start))

    async def _handle_kiosk_netease_task(self, request: web.Request, action):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_body(request, ("task_id",))
        if error:
            return error
        task_id = str(body.get("task_id") or "")
        if not task_id:
            return self._error("缺少 task_id", 400)
        try:
            result = await self._blocking(action, task_id)
        except LoginError as exc:
            status = 404 if exc.code == "not_found" else 409
            return self._error(exc.code, status)
        return self._json(result, 200 if result.get("ok") else 409)

    async def handle_kiosk_netease_refresh(self, request: web.Request):
        return await self._handle_kiosk_netease_task(request, self._netease_manager().refresh)

    async def handle_kiosk_netease_cancel(self, request: web.Request):
        return await self._handle_kiosk_netease_task(request, self._netease_manager().cancel)

    async def handle_kiosk_netease_logout(self, request: web.Request):
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body, error = await self._netease_body(request)
        if error:
            return error
        return self._json(await self._blocking(self._netease_manager().logout))

'''


def _backup(path: Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))


def _once(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count != 1:
        raise ValueError(f"{label} anchor count is {count}, expected 1")
    return text.replace(anchor, replacement, 1)

def apply(root: Path) -> str:
    path = root / "core/admin/admin_handler.py"
    if not path.is_file():
        raise ValueError(f"missing Hub handler: {path}")
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return "already"
    import_line = IMPORT if "import json\n" not in text else "from plugins_func.netease.login import LoginError\n"
    updated = _once(text, IMPORT_ANCHOR, IMPORT_ANCHOR + import_line, "import")
    updated = _once(updated, ROUTE_ANCHOR, ROUTES + ROUTE_ANCHOR, "route")
    updated = _once(updated, METHOD_ANCHOR, METHODS + METHOD_ANCHOR, "method")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _backup(path, stamp)
    path.write_text(updated, encoding="utf-8")
    return "patched"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: apply_netease_login_api.py <xiaozhi-server>")
        return 2
    try:
        result = apply(Path(sys.argv[1]).resolve())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"netease login API: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
