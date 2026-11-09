"""Single-process NetEase QR login state machine."""

import base64
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import eapi, session

TASK_SECONDS = 5 * 60
QR_SECONDS = 2 * 60
POLL_SECONDS = 2.0
QR_COMMAND = "qrencode"
QR_MAX_BYTES = 512 * 1024
QR_DATA_PREFIX = "data:image/png;base64,"
TASK_ID_MAX_LENGTH = 128
TASK_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
ACTIVE_STATES = frozenset(("starting", "waiting_scan", "scanned", "confirmed"))


class LoginError(Exception):
    """Stable error exposed to local API callers."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class QrRenderError(LoginError):
    def __init__(self, code: str = "qr_render_failed"):
        super().__init__(code)


@dataclass
class LoginTask:
    task_id: str
    created_at: float
    deadline: float
    qr_url: str = ""
    qr_data_url: str = ""
    qr_deadline: float = 0.0
    state: str = "starting"
    error: str = ""
    generation: int = 0
    done: threading.Event = field(default_factory=threading.Event)


class LoginManager:
    """Own one cancellable QR login worker per Hub process."""

    def __init__(
        self,
        client_factory: Callable[[], object] = eapi.Client,
        qr_encoder: Optional[Callable[[str], bytes]] = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = POLL_SECONDS,
        task_seconds: float = TASK_SECONDS,
        qr_seconds: float = QR_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._client_factory = client_factory
        self._qr_encoder = qr_encoder or render_qr
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._task_seconds = task_seconds
        self._qr_seconds = qr_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._task: Optional[LoginTask] = None

    def start(self) -> dict:
        with self._lock:
            current = self._task
            if current is not None and current.state in ACTIVE_STATES:
                return self._response(current)
            task = self._new_task()
            self._task = task
            try:
                self._prepare_qr(task)
            except LoginError as exc:
                self._finish(task, "failed", exc.code)
                return self._response(task)
            except Exception:
                self._finish(task, "failed", "network_error")
                return self._response(task)
            thread = threading.Thread(
                target=self._poll,
                args=(task.task_id, task.generation),
                name="netease-login",
                daemon=True,
            )
            thread.start()
            return self._response(task)

    def status(self) -> dict:
        with self._lock:
            task = self._task
            if task is None:
                return {"ok": True, "account": session.public_summary(), "task": None}
            self._expire_if_needed(task)
            return self._response(task)

    def refresh(self, task_id: str) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            if task.state == "succeeded":
                return self._response(task)
            if task.state not in ("expired", "failed", *ACTIVE_STATES):
                return self._error_response(task, "cancelled")
            if self._clock() >= task.deadline:
                self._finish(task, "expired", "expired")
                return self._response(task)
            task.state = "starting"
            task.error = ""
            task.generation += 1
            task.done.clear()
            try:
                self._prepare_qr(task)
            except LoginError as exc:
                self._finish(task, "failed", exc.code)
                return self._response(task)
            thread = threading.Thread(
                target=self._poll,
                args=(task.task_id, task.generation),
                name="netease-login",
                daemon=True,
            )
            thread.start()
            return self._response(task)

    def cancel(self, task_id: str) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            if task.state in ACTIVE_STATES:
                self._finish(task, "cancelled", "cancelled")
            return self._response(task)

    def logout(self) -> dict:
        with self._lock:
            task = self._task
            if task is not None and task.state in ACTIVE_STATES:
                self._finish(task, "cancelled", "cancelled")
            session.clear()
            return {"ok": True, "account": session.public_summary(), "task": self._summary(task)}

    def wait(self, task_id: str, timeout: Optional[float] = None) -> dict:
        with self._lock:
            task = self._require_task(task_id)
            event = task.done
        event.wait(timeout)
        return self.status()

    def _new_task(self) -> LoginTask:
        now = self._clock()
        return LoginTask(
            task_id=secrets.token_urlsafe(18),
            created_at=self._wall_clock(),
            deadline=now + self._task_seconds,
        )

    def _prepare_qr(self, task: LoginTask) -> None:
        client = self._client_factory()
        response = client.eapi_post("/api/login/qrcode/unikey", {"type": 3})
        key = response.get("unikey")
        if not isinstance(key, str) or not key:
            raise LoginError("login_failed")
        task.qr_url = "https://music.163.com/login?codekey=" + key
        task.qr_data_url = QR_DATA_PREFIX + base64.b64encode(self._qr_encoder(task.qr_url)).decode("ascii")
        task.qr_deadline = self._clock() + self._qr_seconds
        task.state = "waiting_scan"
        task.generation += 1

    def _poll(self, task_id: str, generation: int) -> None:
        try:
            client = self._client_factory()
            while True:
                with self._lock:
                    task = self._current(task_id, generation)
                    if task is None:
                        return
                    if self._clock() >= task.deadline:
                        self._finish(task, "expired", "expired")
                        return
                    if self._clock() >= task.qr_deadline:
                        self._finish(task, "expired", "expired")
                        return
                    key = task.qr_url.rsplit("=", 1)[-1]
                response = client.eapi_post(
                    "/api/login/qrcode/client/login", {"key": key, "type": 3}
                )
                code = response.get("code")
                with self._lock:
                    task = self._current(task_id, generation)
                    if task is None:
                        return
                    if code == 801:
                        task.state = "waiting_scan"
                    elif code == 802:
                        task.state = "scanned"
                    elif code == 803:
                        task.state = "confirmed"
                        profile = client.account() or {}
                        uid = int(profile.get("userId") or 0)
                        nickname = str(profile.get("nickname") or "")
                        if not uid and not nickname:
                            self._finish(task, "failed", "login_failed")
                        else:
                            session.save(client.music_u, client.csrf, uid, nickname)
                            self._finish(task, "succeeded", "")
                        return
                    elif code == 800:
                        self._finish(task, "expired", "expired")
                        return
                    else:
                        self._finish(task, "failed", "login_failed")
                        return
                self._sleep(self._poll_seconds)
        except LoginError as exc:
            with self._lock:
                task = self._current(task_id, generation)
                if task is not None:
                    self._finish(task, "failed", exc.code)
        except Exception:
            with self._lock:
                task = self._current(task_id, generation)
                if task is not None:
                    self._finish(task, "failed", "network_error")

    def _current(self, task_id: str, generation: int) -> Optional[LoginTask]:
        task = self._task
        if task is None or task.task_id != task_id or task.generation != generation:
            return None
        if task.state not in ACTIVE_STATES:
            return None
        return task

    def _require_task(self, task_id: str) -> LoginTask:
        if not self._valid_task_id(task_id):
            raise LoginError("not_found")
        task = self._task
        if task is None or task.task_id != task_id:
            raise LoginError("not_found")
        return task

    @staticmethod
    def _valid_task_id(task_id: str) -> bool:
        return bool(task_id) and len(task_id) <= TASK_ID_MAX_LENGTH and all(
            char in TASK_ID_CHARS for char in task_id
        )

    def _expire_if_needed(self, task: LoginTask) -> None:
        if task.state in ACTIVE_STATES and self._clock() >= task.deadline:
            self._finish(task, "expired", "expired")

    def _finish(self, task: LoginTask, state: str, error: str) -> None:
        task.state = state
        task.error = error
        task.qr_data_url = ""
        task.done.set()

    def _summary(self, task: Optional[LoginTask]) -> Optional[dict]:
        if task is None:
            return None
        data = {
            "id": task.task_id,
            "state": task.state,
            "created_at": int(task.created_at),
            "expires_at": int(task.created_at + self._task_seconds),
        }
        if task.qr_url and task.state in ACTIVE_STATES:
            data["qr_url"] = task.qr_url
            data["qr_data_url"] = task.qr_data_url
        if task.error:
            data["error"] = task.error
        return data

    def _response(self, task: LoginTask) -> dict:
        self._expire_if_needed(task)
        return {"ok": True, "account": session.public_summary(), "task": self._summary(task)}

    @staticmethod
    def _error_response(task: LoginTask, error: str) -> dict:
        return {"ok": False, "error": error, "task": {"id": task.task_id, "state": task.state}}


def render_qr(url: str) -> bytes:
    try:
        result = subprocess.run(
            [QR_COMMAND, "-o", "-", "-t", "PNG", url],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise QrRenderError("qrencode_unavailable") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise QrRenderError() from exc
    if not result.stdout or len(result.stdout) > QR_MAX_BYTES:
        raise QrRenderError()
    return result.stdout


_MANAGER = LoginManager()


def manager() -> LoginManager:
    return _MANAGER
