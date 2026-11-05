#!/usr/bin/env python3
"""Insert the wake-word pause hook into the hub's abortHandle.py (idempotent).

Usage: sudo python3 apply_abort_hook.py /path/to/core/handle/abortHandle.py
"""
import sys
from pathlib import Path

NEEDLE = "    conn.client_abort = True\n"
BLOCK = (
    NEEDLE
    + "    if drop_audio:\n"
    + "        try:\n"
    + "            from plugins_func.netease.player import pause_for_abort\n"
    + "            pause_for_abort()\n"
    + "        except Exception:\n"
    + "            pass\n"
)


def main():
    if len(sys.argv) != 2:
        print("usage: apply_abort_hook.py <abortHandle.py>")
        return 2
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    if "pause_for_abort" in text:
        print("already patched")
        return 0
    if NEEDLE not in text:
        print("needle not found; patch by hand")
        return 1
    path.write_text(text.replace(NEEDLE, BLOCK, 1), encoding="utf-8")
    print("patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
