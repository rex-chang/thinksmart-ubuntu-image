# NetEase QR Login Backend Design

## Goal

Provide a reusable, locally scoped NetEase QR-login backend for ThinkSmart devices. The backend must support the existing CLI workflow and a future kiosk settings UI without exposing NetEase credentials or adding a new network listener.

## Scope

This phase implements the backend only:

- QR-login state machine.
- Session persistence and logout.
- Local Hub HTTP API.
- Idempotent patch application for the Hub checkout.
- CLI compatibility.
- Focused unit tests and deployment documentation.

This phase does not modify the kiosk UI. A later phase may consume the API from the existing settings page.

## Existing Boundaries

The repository does not contain the `xiaozhi-hub-server` source tree. It contains the NetEase plugin, CLI, tests, and patch scripts. The Hub currently uses an `aiohttp` admin handler and exposes `/kiosk/*` routes only to requests from the same machine. The implementation must therefore deliver a patch script rather than copying Hub core files into this repository.

Existing session data remains at the Hub plugin data directory in `netease-session.json`. Credential files are written atomically with mode `0600`.

## Architecture

### Login state machine

Add `netease/hub/plugins_func/netease/login.py` as the single owner of QR-login task state. It exposes a small synchronous API that the Hub handler and CLI both call. The implementation uses one process-local task at a time and a background worker for blocking NetEase requests.

Task states:

- `starting`
- `waiting_scan`
- `scanned`
- `confirmed`
- `succeeded`
- `expired`
- `cancelled`
- `failed`

The task has an opaque ID, creation time, expiry time, QR URL, and QR data URL while the QR is valid. A login task expires after five minutes. Each generated QR code expires after approximately two minutes. Refresh cancels the current QR attempt and creates a replacement within the same login task. Explicit cancellation ends the task. A second `start` returns the existing active task instead of creating a second worker.

The worker polls NetEase QR status codes `801`, `802`, `803`, and `800` using the existing EAPI client. `801` means waiting for scan, `802` means scanned and awaiting confirmation, `803` means success, and `800` means QR expiry. Successful login obtains account metadata and atomically saves the session. Network errors and unexpected responses become stable `failed` task results; raw third-party bodies and credential values are not returned to callers or logged.

### QR rendering

The backend reuses the installed `qrencode` executable. It invokes it with a bounded subprocess call to produce PNG bytes in memory, then returns a `data:image/png;base64,...` value. It does not write QR images to `/tmp` or another persistent location. Missing executable, non-zero exit, timeout, empty output, and oversized output map to the stable `qrencode_unavailable` or `qr_render_failed` error categories.

The QR URL remains available in the response for clients that want to render it themselves.

### Session handling

Keep the existing `session.py` file format and atomic write behavior. API responses expose only:

- `logged_in`.
- `uid`.
- `nickname`.
- `saved_at`.

They never expose `music_u`, CSRF, NMTID, device ID, cookies, or raw login responses. Logout removes the session file and reports the resulting logged-out state. If an active login succeeds, a previously stored session is replaced only after the new account metadata is obtained.

### Hub API

Add these local kiosk routes to the Hub:

```text
GET  /kiosk/netease/login/status
POST /kiosk/netease/login/start
POST /kiosk/netease/login/refresh
POST /kiosk/netease/login/cancel
POST /kiosk/netease/logout
```

The handler runs blocking state-machine calls in the Hub's existing admin thread pool so the `aiohttp` event loop remains responsive. It reuses the existing same-machine check. Loopback callers are accepted; non-loopback callers receive `403`. POST routes accept only `application/json` with an object body and reject unknown fields, oversized bodies, and malformed content. Empty objects are the only valid request body in this phase.

Successful responses use JSON with `ok: true`, account summary, and task summary. Task summaries include `id`, `state`, `created_at`, `expires_at`, `qr_url`, and `qr_data_url` only while the QR is valid. Error responses use stable categories such as:

- `already_running`
- `not_logged_in`
- `expired`
- `cancelled`
- `qrencode_unavailable`
- `qr_render_failed`
- `network_error`
- `login_failed`

The handler does not return third-party exception text or credentials.

### Patch integration

Add `netease/hub/patch/apply_netease_login_api.py`. It must:

- Install the login module and handler support into a supplied Hub checkout.
- Make an idempotent, exact-anchor update to the existing admin handler route registration and handler implementation.
- Back up each modified file with a timestamp before the first modification.
- Return `already` when the intended change is present.
- Refuse to modify files if an exact expected anchor is missing or ambiguous.

Update `netease/deploy.sh` to apply this patch after copying the plugin, retain the existing Hub backup, verify `qrencode` and `mpv`, compile the installed Python files, and run the NetEase self-test command. The deploy script must not silently rewrite user configuration or credentials.

### CLI compatibility

Refactor `netease/hub/scripts/netease-login.py` to call the same login state-machine API. The CLI remains an SSH-friendly interface: it renders ANSI QR output and prints the login URL. It may retain a compatibility PNG output for manual use, but the backend's normal API path must remain in-memory. CLI cancellation, expiry, and timeout behavior must match the state machine.

## Error and Concurrency Rules

- At most one login worker exists per Hub process.
- `start` is idempotent for an active task; it does not invalidate a user who is already scanning.
- `refresh` is allowed only for an active task and creates a fresh QR with a new expiry.
- `cancel` is idempotent for an already finished task and must not delete a newly created task.
- A stale worker may not overwrite a newer task's state or session.
- The five-minute task timeout and QR expiry use monotonic time for decisions and wall-clock timestamps only for API display.
- Subprocesses and network requests have finite timeouts.
- No retry loop is unbounded; network failures produce a terminal task state.
- Session writes are atomic and mode `0600`.

## Tests

Add tests for:

- QR state transitions for `801`, `802`, `803`, and `800`.
- Successful account metadata capture and session save.
- Task singleton, refresh, cancel, timeout, and stale-worker protection.
- QR rendering success, missing executable, timeout, empty output, and oversized output.
- API responses never containing credential fields.
- Loopback acceptance and non-loopback rejection.
- JSON/content-type/unknown-field/request-size validation.
- Logout behavior.
- Idempotent patch application and refusal when anchors are missing or ambiguous.
- CLI reuse of the state machine.

Tests must use fake NetEase responses and fake `qrencode`; no real credentials or real login is used in automated tests.

## Security and Operational Notes

The API remains on the existing Hub listener and does not add a port. The same-machine restriction is mandatory because login and logout are credential-changing actions. QR URLs and task IDs are short-lived. Logs must identify state transitions without including QR URLs, cookies, or account tokens.

The deployment documentation must state that the current phase supplies a backend and CLI; the kiosk UI integration is a separate follow-up. It must also document the exact patch command and the self-test command.
