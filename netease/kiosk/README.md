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

## QR login backend

The Hub package includes a local-only QR login backend. It does not change the kiosk UI in this phase.

Deploy the Hub package, including the idempotent API patch:

```bash
./netease/deploy.sh
```

The SSH CLI remains available:

```bash
cd /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
python3 scripts/netease-login.py
```

The same login state is available to a future kiosk settings page through the Hub's loopback-only API (`server.http_port`, 16688 on this device):

```text
GET  /kiosk/netease/login/status
POST /kiosk/netease/login/start
POST /kiosk/netease/login/refresh
POST /kiosk/netease/login/cancel
POST /kiosk/netease/logout
```

POST bodies are JSON objects. `start` and `logout` require `{}`; `refresh` and `cancel` require `{"task_id":"..."}`. The response includes a short-lived QR URL and in-memory PNG data URL, but never includes `MUSIC_U`, CSRF, NMTID, cookies, or device IDs. Requests with forwarding headers or non-loopback addresses are rejected. `python3 scripts/netease-selftest.py --login-api` checks the backend import and safe session summary without contacting NetEase.

说“登录网易云”“帮我扫码登录网易云”或“网易云登录”时，Hub 会调用 `login_netease`，二维码会显示在当前对话里的登录卡片中，不会写入聊天历史。卡片会显示等待扫码、手机确认、成功、过期或取消状态，并提供刷新和取消按钮；手机网易云 App 扫码并确认才会完成授权。取消也可以直接说“取消网易云登录”。本流程不额外播报 TTS。

The login card is ephemeral and separate from persisted chat messages. The existing player bar and Hub music handler are independent of this voice-login flow.

登录成功后卡片移除二维码图片，保留账号与完成状态，并提示可以说“播放一首歌”或“播放我的歌单”。登录只保存授权，不会自动开始播放；不需要再次扫码。

## Conversation regression verification

Client deployments must exercise an ordinary request through `/api/text`, observe an actual reply and the `thinking → speaking → idle/listening` transitions on `/events`, and exercise setting the current volume through `/api/volume`. `/api/health` alone does not verify conversation or volume controls. Preserve `Client.volume_lock`, the `/api/sleep` and `/api/brightness` routes, and the conversation container when integrating login UI. The 2026-11-09 recovery restored these accidentally removed elements without modifying Hub model configuration.

The diagnostics panel renderer must use a distinct name (`renderDeviceStatus`), not shadow the conversation's `renderStatus`. The duplicate declaration caused startup to fail at `undefined.cpu` before `connectEvents()`, leaving models and conversation state visually empty even though the Hub configuration remained intact. Verify the real page consumes SSE and displays the configured model and an ordinary reply after deployment.
