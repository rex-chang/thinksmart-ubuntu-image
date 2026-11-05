# Kiosk now-playing patch (client 0.1.3)

Snapshot of the patched ThinkSmart kiosk client on top of `/opt/thinksmart/client/0.1.3`.
It adds the bottom now-playing bar for NetEase playback and direct playback controls.

## Files

- `thinksmart_client/app.py`
  - handles the hub's `{"type":"music", ...}` events;
  - publishes them on the client's SSE bus;
  - includes the state in the `/events` snapshot;
  - polls mpv's IPC (`time-pos`, `duration`, `pause`, `idle-active`) once a second;
  - `music_action()` sends `{"type":"music","action":...}` to the hub, so the bar's
    buttons do **not** go through the LLM or TTS.
- `thinksmart_client/web.py`
  - `POST /api/music` (`pause` / `resume` / `next` / `prev` / `stop`).
- `web/v2/index.html`, `web/v2/app.js`, `web/v2/app.css`
  - fixed bottom bar: cover, title, artist, progress, prev/pause/next;
  - play/pause/skip glyphs are drawn with CSS pseudo-elements: WebKitGTK did not
    paint the equivalent inline SVGs inside the buttons.
- `deploy.sh` — copies these files over `BASE`, backs up the old ones and restarts
  `thinksmart-client.service` + `thinksmart-kiosk.service`.

## Deploy

```bash
./netease/kiosk/deploy.sh
# different host / client version:
HOST=ubuntu@10.9.1.60 BASE=/opt/thinksmart/client/0.1.4 ./netease/kiosk/deploy.sh
```

A client update replaces the versioned directory; re-apply this snapshot and bump
`BASE` after such an update. The script keeps `*.bak-<timestamp>` files and a
`/tmp/kiosk-web-v2-bak-<timestamp>.tar`.

## Hub side

The hub must also have the direct-control handler and the metadata publisher:

- `netease/hub/patch/apply_music_handler.py` (installs `musicMessageHandler.py` and
  registers `TextMessageType.MUSIC`);
- `netease/hub/plugins_func/netease/api.py` (`Track.cover`);
- `netease/hub/plugins_func/functions/play_netease.py` (`music` events, playback
  start verification);
- `netease/hub/plugins_func/netease/player.py` (mpv self-heal, direct CDN).
