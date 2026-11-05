#!/usr/bin/env python3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins_func.netease import eapi, session


def show(url):
    try:
        subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
    except FileNotFoundError:
        print("(qrencode 未安装，无法在终端显示二维码)")
    try:
        subprocess.run(["qrencode", "-o", "/tmp/netease-login.png", url], check=False)
        print("二维码也写在 /tmp/netease-login.png")
    except FileNotFoundError:
        pass
    print(url)


def main():
    client = eapi.Client()
    res = client.eapi_post("/api/login/qrcode/unikey", {"type": 3})
    key = res.get("unikey")
    if not key:
        print("拿不到二维码 key:", res)
        return 1

    show("https://music.163.com/login?codekey=" + key)
    print("等待扫码…（手机网易云 App 扫码并确认）")

    last = None
    for _ in range(150):
        r = client.eapi_post("/api/login/qrcode/client/login", {"key": key, "type": 3})
        code = r.get("code")
        if code == 800:
            print("二维码过期了，重新运行本脚本")
            return 1
        if code == 802 and last != 802:
            print("已扫码，请在手机上确认")
        if code == 803:
            me = client.account() or {}
            session.save(client.music_u, client.csrf, me.get("userId", 0), me.get("nickname", ""))
            print(f"登录成功：{me.get('nickname', '')} (uid={me.get('userId', '')})")
            return 0
        last = code
        time.sleep(2)

    print("等待超时，重新运行本脚本")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
