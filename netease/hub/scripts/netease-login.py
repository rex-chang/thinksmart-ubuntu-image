#!/usr/bin/env python3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins_func.netease import login


def show(url):
    try:
        subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
    except FileNotFoundError:
        print("(qrencode 未安装，无法在终端显示二维码)")
    print(url)


def main():
    manager = login.manager()
    started = manager.start()
    task = started.get("task") or {}
    task_id = task.get("id")
    if not task_id:
        print("登录失败：没有任务", file=sys.stderr)
        return 1
    if task.get("qr_url"):
        show(task["qr_url"])
    print("等待扫码…（手机网易云 App 扫码并确认）")
    deadline = time.monotonic() + login.TASK_SECONDS + 5
    last_state = task.get("state")
    while time.monotonic() < deadline:
        result = manager.status()
        current = result.get("task") or {}
        state = current.get("state")
        if state != last_state:
            if state == "scanned":
                print("已扫码，请在手机上确认")
            last_state = state
        if state in ("succeeded", "expired", "cancelled", "failed"):
            if state == "succeeded":
                account = result.get("account") or {}
                print(f"登录成功：{account.get('nickname', '')} (uid={account.get('uid', '')})")
                return 0
            print(f"登录失败：{current.get('error') or state}")
            return 1
        time.sleep(2)
    print("登录超时，请重新运行本脚本")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
