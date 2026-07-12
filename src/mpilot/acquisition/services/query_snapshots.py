from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mpilot.acquisition.models import QuerySnapshot, QuerySnapshotEntry, SearchResult


_QUERY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


def create_query_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_urlsafe(6)}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class QuerySnapshotStore:
    def __init__(self, root_dir: str | os.PathLike[str]) -> None:
        self.root_dir = Path(root_dir)

    def create(
        self,
        *,
        query_id: str,
        request: dict[str, Any],
        status: str,
        reason: str,
        results: list[SearchResult],
        metadata: dict[str, Any] | None = None,
    ) -> QuerySnapshot:
        now = utc_now()
        snapshot = QuerySnapshot(
            query_id=query_id,
            status=status,
            created_at=now,
            updated_at=now,
            request=request,
            snapshots=[
                QuerySnapshotEntry(
                    version=1,
                    reason=reason,
                    created_at=now,
                    results=results,
                    metadata=metadata or {},
                )
            ],
        )
        self.write(snapshot)
        return snapshot

    def append(
        self,
        *,
        query_id: str,
        status: str,
        reason: str,
        results: list[SearchResult],
        metadata: dict[str, Any] | None = None,
    ) -> QuerySnapshot:
        snapshot = self.read(query_id)
        now = utc_now()
        snapshot.status = status
        snapshot.updated_at = now
        snapshot.snapshots.append(
            QuerySnapshotEntry(
                version=len(snapshot.snapshots) + 1,
                reason=reason,
                created_at=now,
                results=results,
                metadata=metadata or {},
            )
        )
        self.write(snapshot)
        return snapshot

    def read(self, query_id: str) -> QuerySnapshot:
        path = self._path_for(query_id)
        return QuerySnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def write(self, snapshot: QuerySnapshot) -> None:
        path = self._path_for(snapshot.query_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temp_path.write_text(
            json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def prune(
        self,
        *,
        now: datetime,
        retention: timedelta,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if retention.total_seconds() < 0:
            raise ValueError("retention must be non-negative")
        cutoff = now.astimezone(UTC) - retention
        candidates: list[tuple[str, Path, datetime]] = []
        if not self.root_dir.exists():
            return {"status": "success", "deleted_count": 0, "deleted_query_ids": []}

        for path in self.root_dir.glob("*.json"):
            try:
                snapshot = QuerySnapshot.model_validate_json(path.read_text(encoding="utf-8"))
                updated_at = _parse_snapshot_timestamp(snapshot.updated_at)
            except Exception:
                continue
            if updated_at >= cutoff:
                continue
            candidates.append((snapshot.query_id, path, updated_at))

        candidates.sort(key=lambda item: item[2])
        if not dry_run:
            for _query_id, path, _updated_at in candidates:
                path.unlink(missing_ok=True)
        return {
            "status": "success",
            "deleted_count": len(candidates),
            "deleted_query_ids": [query_id for query_id, _path, _updated_at in candidates],
        }

    def _path_for(self, query_id: str) -> Path:
        if not _QUERY_ID_RE.fullmatch(query_id):
            raise FileNotFoundError(query_id)
        return self.root_dir / f"{query_id}.json"


def _parse_snapshot_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
