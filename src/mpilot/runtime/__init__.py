from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union


WORKFLOW_SCHEMA_VERSION = 1
ACTIVE_SUBTITLE_STATUSES = {"dispatching", "queued", "running"}
OPEN_SUBTITLE_STATUSES = {
    "waiting_for_dependency",
    "ready",
    "dispatching",
    "queued",
    "running",
    "needs_confirmation",
}


class RuntimeStoreError(RuntimeError):
    pass


class QBitlarrHashNotTrackedError(RuntimeStoreError):
    """Raised when a qBitlarr completion event references an info_hash with no tracked workflow."""

    def __init__(self, info_hash: str):
        super().__init__("download task not found for qBitlarr hash: %s" % info_hash)
        self.info_hash = info_hash


def _locked_method(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._store_lock():
            return method(self, *args, **kwargs)

    return wrapper


class MediaWorkflowRuntime:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle = None

    @_locked_method
    def record_qbitlarr_download(
        self,
        *,
        requester_id: str,
        info_hash: str,
        title: Optional[str] = None,
        imdb_id: Optional[str] = None,
        media_type: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        progress: float = 0.0,
        content_path: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        current = self._find_by_qbitlarr_hash(info_hash)
        timestamp = _timestamp(now)
        completed_content_path = _completed_content_path(progress, content_path)
        if current is not None:
            workflow = current
            workflow["updated_at"] = timestamp
            workflow["requester_id"] = requester_id
            media = workflow.setdefault("media", {})
            _set_if_value(media, "title", title)
            _set_if_value(media, "imdb_id", imdb_id)
            _set_if_value(media, "media_type", media_type)
            _set_if_value(media, "season", season)
            _set_if_value(media, "episode", episode)
            download_task = _download_task(workflow)
            qbitlarr = download_task.setdefault("qbitlarr", {})
            previous_download_status = str(download_task.get("status") or "")
            previous_download_updated_at = str(download_task.get("updated_at") or "")
            previously_complete = (
                previous_download_status == "succeeded"
                or _video_path(workflow) is not None
                or _string_value(qbitlarr.get("content_path")) is not None
            )
            download_task["updated_at"] = timestamp
            download_task["status"] = "succeeded" if completed_content_path else "running"
            qbitlarr["info_hash"] = _required(info_hash, "info_hash")
            qbitlarr["progress"] = float(progress)
            if completed_content_path:
                qbitlarr["content_path"] = completed_content_path
                workflow.setdefault("artifacts", {})["video_path"] = completed_content_path
                if _subtitle_tasks_need_replacement_for_completion(
                    workflow,
                    previous_download_status=previous_download_status,
                    previous_download_updated_at=previous_download_updated_at,
                ):
                    _replace_non_waiting_subtitle_tasks(workflow, timestamp)
                _release_waiting_subtitle_tasks(workflow, timestamp)
            else:
                qbitlarr.pop("content_path", None)
                _clear_video_path(workflow)
                if previously_complete:
                    _remove_subtitle_tasks(workflow)
            workflow["status"] = _workflow_status(workflow)
            return self._save(workflow)

        workflow_id = _new_id("workflow")
        task_id = _new_id("task")
        workflow = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "status": "running",
            "requester_id": _required(requester_id, "requester_id"),
            "created_at": timestamp,
            "updated_at": timestamp,
            "media": {
                "title": title,
                "imdb_id": imdb_id,
                "media_type": media_type,
            },
            "artifacts": {"video_path": completed_content_path} if completed_content_path else {},
            "tasks": [
                {
                    "task_id": task_id,
                    "task_type": "download_media",
                    "status": "succeeded" if completed_content_path else "running",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "qbitlarr": {
                        "info_hash": _required(info_hash, "info_hash"),
                        "progress": float(progress),
                    },
                }
            ],
        }
        _set_if_value(workflow["media"], "season", season)
        _set_if_value(workflow["media"], "episode", episode)
        if completed_content_path:
            workflow["tasks"][0]["qbitlarr"]["content_path"] = completed_content_path
        return self._save(workflow)

    @_locked_method
    def attach_subtitle_intent_to_current_download(
        self,
        *,
        requester_id: str,
        source_language: str,
        target_language: str,
        output_mode: str,
        notification_language: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        candidates = self._attachable_downloads(requester_id)
        if not candidates:
            raise RuntimeStoreError("no active download task for requester: %s" % requester_id)
        if len(candidates) > 1:
            raise RuntimeStoreError("multiple active downloads for requester: %s" % requester_id)

        workflow = candidates[0]
        return self._attach_subtitle_intent_to_workflow(
            workflow,
            source_language=source_language,
            target_language=target_language,
            output_mode=output_mode,
            notification_language=notification_language,
            now=now,
        )

    def _attach_subtitle_intent_to_workflow(
        self,
        workflow: Dict[str, Any],
        *,
        source_language: str,
        target_language: str,
        output_mode: str,
        notification_language: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        timestamp = _timestamp(now)
        download_task = _download_task(workflow)
        dependency_ready = _download_dependency_satisfied(workflow)
        subtitle = {
            "source_language": _required(source_language, "source_language"),
            "target_language": _required(target_language, "target_language"),
            "output_mode": _required(output_mode, "output_mode"),
        }
        _set_if_value(subtitle, "notification_language", _string_value(notification_language))
        subtitle_task = {
            "task_id": _new_id("task"),
            "task_type": "translate_subtitle",
            "status": "ready" if dependency_ready else "waiting_for_dependency",
            "depends_on": [download_task["task_id"]],
            "created_at": timestamp,
            "updated_at": timestamp,
            "subtitle": subtitle,
        }
        workflow.setdefault("tasks", []).append(subtitle_task)
        workflow["updated_at"] = timestamp
        return self._save(workflow)

    @_locked_method
    def record_qbitlarr_download_with_subtitle_intent(
        self,
        *,
        requester_id: str,
        info_hash: str,
        source_language: str,
        target_language: str,
        output_mode: str,
        title: Optional[str] = None,
        imdb_id: Optional[str] = None,
        media_type: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        progress: float = 0.0,
        content_path: Optional[str] = None,
        notification_language: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        workflow = self.record_qbitlarr_download(
            requester_id=requester_id,
            info_hash=info_hash,
            title=title,
            imdb_id=imdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            progress=progress,
            content_path=content_path,
            now=now,
        )
        if _has_subtitle_task(workflow):
            return self._save(_update_subtitle_notification_language(workflow, notification_language))
        return self._attach_subtitle_intent_to_workflow(
            self._workflow_by_id(str(workflow.get("workflow_id") or "")),
            source_language=source_language,
            target_language=target_language,
            output_mode=output_mode,
            notification_language=notification_language,
            now=now,
        )

    @_locked_method
    def record_local_video_subtitle_intent(
        self,
        *,
        requester_id: str,
        video_path: str,
        source_language: str,
        target_language: str,
        output_mode: str,
        title: Optional[str] = None,
        imdb_id: Optional[str] = None,
        media_type: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        notification_language: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        timestamp = _timestamp(now)
        subtitle = {
            "source_language": _required(source_language, "source_language"),
            "target_language": _required(target_language, "target_language"),
            "output_mode": _required(output_mode, "output_mode"),
        }
        _set_if_value(subtitle, "notification_language", _string_value(notification_language))
        workflow = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "workflow_id": _new_id("workflow"),
            "status": "running",
            "requester_id": _required(requester_id, "requester_id"),
            "created_at": timestamp,
            "updated_at": timestamp,
            "media": {
                "title": title,
                "imdb_id": imdb_id,
                "media_type": media_type,
            },
            "artifacts": {"video_path": _required(video_path, "video_path")},
            "tasks": [
                {
                    "task_id": _new_id("task"),
                    "task_type": "translate_subtitle",
                    "status": "ready",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "subtitle": subtitle,
                }
            ],
        }
        _set_if_value(workflow["media"], "season", season)
        _set_if_value(workflow["media"], "episode", episode)
        return self._save(workflow)

    @_locked_method
    def mark_qbitlarr_download_complete(
        self,
        *,
        info_hash: str,
        content_path: str,
        now: Optional[Union[str, datetime]] = None,
    ) -> List[Dict[str, Any]]:
        workflow = self._find_by_qbitlarr_hash(info_hash)
        if workflow is None:
            raise QBitlarrHashNotTrackedError(info_hash)

        timestamp = _timestamp(now)
        workflow["updated_at"] = timestamp
        workflow.setdefault("artifacts", {})["video_path"] = _required(content_path, "content_path")
        download_task = _download_task(workflow)
        previous_download_status = str(download_task.get("status") or "")
        previous_download_updated_at = str(download_task.get("updated_at") or "")
        download_task["status"] = "succeeded"
        download_task["updated_at"] = timestamp
        qbitlarr = download_task.setdefault("qbitlarr", {})
        qbitlarr["info_hash"] = _required(info_hash, "info_hash")
        qbitlarr["progress"] = 1.0
        qbitlarr["content_path"] = content_path

        if _subtitle_tasks_need_replacement_for_completion(
            workflow,
            previous_download_status=previous_download_status,
            previous_download_updated_at=previous_download_updated_at,
        ):
            _replace_non_waiting_subtitle_tasks(workflow, timestamp)
        actions = _release_waiting_subtitle_tasks(workflow, timestamp)

        self._save(workflow)
        return actions

    @_locked_method
    def clear_qbitlarr_download_workflow(
        self,
        *,
        info_hash: str,
        reason: str = "download_removed",
        error: Optional[Mapping[str, Any]] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        normalized_hash = _required(info_hash, "info_hash")
        workflow = self._find_by_qbitlarr_hash(normalized_hash)
        if workflow is None:
            return {
                "status": "not_found",
                "info_hash": normalized_hash,
                "reason": reason,
                "error": dict(error) if error is not None else None,
                "updated_at": _timestamp(now),
            }

        workflow_id = _required(str(workflow.get("workflow_id") or ""), "workflow_id")
        tasks_cleared = [
            str(task.get("task_type") or "unknown")
            for task in workflow.get("tasks", [])
        ]
        path = self.root / ("%s.json" % workflow_id)
        if path.exists():
            path.unlink()

        return {
            "status": "cleared",
            "workflow_id": workflow_id,
            "info_hash": normalized_hash,
            "reason": reason,
            "tasks_cleared": tasks_cleared,
            "previous_status": workflow.get("status"),
            "error": dict(error) if error is not None else None,
            "updated_at": _timestamp(now),
        }

    @_locked_method
    def workflow_for_qbitlarr_hash(self, info_hash: str) -> Dict[str, Any]:
        workflow = self._find_by_qbitlarr_hash(info_hash)
        if workflow is None:
            raise QBitlarrHashNotTrackedError(info_hash)
        return workflow

    @_locked_method
    def ready_babelarr_job_create_video_actions(self) -> List[Dict[str, Any]]:
        return self.ready_mst_job_create_video_actions()

    @_locked_method
    def ready_mst_job_create_video_actions(self) -> List[Dict[str, Any]]:
        actions = []
        for item in _subtitle_queue_items(self.list_workflows()):
            workflow = item["workflow"]
            task = item["task"]
            if task.get("status") != "ready":
                continue
            if not _subtitle_resource_ready(workflow, task):
                continue
            actions.append(_mst_job_create_video_action(workflow, task))
        return actions

    @_locked_method
    def claim_ready_babelarr_job_create_video_actions(
        self,
        *,
        limit: Optional[int] = None,
        workflow_id: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> List[Dict[str, Any]]:
        return self.claim_ready_mst_job_create_video_actions(limit=limit, workflow_id=workflow_id, now=now)

    @_locked_method
    def claim_ready_mst_job_create_video_actions(
        self,
        *,
        limit: Optional[int] = None,
        workflow_id: Optional[str] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> List[Dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        timestamp = _timestamp(now)
        workflows = self.list_workflows()
        if _has_active_subtitle_task(workflows):
            return []

        for item in _subtitle_queue_items(workflows, workflow_id=workflow_id):
            workflow = item["workflow"]
            task = item["task"]
            status = task.get("status")
            if status not in {"ready", "waiting_for_dependency"}:
                continue
            if not _subtitle_resource_ready(workflow, task):
                if status != "waiting_for_dependency":
                    task["status"] = "waiting_for_dependency"
                    task["updated_at"] = timestamp
                    workflow["updated_at"] = timestamp
                    workflow["status"] = _workflow_status(workflow)
                    self._save(workflow)
                continue
            task["status"] = "dispatching"
            task["updated_at"] = timestamp
            task["dispatch"] = {
                "action": "babelarr_job_create_video",
                "claimed_at": timestamp,
            }
            workflow["updated_at"] = timestamp
            workflow["status"] = _workflow_status(workflow)
            self._save(workflow)
            return [_mst_job_create_video_action(workflow, task)]
        return []

    @_locked_method
    def record_babelarr_job_created(
        self,
        *,
        workflow_id: str,
        task_id: str,
        babelarr_job_id: str,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        return self.record_mst_job_created(
            workflow_id=workflow_id,
            task_id=task_id,
            mst_job_id=babelarr_job_id,
            now=now,
        )

    @_locked_method
    def record_mst_job_created(
        self,
        *,
        workflow_id: str,
        task_id: str,
        mst_job_id: str,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        workflow = self._workflow_by_id(workflow_id)
        task = _task_by_id(workflow, task_id)
        if task.get("task_type") != "translate_subtitle":
            raise RuntimeStoreError("task is not a translate_subtitle task: %s" % task_id)

        timestamp = _timestamp(now)
        task["status"] = "queued"
        task["updated_at"] = timestamp
        babelarr = task.setdefault("babelarr", {})
        babelarr["job_id"] = _required(mst_job_id, "mst_job_id")
        babelarr["status"] = "queued"
        babelarr["created_at"] = timestamp
        babelarr["updated_at"] = timestamp
        workflow["updated_at"] = timestamp
        workflow["status"] = _workflow_status(workflow)
        return self._save(workflow)

    @_locked_method
    def record_babelarr_job_status(
        self,
        *,
        workflow_id: str,
        task_id: str,
        status: str,
        status_detail: Optional[Mapping[str, Any]] = None,
        result: Optional[Mapping[str, Any]] = None,
        error: Optional[Mapping[str, Any]] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        return self.record_mst_job_status(
            workflow_id=workflow_id,
            task_id=task_id,
            status=status,
            status_detail=status_detail,
            result=result,
            error=error,
            now=now,
        )

    @_locked_method
    def record_mst_job_status(
        self,
        *,
        workflow_id: str,
        task_id: str,
        status: str,
        status_detail: Optional[Mapping[str, Any]] = None,
        result: Optional[Mapping[str, Any]] = None,
        error: Optional[Mapping[str, Any]] = None,
        now: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        if status not in {"queued", "running", "succeeded", "failed", "needs_confirmation"}:
            raise RuntimeStoreError("unsupported Babelarr job status: %s" % status)

        workflow = self._workflow_by_id(workflow_id)
        task = _task_by_id(workflow, task_id)
        if task.get("task_type") != "translate_subtitle":
            raise RuntimeStoreError("task is not a translate_subtitle task: %s" % task_id)

        timestamp = _timestamp(now)
        task["status"] = status
        task["updated_at"] = timestamp
        babelarr = task.setdefault("babelarr", {})
        babelarr["status"] = status
        babelarr["updated_at"] = timestamp
        if status_detail is not None:
            babelarr["status_detail"] = dict(status_detail)
        if result is not None:
            babelarr["result"] = dict(result)
            resolved_video_path = _mst_result_video_path(result)
            if resolved_video_path:
                workflow.setdefault("artifacts", {})["video_path"] = resolved_video_path
        if error is not None:
            babelarr["error"] = dict(error)
        workflow["updated_at"] = timestamp
        workflow["status"] = _workflow_status(workflow)
        return self._save(workflow)

    @_locked_method
    def workflow_summary(self, workflow_id: str) -> Dict[str, Any]:
        workflow = self._workflow_by_id(workflow_id)
        workflow["status"] = _workflow_status(workflow)
        return workflow

    @_locked_method
    def queue_status(self, *, requester_id: Optional[str] = None) -> Dict[str, Any]:
        workflows = self.list_workflows()
        items = _subtitle_queue_items(workflows)
        active_items = [item for item in items if item["task"].get("status") in ACTIVE_SUBTITLE_STATUSES]
        ready_items = [
            item
            for item in items
            if item["task"].get("status") in {"ready", "waiting_for_dependency"}
            and _subtitle_resource_ready(item["workflow"], item["task"])
        ]
        ready_positions = {
            str(item["task"].get("task_id") or ""): index + 1
            for index, item in enumerate(ready_items)
        }
        waiting_for_resource_count = sum(
            1
            for item in items
            if item["task"].get("status") in {"ready", "waiting_for_dependency"}
            and not _subtitle_resource_ready(item["workflow"], item["task"])
        )
        blocked_confirmation_count = sum(1 for item in items if item["task"].get("status") == "needs_confirmation")
        total_open_count = sum(1 for item in items if item["task"].get("status") in OPEN_SUBTITLE_STATUSES)
        active_count = len(active_items)
        requester_tasks = [
            _queue_task_summary(
                item,
                requester_id=requester_id,
                ready_positions=ready_positions,
                active_count=active_count,
            )
            for item in items
            if requester_id is not None
            and item["workflow"].get("requester_id") == requester_id
            and item["task"].get("status") in OPEN_SUBTITLE_STATUSES
        ]
        active_task = None
        if active_items:
            active_task = _queue_task_summary(
                active_items[0],
                requester_id=requester_id,
                ready_positions=ready_positions,
                active_count=active_count,
                hide_non_owner_details=requester_id is not None,
            )
        payload: Dict[str, Any] = {
            "global": {
                "active_count": active_count,
                "ready_count": len(ready_items),
                "waiting_for_resource_count": waiting_for_resource_count,
                "blocked_confirmation_count": blocked_confirmation_count,
                "total_open_count": total_open_count,
            },
            "requester_id": requester_id,
            "requester_tasks": requester_tasks,
            "active_task": active_task,
        }
        if requester_id is None:
            payload["tasks"] = [
                _queue_task_summary(
                    item,
                    requester_id=None,
                    ready_positions=ready_positions,
                    active_count=active_count,
                )
                for item in items
                if item["task"].get("status") in OPEN_SUBTITLE_STATUSES
            ]
        return payload

    @_locked_method
    def list_workflows(self) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        workflows = []
        for path in self.root.glob("*.json"):
            workflows.append(json.loads(path.read_text(encoding="utf-8")))
        workflows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return workflows

    def _attachable_downloads(self, requester_id: str) -> List[Dict[str, Any]]:
        matches = []
        for workflow in self.list_workflows():
            if workflow.get("requester_id") != requester_id:
                continue
            if _has_subtitle_task(workflow):
                continue
            try:
                task = _download_task(workflow)
            except RuntimeStoreError:
                continue
            if task.get("status") in {"running", "succeeded"}:
                matches.append(workflow)
        return matches

    def _find_by_qbitlarr_hash(self, info_hash: str) -> Optional[Dict[str, Any]]:
        target = _required(info_hash, "info_hash").casefold()
        for workflow in self.list_workflows():
            try:
                task = _download_task(workflow)
            except RuntimeStoreError:
                continue
            qbitlarr = task.get("qbitlarr") or {}
            if str(qbitlarr.get("info_hash") or "").casefold() == target:
                return workflow
        return None

    def _workflow_by_id(self, workflow_id: str) -> Dict[str, Any]:
        target = _required(workflow_id, "workflow_id")
        path = self.root / ("%s.json" % target)
        if not path.exists():
            raise RuntimeStoreError("workflow not found: %s" % target)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, workflow: Mapping[str, Any]) -> Dict[str, Any]:
        workflow_id = _required(str(workflow.get("workflow_id") or ""), "workflow_id")
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True)
        path = self.root / ("%s.json" % workflow_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        tmp_path.replace(path)
        return dict(workflow)

    @contextlib.contextmanager
    def _store_lock(self):
        with self._thread_lock:
            if self._lock_depth == 0:
                self.root.mkdir(parents=True, exist_ok=True)
                handle = (self.root / ".runtime.lock").open("a+")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                except Exception:
                    handle.close()
                    raise
                self._lock_handle = handle
            self._lock_depth += 1
        try:
            yield
        finally:
            with self._thread_lock:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    handle = self._lock_handle
                    self._lock_handle = None
                    if handle is not None:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_UN)
                        finally:
                            handle.close()


def _download_task(workflow: Mapping[str, Any]) -> Dict[str, Any]:
    for task in workflow.get("tasks", []):
        if task.get("task_type") == "download_media":
            return task
    raise RuntimeStoreError("workflow has no download_media task: %s" % workflow.get("workflow_id"))


def _task_by_id(workflow: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    target = _required(task_id, "task_id")
    for task in workflow.get("tasks", []):
        if task.get("task_id") == target:
            return task
    raise RuntimeStoreError("task not found: %s" % target)


def _has_subtitle_task(workflow: Mapping[str, Any]) -> bool:
    return any(task.get("task_type") == "translate_subtitle" for task in workflow.get("tasks", []))


def _update_subtitle_notification_language(
    workflow: Dict[str, Any], notification_language: Optional[str]
) -> Dict[str, Any]:
    language = _string_value(notification_language)
    if not language:
        return workflow
    for task in workflow.get("tasks", []):
        if task.get("task_type") != "translate_subtitle":
            continue
        subtitle = task.setdefault("subtitle", {})
        if isinstance(subtitle, dict):
            subtitle["notification_language"] = language
    return workflow


def _remove_subtitle_tasks(workflow: Dict[str, Any]) -> None:
    workflow["tasks"] = [task for task in workflow.get("tasks", []) if task.get("task_type") != "translate_subtitle"]


def _video_path(workflow: Mapping[str, Any]) -> Optional[str]:
    artifacts = workflow.get("artifacts") or {}
    value = artifacts.get("video_path")
    return value if isinstance(value, str) and value.strip() else None


def _clear_video_path(workflow: Dict[str, Any]) -> None:
    artifacts = workflow.get("artifacts")
    if not isinstance(artifacts, dict):
        return
    artifacts.pop("video_path", None)
    if not artifacts:
        workflow.pop("artifacts", None)


def _download_dependency_satisfied(workflow: Mapping[str, Any]) -> bool:
    try:
        download_task = _download_task(workflow)
    except RuntimeStoreError:
        return False
    return download_task.get("status") == "succeeded" and _video_path(workflow) is not None


def _subtitle_resource_ready(workflow: Mapping[str, Any], subtitle_task: Mapping[str, Any]) -> bool:
    if subtitle_task.get("depends_on"):
        return _download_dependency_satisfied(workflow)
    try:
        _download_task(workflow)
    except RuntimeStoreError:
        return _video_path(workflow) is not None
    return _download_dependency_satisfied(workflow)


def _subtitle_queue_items(
    workflows: List[Dict[str, Any]],
    *,
    workflow_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items = []
    for workflow in workflows:
        if workflow_id is not None and workflow.get("workflow_id") != workflow_id:
            continue
        for index, task in enumerate(workflow.get("tasks", [])):
            if task.get("task_type") != "translate_subtitle":
                continue
            items.append(
                {
                    "workflow": workflow,
                    "task": task,
                    "index": index,
                }
            )
    items.sort(
        key=lambda item: (
            str(item["task"].get("created_at") or item["workflow"].get("created_at") or ""),
            str(item["workflow"].get("created_at") or ""),
            str(item["workflow"].get("workflow_id") or ""),
            str(item["task"].get("task_id") or ""),
        )
    )
    return items


def _has_active_subtitle_task(workflows: List[Dict[str, Any]]) -> bool:
    for item in _subtitle_queue_items(workflows):
        if item["task"].get("status") in ACTIVE_SUBTITLE_STATUSES:
            return True
    return False


def _queue_task_summary(
    item: Mapping[str, Any],
    *,
    requester_id: Optional[str],
    ready_positions: Mapping[str, int],
    active_count: int,
    hide_non_owner_details: bool = False,
) -> Dict[str, Any]:
    workflow = item["workflow"]
    task = item["task"]
    is_requester_task = requester_id is None or workflow.get("requester_id") == requester_id
    task_id = str(task.get("task_id") or "")
    queue_position = ready_positions.get(task_id)
    status = str(task.get("status") or "unknown")
    resource_ready = _subtitle_resource_ready(workflow, task)
    summary: Dict[str, Any] = {
        "is_requester_task": is_requester_task,
        "execution_status": status,
        "resource_status": "ready" if resource_ready else "waiting",
        "queue_position": queue_position,
        "tasks_ahead": active_count + queue_position - 1 if queue_position is not None else None,
        "is_active": status in ACTIVE_SUBTITLE_STATUSES,
    }
    if hide_non_owner_details and not is_requester_task:
        return summary

    media = workflow.get("media") if isinstance(workflow.get("media"), Mapping) else {}
    subtitle = task.get("subtitle") if isinstance(task.get("subtitle"), Mapping) else {}
    babelarr = task.get("babelarr") if isinstance(task.get("babelarr"), Mapping) else {}
    summary.update(
        {
            "workflow_id": workflow.get("workflow_id"),
            "task_id": task.get("task_id"),
            "requester_id": workflow.get("requester_id"),
            "title": media.get("title"),
            "imdb_id": media.get("imdb_id"),
            "media_type": media.get("media_type"),
            "source_language": subtitle.get("source_language"),
            "target_language": subtitle.get("target_language"),
            "output_mode": subtitle.get("output_mode"),
            "babelarr_job_id": babelarr.get("job_id"),
            "status_detail": babelarr.get("status_detail"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
        }
    )
    _set_if_value(summary, "season", media.get("season"))
    _set_if_value(summary, "episode", media.get("episode"))
    return summary


def _replace_non_waiting_subtitle_tasks(workflow: Dict[str, Any], timestamp: str) -> None:
    download_task = _download_task(workflow)
    replaced_tasks = []
    changed = False
    for task in workflow.get("tasks", []):
        if task.get("task_type") != "translate_subtitle":
            replaced_tasks.append(task)
            continue
        if task.get("status") == "waiting_for_dependency":
            replaced_tasks.append(task)
            continue
        subtitle = task.get("subtitle") if isinstance(task.get("subtitle"), Mapping) else {}
        replaced_tasks.append(
            {
                "task_id": _new_id("task"),
                "task_type": "translate_subtitle",
                "status": "waiting_for_dependency",
                "depends_on": [download_task["task_id"]],
                "created_at": timestamp,
                "updated_at": timestamp,
                "subtitle": dict(subtitle),
            }
        )
        changed = True
    if changed:
        workflow["tasks"] = replaced_tasks


def _subtitle_tasks_need_replacement_for_completion(
    workflow: Mapping[str, Any],
    *,
    previous_download_status: str,
    previous_download_updated_at: str,
) -> bool:
    for task in workflow.get("tasks", []):
        if task.get("task_type") != "translate_subtitle":
            continue
        status = task.get("status")
        if status == "waiting_for_dependency":
            continue
        if previous_download_status != "succeeded":
            return True
        if status in {"dispatching", "queued", "running", "failed", "succeeded", "needs_confirmation"}:
            task_updated_at = str(task.get("updated_at") or "")
            if task_updated_at and previous_download_updated_at and task_updated_at < previous_download_updated_at:
                return True
    return False


def _mst_job_create_video_action(workflow: Mapping[str, Any], subtitle_task: Mapping[str, Any]) -> Dict[str, Any]:
    media = workflow.get("media") or {}
    subtitle = subtitle_task.get("subtitle") or {}
    arguments = {
        "video_path": _required(_video_path(workflow), "video_path"),
        "imdb_id": media.get("imdb_id"),
        "title": media.get("title"),
        "media_type": _mst_media_type(media.get("media_type")),
        "source_language": subtitle.get("source_language"),
        "target_language": subtitle.get("target_language"),
        "output_mode": subtitle.get("output_mode"),
    }
    _set_if_value(arguments, "season", media.get("season"))
    _set_if_value(arguments, "episode", media.get("episode"))
    action = {
        "action": "babelarr_job_create_video",
        "workflow_id": workflow.get("workflow_id"),
        "task_id": subtitle_task.get("task_id"),
        "requester_id": workflow.get("requester_id"),
        "arguments": arguments,
    }
    _set_if_value(action, "notification_language", _string_value(subtitle.get("notification_language")))
    return action


def _mst_media_type(value: Any) -> Optional[str]:
    if value == "tv":
        return "episode"
    return value if isinstance(value, str) else None


def _mst_result_video_path(result: Mapping[str, Any]) -> Optional[str]:
    for key in ("video_path", "input"):
        value = _string_value(result.get(key))
        if value:
            return value
    return None


def _release_waiting_subtitle_tasks(workflow: Dict[str, Any], timestamp: str) -> List[Dict[str, Any]]:
    actions = []
    if not _download_dependency_satisfied(workflow):
        return actions
    for task in workflow.get("tasks", []):
        if task.get("task_type") != "translate_subtitle":
            continue
        if task.get("status") != "waiting_for_dependency":
            continue
        task["status"] = "ready"
        task["updated_at"] = timestamp
        actions.append(_mst_job_create_video_action(workflow, task))
    workflow["status"] = _workflow_status(workflow)
    return actions


def _workflow_status(workflow: Mapping[str, Any]) -> str:
    tasks = list(workflow.get("tasks", []))
    if not tasks:
        return "running"
    statuses = {task.get("status") for task in tasks}
    if "failed" in statuses:
        return "failed"
    if "needs_confirmation" in statuses:
        return "needs_confirmation"
    if all(status == "succeeded" for status in statuses):
        return "succeeded"
    return "running"


def _set_if_value(target: Dict[str, Any], key: str, value: Optional[str]) -> None:
    if value is not None:
        target[key] = value


def _completed_content_path(progress: float, content_path: Optional[str]) -> Optional[str]:
    path = str(content_path).strip() if content_path is not None else ""
    if not path:
        return None
    try:
        numeric_progress = float(progress)
    except (TypeError, ValueError):
        return None
    return path if numeric_progress >= 1.0 else None


def _string_value(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required(value: Optional[str], name: str) -> str:
    if value is None or not str(value).strip():
        raise RuntimeStoreError("%s must not be empty" % name)
    return str(value).strip()


def _timestamp(value: Optional[Union[str, datetime]] = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])
