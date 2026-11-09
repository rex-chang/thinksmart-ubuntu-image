# Complete client deployment (Bluetooth included)

The client is one versioned release; this directory no longer carries a patched copy of the app or
the web UI. The source of truth is the integrated client checkout, a sibling of this repository:

```
../xiaozhi-thinksmart-client-integration
```

It is the upstream client plus the NetEase now-playing bar, the inline QR login card and the
Bluetooth support (`thinksmart_client/bt.py`, `bt_helper.py`, audio routing). Every deployment
ships that checkout's whole tracked tree as one version.

The old `thinksmart_client/` and `web/v2/` partial snapshots have been removed. Build and preview
only the complete client checkout. The legacy `thinksmart-status` helper remains a separate
optional CLI; it is not part of the complete-client release.

## Deploy

```bash
CLIENT_DIR=/Users/admin/rexob/others/xiaozhi-thinksmart-client-integration \
    ./netease/kiosk/deploy.sh
```

`CLIENT_DIR` is required and must be a **clean, committed** checkout (the script refuses a dirty
tree: only tracked files are packaged, so untracked files would otherwise vanish silently). The
version in `thinksmart_client/__init__.py` must be newer than what the device runs; the updater
refuses equal or older versions.

Other variables: `HOST` (default `ubuntu@10.9.1.60`), `SSH_OPTS` (as `ts.sh` sets it),
`OUT` (build the release into this directory instead of a temporary one),
`ENV_CACHE` (make-release's Python environment cache, default
`~/.cache/thinksmart-release/envs`) and `DEPLOY_KEEP=1` (keep the remote staging).

Preview the client's web UI locally (no device):

```bash
CLIENT_DIR=/Users/admin/rexob/others/xiaozhi-thinksmart-client-integration \
    ./netease/kiosk/preview.sh
```

## What the script does

1. Validates `CLIENT_DIR` (git checkout, tracked `thinksmart_client/__init__.py`,
   `requirements.lock`, `deploy/thinksmart-client.service`, clean tree, numeric version), checks
   the device has `/usr/local/sbin/thinksmart-update`, and stops if the version is not newer than
   the device's `/opt/thinksmart/client/current`.
2. Builds a client-only release locally with `update/make-release.py`: a deterministic tarball of
   every tracked file plus `client-env-<id>.tar.gz` for the `requirements.lock` it ships (built in
   an arm64 Ubuntu 24.04 container when the environment is not cached, so Docker must be available
   the first time).
3. Copies `latest/` and `download/` to `/var/tmp/thinksmart-client-deploy-<stamp>/` on the device.
4. Runs the device's own updater as `thinksmart-update install VERSION`, in-process
   (`runpy`), with a temporary configuration whose only source is a loopback
   `http://127.0.0.1:<ephemeral>/` server over that staging directory. The real
   `/etc/thinksmart-update.conf` is not touched.

Because the real updater does the install, the usual guarantees apply:

- updater lock and interrupted-update recovery first (`boot` finishes or undoes a cut-off install);
- every file is checked against the manifest's size and sha256;
- the Python environment is installed from `requirements.lock` (Bluetooth dependencies included);
- `current` is switched atomically and the old version is kept as `previous`;
- the new version must answer `/api/health` with its version, audio and the hub, and stay up;
  otherwise the updater switches `current` back to `previous` and reports `rolled_back`.

User data lives outside `/opt/thinksmart/client`: `~/.config/thinksmart-client` (device preferences),
the hub's `data/` (including `netease-session.json`), the kiosk configuration and Wi-Fi. It is
never copied, replaced or removed.

On success the release staging is deleted and `/opt/thinksmart/client/previous` keeps the old
version; on failure the staging is kept for inspection and
`/var/lib/thinksmart-update/status.json` holds the updater's message. The updater's success path
also removes version directories older than `current` and `previous`.

Publishing the same release through GitHub and letting the device's 软件更新 page install it uses
this identical `install` command; this script only supplies the release from a local build.

## Hub side

`./netease/deploy.sh` deploys **only the Hub**: it backs up the touched Hub files, copies
`netease/hub/`, applies the idempotent music/login patches, installs `mpv`/`qrencode`, compiles and
self-tests, and restarts `xiaozhi-hub.service`. It no longer copies client files, so the device
must already run a complete client release (above) for the now-playing bar and the login card.

The hub must have the direct-control handler and the metadata publisher:

- `netease/hub/patch/apply_music_handler.py` (installs `musicMessageHandler.py` and
  registers `TextMessageType.MUSIC`);
- `netease/hub/plugins_func/netease/api.py` (`Track.cover`);
- `netease/hub/plugins_func/functions/play_netease.py` (`music` events, playback
  start verification);
- `netease/hub/plugins_func/netease/player.py` (mpv self-heal, direct CDN).

## QR login backend

The Hub package includes a local-only QR login backend; the card that displays the QR code is part
of the complete client. Deploy the Hub package first:

```bash
./netease/deploy.sh
```

The SSH CLI remains available:

```bash
cd /home/ubuntu/xiaozhi-hub-server/main/xiaozhi-server
.venv/bin/python3 scripts/netease-login.py
```

The client reaches the same login state through the Hub's loopback-only API (`server.http_port`,
16688 on this device):

```text
GET  /kiosk/netease/login/status
POST /kiosk/netease/login/start
POST /kiosk/netease/login/refresh
POST /kiosk/netease/login/cancel
POST /kiosk/netease/logout
```

POST bodies are JSON objects. `start` and `logout` require `{}`; `refresh` and `cancel` require
`{"task_id":"..."}`. The response includes a short-lived QR URL and in-memory PNG data URL, but
never includes `MUSIC_U`, CSRF, NMTID, cookies, or device IDs. Requests with forwarding headers or
non-loopback addresses are rejected. `.venv/bin/python3 scripts/netease-selftest.py --login-api`
checks the backend import and safe session summary without contacting NetEase.

说“登录网易云”“帮我扫码登录网易云”或“网易云登录”时，Hub 会调用 `login_netease`，二维码会显示在当前对话里的登录卡片中，不会写入聊天历史。卡片会显示等待扫码、手机确认、成功、过期或取消状态，并提供刷新和取消按钮；手机网易云 App 扫码并确认才会完成授权。取消也可以直接说“取消网易云登录”。本流程不额外播报 TTS。

The login card is ephemeral and separate from persisted chat messages. The player bar and the Hub
music handler are independent of this voice-login flow.

登录成功后卡片移除二维码图片，保留账号与完成状态，并提示可以说“播放一首歌”或“播放我的歌单”。登录只保存授权，不会自动开始播放；不需要再次扫码。

## Conversation regression verification

Client deployments must exercise an ordinary request through `/api/text`, observe an actual reply
and the `thinking → speaking → idle/listening` transitions on `/events`, and exercise setting the
current volume through `/api/volume`. `/api/health` alone does not verify conversation or volume
controls. Preserve `Client.volume_lock`, the `/api/sleep` and `/api/brightness` routes, and the
conversation container when integrating login UI. The 2026-11-09 recovery restored these
accidentally removed elements without modifying Hub model configuration.

The diagnostics panel renderer must use a distinct name (`renderDeviceStatus`), not shadow the
conversation's `renderStatus`. The duplicate declaration caused startup to fail at `undefined.cpu`
before `connectEvents()`, leaving models and conversation state visually empty even though the Hub
configuration remained intact. Verify the real page consumes SSE and displays the configured model
and an ordinary reply after deployment.
