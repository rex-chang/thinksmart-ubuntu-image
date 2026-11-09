# Voice-Triggered Inline NetEase QR Login

## Goal

Let a user say “登录网易云” in the existing conversation and receive a QR-login card in that conversation. The card is an ephemeral UI event, not a chat message or persisted history entry.

## Behavior

- The Hub model invokes a `login_netease` tool for natural requests to log into NetEase Cloud Music.
- The tool starts the existing local `LoginManager` and sends a `netease_login` WebSocket event to the current device connection.
- The client forwards that event through its existing SSE stream.
- The kiosk inserts or updates an inline conversation card.
- The card shows QR data, status (`waiting_scan`, `scanned`, `succeeded`, `expired`, `cancelled`, `failed`), account nickname on success, refresh, and cancel actions.
- The card remains in the conversation after terminal state but is never added to `ui.thread.messages` or Hub chat history.
- No additional TTS is generated.
- The phone's NetEase app remains the authorization step; voice only opens the QR.

## Architecture

### Hub

Add `login_netease` to the existing NetEase function module. It uses `login.manager().start()` in a worker thread, returns a direct short response such as “请看屏幕上的二维码扫码登录网易云”, and sends the task summary to `conn.websocket` as:

```json
{"type":"netease_login","task":{...},"account":{...}}
```

Add a small helper that sends login status updates without credentials. The tool starts a background poller for the task and sends updates until terminal state. The existing `/kiosk/netease/login/*` API remains the authority for refresh/cancel/status.

Add `cancel_netease_login` as a second tool for spoken cancellation. It cancels the active task for the current process and sends the terminal event.

### Client

In `thinksmart_client/app.py`, handle WebSocket `type=netease_login` and publish an SSE `netease_login` event. Store the latest ephemeral state in `self.netease_login` for reconnect snapshots, but do not put it in the thread message list. Add local HTTP proxy endpoints:

```text
POST /api/netease/login/refresh {"task_id":"..."}
POST /api/netease/login/cancel  {"task_id":"..."}
```

The proxy calls the Hub's loopback-only endpoint through `_hub_http`, then publishes the returned state. It validates the action and task ID format before forwarding.

### Kiosk

Add a `netease_login` card container inside `#messages` or immediately after it in the conversation pane. JavaScript keeps `ui.neteaseLogin` separately from `ui.thread.messages`. The SSE handler updates the card and scrolls the conversation area. The card uses the returned `qr_data_url` directly in an `<img>` with no HTML interpolation of the data URL. Buttons call the client proxy endpoints.

The card refreshes status while an active task exists by polling the client proxy status path at a bounded interval; polling stops at terminal state or page disconnect. The card retains terminal state and never adds chat-history records.

## Security

- Hub login API remains loopback-only and rejects forwarding headers.
- Tool events include only the public task summary and account summary. They never include `MUSIC_U`, CSRF, NMTID, cookies, or device IDs.
- Task IDs are validated by the existing login manager and client proxy.
- QR data is held in memory/SSE only and is not persisted in chat history.
- Refresh/cancel use the existing strict JSON API validation.

## Tests

- Hub tool tests verify start/cancel call the manager, return direct responses, and emit only public fields.
- Client tests verify `netease_login` WebSocket messages publish SSE events and proxy actions call only the allowlisted Hub paths.
- Kiosk contract tests verify the card is separate from `ui.thread.messages`, renders the QR data URL, updates terminal states, and wires refresh/cancel.
- Existing login, API, patch, and playback tests remain green.
- Device verification says “登录网易云”, confirms the inline card appears, then cancels it from the card; no real logout or credential deletion is performed.
