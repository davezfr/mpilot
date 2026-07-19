from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mpilot.core.env import env_first, float_env_any, int_env_any, telegram_bot_token
from mpilot.core.hermes import DEFAULT_HERMES_SEND_TIMEOUT_SECONDS, send_hermes_message as core_send_hermes_message
from mpilot.core.json_store import LockedJsonStore
from mpilot.core.targets import parse_telegram_target, resolve_notification_target as core_resolve_notification_target
from mpilot.core.telegram import (
    coerce_telegram_int,
    send_or_edit_telegram_status_message,
    telegram_api_post,
    telegram_message_id,
)

from .jobs import JobStore, default_job_store_dir


TERMINAL_JOB_STATUSES = {"succeeded", "failed", "needs_confirmation"}
DEFAULT_MAX_ERRORS = 10
DEFAULT_STATUS_MAX_ERRORS = 3
DEFAULT_TERMINAL_CLAIM_TIMEOUT_SECONDS = 600.0
DEFAULT_INITIAL_NOTIFICATION_DELAY_SECONDS = 0.0


SendMessage = Callable[[str, str], None]
SendStatusMessage = Callable[[str, str, str, Optional[str]], Optional[str]]


class JobNotificationStore(LockedJsonStore):
    def upsert_watch(
        self,
        *,
        job_id: str,
        job_store_dir: str | Path,
        notification_target: str,
        title: Optional[str] = None,
        requester_id: Optional[str] = None,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        notification_not_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._store_lock():
            return self._upsert_watch_locked(
                job_id=job_id,
                job_store_dir=job_store_dir,
                notification_target=notification_target,
                title=title,
                requester_id=requester_id,
                language=language,
                metadata=metadata,
                notification_not_before=notification_not_before,
            )

    def _upsert_watch_locked(
        self,
        *,
        job_id: str,
        job_store_dir: str | Path,
        notification_target: str,
        title: Optional[str] = None,
        requester_id: Optional[str] = None,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        notification_not_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_job_id = _normalize_job_id(job_id)
        normalized_target = _normalize_target(notification_target)
        payload = self._read()
        now = _now()

        for watch in payload["watches"]:
            if watch["job_id"] == normalized_job_id and watch["notification_target"] == normalized_target:
                watch["job_store_dir"] = str(Path(job_store_dir).expanduser())
                watch["title"] = _string_or_none(title) or watch.get("title") or normalized_job_id
                watch["requester_id"] = _string_or_none(requester_id) or watch.get("requester_id")
                watch["language"] = _string_or_none(language) or watch.get("language")
                watch["metadata"] = _merge_metadata(watch.get("metadata"), metadata)
                not_before = _string_or_none(notification_not_before)
                if not_before is not None:
                    watch["notification_not_before"] = not_before
                watch["updated_at"] = now
                if watch.get("notified_at") or watch.get("abandoned_at"):
                    _revive_watch(watch)
                self._write(payload)
                return dict(watch)

        watch = {
            "job_id": normalized_job_id,
            "job_store_dir": str(Path(job_store_dir).expanduser()),
            "title": _string_or_none(title) or normalized_job_id,
            "notification_target": normalized_target,
            "requester_id": _string_or_none(requester_id),
            "language": _string_or_none(language),
            "metadata": _normalize_metadata(metadata),
            "created_at": now,
            "updated_at": now,
            "notified_at": None,
            "abandoned_at": None,
            "terminal_notification_claimed_at": None,
            "error_count": 0,
            "last_error": None,
        }
        not_before = _string_or_none(notification_not_before)
        if not_before is not None:
            watch["notification_not_before"] = not_before
        payload["watches"].append(watch)
        self._write(payload)
        return dict(watch)

    def pending_watches(self) -> List[Dict[str, Any]]:
        with self._store_lock():
            return [
                dict(watch)
                for watch in self._read()["watches"]
                if not watch.get("notified_at") and not watch.get("abandoned_at")
            ]

    def mark_notified(self, *, job_id: str, notification_target: str, terminal_status: str) -> None:
        self._update_watch(
            job_id=job_id,
            notification_target=notification_target,
            updates={
                "notified_at": _now(),
                "terminal_status": terminal_status,
                "terminal_notification_claimed_at": None,
                "last_error": None,
                "updated_at": _now(),
            },
        )

    def mark_error(self, *, job_id: str, notification_target: str, error: str) -> Optional[Dict[str, Any]]:
        return self._update_watch_with(
            job_id=job_id,
            notification_target=notification_target,
            update_fn=lambda watch: {
                "error_count": _error_count(watch) + 1,
                "last_error": error[:500],
                "terminal_notification_claimed_at": None,
                "updated_at": _now(),
            },
        )

    def mark_abandoned(self, *, job_id: str, notification_target: str, error: Optional[str] = None) -> None:
        updates = {"abandoned_at": _now(), "updated_at": _now()}
        if error:
            updates["last_error"] = error[:500]
        self._update_watch(job_id=job_id, notification_target=notification_target, updates=updates)

    def mark_status_published(
        self,
        *,
        job_id: str,
        notification_target: str,
        message: str,
        message_id: Optional[str],
        tick: int,
    ) -> Optional[Dict[str, Any]]:
        def update(watch: Dict[str, Any]) -> Dict[str, Any]:
            tracking = _status_tracking(watch)
            tracking["last_message"] = message
            tracking["updated_at"] = _now()
            tracking["tick"] = tick
            tracking["error_count"] = 0
            tracking["last_error"] = None
            tracking.pop("paused_at", None)
            if message_id:
                tracking["message_id"] = message_id
            return {"status_tracking": tracking, "updated_at": _now()}

        return self._update_watch_with(
            job_id=job_id,
            notification_target=notification_target,
            update_fn=update,
        )

    def claim_terminal_notification(
        self,
        *,
        job_id: str,
        notification_target: str,
        terminal_status: str,
        claim_timeout_seconds: float = DEFAULT_TERMINAL_CLAIM_TIMEOUT_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        def update(watch: Dict[str, Any]) -> Dict[str, Any]:
            if watch.get("notified_at") or watch.get("abandoned_at"):
                return {}
            claimed_at = _parse_timestamp(watch.get("terminal_notification_claimed_at"))
            if claimed_at is not None:
                claim_age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
                if claim_age < claim_timeout_seconds:
                    return {}
            return {
                "terminal_notification_claimed_at": _now(),
                "terminal_status": terminal_status,
                "updated_at": _now(),
            }

        with self._store_lock():
            normalized_job_id = _normalize_job_id(job_id)
            normalized_target = _normalize_target(notification_target)
            payload = self._read()
            for watch in payload["watches"]:
                if watch["job_id"] != normalized_job_id or watch["notification_target"] != normalized_target:
                    continue
                updates = update(watch)
                if not updates:
                    return None
                watch.update(updates)
                self._write(payload)
                return dict(watch)
        return None

    def publish_status_message(
        self,
        *,
        job_id: str,
        notification_target: str,
        job: Dict[str, Any],
        send_status_message: SendStatusMessage,
    ) -> Optional[Dict[str, Any]]:
        send_target = None
        status_key = None
        message = None
        message_id = None
        tick = 1
        with self._store_lock():
            normalized_job_id = _normalize_job_id(job_id)
            normalized_target = _normalize_target(notification_target)
            payload = self._read()
            for watch in payload["watches"]:
                if watch["job_id"] != normalized_job_id or watch["notification_target"] != normalized_target:
                    continue
                if watch.get("notified_at") or watch.get("abandoned_at"):
                    return dict(watch)
                tracking = _status_tracking(watch)
                if _status_error_count(tracking) >= DEFAULT_STATUS_MAX_ERRORS:
                    return dict(watch)
                message_id = _tracking_string(tracking, "message_id")
                if tracking.get("last_message") and not message_id:
                    # The transport could not return an editable message id, so
                    # each update would land as a new chat message; send the
                    # running status once and rely on the terminal notice.
                    return dict(watch)
                tick = _next_tick(tracking)
                message = _running_status_message(watch, job, tick)
                if tracking.get("last_message") == message:
                    return dict(watch)
                send_target = watch["notification_target"]
                status_key = _status_key(watch["job_id"])
                break
            else:
                return None

        try:
            new_message_id = send_status_message(send_target, status_key, message, message_id)
        except Exception as error:
            return self.mark_status_error(
                job_id=job_id,
                notification_target=notification_target,
                error=str(error),
            )

        return self.mark_status_published(
            job_id=job_id,
            notification_target=notification_target,
            message=message,
            message_id=new_message_id or message_id,
            tick=tick,
        )

    def publish_terminal_status_message(
        self,
        *,
        job_id: str,
        notification_target: str,
        job: Dict[str, Any],
        send_status_message: SendStatusMessage,
    ) -> Optional[Dict[str, Any]]:
        send_target = None
        status_key = None
        message = None
        message_id = None
        with self._store_lock():
            normalized_job_id = _normalize_job_id(job_id)
            normalized_target = _normalize_target(notification_target)
            payload = self._read()
            for watch in payload["watches"]:
                if watch["job_id"] != normalized_job_id or watch["notification_target"] != normalized_target:
                    continue
                tracking = _status_tracking(watch)
                message_id = _tracking_string(tracking, "message_id")
                if not message_id:
                    return dict(watch)
                message = _terminal_status_message(watch, job)
                if tracking.get("last_message") == message:
                    return dict(watch)
                send_target = watch["notification_target"]
                status_key = _status_key(watch["job_id"])
                break
            else:
                return None

        try:
            new_message_id = send_status_message(send_target, status_key, message, message_id)
        except Exception as error:
            return self.mark_status_error(
                job_id=job_id,
                notification_target=notification_target,
                error=str(error),
            )

        return self.mark_status_published(
            job_id=job_id,
            notification_target=notification_target,
            message=message,
            message_id=new_message_id or message_id,
            tick=0,
        )

    def mark_status_error(self, *, job_id: str, notification_target: str, error: str) -> Optional[Dict[str, Any]]:
        def update(watch: Dict[str, Any]) -> Dict[str, Any]:
            tracking = _status_tracking(watch)
            tracking["error_count"] = _status_error_count(tracking) + 1
            tracking["last_error"] = error[:500]
            tracking["updated_at"] = _now()
            if _status_error_count(tracking) >= DEFAULT_STATUS_MAX_ERRORS:
                tracking["paused_at"] = _now()
            return {"status_tracking": tracking, "updated_at": _now()}

        return self._update_watch_with(
            job_id=job_id,
            notification_target=notification_target,
            update_fn=update,
        )

    def _update_watch(
        self,
        *,
        job_id: str,
        notification_target: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return self._update_watch_with(
            job_id=job_id,
            notification_target=notification_target,
            update_fn=lambda _watch: updates,
        )

    def _update_watch_with(
        self,
        *,
        job_id: str,
        notification_target: str,
        update_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        with self._store_lock():
            normalized_job_id = _normalize_job_id(job_id)
            normalized_target = _normalize_target(notification_target)
            payload = self._read()
            for watch in payload["watches"]:
                if watch["job_id"] == normalized_job_id and watch["notification_target"] == normalized_target:
                    watch.update(update_fn(watch))
                    self._write(payload)
                    return dict(watch)
        return None

class JobCompletionNotifier:
    def __init__(
        self,
        *,
        store: JobNotificationStore,
        send_message: SendMessage,
        send_status_message: Optional[SendStatusMessage] = None,
        poll_interval_seconds: float = 60.0,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self.store = store
        self.send_message = send_message
        self.send_status_message = send_status_message
        self.poll_interval_seconds = poll_interval_seconds
        self.max_errors = max_errors

    @classmethod
    def from_env(cls) -> "JobCompletionNotifier":
        return cls(
            store=JobNotificationStore(default_notification_watch_store_path()),
            send_message=send_hermes_message,
            send_status_message=send_status_message_from_env,
            poll_interval_seconds=float_env_any(
                "MPILOT_SUBTITLE_JOB_NOTIFICATION_INTERVAL_SECONDS",
                "BABELARR_JOB_NOTIFICATION_INTERVAL_SECONDS",
                "MST_JOB_NOTIFICATION_INTERVAL_SECONDS",
                default=3.0,
            ),
            max_errors=int_env_any(
                "MPILOT_SUBTITLE_JOB_NOTIFICATION_MAX_ERRORS",
                "BABELARR_JOB_NOTIFICATION_MAX_ERRORS",
                "MST_JOB_NOTIFICATION_MAX_ERRORS",
                default=DEFAULT_MAX_ERRORS,
            ),
        )

    def register_watch(
        self,
        *,
        job_id: str,
        job_store_dir: str | Path,
        notification_target: str,
        title: Optional[str] = None,
        requester_id: Optional[str] = None,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        initial_notification_delay_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        watch = self.store.upsert_watch(
            job_id=job_id,
            job_store_dir=job_store_dir,
            title=title,
            notification_target=notification_target,
            metadata=metadata,
            requester_id=requester_id,
            language=language,
            notification_not_before=_notification_not_before_after_seconds(initial_notification_delay_seconds),
        )
        return watch

    def poll_once(self) -> None:
        for watch in self.store.pending_watches():
            if not _notification_due(watch):
                continue
            try:
                job = JobStore(Path(watch["job_store_dir"])).get(watch["job_id"])
            except Exception as error:
                updated_watch = self.store.mark_error(
                    job_id=watch["job_id"],
                    notification_target=watch["notification_target"],
                    error=str(error),
                )
                if updated_watch and _error_count(updated_watch) >= self.max_errors:
                    self.store.mark_abandoned(
                        job_id=watch["job_id"],
                        notification_target=watch["notification_target"],
                        error=str(error),
                    )
                    try:
                        self.send_message(watch["notification_target"], _abandoned_message(updated_watch))
                    except Exception:
                        pass
                continue

            status = str(job.get("status") or "")
            if status not in TERMINAL_JOB_STATUSES:
                try:
                    self.publish_running_status(watch, job)
                except Exception:
                    pass
                continue

            claimed_watch = self.store.claim_terminal_notification(
                job_id=watch["job_id"],
                notification_target=watch["notification_target"],
                terminal_status=status,
            )
            if claimed_watch is None:
                continue
            try:
                self.publish_terminal_status(claimed_watch, job)
                self.send_message(claimed_watch["notification_target"], _completion_message(claimed_watch, job))
            except Exception as error:
                updated_watch = self.store.mark_error(
                    job_id=claimed_watch["job_id"],
                    notification_target=claimed_watch["notification_target"],
                    error=str(error),
                )
                if updated_watch and _error_count(updated_watch) >= self.max_errors:
                    self.store.mark_abandoned(
                        job_id=claimed_watch["job_id"],
                        notification_target=claimed_watch["notification_target"],
                        error=str(error),
                    )
                continue
            self.store.mark_notified(
                job_id=claimed_watch["job_id"],
                notification_target=claimed_watch["notification_target"],
                terminal_status=status,
            )

    def publish_running_status(self, watch: Dict[str, Any], job: Dict[str, Any]) -> None:
        if self.send_status_message is None:
            return
        self.store.publish_status_message(
            job_id=watch["job_id"],
            notification_target=watch["notification_target"],
            job=job,
            send_status_message=self.send_status_message,
        )

    def publish_terminal_status(self, watch: Dict[str, Any], job: Dict[str, Any]) -> None:
        if self.send_status_message is None:
            return
        with contextlib.suppress(Exception):
            self.store.publish_terminal_status_message(
                job_id=watch["job_id"],
                notification_target=watch["notification_target"],
                job=job,
                send_status_message=self.send_status_message,
            )


def default_notification_watch_store_path() -> Path:
    configured = env_first(
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_WATCHES_PATH",
        "BABELARR_JOB_NOTIFICATION_WATCHES_PATH",
        "MST_JOB_NOTIFICATION_WATCHES_PATH",
    )
    if configured:
        return Path(configured).expanduser()
    return default_job_store_dir().parent / "job-notification-watches.json"


def default_notification_daemon_lock_path() -> Path:
    configured = env_first(
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_DAEMON_LOCK_PATH",
        "BABELARR_JOB_NOTIFICATION_DAEMON_LOCK_PATH",
        "MST_JOB_NOTIFICATION_DAEMON_LOCK_PATH",
    )
    if configured:
        return Path(configured).expanduser()
    path = default_notification_watch_store_path()
    return path.with_suffix(path.suffix + ".daemon.lock")


def default_notification_wake_path() -> Path:
    configured = env_first(
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_WAKE_PATH",
        "BABELARR_JOB_NOTIFICATION_WAKE_PATH",
        "MST_JOB_NOTIFICATION_WAKE_PATH",
    )
    if configured:
        return Path(configured).expanduser()
    path = default_notification_watch_store_path()
    return path.with_suffix(path.suffix + ".wake")


def acquire_notification_daemon_lock(lock_path: str | Path):
    path = Path(lock_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise
    return handle


def touch_notification_wake_file(wake_path: Optional[str | Path] = None) -> Path:
    path = Path(wake_path).expanduser() if wake_path is not None else default_notification_wake_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def start_notification_daemon_from_env(*, popen=subprocess.Popen):
    command = [sys.executable, "-m", "mpilot.subtitles", "notify-daemon"]
    cwd = Path(__file__).resolve().parents[3]
    return popen(
        command,
        cwd=str(cwd),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def run_notification_daemon(
    *,
    notifier: Optional[JobCompletionNotifier] = None,
    lock_path: Optional[str | Path] = None,
    wake_path: Optional[str | Path] = None,
    idle_exit_seconds: float = 300.0,
    poll_interval_seconds: Optional[float] = None,
    run_once: bool = False,
    lock_acquire_timeout_seconds: float = 0.0,
) -> Dict[str, Any]:
    daemon_lock_path = Path(lock_path).expanduser() if lock_path is not None else default_notification_daemon_lock_path()
    lock_handle = acquire_notification_daemon_lock(daemon_lock_path)
    # A previous daemon may be a moment away from idle-exit; waiting briefly for
    # its lock keeps a watch registered in that window from going unserved.
    acquire_deadline = time.monotonic() + max(0.0, lock_acquire_timeout_seconds)
    while lock_handle is None and time.monotonic() < acquire_deadline:
        time.sleep(0.2)
        lock_handle = acquire_notification_daemon_lock(daemon_lock_path)
    if lock_handle is None:
        return {"status": "already_running", "lock_path": str(daemon_lock_path)}

    active_notifier = notifier or JobCompletionNotifier.from_env()
    if poll_interval_seconds is not None:
        active_notifier.poll_interval_seconds = poll_interval_seconds
    active_wake_path = Path(wake_path).expanduser() if wake_path is not None else default_notification_wake_path()
    last_wake_mtime = _file_mtime(active_wake_path)
    last_activity = time.monotonic()
    next_poll_at = 0.0
    poll_count = 0

    try:
        while True:
            now = time.monotonic()
            wake_mtime = _file_mtime(active_wake_path)
            wake_changed = wake_mtime is not None and wake_mtime != last_wake_mtime
            if now >= next_poll_at or wake_changed:
                last_wake_mtime = wake_mtime
                active_notifier.poll_once()
                poll_count += 1
                pending = active_notifier.store.pending_watches()
                if pending:
                    last_activity = now
                if run_once:
                    return {"status": "ran_once", "lock_path": str(daemon_lock_path), "poll_count": poll_count}
                next_poll_at = time.monotonic() + max(0.1, active_notifier.poll_interval_seconds)
            if idle_exit_seconds >= 0 and time.monotonic() - last_activity >= idle_exit_seconds:
                return {"status": "idle_exit", "lock_path": str(daemon_lock_path), "poll_count": poll_count}
            time.sleep(1.0)
    finally:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def _send_hermes_message(target: str, message: str) -> None:
    core_send_hermes_message(
        target,
        message,
        bin_env_names=("MPILOT_HERMES_BIN", "BABELARR_HERMES_BIN", "MST_HERMES_BIN"),
        profile_env_names=("MPILOT_HERMES_PROFILE", "BABELARR_HERMES_PROFILE", "MST_HERMES_PROFILE"),
        timeout_env_names=(
            "MPILOT_HERMES_SEND_TIMEOUT_SECONDS",
            "BABELARR_HERMES_SEND_TIMEOUT_SECONDS",
            "MST_HERMES_SEND_TIMEOUT_SECONDS",
        ),
        default_timeout=DEFAULT_HERMES_SEND_TIMEOUT_SECONDS,
    )


send_hermes_message = _send_hermes_message


def send_status_message_from_env(
    target: str,
    status_key: str,
    message: str,
    message_id: Optional[str] = None,
) -> Optional[str]:
    token = _telegram_bot_token()
    telegram_target = parse_telegram_target(target)
    if token and telegram_target:
        return _send_or_edit_telegram_status_message(
            token=token,
            chat_id=telegram_target[0],
            thread_id=telegram_target[1],
            message=message,
            message_id=message_id,
        )
    send_hermes_message(target, message)
    return None


def resolve_notification_target(notification_target: Optional[str], requester_id: Optional[str]) -> Optional[str]:
    return core_resolve_notification_target(notification_target, requester_id)


def initial_notification_delay_seconds_from_env() -> float:
    return max(
        0.0,
        float_env_any(
            "MPILOT_SUBTITLE_JOB_NOTIFICATION_INITIAL_DELAY_SECONDS",
            "BABELARR_JOB_NOTIFICATION_INITIAL_DELAY_SECONDS",
            "MST_JOB_NOTIFICATION_INITIAL_DELAY_SECONDS",
            default=DEFAULT_INITIAL_NOTIFICATION_DELAY_SECONDS,
        ),
    )


def _completion_message(watch: Dict[str, Any], job: Dict[str, Any]) -> str:
    status = str(job.get("status") or "")
    language = _language_for_watch(watch)
    title = _title_for_watch(watch)
    if status == "succeeded":
        return _succeeded_message(language, title)
    if status == "needs_confirmation":
        return _needs_confirmation_message(language, title)
    if status == "failed":
        reason = ""
        last_error = job.get("last_error")
        if isinstance(last_error, dict):
            reason = str(last_error.get("message") or "")
        return _failed_message(language, title, reason)
    return _generic_message(language, title, status)


def _succeeded_message(language: str, title: str) -> str:
    if language == "zh":
        return "字幕已制作完成：%s\n请先退回到影片详情页，再重新进入播放，播放器就会加载最新字幕。" % title
    if language == "fr":
        return "Les sous-titres sont prets : %s\nReviens a la fiche du film, puis relance la lecture pour charger les derniers sous-titres." % title
    return "Subtitles are ready: %s\nGo back to the movie details page, then reopen playback so Plex loads the latest subtitles." % title


def _failed_message(language: str, title: str, reason: str) -> str:
    if language == "zh":
        suffix = "\n原因：%s" % reason if reason else ""
        return "字幕处理失败：%s%s" % (title, suffix)
    if language == "fr":
        suffix = "\nRaison : %s" % reason if reason else ""
        return "Le traitement des sous-titres a échoué : %s%s" % (title, suffix)
    suffix = "\nReason: %s" % reason if reason else ""
    return "Subtitle processing failed: %s%s" % (title, suffix)


def _needs_confirmation_message(language: str, title: str) -> str:
    if language == "zh":
        return "我找到了一个可能可用的字幕：%s\n但时间轴可能不完全匹配。请回复确认是否继续尝试。" % title
    if language == "fr":
        return "J'ai trouvé un sous-titre possible : %s\nLe minutage risque de ne pas être parfait. Dis-moi si tu veux continuer." % title
    return "I found a possible subtitle for %s, but the timing may not match perfectly. Please confirm if you want to try it." % title


def _generic_message(language: str, title: str, status: str) -> str:
    if language == "zh":
        return "字幕任务状态更新：%s\n状态：%s" % (title, status)
    if language == "fr":
        return "Mise a jour des sous-titres : %s\nStatut : %s" % (title, status)
    return "Subtitle job update: %s\nStatus: %s" % (title, status)


def _abandoned_message(watch: Dict[str, Any]) -> str:
    language = _language_for_watch(watch)
    title = _title_for_watch(watch)
    if language == "zh":
        return "字幕任务状态跟踪已停止：%s\n我多次无法确认这个任务的状态。" % title
    if language == "fr":
        return "Le suivi du traitement des sous-titres s'est arrete : %s\nJe n'ai pas pu verifier son etat apres plusieurs essais." % title
    return "Subtitle job tracking stopped: %s\nI could not verify this job status after repeated attempts." % title


def _title_for_watch(watch: Dict[str, Any]) -> str:
    title = _string_or_none(watch.get("title"))
    if title:
        return title
    language = _language_for_watch(watch)
    if language == "zh":
        return "字幕任务"
    if language == "fr":
        return "votre tâche de sous-titres"
    return "your subtitle job"


def _language_for_watch(watch: Dict[str, Any]) -> str:
    language = (_string_or_none(watch.get("language")) or "").casefold()
    if language.startswith("zh") or language in {"chinese", "cn"}:
        return "zh"
    if language.startswith("fr") or language == "french":
        return "fr"
    return "en"


def _running_status_message(watch: Dict[str, Any], job: Dict[str, Any], tick: int) -> str:
    language = _language_for_watch(watch)
    title = _title_for_watch(watch)
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    stage = str(progress.get("stage") or job.get("status") or "running")
    details = progress.get("details") if isinstance(progress.get("details"), dict) else {}

    if language == "zh":
        rendered = ["字幕处理中：%s" % title, _activity_label(_zh_stage_label(stage), tick)]
        chunk_line = _zh_translation_progress_line(stage, details)
        if chunk_line:
            rendered.append(chunk_line)
        return "\n".join(rendered)
    if language == "fr":
        rendered = ["Traitement des sous-titres : %s" % title, _activity_label(_fr_stage_label(stage), tick)]
        chunk_line = _fr_translation_progress_line(stage, details)
        if chunk_line:
            rendered.append(chunk_line)
        return "\n".join(rendered)
    rendered = ["Subtitle processing: %s" % title, _activity_label(_en_stage_label(stage), tick)]
    chunk_line = _en_translation_progress_line(stage, details)
    if chunk_line:
        rendered.append(chunk_line)
    return "\n".join(rendered)


def _terminal_status_message(watch: Dict[str, Any], job: Dict[str, Any]) -> str:
    language = _language_for_watch(watch)
    title = _title_for_watch(watch)
    status = str(job.get("status") or "")
    if language == "zh":
        if status == "succeeded":
            return "字幕已完成：%s" % title
        if status == "failed":
            return "字幕处理失败：%s" % title
        if status == "needs_confirmation":
            return "字幕需要确认：%s" % title
        return "字幕任务状态更新：%s" % title
    if language == "fr":
        if status == "succeeded":
            return "Sous-titres prets : %s" % title
        if status == "failed":
            return "Traitement des sous-titres echoue : %s" % title
        if status == "needs_confirmation":
            return "Confirmation requise : %s" % title
        return "Mise a jour des sous-titres : %s" % title
    if status == "succeeded":
        return "Subtitles are ready: %s" % title
    if status == "failed":
        return "Subtitle processing failed: %s" % title
    if status == "needs_confirmation":
        return "Subtitle confirmation needed: %s" % title
    return "Subtitle job update: %s" % title


def _zh_stage_label(stage: str) -> str:
    labels = {
        "queued": "字幕任务排队中",
        "started": "正在准备字幕处理",
        "running": "字幕任务正在处理",
        "reading_source_subtitle": "正在读取源字幕",
        "resolving_media": "正在识别影片信息",
        "media_resolved": "已识别影片信息，正在准备字幕处理",
        "checking_local_subtitles": "正在查找本地字幕",
        "checking_source_sidecar": "正在查找同目录字幕",
        "probing_embedded_subtitles": "正在检查视频内嵌字幕流",
        "extracting_embedded_subtitle": "正在提取内嵌字幕",
        "using_remote_source_executor": "正在使用 NAS 处理视频字幕",
        "local_source_missing": "未找到可用本地源字幕，正在准备其他来源",
        "searching_online_subtitles": "正在搜索在线字幕",
        "online_subtitle_candidates": "正在筛选在线字幕",
        "online_subtitle_selected": "已选定源字幕",
        "source_subtitle_ready": "已找到源字幕，正在准备翻译",
        "normalizing_source": "正在规范化源字幕",
        "translating": "正在翻译字幕",
        "rendering_output": "正在渲染字幕文件",
        "using_remote_output_writer": "正在准备写入 NAS 媒体库",
        "writing_remote_output": "正在写入 NAS 媒体库",
        "output_ready": "字幕输出已生成，正在收尾",
        "writing_back": "正在写入影片旁边的字幕文件",
        "write_back_complete": "字幕文件已写入，正在收尾",
        "refreshing_plex": "正在刷新 Plex 媒体库",
        "plex_refresh_complete": "Plex 刷新请求已完成，正在收尾",
    }
    return labels.get(stage, "字幕任务正在处理")


def _en_stage_label(stage: str) -> str:
    labels = {
        "queued": "Subtitle job is queued",
        "started": "Preparing subtitle processing",
        "running": "Processing subtitles",
        "reading_source_subtitle": "Reading source subtitle",
        "resolving_media": "Resolving media",
        "media_resolved": "Media resolved, preparing subtitles",
        "checking_local_subtitles": "Checking local subtitles",
        "checking_source_sidecar": "Checking sidecar subtitles",
        "probing_embedded_subtitles": "Probing embedded subtitle streams",
        "extracting_embedded_subtitle": "Extracting embedded subtitle",
        "using_remote_source_executor": "Using NAS media host for subtitle processing",
        "local_source_missing": "No local source subtitle found, preparing fallback",
        "searching_online_subtitles": "Searching online subtitles",
        "online_subtitle_candidates": "Reviewing subtitle candidates",
        "online_subtitle_selected": "Source subtitle selected",
        "source_subtitle_ready": "Source subtitle ready",
        "normalizing_source": "Normalizing source subtitle",
        "translating": "Translating subtitles",
        "rendering_output": "Rendering subtitle file",
        "using_remote_output_writer": "Preparing NAS media library output",
        "writing_remote_output": "Writing to NAS media library",
        "output_ready": "Subtitle output ready, finishing up",
        "writing_back": "Writing subtitle next to media",
        "write_back_complete": "Subtitle file written, finishing up",
        "refreshing_plex": "Refreshing Plex",
        "plex_refresh_complete": "Plex refresh requested, finishing up",
    }
    return labels.get(stage, "Processing subtitles")


def _fr_stage_label(stage: str) -> str:
    labels = {
        "queued": "Traitement en attente",
        "started": "Preparation du traitement",
        "running": "Traitement des sous-titres",
        "reading_source_subtitle": "Lecture du sous-titre source",
        "resolving_media": "Identification du media",
        "media_resolved": "Media identifie, preparation des sous-titres",
        "checking_local_subtitles": "Recherche des sous-titres locaux",
        "checking_source_sidecar": "Recherche des sous-titres voisins",
        "probing_embedded_subtitles": "Inspection des pistes de sous-titres integrees",
        "extracting_embedded_subtitle": "Extraction du sous-titre integre",
        "using_remote_source_executor": "Traitement des sous-titres sur le NAS",
        "local_source_missing": "Aucun sous-titre local source trouve, preparation du repli",
        "searching_online_subtitles": "Recherche de sous-titres en ligne",
        "online_subtitle_candidates": "Selection des candidats",
        "online_subtitle_selected": "Sous-titre source selectionne",
        "source_subtitle_ready": "Sous-titre source pret",
        "normalizing_source": "Normalisation du sous-titre source",
        "translating": "Traduction des sous-titres",
        "rendering_output": "Generation du fichier de sous-titres",
        "using_remote_output_writer": "Preparation de l'ecriture NAS",
        "writing_remote_output": "Ecriture vers la bibliotheque NAS",
        "output_ready": "Fichier de sous-titres pret, finalisation",
        "writing_back": "Ecriture du sous-titre pres du media",
        "write_back_complete": "Fichier ecrit, finalisation",
        "refreshing_plex": "Actualisation de Plex",
        "plex_refresh_complete": "Actualisation Plex demandee, finalisation",
    }
    return labels.get(stage, "Traitement des sous-titres")


ACTIVITY_SPINNER_FRAMES = ("🕐", "🕓", "🕗")


def _activity_label(label: str, tick: int) -> str:
    try:
        count = int(tick)
    except (TypeError, ValueError):
        count = 1
    frame = ACTIVITY_SPINNER_FRAMES[(max(1, count) - 1) % len(ACTIVITY_SPINNER_FRAMES)]
    return "%s %s" % (frame, label)


def _zh_translation_progress_line(stage: str, details: Dict[str, Any]) -> Optional[str]:
    counts = _translation_chunk_counts(stage, details)
    if counts is None:
        return None
    return "翻译进度：%s/%s" % counts


def _fr_translation_progress_line(stage: str, details: Dict[str, Any]) -> Optional[str]:
    counts = _translation_chunk_counts(stage, details)
    if counts is None:
        return None
    return "Progression traduction : %s/%s segments" % counts


def _en_translation_progress_line(stage: str, details: Dict[str, Any]) -> Optional[str]:
    counts = _translation_chunk_counts(stage, details)
    if counts is None:
        return None
    return "Translation progress: %s/%s chunks" % counts


def _translation_chunk_counts(stage: str, details: Dict[str, Any]) -> Optional[tuple[int, int]]:
    if stage != "translating":
        return None
    total = _positive_int(details.get("total_chunks"))
    if total is None:
        return None
    completed = _non_negative_int(details.get("completed_chunks"))
    if completed is None:
        completed = 0
    return min(completed, total), total


def _revive_watch(watch: Dict[str, Any]) -> None:
    # A confirmed or retried job reuses its job_id, so re-registration must
    # clear the previous run's terminal state or the daemon will never watch
    # the new run. status_tracking.message_id survives so the new run keeps
    # editing the same status message.
    watch["notified_at"] = None
    watch["abandoned_at"] = None
    watch["terminal_notification_claimed_at"] = None
    watch["terminal_status"] = None
    watch["error_count"] = 0
    watch["last_error"] = None
    tracking = _status_tracking(watch)
    tracking.pop("last_message", None)
    tracking.pop("paused_at", None)
    tracking["error_count"] = 0
    tracking["last_error"] = None
    watch["status_tracking"] = tracking


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _notification_due(watch: Dict[str, Any]) -> bool:
    not_before = _parse_timestamp(watch.get("notification_not_before"))
    if not_before is None:
        return True
    return datetime.now(timezone.utc) >= not_before


def _notification_not_before_after_seconds(delay_seconds: Optional[float]) -> Optional[str]:
    if delay_seconds is None:
        return None
    try:
        delay = float(delay_seconds)
    except (TypeError, ValueError):
        return None
    if delay <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _status_tracking(watch: Dict[str, Any]) -> Dict[str, Any]:
    tracking = watch.get("status_tracking")
    return dict(tracking) if isinstance(tracking, dict) else {}


def _status_key(job_id: str) -> str:
    return "mpilot-subtitle:%s" % _normalize_job_id(job_id)


def _next_tick(tracking: Dict[str, Any]) -> int:
    try:
        current = int(tracking.get("tick") or 0)
    except (TypeError, ValueError):
        current = 0
    return current % 3 + 1


def _tracking_string(tracking: Dict[str, Any], key: str) -> Optional[str]:
    value = tracking.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _telegram_bot_token() -> Optional[str]:
    return telegram_bot_token(
        "MPILOT_TELEGRAM_BOT_TOKEN",
        "BABELARR_TELEGRAM_BOT_TOKEN",
        "MST_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        hermes_env_path_names=("MPILOT_HERMES_ENV_PATH", "BABELARR_HERMES_ENV_PATH", "MST_HERMES_ENV_PATH"),
    )


def _send_or_edit_telegram_status_message(
    *,
    token: str,
    chat_id: str,
    thread_id: Optional[str],
    message: str,
    message_id: Optional[str],
) -> Optional[str]:
    return send_or_edit_telegram_status_message(
        token=token,
        chat_id=chat_id,
        thread_id=thread_id,
        message=message,
        message_id=message_id,
        api_post=_telegram_api_post,
        include_message_id_invalid=False,
    )


def _telegram_api_post(token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return telegram_api_post(token, method, payload)


def _telegram_message_id(data: Dict[str, Any]) -> Optional[str]:
    return telegram_message_id(data)


def _coerce_telegram_int(value: str) -> Any:
    return coerce_telegram_int(value)


def _normalize_job_id(job_id: str) -> str:
    normalized = job_id.strip()
    if not normalized:
        raise ValueError("job_id must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("job_id must be one line")
    return normalized


def _normalize_target(notification_target: str) -> str:
    target = notification_target.strip()
    if not target:
        raise ValueError("notification_target must not be empty")
    if "\n" in target or "\r" in target:
        raise ValueError("notification_target must be one line")
    return target


def _normalize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            normalized[key] = value
    return normalized


def _merge_metadata(existing: Any, metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    merged = _normalize_metadata(existing if isinstance(existing, dict) else None)
    merged.update(_normalize_metadata(metadata))
    return merged


def _string_or_none(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _error_count(watch: Dict[str, Any]) -> int:
    try:
        return int(watch.get("error_count") or 0)
    except (TypeError, ValueError):
        return 0


def _status_error_count(tracking: Dict[str, Any]) -> int:
    try:
        return int(tracking.get("error_count") or 0)
    except (TypeError, ValueError):
        return 0


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
