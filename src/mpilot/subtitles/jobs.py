from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union


JOB_SCHEMA_VERSION = 1
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "needs_confirmation"}
RECOVERABLE_STATUSES = {"queued", "failed"}
DEFAULT_PRUNABLE_STATUSES = ("succeeded",)


class JobStoreError(RuntimeError):
    pass


class JobNeedsConfirmation(RuntimeError):
    def __init__(self, proposal: Mapping[str, Any]):
        super().__init__("job needs user confirmation")
        self.proposal = dict(proposal)


def default_job_store_dir(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    environment = env if env is not None else os.environ
    base_home = home if home is not None else Path.home()
    configured = (
        environment.get("MPILOT_SUBTITLE_JOB_STORE_DIR")
        or environment.get("BABELARR_JOB_STORE_DIR")
        or environment.get("MST_JOB_STORE_DIR")
    )
    if configured:
        if configured == "~":
            return base_home
        if configured.startswith("~/"):
            return base_home / configured[2:]
        return Path(configured).expanduser()
    default_path = base_home / ".local" / "share" / "mpilot" / "subtitles" / "jobs"
    babelarr_path = base_home / ".local" / "share" / "babelarr" / "jobs"
    legacy_dir = "media-" + "subtitle-translator"
    legacy_path = base_home / ".local" / "share" / legacy_dir / "jobs"
    if babelarr_path.exists() and not default_path.exists():
        return babelarr_path
    if legacy_path.exists() and not default_path.exists():
        return legacy_path
    return default_path


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    def create(self, job_type: str, request: Mapping[str, Any], now: Optional[Union[str, datetime]] = None) -> Dict[str, Any]:
        timestamp = _timestamp(now)
        job = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": _new_job_id(),
            "job_type": job_type,
            "status": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "attempts": [],
            "request": _json_copy(request),
            "result": None,
            "last_error": None,
            "needs_confirmation": None,
            "progress": None,
            "progress_events": [],
            "progress_milestones": {},
        }
        self.save(job)
        return job

    def get(self, job_id: str) -> Dict[str, Any]:
        path = self._path(job_id)
        if not path.exists():
            raise JobStoreError("job not found: %s" % job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, job: Mapping[str, Any]) -> Dict[str, Any]:
        job_id = str(job.get("job_id") or "")
        path = self._path(job_id)
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return dict(job)

    def list(self, status: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if status and status not in JOB_STATUSES:
            raise JobStoreError("unknown job status: %s" % status)
        if not self.root.exists():
            return []
        jobs = []
        for path in self.root.glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if status and job.get("status") != status:
                continue
            jobs.append(job)
        jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
        if limit is not None:
            jobs = jobs[:limit]
        return jobs

    def mark_running(self, job_id: str, now: Optional[Union[str, datetime]] = None) -> Dict[str, Any]:
        job = self.get(job_id)
        timestamp = _timestamp(now)
        attempt = {
            "attempt": len(job.get("attempts") or []) + 1,
            "status": "running",
            "started_at": timestamp,
            "finished_at": None,
        }
        job["status"] = "running"
        job["started_at"] = timestamp
        job["updated_at"] = timestamp
        job["last_error"] = None
        job["needs_confirmation"] = None
        progress = _progress_payload(
            {"stage": "started", "message": "Subtitle job started."},
            timestamp,
        )
        job["progress"] = progress
        job.setdefault("progress_events", []).append(progress)
        job["progress_events"] = job["progress_events"][-100:]
        job.setdefault("progress_milestones", {})["started"] = progress
        attempt["progress"] = progress
        job.setdefault("attempts", []).append(attempt)
        return self.save(job)

    def mark_progress(
        self,
        job_id: str,
        progress: Mapping[str, Any],
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        timestamp = _timestamp(now)
        payload = _progress_payload(progress, timestamp)
        job["progress"] = payload
        events = job.setdefault("progress_events", [])
        events.append(payload)
        job["progress_events"] = events[-100:]
        milestones = job.setdefault("progress_milestones", {})
        milestones[payload["stage"]] = payload
        job["updated_at"] = timestamp
        attempts = job.setdefault("attempts", [])
        if attempts:
            attempts[-1]["progress"] = payload
        return self.save(job)

    def mark_succeeded(
        self,
        job_id: str,
        result: Mapping[str, Any],
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        timestamp = _timestamp(now)
        job["status"] = "succeeded"
        job["updated_at"] = timestamp
        job["finished_at"] = timestamp
        job["result"] = _json_copy(result)
        job["last_error"] = None
        job["needs_confirmation"] = None
        progress = _progress_payload(
            {
                "stage": "succeeded",
                "message": "Subtitle job completed.",
                "details": {"output": result.get("output")},
            },
            timestamp,
        )
        job["progress"] = progress
        job.setdefault("progress_events", []).append(progress)
        job["progress_events"] = job["progress_events"][-100:]
        job.setdefault("progress_milestones", {})["succeeded"] = progress
        _finish_latest_attempt(job, "succeeded", timestamp, result=_json_copy(result))
        return self.save(job)

    def mark_failed(
        self,
        job_id: str,
        error: BaseException,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        timestamp = _timestamp(now)
        last_error = {
            "type": error.__class__.__name__,
            "message": str(error),
        }
        job["status"] = "failed"
        job["updated_at"] = timestamp
        job["finished_at"] = timestamp
        job["last_error"] = last_error
        progress = _progress_payload(
            {
                "stage": "failed",
                "message": "Subtitle job failed.",
                "details": last_error,
            },
            timestamp,
        )
        job["progress"] = progress
        job.setdefault("progress_events", []).append(progress)
        job["progress_events"] = job["progress_events"][-100:]
        job.setdefault("progress_milestones", {})["failed"] = progress
        _finish_latest_attempt(job, "failed", timestamp, error=last_error)
        return self.save(job)

    def mark_needs_confirmation(
        self,
        job_id: str,
        proposal: Mapping[str, Any],
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        timestamp = _timestamp(now)
        job["status"] = "needs_confirmation"
        job["updated_at"] = timestamp
        job["finished_at"] = timestamp
        job["needs_confirmation"] = _json_copy(proposal)
        progress = _progress_payload(
            {
                "stage": "needs_confirmation",
                "message": "Subtitle job needs user confirmation.",
                "details": _json_copy(proposal),
            },
            timestamp,
        )
        job["progress"] = progress
        job.setdefault("progress_events", []).append(progress)
        job["progress_events"] = job["progress_events"][-100:]
        job.setdefault("progress_milestones", {})["needs_confirmation"] = progress
        _finish_latest_attempt(job, "needs_confirmation", timestamp, proposal=_json_copy(proposal))
        return self.save(job)

    def confirm_low_confidence(
        self,
        job_id: str,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        _ensure_job_can_confirm_low_confidence(job)
        timestamp = _timestamp(now)
        request = job.setdefault("request", {})
        provider = request.setdefault("provider", {})
        provider["allow_low_confidence_subtitle"] = True
        job["low_confidence_confirmed_at"] = timestamp
        job["updated_at"] = timestamp
        return self.save(job)

    def confirm_provider_fallback_language(
        self,
        job_id: str,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        job = self.get(job_id)
        _ensure_job_can_confirm_provider_fallback_language(job)
        timestamp = _timestamp(now)
        request = job.setdefault("request", {})
        provider = request.setdefault("provider", {})
        provider["allow_provider_fallback_language"] = True
        job["provider_fallback_language_confirmed_at"] = timestamp
        job["updated_at"] = timestamp
        return self.save(job)

    def recoverable_jobs(self, now: datetime, stale_after: timedelta) -> List[Dict[str, Any]]:
        recoverable = []
        for job in self.list():
            status = job.get("status")
            if status in RECOVERABLE_STATUSES:
                recoverable.append(job)
                continue
            if status == "running" and _is_stale(job, now, stale_after):
                recoverable.append(job)
        recoverable.sort(key=lambda job: str(job.get("created_at") or ""))
        return recoverable

    def prune(
        self,
        now: datetime,
        retention: timedelta,
        statuses: Iterable[str] = DEFAULT_PRUNABLE_STATUSES,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        if retention.total_seconds() < 0:
            raise JobStoreError("retention must be non-negative")
        allowed_statuses = tuple(statuses)
        unknown_statuses = [status for status in allowed_statuses if status not in JOB_STATUSES]
        if unknown_statuses:
            raise JobStoreError("unknown job status: %s" % ", ".join(unknown_statuses))
        cutoff = now.astimezone(timezone.utc) - retention
        candidates = []
        for job in self.list():
            if job.get("status") not in allowed_statuses:
                continue
            job_timestamp = _job_prune_timestamp(job)
            if job_timestamp >= cutoff:
                continue
            pruned_job = {
                "job_id": job["job_id"],
                "status": job.get("status"),
                "timestamp": _timestamp(job_timestamp),
                "path": str(self._path(job["job_id"])),
            }
            candidates.append(pruned_job)
            if not dry_run:
                self._path(job["job_id"]).unlink(missing_ok=True)
                self._lock_path(job["job_id"]).unlink(missing_ok=True)
        candidates.sort(key=lambda job: job["timestamp"])
        temp_paths = self._old_temp_paths(cutoff)
        if not dry_run:
            for path in temp_paths:
                path.unlink(missing_ok=True)
        return {
            "dry_run": dry_run,
            "retention": {
                "seconds": int(retention.total_seconds()),
                "cutoff": _timestamp(cutoff),
                "statuses": list(allowed_statuses),
            },
            "count": len(candidates),
            "jobs": candidates,
        }

    def _old_temp_paths(self, cutoff: datetime) -> List[Path]:
        paths = []
        for path in self.root.glob("*.tmp"):
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified_at < cutoff:
                paths.append(path)
        return paths

    def _path(self, job_id: str) -> Path:
        if not re.match(r"^job_[A-Za-z0-9_.-]+$", job_id):
            raise JobStoreError("invalid job_id: %s" % job_id)
        return self.root / ("%s.json" % job_id)

    def _lock_path(self, job_id: str) -> Path:
        return self._path(job_id).with_suffix(".json.lock")

    @contextlib.contextmanager
    def lock(self, job_id: str):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(job_id)
        handle = lock_path.open("a", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise JobStoreError("job is already locked by another worker: %s" % job_id) from error
                raise
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def run_job(
    store: JobStore,
    job_id: str,
    executor: Callable[[Dict[str, Any]], Mapping[str, Any]],
    now: Optional[Union[str, datetime]] = None,
    allow_needs_confirmation: bool = False,
    allow_running: bool = False,
) -> Dict[str, Any]:
    with store.lock(job_id):
        _ensure_job_can_run(
            store.get(job_id),
            allow_needs_confirmation=allow_needs_confirmation,
            allow_running=allow_running,
        )
        store.mark_running(job_id, now=now)
        try:
            result = executor(store.get(job_id))
        except JobNeedsConfirmation as error:
            return store.mark_needs_confirmation(job_id, error.proposal, now=now)
        except Exception as error:
            return store.mark_failed(job_id, error, now=now)
        return store.mark_succeeded(job_id, result, now=now)


def _ensure_job_can_run(
    job: Mapping[str, Any],
    *,
    allow_needs_confirmation: bool,
    allow_running: bool,
) -> None:
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "")
    if status == "succeeded":
        raise JobStoreError("job already succeeded: %s" % job_id)
    if status == "running" and not allow_running:
        raise JobStoreError("job already running: %s" % job_id)
    if status == "needs_confirmation" and not allow_needs_confirmation:
        raise JobStoreError("job needs confirmation before retry: %s" % job_id)
    if status not in {"queued", "failed", "running", "needs_confirmation"}:
        raise JobStoreError("job cannot run from status %s: %s" % (status, job_id))


def _ensure_job_can_confirm_low_confidence(job: Mapping[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "")
    if status == "succeeded":
        raise JobStoreError("job already succeeded: %s" % job_id)
    if status == "running":
        raise JobStoreError("job is already running; retry with job-resume when stale: %s" % job_id)


def _ensure_job_can_confirm_provider_fallback_language(job: Mapping[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    status = str(job.get("status") or "")
    if status == "succeeded":
        raise JobStoreError("job already succeeded: %s" % job_id)
    if status == "running":
        raise JobStoreError("job is already running; retry with job-resume when stale: %s" % job_id)


def _timestamp(value: Optional[Union[str, datetime]] = None) -> str:
    if isinstance(value, str):
        return value
    instant = value if value is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _new_job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "job_%s_%s" % (timestamp, uuid.uuid4().hex[:8])


def _json_copy(value: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _progress_payload(progress: Mapping[str, Any], timestamp: str) -> Dict[str, Any]:
    stage = str(progress.get("stage") or "working").strip() or "working"
    message = str(progress.get("message") or stage).strip() or stage
    details = progress.get("details")
    if not isinstance(details, Mapping):
        details = {}
    return {
        "stage": stage,
        "message": message,
        "updated_at": timestamp,
        "details": _json_copy(details),
    }


def _finish_latest_attempt(
    job: Dict[str, Any],
    status: str,
    timestamp: str,
    *,
    result: Optional[Mapping[str, Any]] = None,
    error: Optional[Mapping[str, Any]] = None,
    proposal: Optional[Mapping[str, Any]] = None,
) -> None:
    attempts = job.setdefault("attempts", [])
    if not attempts:
        attempts.append({"attempt": 1, "started_at": timestamp})
    attempt = attempts[-1]
    attempt["status"] = status
    attempt["finished_at"] = timestamp
    if result is not None:
        attempt["result"] = dict(result)
    if error is not None:
        attempt["error"] = dict(error)
    if proposal is not None:
        attempt["needs_confirmation"] = dict(proposal)


def _is_stale(job: Mapping[str, Any], now: datetime, stale_after: timedelta) -> bool:
    timestamp = job.get("started_at") or job.get("updated_at")
    if not timestamp:
        return True
    return now.astimezone(timezone.utc) - _parse_timestamp(str(timestamp)) >= stale_after


def _job_prune_timestamp(job: Mapping[str, Any]) -> datetime:
    timestamp = job.get("finished_at") or job.get("updated_at") or job.get("created_at")
    if not timestamp:
        return datetime.now(timezone.utc)
    return _parse_timestamp(str(timestamp))
