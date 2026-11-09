# Voice-Triggered Inline NetEase QR Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open and track a NetEase QR-login card in the active conversation when the user requests login by voice.

**Architecture:** The Hub registers `login_netease` and `cancel_netease_login`, sends public `netease_login` WebSocket events, and keeps LoginManager authoritative. The client forwards those events over SSE and proxies refresh/cancel through its existing Hub HTTP connection. The kiosk renders an ephemeral card separate from persisted conversation messages.

**Tech Stack:** Python 3, aiohttp, existing Hub WebSocket, SSE, vanilla JavaScript/CSS.

## Global Constraints

- Do not persist login cards in chat history.
- Do not add a network listener or expose NetEase credentials.
- Only the existing local Hub login API may receive refresh/cancel requests.
- Validate task IDs and actions with allowlists.
- Do not add TTS for login-card activation.
- Preserve existing music/player behavior.

---

### Task 1: Add Hub login tools and events

**Files:**
- Modify: `netease/hub/plugins_func/functions/play_netease.py`
- Test: `netease/tests/test_login_tools.py`

- [ ] Add tests with fake manager and fake WebSocket for `login_netease` start, public event payload, terminal poll update, and `cancel_netease_login`.
- [ ] Implement `LOGIN_DESC` and `CANCEL_LOGIN_DESC` with explicit natural-language descriptions and no credentials in results.
- [ ] Add `_send_login(conn, result)` that sends `{"type":"netease_login", ...}` and strips any non-public fields before serialization.
- [ ] Add a bounded async watcher that polls `manager.status()` every two seconds until `succeeded`, `expired`, `cancelled`, or `failed`; do not create duplicate watchers for one task.
- [ ] Register both functions with `ToolType.SYSTEM_CTL`; use `ActionResponse(Action.RESPONSE, ...)` so the model receives a short spoken/text response while the card carries the QR.

### Task 2: Forward login events through the client

**Files:**
- Modify: `netease/kiosk/thinksmart_client/app.py`
- Modify: `netease/kiosk/thinksmart_client/web.py`
- Test: `netease/tests/test_client_login.py`

- [ ] Test WebSocket login messages publish an SSE `netease_login` event and update snapshot-only ephemeral state.
- [ ] Add `self.netease_login = None`, include it in `snapshot()` under a separate key, and handle `type == 'netease_login'` in `_on_json`.
- [ ] Add `netease_login_action(action, task_id)` with action allowlist `refresh/cancel`, task-ID length/character validation, and Hub paths `/kiosk/netease/login/refresh` and `/kiosk/netease/login/cancel`.
- [ ] Add `POST /api/netease/login/refresh` and `/api/netease/login/cancel`; reject malformed JSON, unknown actions, and invalid task IDs with 400.
- [ ] Publish the returned public task state to SSE; never log or forward credential fields.

### Task 3: Render the inline conversation card

**Files:**
- Modify: `netease/kiosk/web/v2/index.html`
- Modify: `netease/kiosk/web/v2/app.js`
- Modify: `netease/kiosk/web/v2/app.css`
- Test: `netease/tests/test_login_card_contract.py`

- [ ] Add a dedicated `#neteaseLoginCard` container adjacent to the messages list, with QR image, status, account text, refresh, and cancel controls.
- [ ] Add `ui.neteaseLogin` outside `ui.thread`; implement `handlers.netease_login` and render functions for active and terminal states.
- [ ] Use `img.src = task.qr_data_url` only after checking the `data:image/png;base64,` prefix; do not inject it into HTML.
- [ ] Wire refresh/cancel to `/api/netease/login/refresh` and `/api/netease/login/cancel`; retain terminal card state and stop polling when terminal.
- [ ] Add a bounded active-task poll (maximum one request every two seconds) through a new client `GET /api/netease/login/status` proxy, or stop polling when the Hub event watcher supplies terminal state.
- [ ] Keep card text/status out of `ui.thread.messages` and out of retry/history rendering.

### Task 4: Deployment and documentation

**Files:**
- Modify: `netease/deploy.sh`
- Modify: `netease/kiosk/README.md`
- Test: `netease/tests/test_deploy_contract.py`

- [ ] Ensure deployment copies the updated Hub function module, client source, web assets, and applies the existing login API patch.
- [ ] Compile Hub and client Python files and retain `--login-api` self-test.
- [ ] Document spoken trigger examples, inline card behavior, refresh/cancel, and the fact that phone confirmation is required.
- [ ] Add contract assertions for the new tool names, client proxy routes, and UI card identifiers.

### Task 5: Verify end to end

**Files:**
- No source changes unless verification finds a defect.

- [ ] Run `PYTHONPATH=netease/hub uv run --no-project --with pytest --with requests --with cryptography --with aiohttp python -m pytest -q netease/tests`.
- [ ] Run `bash -n netease/deploy.sh` and Python syntax checks.
- [ ] Deploy with the existing `SSH_OPTS` sandbox and verify Hub/client/kiosk services.
- [ ] Use the actual device to say “登录网易云”; verify the inline card appears with a QR and no extra TTS, then click cancel.
- [ ] Verify the device health endpoint and NetEase playback self-test after the change.
