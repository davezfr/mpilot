from __future__ import annotations

import asyncio
import contextlib
import fcntl
import inspect
import json
import logging
import os
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from mpilot.acquisition.client import QbitlarrApiClient, QbitlarrApiError, get_qbitlarr_client
from mpilot.acquisition.domain.download_progress import (
    dynamic_progress_watch_policy,
    render_download_status_payload,
    render_download_status,
    render_tracking_expired_status,
)
from mpilot.acquisition.env import env_first as acquisition_env_first
from mpilot.core.env import float_env_any, int_env_any, telegram_bot_token
from mpilot.core.hermes import (
    DEFAULT_HERMES_SEND_TIMEOUT_SECONDS,
    decode_process_output,
    hermes_send_command,
    hermes_send_config,
)
from mpilot.core.json_store import LockedJsonStore
from mpilot.core.targets import parse_telegram_target
from mpilot.core.telegram import (
    async_send_or_edit_telegram_status_message,
    async_telegram_api_post,
    coerce_telegram_int,
    telegram_message_id,
)


SendMessage = Callable[[str, str], Awaitable[None]]
SendStatusMessage = Callable[[str, str, str, str | None, list[dict[str, str]] | None], Awaitable[str | None]]
CompletionHook = Callable[[dict[str, Any]], Awaitable[None]]
COMPLETE_STATES = {"uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP"}
DEFAULT_MAX_ERRORS = 10
logger = logging.getLogger("qbitlarr-mcp.notifications")


