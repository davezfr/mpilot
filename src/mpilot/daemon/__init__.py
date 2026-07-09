from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from mpilot.mcp.qbitlarr_notifications import DownloadCompletionNotifier
from mpilot.runtime import MediaWorkflowRuntime
from mpilot.runtime.cli import default_runtime_store_dir
from mpilot.runtime.dispatcher import dispatch_ready_babelarr_actions
from mpilot.subtitles.jobs import default_job_store_dir
from mpilot.subtitles.notifications import run_notification_daemon


logger = logging.getLogger("mpilot-daemon")


StepResult = dict[str, Any]


def default_daemon_lock_path() -> Path:
    configured = os.environ.get("MPILOT_DAEMON_LOCK_PATH")
    if configured and configured.strip():
        return Path(configured).expanduser()
    data_home = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_DATA_HOME")
    if data_home and data_home.strip():
        return Path(data_home).expanduser() / "mpilot" / "mpilot-daemon.lock"
    return Path.home() / ".local" / "share" / "mpilot" / "mpilot-daemon.lock"


def acquire_daemon_lock(lock_path: str | Path | None = None):
    path = Path(lock_path).expanduser() if lock_path is not None else default_daemon_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def release_daemon_lock(handle: Any) -> None:
    with contextlib.suppress(Exception):
        fcntl.flock(handle, fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()


async def run_daemon_once(
    *,
    qbitlarr_notifier: DownloadCompletionNotifier | None = None,
    run_qbitlarr: bool = True,
    run_babelarr_notifications_step: bool = True,
    run_runtime_dispatch: bool = True,
    runtime_store_dir: str | Path | None = None,
    job_store_dir: str | Path | None = None,
    runtime: MediaWorkflowRuntime | None = None,
    runtime_dispatcher: Callable[..., dict[str, Any]] | None = None,
    babelarr_notification_step: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps: list[StepResult] = []

    if run_qbitlarr:
        notifier = qbitlarr_notifier or DownloadCompletionNotifier.from_env()
        steps.append(await _run_async_step("qbitlarr_downloads", notifier.poll_once))

    if run_babelarr_notifications_step:
        step = babelarr_notification_step or _default_babelarr_notification_step
        steps.append(await _run_sync_step("babelarr_notifications", step))

    if run_runtime_dispatch:
        active_runtime = runtime or MediaWorkflowRuntime(
            Path(runtime_store_dir).expanduser() if runtime_store_dir is not None else default_runtime_store_dir()
        )
        dispatcher = runtime_dispatcher or dispatch_ready_babelarr_actions
        active_job_store = str(Path(job_store_dir).expanduser()) if job_store_dir is not None else str(default_job_store_dir())
        steps.append(
            await _run_sync_step(
                "runtime_dispatch",
                lambda: dispatcher(active_runtime, job_store_dir=active_job_store),
            )
        )

    return _summary_from_steps(steps)


def run_daemon(
    *,
    once: bool = False,
    lock_path: str | Path | None = None,
    interval_seconds: float = 5.0,
    **kwargs: Any,
) -> dict[str, Any]:
    path = Path(lock_path).expanduser() if lock_path is not None else default_daemon_lock_path()
    lock_handle = acquire_daemon_lock(path)
    if lock_handle is None:
        return {"status": "already_running", "lock_path": str(path)}

    try:
        if once:
            summary = asyncio.run(run_daemon_once(**kwargs))
            return {"lock_path": str(path), **summary}

        cycles = 0
        while True:
            cycles += 1
            summary = asyncio.run(run_daemon_once(**kwargs))
            if summary.get("status") not in {"ok", "partial_failure"}:
                logger.warning("MPilot daemon cycle returned %s", summary)
            time.sleep(max(0.1, interval_seconds))
    finally:
        release_daemon_lock(lock_handle)


async def _run_async_step(name: str, callback: Callable[[], Awaitable[Any]]) -> StepResult:
    try:
        result = await callback()
    except Exception as error:
        logger.exception("MPilot daemon step failed: %s", name)
        return _error_step(name, error)
    return {"name": name, "status": "ok", "result": result}


async def _run_sync_step(name: str, callback: Callable[[], Any]) -> StepResult:
    try:
        result = callback()
    except Exception as error:
        logger.exception("MPilot daemon step failed: %s", name)
        return _error_step(name, error)
    return {"name": name, "status": "ok", "result": result}


def _default_babelarr_notification_step() -> dict[str, Any]:
    return run_notification_daemon(run_once=True, lock_acquire_timeout_seconds=0.0)


def _summary_from_steps(steps: list[StepResult]) -> dict[str, Any]:
    failed = [step for step in steps if step.get("status") != "ok"]
    return {
        "status": "partial_failure" if failed else "ok",
        "steps": steps,
        "errors": failed,
    }


def _error_step(name: str, error: Exception) -> StepResult:
    return {
        "name": name,
        "status": "error",
        "error": {"type": type(error).__name__, "message": str(error)},
    }
