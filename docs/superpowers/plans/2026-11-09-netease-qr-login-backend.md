# NetEase QR Login Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable, local-only NetEase QR-login state machine and Hub patch while preserving the existing SSH CLI.

**Architecture:** `login.py` owns one process-local background login task, QR rendering, state transitions, and safe session summaries. The existing CLI calls that manager. `apply_netease_login_api.py` installs the module and applies an exact, idempotent patch to `core/admin/admin_handler.py`, where the existing `/kiosk/*` same-machine guard and admin thread pool are reused.

**Tech Stack:** Python 3, `requests`, `aiohttp`, `qrencode`, `unittest`/`pytest`, exact-anchor patching.

## Global Constraints

- Keep the API on the existing Hub listener; add no port or daemon.
- Accept only loopback requests without proxy forwarding headers.
- Never return or log `MUSIC_U`, CSRF, NMTID, device IDs, cookies, QR URLs in logs, or raw third-party errors.
- Keep session writes atomic with mode `0600`.
- At most one login worker per Hub process; stale workers cannot mutate newer tasks.
- All network and subprocess operations use finite timeouts.
- No TypeScript `any`; no new dependency beyond the already required `qrencode` executable.
- Do not modify kiosk UI in this phase.

---

### Task 1: Build the QR-login state manager

**Files:**
- Create: `netease/hub/plugins_func/netease/login.py`
- Modify: `netease/hub/plugins_func/netease/session.py`
- Test: `netease/tests/test_login.py`

**Interfaces:**
- `LoginManager.start() -> dict`
- `LoginManager.status() -> dict`
- `LoginManager.refresh(task_id: str) -> dict`
- `LoginManager.cancel(task_id: str) -> dict`
- `LoginManager.logout() -> dict`
- `LoginManager.wait(task_id: str, timeout: float | None = None) -> dict` for the CLI.
- `session.public_summary() -> dict` returns only `logged_in`, `uid`, `nickname`, `saved_at`.

- [ ] **Step 1: Add failing state-machine tests**

Use a fake EAPI client and fake QR renderer. Assert status codes `801`, `802`, `803`, and `800` produce `waiting_scan`, `scanned`, `succeeded`, and `expired`; assert successful login saves the account and public summaries contain no credential keys.

- [ ] **Step 2: Run the focused test file and verify failure**

Run: `python3 -m pytest -q netease/tests/test_login.py`
Expected: FAIL because `login.py` and `public_summary()` do not exist.

- [ ] **Step 3: Implement bounded QR rendering and task ownership**

Use a `LoginTask` dataclass and a `threading.Lock`. Generate opaque IDs with `secrets.token_urlsafe(18)`. Use `time.monotonic()` for expiry decisions and `time.time()` for API timestamps. Call `qrencode -o - -t PNG <url>` with `subprocess.run(..., timeout=5, check=True, capture_output=True)` and cap output at `512 * 1024` bytes. Map executable absence to `qrencode_unavailable` and all other renderer failures to `qr_render_failed`.

The worker must capture its task ID and compare it with the manager's current task before every state mutation and before saving the session. Poll every two seconds, stop at the five-minute task deadline, and use `eapi.Client.eapi_post()` for the two QR endpoints. On `803`, call `account()`, require a profile object, then call `session.save()`.

- [ ] **Step 4: Add safe public session summary**

Implement `public_summary()` without returning any stored credential field. Keep `load()`, `save()`, and `clear()` behavior unchanged.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m pytest -q netease/tests/test_login.py netease/tests/test_session.py`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add netease/hub/plugins_func/netease/login.py netease/hub/plugins_func/netease/session.py netease/tests/test_login.py
git commit -m "feat: add NetEase QR login state manager"
```

### Task 2: Refactor the CLI to reuse the manager

**Files:**
- Modify: `netease/hub/scripts/netease-login.py`
- Test: `netease/tests/test_login_cli.py`

**Interfaces:**
- CLI calls `LoginManager.start()` and `LoginManager.wait()`; it does not implement a second polling loop.

- [ ] **Step 1: Add failing CLI delegation test**

Patch the manager with a fake object, invoke `main()`, and assert the CLI prints the QR URL/terminal rendering and waits on the manager rather than calling `eapi_post()` itself.

- [ ] **Step 2: Implement CLI compatibility**

Keep ANSI output by rendering the returned QR URL with `qrencode -t ANSIUTF8`. Do not write the backend's QR data URL to disk. Print stable terminal messages for success, expiry, cancellation, and failure. Return non-zero for terminal failure states.

- [ ] **Step 3: Run tests and commit**

