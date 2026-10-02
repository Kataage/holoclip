from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from holoclip.execution import make_input_fingerprint, utc_now_iso
from holoclip.examples import make_human_clip_example_id


BATCH_VERSION = "human-example-batch-v1"


class BatchStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    NEEDS_SOURCE = "needs_source"
    POLICY_BLOCKED = "policy_blocked"
    POLICY_REVIEW = "policy_review"
    ALIGNMENT_REJECTED = "alignment_rejected"
    RETRYABLE = "retryable"
    FAILED = "failed"


class ExampleBatchItem(BaseModel):
    clip_url: str
    source_url: str | None = None
    note: str | None = None
    enabled: bool = True

    @property
    def id(self) -> str:
        return make_human_clip_example_id(self.clip_url)

    @property
    def fingerprint(self) -> str:
        return make_input_fingerprint(
            {
                "batch_version": BATCH_VERSION,
                "clip_url": self.clip_url,
                "source_url": self.source_url,
                "enabled": self.enabled,
            }
        )


class ExampleBatchItemState(BaseModel):
    item_id: str
    clip_url: str
    source_url: str | None = None
    fingerprint: str
    status: BatchStatus
    attempts: int = 0
    example_id: str | None = None
    last_error: str | None = None
    details: dict[str, object] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utc_now_iso)


class ExampleBatchState(BaseModel):
    version: str = BATCH_VERSION
    manifest_path: str
    items: dict[str, ExampleBatchItemState] = Field(default_factory=dict)


class ExampleBatchSummary(BaseModel):
    total: int
    processed: int
    skipped_existing_success: int
    counts: dict[str, int]
    state_path: str


def load_batch_manifest(path: Path) -> list[ExampleBatchItem]:
    items: list[ExampleBatchItem] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = ExampleBatchItem.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(
                f"invalid batch item at {path}:{line_number}: {exc}"
            ) from exc
        if item.id in seen:
            raise ValueError(
                f"duplicate clip URL/example id at {path}:{line_number}: {item.clip_url}"
            )
        seen.add(item.id)
        items.append(item)
    return items


def load_batch_state(
    path: Path,
    *,
    manifest_path: Path,
) -> ExampleBatchState:
    if not path.exists():
        return ExampleBatchState(manifest_path=str(manifest_path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = ExampleBatchState.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid batch state {path}: {exc}") from exc
    return state


def save_batch_state(path: Path, state: ExampleBatchState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def should_skip_succeeded(
    item: ExampleBatchItem,
    state: ExampleBatchState,
    *,
    force: bool = False,
) -> bool:
    if force:
        return False
    existing = state.items.get(item.id)
    if existing is None:
        return False
    return (
        existing.status is BatchStatus.SUCCEEDED
        and existing.fingerprint == item.fingerprint
    )


def update_batch_item_state(
    state: ExampleBatchState,
    item: ExampleBatchItem,
    *,
    status: BatchStatus,
    example_id: str | None = None,
    last_error: str | None = None,
    details: dict[str, object] | None = None,
) -> ExampleBatchItemState:
    previous = state.items.get(item.id)
    record = ExampleBatchItemState(
        item_id=item.id,
        clip_url=item.clip_url,
        source_url=item.source_url,
        fingerprint=item.fingerprint,
        status=status,
        attempts=(previous.attempts if previous is not None else 0) + 1,
        example_id=example_id or (previous.example_id if previous is not None else None),
        last_error=last_error,
        details=details or {},
        updated_at=utc_now_iso(),
    )
    state.items[item.id] = record
    return record


def summarize_batch_state(
    items: list[ExampleBatchItem],
    state: ExampleBatchState,
    *,
    processed: int,
    skipped_existing_success: int,
    state_path: Path,
) -> ExampleBatchSummary:
    counts = {status.value: 0 for status in BatchStatus}
    for item in items:
        record = state.items.get(item.id)
        if record is None:
            continue
        counts[record.status.value] += 1
    return ExampleBatchSummary(
        total=len(items),
        processed=processed,
        skipped_existing_success=skipped_existing_success,
        counts=counts,
        state_path=str(state_path),
    )
