from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from holoclip.domain import stable_id


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def file_input_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def make_input_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    BLOCKED_QUOTA = "blocked_quota"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    CANCELLED = "cancelled"
    STALE = "stale"


class ArtifactRecord(BaseModel):
    id: str
    kind: str
    locator: str
    checksum: str
    size_bytes: int = Field(ge=0)
    content_type: str
    logical_owner_type: str
    logical_owner_id: str
    temporary: bool = False
    expires_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StageRun(BaseModel):
    id: str
    stage: str
    subject_type: str
    subject_id: str
    input_fingerprint: str
    stage_version: str
    status: StageStatus
    attempt: int = Field(default=1, ge=1)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    output_artifact_ids: list[str] = Field(default_factory=list)
    error_class: str | None = None
    error_detail: str | None = None
    retry_after: float | None = Field(default=None, ge=0)


def stage_cache_key(
    *,
    stage: str,
    subject_type: str,
    subject_id: str,
    input_fingerprint: str,
    stage_version: str,
    provider: str | None,
    model: str | None,
    prompt_version: str | None,
) -> str:
    return stable_id(
        "stagekey",
        stage,
        subject_type,
        subject_id,
        input_fingerprint,
        stage_version,
        provider,
        model,
        prompt_version,
    )


def make_stage_run_id(cache_key: str, attempt: int) -> str:
    return stable_id("run", cache_key, attempt)


def checksum_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StageArtifactCache:
    def __init__(
        self,
        repository: Any,
        artifact_dir: Path | str,
    ) -> None:
        self.repository = repository
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def reusable_json(
        self,
        *,
        stage: str,
        subject_type: str,
        subject_id: str,
        input_fingerprint: str,
        stage_version: str,
        provider: str | None,
        model: str | None,
        prompt_version: str | None = None,
    ) -> Any | None:
        run = self.repository.find_reusable_stage_run(
            stage=stage,
            subject_type=subject_type,
            subject_id=subject_id,
            input_fingerprint=input_fingerprint,
            stage_version=stage_version,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        if run is None or not run.output_artifact_ids:
            return None

        artifact = self.repository.get_artifact(run.output_artifact_ids[0])
        if artifact is None:
            return None
        path = Path(artifact.locator)
        if not path.exists():
            return None

        raw = path.read_bytes()
        if checksum_bytes(raw) != artifact.checksum:
            return None
        return json.loads(raw.decode("utf-8"))

    def start(
        self,
        *,
        stage: str,
        subject_type: str,
        subject_id: str,
        input_fingerprint: str,
        stage_version: str,
        provider: str | None,
        model: str | None,
        prompt_version: str | None = None,
    ) -> StageRun:
        previous = self.repository.latest_stage_run(
            stage=stage,
            subject_type=subject_type,
            subject_id=subject_id,
            input_fingerprint=input_fingerprint,
            stage_version=stage_version,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        attempt = 1 if previous is None else previous.attempt + 1
        key = stage_cache_key(
            stage=stage,
            subject_type=subject_type,
            subject_id=subject_id,
            input_fingerprint=input_fingerprint,
            stage_version=stage_version,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        run = StageRun(
            id=make_stage_run_id(key, attempt),
            stage=stage,
            subject_type=subject_type,
            subject_id=subject_id,
            input_fingerprint=input_fingerprint,
            stage_version=stage_version,
            status=StageStatus.RUNNING,
            attempt=attempt,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            started_at=utc_now_iso(),
        )
        self.repository.upsert_stage_run(run)
        return run

    def succeed_json(
        self,
        run: StageRun,
        payload: Any,
        *,
        kind: str,
        content_type: str = "application/json",
    ) -> ArtifactRecord:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        checksum = checksum_bytes(raw)
        artifact_id = stable_id("artifact", run.id, kind, checksum)
        path = self.artifact_dir / f"{artifact_id}.json"
        path.write_bytes(raw)

        artifact = ArtifactRecord(
            id=artifact_id,
            kind=kind,
            locator=str(path),
            checksum=checksum,
            size_bytes=len(raw),
            content_type=content_type,
            logical_owner_type="stage_run",
            logical_owner_id=run.id,
        )
        self.repository.upsert_artifact(artifact)

        completed = run.model_copy(
            update={
                "status": StageStatus.SUCCEEDED,
                "completed_at": utc_now_iso(),
                "output_artifact_ids": [artifact.id],
                "error_class": None,
                "error_detail": None,
                "retry_after": None,
            }
        )
        self.repository.upsert_stage_run(completed)
        return artifact

    def fail(
        self,
        run: StageRun,
        *,
        status: StageStatus,
        error: BaseException,
        retry_after: float | None = None,
    ) -> StageRun:
        if status not in {
            StageStatus.FAILED_RETRYABLE,
            StageStatus.FAILED_TERMINAL,
            StageStatus.BLOCKED_QUOTA,
            StageStatus.BLOCKED_DEPENDENCY,
            StageStatus.CANCELLED,
        }:
            raise ValueError(f"invalid failure status: {status}")

        failed = run.model_copy(
            update={
                "status": status,
                "completed_at": utc_now_iso(),
                "error_class": type(error).__name__,
                "error_detail": str(error),
                "retry_after": retry_after,
            }
        )
        self.repository.upsert_stage_run(failed)
        return failed
