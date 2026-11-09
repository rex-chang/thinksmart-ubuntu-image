# Upstream client Bluetooth integration

## Goal and source ownership

Merge `justhalfbit/xiaozhi-thinksmart-client` commit `e7ec0de` into the existing custom client without losing NetEase playback, voice-triggered login cards, alarms, or conversation recovery fixes. The client repository becomes the authoritative complete source; remove obsolete image-repository client snapshots and make all deployment/preview entry points require the complete client checkout.

## Merge sequence

1. Create a `dev` worktree in the client repository from `client-v0.1.3` (`afdd791`). The existing 0.1.8 release differs from this base in exactly six tracked source files; extract its full source without its generated updater payload, then overlay the current committed custom app/web/UI files.
2. Commit that recovered complete custom source. Merge `upstream/main`, preserving both Bluetooth/Wi-Fi/system-updater/follow-up fixes and custom NetEase playback/login/status behavior. Resolve shared functions rather than duplicating them. Use a client version newer than local 0.1.8, `0.1.9`.
3. Retain upstream `bt.py`, `bt_helper.py`, audio mixer, configuration defaults, deployment bootstrap, and existing behavior tests. Verify lock-file hash before reusing an environment archive. Run existing focused Bluetooth/follow-up/client tests after integration, plus actual UI startup and conversation checks.
4. Remove the obsolete image-repository client overlay after committing the integrated client. Make deployment and preview consume the complete client checkout; install through the release/updater workflow rather than overwrite a partial client tree.
5. Build a client-only release from the tracked client branch. Back up current installed client metadata; use the existing updater to install the new version with self-test/rollback. Never replace Hub model settings, NetEase credentials, Wi-Fi profiles, user preferences, alarms, or chat history.
6. On device, verify actual conversation reply, model UI, NetEase login-success card and playback, Bluetooth helper/API/UI, and readiness for a 3-minute pairing window. Do not pair unknown nearby devices. Phone pairing and acoustic playback require the user's phone; report that boundary explicitly if no authorized phone is available.

## Acceptance

- The integrated client records upstream ancestry and reproducible custom source.
- NetEase UI, tools, and login are retained; no duplicate renderer names or missing initialization/routes.
- Bluetooth settings show adapter status and allow adding a phone; incoming A2DP audio is mixed through upstream Background/AEC integration.
- No configuration or credential mutation during migration.
- Verification evidence includes exact commands/results, real UI, and explicit physical-phone limitations.

## Verified result

- Client `dev` worktree: `../xiaozhi-thinksmart-client-integration`; custom baseline `22fc700`; merge commit `8cd8a23` includes upstream `e7ec0de`. Version `0.1.9`.
- Existing focused client tests: 53 passed (Bluetooth mixing/ducking, follow-up wait, conversation interruption, connections, models, networks, updater requests, playback buffer).
- Hub NetEase tests: 35 passed. Source-text-only deployment/forwarding assertions were removed, replaced by the actual installation and UI flow exercised here.
- Release code SHA-256: `08e3208c81519999a7a2aa8d34e0930977f6795fb0c6fc38cb69c66a5164332d`. Dependency lock/environment unchanged (`c962717d64772981`). Artifact directory `.tmp/xiaozhi-release-019/`.
- Installed through real updater `install 0.1.9` with temporary loopback source: self-test passed, `current=0.1.9`, `previous=0.1.8`. Real updater configuration, Hub model configuration, client preferences and NetEase session hashes unchanged.
- Device conversation: `/api/text` produced `蓝牙客户端会话正常`, TTS reached speaking and returned idle; current-volume `/api/volume` returned 200.
- Device NetEase self-test: account, search, 5-second playback, seek, pause and stop passed.
- Live browser: model MiniMax-M3.1-Flash, Bluetooth card name ThinkSmart View, no page errors; clicking Add Phone changed adapter to Discoverable/Pairable with a 180-second window, which expired closed normally. Zero paired devices; actual phone pairing/acoustic Bluetooth playback is not yet verified.
- Login card renderer verified in live page using a UI-only successful-state fixture: no QR image after success, next-step text visible, message history count unchanged. Real NetEase credentials were not altered or reauthorized.
- Browser screenshot attempts timed out; DOM/state checks and actual user interactions succeeded. No screenshot is claimed as evidence.
