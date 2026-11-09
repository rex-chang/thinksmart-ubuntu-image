#!/usr/bin/env python3
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins_func.netease import api, eapi, login, player, session


def login_api_selftest():
    summary = session.public_summary()
    print("login API: ready", "logged_in=" + str(summary["logged_in"]).lower())
    return 0


def playback_selftest():
    if not session.load():
        print("FAIL: 没有登录态，先运行 scripts/netease-login.py")
        return 1

    client = eapi.Client()
    me = client.account()
    print("account:", (me or {}).get("nickname") or "FAIL")
    if not me:
        return 1

    tracks = api.search_songs(client, "稻香", 3)
    print("search:", len(tracks), "OK" if tracks else "FAIL")
    if not tracks:
        return 1

    p = player.get_player(
        {"level": "exhigh", "volume": 70},
        lambda tid, lv: api.song_url(client, tid, lv),
    )
    try:
        p.play_queue([tracks[0]])
        print("play:", tracks[0].name, "-", "、".join(tracks[0].artists))
        time.sleep(5)
        st = p.status()
        print("status:", st)
        if not st:
            print("FAIL: 没有播放状态")
            return 1
        p.seek(5)
        print("seek 5s OK")
        p.pause()
        print("paused:", p.status()["paused"])
        p.resume()
        p.stop()
        print("stopped")
        print("PASS")
        return 0
    finally:
        p.mpv.quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-api", action="store_true")
    args = parser.parse_args()
    if args.login_api:
        return login_api_selftest()
    return playback_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