class DownloadWatchStore(LockedJsonStore):
    def upsert_watch(
        self,
        *,
        info_hash: str,
        title: str,
        notification_target: str,
        metadata: dict[str, Any] | None = None,
        requester_id: str | None = None,
        track_progress: bool = False,
    ) -> dict[str, Any]:
        with self._store_lock():
            normalized_hash = _normalize_hash(info_hash)
            normalized_target = _normalize_target(notification_target)
            payload = self._read()
            now = _now()

            for watch in payload["watches"]:
                if watch["info_hash"] == normalized_hash and watch["notification_target"] == normalized_target:
                    watch["title"] = title.strip() or watch["title"]
                    watch["requester_id"] = requester_id or watch.get("requester_id")
                    watch["metadata"] = _merge_metadata(watch.get("metadata"), metadata)
                    watch["notified_at"] = None
                    watch["abandoned_at"] = None
                    watch["completion_notified_at"] = None
                    watch["removal_notified_at"] = None
                    watch["error_count"] = 0
                    watch["last_error"] = None
                    if track_progress:
                        watch["progress_tracking"] = _new_progress_tracking(normalized_hash, now=now)
                    watch["updated_at"] = now
                    self._write(payload)
                    return dict(watch)

            watch = {
                "info_hash": normalized_hash,
                "title": title.strip() or normalized_hash,
                "notification_target": normalized_target,
                "requester_id": requester_id,
                "metadata": _normalize_metadata(metadata),
                "created_at": now,
                "updated_at": now,
                "notified_at": None,
                "completion_notified_at": None,
                "removal_notified_at": None,
                "abandoned_at": None,
                "error_count": 0,
                "last_error": None,
            }
            if track_progress:
                watch["progress_tracking"] = _new_progress_tracking(normalized_hash, now=now)
            payload["watches"].append(watch)
            self._write(payload)
            return dict(watch)

    def pending_watches(self) -> list[dict[str, Any]]:
        with self._store_lock():
            return [
                dict(watch)
                for watch in self._read()["watches"]
                if not watch.get("notified_at") and not watch.get("abandoned_at")
            ]

    def mark_notified(self, *, info_hash: str, notification_target: str) -> None:
        self._update_watch(
            info_hash=info_hash,
            notification_target=notification_target,
            updates={"notified_at": _now(), "last_error": None},
        )

    def mark_completion_notified(self, *, info_hash: str, notification_target: str) -> dict[str, Any] | None:
        return self._update_watch(
            info_hash=info_hash,
            notification_target=notification_target,
            updates={"completion_notified_at": _now(), "updated_at": _now()},
        )

    def mark_removal_notified(self, *, info_hash: str, notification_target: str) -> dict[str, Any] | None:
        return self._update_watch(
            info_hash=info_hash,
            notification_target=notification_target,
            updates={"removal_notified_at": _now(), "updated_at": _now()},
        )

    def mark_progress_published(
        self,
        *,
        info_hash: str,
        notification_target: str,
        message: str,
        message_id: str | None,
        completed: bool = False,
        expired: bool = False,
    ) -> dict[str, Any] | None:
        def update(watch: dict[str, Any]) -> dict[str, Any]:
            tracking = _progress_tracking(watch)
            if tracking is None:
                tracking = _new_progress_tracking(_normalize_hash(info_hash), now=_now())
            tracking["last_message"] = message
            tracking["updated_at"] = _now()
            if message_id:
                tracking["message_id"] = message_id
            if completed:
                tracking["completed_at"] = _now()
            if expired:
                tracking["expired_at"] = _now()
            return {"progress_tracking": tracking, "updated_at": _now()}

        return self._update_watch_with(
            info_hash=info_hash,
            notification_target=notification_target,
            update_fn=update,
        )

    def mark_error(self, *, info_hash: str, notification_target: str, error: str) -> dict[str, Any] | None:
        return self._update_watch_with(
            info_hash=info_hash,
            notification_target=notification_target,
            update_fn=lambda watch: {
                "error_count": _error_count(watch) + 1,
                "last_error": error[:500],
                "updated_at": _now(),
            },
        )

    def mark_abandoned(self, *, info_hash: str, notification_target: str, error: str | None = None) -> None:
        updates = {"abandoned_at": _now(), "updated_at": _now()}
        if error:
            updates["last_error"] = error[:500]
        self._update_watch(
            info_hash=info_hash,
            notification_target=notification_target,
            updates=updates,
        )

    def _update_watch(self, *, info_hash: str, notification_target: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        return self._update_watch_with(
            info_hash=info_hash,
            notification_target=notification_target,
            update_fn=lambda _watch: updates,
        )

    def _update_watch_with(
        self,
        *,
        info_hash: str,
        notification_target: str,
        update_fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any] | None:
        normalized_hash = _normalize_hash(info_hash)
        normalized_target = _normalize_target(notification_target)
        with self._store_lock():
            payload = self._read()
            for watch in payload["watches"]:
                if watch["info_hash"] == normalized_hash and watch["notification_target"] == normalized_target:
                    watch.update(update_fn(watch))
                    self._write(payload)
                    return dict(watch)
        return None

class DownloadCompletionNotifier:
    def __init__(
        self,
        *,
        store: DownloadWatchStore,
        client: QbitlarrApiClient | Any,
        send_message: SendMessage,
        send_status_message: SendStatusMessage | None = None,
        completion_hook: CompletionHook | None = None,
        poll_interval_seconds: float = 60.0,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self.store = store
        self.client = client
        self.send_message = send_message
        self.send_status_message = send_status_message or send_status_message_from_env
        self.completion_hook = completion_hook
        self.poll_interval_seconds = poll_interval_seconds
        self.max_errors = max_errors
        self._task: asyncio.Task | None = None

    @classmethod
    def from_env(cls) -> "DownloadCompletionNotifier":
        return cls(
            store=DownloadWatchStore(default_watch_store_path()),
            client=get_qbitlarr_client(),
            send_message=send_hermes_message,
            send_status_message=send_status_message_from_env,
            completion_hook=completion_hook_from_env(),
            poll_interval_seconds=float_env_any(
                "MPILOT_ACQUISITION_NOTIFICATION_INTERVAL_SECONDS",
                "QBITLARR_NOTIFICATION_INTERVAL_SECONDS",
                default=3.0,
            ),
            max_errors=int_env_any(
                "MPILOT_ACQUISITION_NOTIFICATION_MAX_ERRORS",
                "QBITLARR_NOTIFICATION_MAX_ERRORS",
                default=DEFAULT_MAX_ERRORS,
            ),
        )

    async def register_watch(
        self,
        *,
        info_hash: str,
        title: str,
        notification_target: str,
        metadata: dict[str, Any] | None = None,
        requester_id: str | None = None,
        track_progress: bool = False,
        start: bool = True,
    ) -> dict[str, Any]:
        watch = self.store.upsert_watch(
            info_hash=info_hash,
            title=title,
            notification_target=notification_target,
            metadata=metadata,
            requester_id=requester_id,
            track_progress=track_progress,
        )
        if start:
            self.start()
        return watch

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            self._task.add_done_callback(_log_task_failure)

    async def poll_once(self) -> None:
        for watch in self.store.pending_watches():
            try:
                await self._poll_watch(watch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Download notification watch failed for info_hash=%s target=%s",
                    watch.get("info_hash"),
                    watch.get("notification_target"),
                )
                self._mark_error_safely(watch, "watch poll failed", exc)

    async def _poll_watch(self, watch: dict[str, Any]) -> None:
        try:
            status = await self.client.get_download_status(watch["info_hash"])
        except Exception as exc:
            if _download_was_removed(exc):
                await self._handle_removed_watch(watch, exc)
                return

            updated_watch = self.store.mark_error(
                info_hash=watch["info_hash"],
                notification_target=watch["notification_target"],
                error=str(exc),
            )
            if updated_watch and _error_count(updated_watch) >= self.max_errors:
                self.store.mark_abandoned(
                    info_hash=watch["info_hash"],
                    notification_target=watch["notification_target"],
                    error=str(exc),
                )
                with contextlib.suppress(Exception):
                    await self.send_message(
                        watch["notification_target"],
                        _abandoned_message(updated_watch),
                    )
            return

        completed = _download_complete(status)
        if not completed:
            try:
                await self.publish_progress_snapshot(watch, status)
            except Exception as exc:
                self.store.mark_error(
                    info_hash=watch["info_hash"],
                    notification_target=watch["notification_target"],
                    error="progress update failed: %s" % exc,
                )
            return

        try:
            await self.publish_progress_snapshot(watch, status)
        except Exception as exc:
            self.store.mark_error(
                info_hash=watch["info_hash"],
                notification_target=watch["notification_target"],
                error="progress update failed: %s" % exc,
            )

        if not watch.get("completion_notified_at"):
            try:
                await self.send_message(watch["notification_target"], _completion_message(watch, status))
            except Exception as exc:
                self._mark_error_safely(watch, "completion notification failed", exc)
                return
            self.store.mark_completion_notified(
                info_hash=watch["info_hash"],
                notification_target=watch["notification_target"],
            )

        if self.completion_hook is not None:
            try:
                await self.completion_hook(_completion_event(watch, status))
            except Exception as exc:
                updated_watch = self.store.mark_error(
                    info_hash=watch["info_hash"],
                    notification_target=watch["notification_target"],
                    error="completion hook failed: %s" % exc,
                )
                if updated_watch and _error_count(updated_watch) >= self.max_errors:
                    self.store.mark_abandoned(
                        info_hash=watch["info_hash"],
                        notification_target=watch["notification_target"],
                        error=str(exc),
                    )
                    with contextlib.suppress(Exception):
                        await self.send_message(
                            watch["notification_target"],
                            _completion_hook_failed_message(updated_watch, status, exc),
                        )
                return

        self.store.mark_notified(
            info_hash=watch["info_hash"],
            notification_target=watch["notification_target"],
        )

    async def _handle_removed_watch(self, watch: dict[str, Any], exc: Exception) -> None:
        if not watch.get("removal_notified_at"):
            try:
                await self.send_message(
                    watch["notification_target"],
                    _removed_message(watch),
                )
            except Exception as send_exc:
                self._mark_error_safely(watch, "removal notification failed", send_exc)
                return
            self.store.mark_removal_notified(
                info_hash=watch["info_hash"],
                notification_target=watch["notification_target"],
            )

        if self.completion_hook is not None:
            try:
                await self.completion_hook(_removed_event(watch, exc))
            except Exception as hook_exc:
                updated_watch = self.store.mark_error(
                    info_hash=watch["info_hash"],
                    notification_target=watch["notification_target"],
                    error="removal hook failed: %s" % hook_exc,
                )
                if updated_watch and _error_count(updated_watch) >= self.max_errors:
                    self.store.mark_abandoned(
                        info_hash=watch["info_hash"],
                        notification_target=watch["notification_target"],
                        error=str(hook_exc),
                    )
                return

        self.store.mark_abandoned(
            info_hash=watch["info_hash"],
            notification_target=watch["notification_target"],
            error=str(exc),
        )

    def _mark_error_safely(self, watch: dict[str, Any], prefix: str, exc: Exception) -> None:
        try:
            updated_watch = self.store.mark_error(
                info_hash=watch["info_hash"],
                notification_target=watch["notification_target"],
                error=f"{prefix}: {exc}",
            )
            if updated_watch and _error_count(updated_watch) >= self.max_errors:
                self.store.mark_abandoned(
                    info_hash=watch["info_hash"],
                    notification_target=watch["notification_target"],
                    error=str(exc),
                )
        except Exception:
            logger.exception(
                "Could not record notification watch error for info_hash=%s target=%s",
                watch.get("info_hash"),
                watch.get("notification_target"),
            )

    async def publish_progress_snapshot(self, watch: dict[str, Any], status: dict[str, Any]) -> None:
        tracking = _progress_tracking(watch)
        if not tracking or not tracking.get("enabled"):
            return
        if tracking.get("completed_at"):
            return

        completed = _download_complete(status)
        message_id = _tracking_string(tracking, "message_id")
        if completed and not message_id:
            return
        buttons: list[dict[str, str]] = []

        expired = False
        if completed:
            rendered = render_download_status_payload(status)
            message = rendered["message"]
            buttons = rendered["buttons"]
        elif _progress_tracking_expired(tracking):
            if tracking.get("expired_at"):
                return
            message = render_tracking_expired_status(status, timeout_message=_progress_timeout_message(watch))
            buttons = render_download_status_payload(status)["buttons"]
            expired = True
        else:
            rendered = render_download_status_payload(status)
            message = rendered["message"]
            buttons = rendered["buttons"]

        if message == tracking.get("last_message") and not completed and not expired:
            return

        new_message_id = await _call_send_status_message(
            self.send_status_message,
            watch["notification_target"],
            _tracking_string(tracking, "status_key") or _progress_status_key(watch["info_hash"]),
            message,
            message_id,
            buttons,
        )
        self.store.mark_progress_published(
            info_hash=watch["info_hash"],
            notification_target=watch["notification_target"],
            message=message,
            message_id=new_message_id or message_id,
            completed=completed,
            expired=expired,
        )

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Download notification poll failed")
            await asyncio.sleep(self.poll_interval_seconds)


def default_watch_store_path() -> Path:
    configured = (acquisition_env_first("QBITLARR_NOTIFICATION_WATCHES_PATH", default="") or "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg_data_home = os.getenv("XDG_DATA_HOME", "").strip()
    data_home = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    default_path = data_home / "mpilot" / "acquisition" / "download-notification-watches.json"
    legacy_path = data_home / "qbitlarr" / "download-notification-watches.json"
    if legacy_path.exists() and not default_path.exists():
        return legacy_path
    return default_path


async def _send_hermes_message(target: str, message: str) -> None:
    hermes_bin, hermes_profile, timeout_seconds = hermes_send_config(
        bin_env_names=("MPILOT_HERMES_BIN", "MPILOT_ACQUISITION_HERMES_BIN", "QBITLARR_HERMES_BIN"),
        profile_env_names=("MPILOT_HERMES_PROFILE", "MPILOT_ACQUISITION_HERMES_PROFILE", "QBITLARR_HERMES_PROFILE"),
        timeout_env_names=(
            "MPILOT_HERMES_SEND_TIMEOUT_SECONDS",
            "MPILOT_ACQUISITION_HERMES_SEND_TIMEOUT_SECONDS",
            "QBITLARR_HERMES_SEND_TIMEOUT_SECONDS",
        ),
        default_timeout=DEFAULT_HERMES_SEND_TIMEOUT_SECONDS,
    )
    command = hermes_send_command(
        target=target,
        message=message,
        bin_name=hermes_bin,
        profile=hermes_profile,
    )
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate = getattr(proc, "communicate", None)
    operation = communicate() if callable(communicate) else proc.wait()
    operation_task = asyncio.create_task(operation)
    try:
        result = await asyncio.wait_for(operation_task, timeout=timeout_seconds)
        if callable(communicate):
            stdout, stderr = result
            return_code = proc.returncode
        else:
            stdout = b""
            stderr = b""
            return_code = result
    except TimeoutError as exc:
        operation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await operation_task
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise RuntimeError(f"hermes send timed out after {timeout_seconds:g} seconds") from exc
    if return_code != 0:
        detail = decode_process_output(stderr or stdout)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"hermes send failed with exit code {return_code}{suffix}")


send_hermes_message = _send_hermes_message


def _log_task_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Download notification task stopped unexpectedly")


async def _call_send_status_message(
    send_status_message: SendStatusMessage,
    target: str,
    status_key: str,
    message: str,
    message_id: str | None,
    buttons: list[dict[str, str]] | None,
) -> str | None:
    if not _send_status_message_accepts_buttons(send_status_message):
        return await send_status_message(target, status_key, message, message_id)  # type: ignore[misc]
    return await send_status_message(target, status_key, message, message_id, buttons)


def _send_status_message_accepts_buttons(send_status_message: SendStatusMessage) -> bool:
    try:
        signature = inspect.signature(send_status_message)
    except (TypeError, ValueError):
        return True
    positional_count = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1
    return positional_count >= 5


async def send_status_message_from_env(
    target: str,
    status_key: str,
    message: str,
    message_id: str | None = None,
    buttons: list[dict[str, str]] | None = None,
) -> str | None:
    token = _telegram_bot_token()
    telegram_target = parse_telegram_target(target)
    if token and telegram_target:
        return await _send_or_edit_telegram_status_message(
            token=token,
            chat_id=telegram_target[0],
            thread_id=telegram_target[1],
            message=message,
            message_id=message_id,
            buttons=buttons,
        )

    await send_hermes_message(target, message)
    return None


def completion_hook_from_env() -> CompletionHook | None:
    if (acquisition_env_first("QBITLARR_COMPLETION_HOOK_COMMAND", default="") or "").strip():
        return run_completion_hook_from_env
    return None


async def run_completion_hook_from_env(event: dict[str, Any]) -> None:
    command_value = (acquisition_env_first("QBITLARR_COMPLETION_HOOK_COMMAND", default="") or "").strip()
    if not command_value:
        return
    command = shlex.split(command_value)
    if not command:
        return
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
    cwd = (acquisition_env_first("QBITLARR_COMPLETION_HOOK_CWD", default="") or "").strip() or None
    env = _completion_hook_env()
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    proc = await asyncio.create_subprocess_exec(
        *command,
        **kwargs,
    )
    stdout, stderr = await proc.communicate(payload)
    if proc.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        suffix = ": %s" % detail if detail else ""
        raise RuntimeError("completion hook failed with exit code %s%s" % (proc.returncode, suffix))


def _completion_hook_env() -> dict[str, str] | None:
    pythonpath = (acquisition_env_first("QBITLARR_COMPLETION_HOOK_PYTHONPATH", default="") or "").strip()
    if not pythonpath:
        return None
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = pythonpath if not existing else pythonpath + os.pathsep + existing
    return env


async def _send_or_edit_telegram_status_message(
    *,
    token: str,
    chat_id: str,
    thread_id: str | None,
    message: str,
    message_id: str | None,
    buttons: list[dict[str, str]] | None = None,
) -> str | None:
    reply_markup = _telegram_inline_keyboard(buttons)
    return await async_send_or_edit_telegram_status_message(
        token=token,
        chat_id=chat_id,
        thread_id=thread_id,
        message=message,
        message_id=message_id,
        buttons=buttons,
        reply_markup=reply_markup,
        api_post=_telegram_api_post,
        include_message_id_invalid=True,
        return_existing_on_unreplaceable_edit_error=True,
    )


def _telegram_inline_keyboard(buttons: list[dict[str, str]] | None) -> dict[str, Any] | None:
    if not buttons:
        return None
    row = []
    for button in buttons:
        text = str(button.get("text") or "").strip()
        callback_data = str(button.get("callback_data") or "").strip()
        if not text or not callback_data.startswith("dl:") or len(callback_data.encode("utf-8")) > 64:
            continue
        row.append({"text": text, "callback_data": callback_data})
    if not row:
        return None
    return {"inline_keyboard": [row]}


async def _telegram_api_post(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await async_telegram_api_post(client, token, method, payload)


def _telegram_message_id(data: dict[str, Any]) -> str | None:
    return telegram_message_id(data)


def _coerce_telegram_int(value: str) -> int | str:
    return coerce_telegram_int(value)


def _download_complete(status: dict[str, Any]) -> bool:
    try:
        progress = float(status.get("progress", 0.0))
    except (TypeError, ValueError):
        progress = 0.0
    return progress >= 1.0 or str(status.get("state", "")) in COMPLETE_STATES


def _completion_event(watch: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
    content_path = _status_string(status, "content_path") or _metadata_string(watch, "content_path")
    event: dict[str, Any] = {
        "event": "download_complete",
        "info_hash": watch["info_hash"],
        "title": str(watch.get("title") or status.get("name") or watch["info_hash"]).strip(),
        "notification_target": watch["notification_target"],
        "requester_id": watch.get("requester_id"),
        "metadata": dict(metadata),
        "download_status": status,
    }
    imdb_id = _metadata_string(watch, "imdb_id")
    media_type = _metadata_string(watch, "media_type")
    if imdb_id:
        event["imdb_id"] = imdb_id
    if media_type:
        event["media_type"] = media_type
    if content_path:
        event["content_path"] = content_path
    return event


def _removed_event(watch: dict[str, Any], error: Exception) -> dict[str, Any]:
    metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
    return {
        "event": "download_removed",
        "info_hash": watch["info_hash"],
        "title": str(watch.get("title") or watch["info_hash"]).strip(),
        "notification_target": watch["notification_target"],
        "requester_id": watch.get("requester_id"),
        "metadata": dict(metadata),
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }


def _new_progress_tracking(info_hash: str, *, now: str) -> dict[str, Any]:
    policy = dynamic_progress_watch_policy()
    started_at = _parse_time(now) or datetime.now(UTC)
    expires_at = started_at + timedelta(seconds=int(policy["max_duration_seconds"]))
    return {
        "enabled": True,
        "status_key": _progress_status_key(info_hash),
        "started_at": now,
        "updated_at": now,
        "expires_at": expires_at.isoformat(),
        "message_id": None,
        "last_message": None,
        "completed_at": None,
        "expired_at": None,
    }


def _progress_tracking(watch: dict[str, Any]) -> dict[str, Any] | None:
    tracking = watch.get("progress_tracking")
    return dict(tracking) if isinstance(tracking, dict) else None


def _progress_tracking_expired(tracking: dict[str, Any]) -> bool:
    expires_at = _parse_time(_tracking_string(tracking, "expires_at"))
    return expires_at is not None and datetime.now(UTC) >= expires_at


def _progress_timeout_message(watch: dict[str, Any]) -> str | None:
    metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
    value = metadata.get("progress_timeout_message") or acquisition_env_first("QBITLARR_PROGRESS_TIMEOUT_MESSAGE")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _progress_status_key(info_hash: str) -> str:
    return "qbitlarr-download:%s" % _normalize_hash(info_hash)


def _telegram_bot_token() -> str | None:
    return telegram_bot_token(
        "MPILOT_TELEGRAM_BOT_TOKEN",
        "MPILOT_ACQUISITION_TELEGRAM_BOT_TOKEN",
        "QBITLARR_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        hermes_env_path_names=("MPILOT_HERMES_ENV_PATH", "MPILOT_ACQUISITION_HERMES_ENV_PATH", "QBITLARR_HERMES_ENV_PATH"),
    )


def _tracking_string(tracking: dict[str, Any], key: str) -> str | None:
    value = tracking.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _completion_message(watch: dict[str, Any], status: dict[str, Any]) -> str:
    title = str(watch.get("title") or status.get("name") or watch["info_hash"]).strip()
    imdb_id = _metadata_string(watch, "imdb_id")
    followup_message = _metadata_string(watch, "completion_followup_message")
    title_part = f"{title} ({imdb_id})" if imdb_id else title
    lines = [f"Download complete: {title_part}"]
    if followup_message:
        lines.append(followup_message)
    return "\n".join(lines)


def _status_string(status: dict[str, Any], key: str) -> str | None:
    value = status.get(key)
    return str(value).strip() if value and str(value).strip() else None


def _abandoned_message(watch: dict[str, Any]) -> str:
    title = str(watch.get("title") or watch["info_hash"]).strip()
    imdb_id = _metadata_string(watch, "imdb_id")
    suffix = f" ({imdb_id})" if imdb_id else ""
    return f"Download tracking stopped: {title}{suffix}\nI couldn't verify this torrent status after repeated attempts."


def _completion_hook_failed_message(watch: dict[str, Any], status: dict[str, Any], error: Exception) -> str:
    title = str(watch.get("title") or status.get("name") or watch["info_hash"]).strip()
    imdb_id = _metadata_string(watch, "imdb_id")
    suffix = f" ({imdb_id})" if imdb_id else ""
    return (
        f"Download complete: {title}{suffix}\n"
        "I couldn't start the follow-up workflow after repeated attempts.\n"
        f"Last error: {error}"
    )


def _removed_message(watch: dict[str, Any]) -> str:
    title = str(watch.get("title") or watch["info_hash"]).strip()
    imdb_id = _metadata_string(watch, "imdb_id")
    suffix = f" ({imdb_id})" if imdb_id else ""
    return (
        f"Download task removed: {title}{suffix}\n"
        "It was deleted from qBittorrent before it finished, possibly by an administrator."
    )


def _download_was_removed(error: Exception) -> bool:
    return isinstance(error, QbitlarrApiError) and error.status_code == 404


def _normalize_hash(info_hash: str) -> str:
    normalized = info_hash.strip().casefold()
    if not normalized:
        raise ValueError("info_hash must not be empty")
    return normalized


def _normalize_target(notification_target: str) -> str:
    target = notification_target.strip()
    if not target:
        raise ValueError("notification_target must not be empty")
    if "\n" in target or "\r" in target:
        raise ValueError("notification_target must be one line")
    return target


def _normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key = key.strip()
        value = value.strip()
        if key and value:
            normalized[key] = value
    return normalized


def _merge_metadata(existing: Any, metadata: dict[str, Any] | None) -> dict[str, str]:
    merged = _normalize_metadata(existing if isinstance(existing, dict) else None)
    merged.update(_normalize_metadata(metadata))
    return merged


def _metadata_string(watch: dict[str, Any], key: str) -> str | None:
    metadata = watch.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _error_count(watch: dict[str, Any]) -> int:
    try:
        return int(watch.get("error_count") or 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(UTC).isoformat()