Run: `python3 -m pytest -q netease/tests/test_login_cli.py`

```bash
git add netease/hub/scripts/netease-login.py netease/tests/test_login_cli.py
git commit -m "refactor: reuse NetEase login manager from CLI"
```

### Task 3: Add the Hub route patch

**Files:**
- Create: `netease/hub/patch/apply_netease_login_api.py`
- Test: `netease/tests/test_login_patch.py`

**Interfaces:**
- `apply_netease_login_api.py <xiaozhi-server-root>` exits `0` on `patched` or `already`, and exits non-zero without modifying files when an exact anchor is missing or duplicated.
- The injected `AdminHandler` methods register the five routes and return the `login.LoginManager` summaries.

- [ ] **Step 1: Add patcher tests with a synthetic Hub tree**

Create minimal `core/admin/admin_handler.py`, `core/admin/__init__.py`, and package directories. Assert first application installs the module and adds routes/handlers, second application reports `already` with byte-identical files, and a missing/duplicated anchor returns an error without partial edits.

- [ ] **Step 2: Implement exact-anchor patching**

The patcher must require one exact route anchor around `web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),` and one exact class-method insertion anchor immediately before the existing kiosk section. Back up the target handler with a timestamp before modification. Install a generated handler module or a copied repository support module without using broad regex replacement.

Injected handlers must:

- call `_from_this_machine()` and return `_error("仅限本机", 403)` when false;
- call `_read_json()` for POST requests;
- reject non-empty request bodies with a stable `不支持请求参数` error;
- use `await self._blocking(...)` for manager calls;
- return `_json()` with no secrets;
- map manager errors to stable HTTP 409/404/500 categories without third-party text.

- [ ] **Step 3: Run patch tests and commit**

Run: `python3 -m pytest -q netease/tests/test_login_patch.py`

```bash
git add netease/hub/patch/apply_netease_login_api.py netease/tests/test_login_patch.py
git commit -m "feat: patch Hub with local NetEase login API"
```

### Task 4: Integrate deployment and document the backend

**Files:**
- Modify: `netease/deploy.sh`
- Modify: `netease/kiosk/README.md`
- Modify: `netease/hub/scripts/netease-selftest.py`
- Test: `netease/tests/test_deploy_contract.py`

**Interfaces:**
- Deployment invokes `apply_netease_login_api.py`, compiles the copied NetEase modules and patched handler, and runs the self-test.
- Self-test checks the session summary and QR-login manager import without starting a login task.

- [ ] **Step 1: Add deployment contract tests**

Read the shell script as text and assert it contains the patch invocation, `python3 -m py_compile`, `qrencode`, `mpv`, and self-test commands. Assert the documentation states the backend is local-only and kiosk UI integration is a separate phase.

- [ ] **Step 2: Update deployment**

Copy `login.py`, apply the new patch after `apply_music_handler.py`, install dependencies only with the existing `apt-get` command, compile the installed modules, and run `scripts/netease-selftest.py`. Do not edit Hub config or write credentials.

- [ ] **Step 3: Update self-test and documentation**

Self-test reports `login API: ready` when the module imports and reports the public session state without printing credential fields. Document:

```bash
python3 scripts/netease-login.py
curl http://127.0.0.1:8003/kiosk/netease/login/status
```

and the exact patch command. State that this phase has no kiosk UI.

- [ ] **Step 4: Run tests and commit**

Run: `python3 -m pytest -q netease/tests`

```bash
git add netease/deploy.sh netease/kiosk/README.md netease/hub/scripts/netease-selftest.py netease/tests/test_deploy_contract.py
git commit -m "docs: integrate NetEase login deployment"
```

### Task 5: Verify the complete repository workflow

**Files:**
- No source changes unless a focused verification exposes a defect.

- [ ] **Step 1: Run all NetEase tests**

Run: `python3 -m pytest -q netease/tests`
Expected: all tests pass without network access or real credentials.

- [ ] **Step 2: Run syntax checks**

Run: `python3 -m py_compile netease/hub/plugins_func/netease/*.py netease/hub/scripts/netease-*.py netease/hub/patch/apply_netease_login_api.py`
Expected: exit code 0.

- [ ] **Step 3: Apply the patch to a synthetic Hub checkout twice**

Run the patcher twice against the fixture used by `test_login_patch.py`; verify the second run reports `already` and the fixture is byte-identical after the second run.

- [ ] **Step 4: Review the final diff**

Run: `git diff HEAD~4..HEAD --check` and inspect that no QR URL, cookie, token, or personal credential appears in source, tests, logs, or documentation.
