from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

from holoclip.domain import (
    SourceAsset,
    SourcePlatform,
    SourceType,
    make_source_asset_id,
)
from holoclip.domain_adapters import (
    candidate_to_source_moment,
    canonicalize_transcript_segments,
    human_example_to_clip_timeline,
    local_source_asset,
)
from holoclip.evaluation import EvaluationRun, load_run
from holoclip.examples import HumanClipExample
from holoclip.repository import SQLiteCanonicalRepository
from holoclip.transcript import load_transcript


class MigrationDiagnostic(BaseModel):
    level: str
    source: str
    message: str


class MigrationReport(BaseModel):
    evaluation_runs_seen: int = 0
    source_assets_processed: int = 0
    transcript_segments_processed: int = 0
    source_moments_processed: int = 0
    human_examples_seen: int = 0
    clip_timelines_processed: int = 0
    diagnostics: list[MigrationDiagnostic] = Field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for item in self.diagnostics if item.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for item in self.diagnostics if item.level == "warning")


def _diagnostic(
    report: MigrationReport,
    *,
    level: str,
    source: Path | str,
    message: str,
) -> None:
    report.diagnostics.append(
        MigrationDiagnostic(
            level=level,
            source=str(source),
            message=message,
        )
    )


def _resolve_legacy_path(raw: str, owner_path: Path) -> Path | None:
    candidate = Path(raw)
    if candidate.exists():
        return candidate
    if not candidate.is_absolute():
        relative = owner_path.parent / candidate
        if relative.exists():
            return relative
    return None


def _evaluation_source_asset(run: EvaluationRun, run_path: Path) -> SourceAsset:
    if not run.stream_id.strip():
        raise ValueError("evaluation run has an empty stream_id")

    if run.media_path:
        asset = local_source_asset(
            external_id=run.stream_id,
            title=run.stream_id,
        )
        return asset.model_copy(
            update={
                "provenance": {
                    "legacy_kind": "evaluation_run",
                    "legacy_run_path": str(run_path),
                    "legacy_media_path": run.media_path,
                }
            }
        )

    asset_id = make_source_asset_id(SourcePlatform.OTHER, run.stream_id)
    return SourceAsset(
        id=asset_id,
        platform=SourcePlatform.OTHER,
        external_id=run.stream_id,
        title=run.stream_id,
        source_type=SourceType.VIDEO,
        provenance={
            "legacy_kind": "evaluation_run",
            "legacy_run_path": str(run_path),
        },
    )


def migrate_evaluation_run(
    repository: SQLiteCanonicalRepository,
    run_path: Path,
    report: MigrationReport,
) -> dict[str, str]:
    report.evaluation_runs_seen += 1
    try:
        run = load_run(run_path)
        source_asset = _evaluation_source_asset(run, run_path)
    except (OSError, ValueError, TypeError) as exc:
        _diagnostic(
            report,
            level="error",
            source=run_path,
            message=f"evaluation run could not be parsed: {exc}",
        )
        return {}

    repository.upsert_source_asset(source_asset)
    report.source_assets_processed += 1

    canonical_segments = []
    transcript_path = _resolve_legacy_path(run.transcript_path, run_path)
    if transcript_path is None:
        _diagnostic(
            report,
            level="warning",
            source=run_path,
            message=(
                "transcript_path could not be resolved; candidates were migrated "
                "without transcript segment references"
            ),
        )
    else:
        try:
            legacy_segments = load_transcript(transcript_path)
            canonical_segments = canonicalize_transcript_segments(
                source_asset.id,
                legacy_segments,
            )
        except (OSError, ValueError, TypeError) as exc:
            _diagnostic(
                report,
                level="warning",
                source=transcript_path,
                message=f"transcript could not be migrated: {exc}",
            )
            canonical_segments = []
        else:
            for segment in canonical_segments:
                repository.upsert_transcript_segment(segment)
            report.transcript_segments_processed += len(canonical_segments)

    candidate_mapping: dict[str, str] = {}
    for item in run.candidates:
        segment_ids = [
            segment.id
            for segment in canonical_segments
            if segment.end > item.candidate.start
            and segment.start < item.candidate.end
        ]
        moment = candidate_to_source_moment(
            source_asset.id,
            item.candidate,
            transcript_segment_ids=segment_ids,
        )
        repository.upsert_source_moment(moment)
        candidate_mapping[item.id] = moment.id
        report.source_moments_processed += 1

    return candidate_mapping


def _youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = parsed.hostname.lower() if parsed.hostname else ""
    if host in {"youtu.be", "www.youtu.be"}:
        value = parsed.path.strip("/").split("/", 1)[0]
        return value or None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            return values[0] if values and values[0] else None
        if parsed.path.startswith("/live/") or parsed.path.startswith("/shorts/"):
            parts = [part for part in parsed.path.split("/") if part]
            return parts[1] if len(parts) >= 2 else None
    return None


def _example_source_asset(
    example: HumanClipExample,
    source_path: Path,
) -> SourceAsset:
    source_url = (example.source_url or "").strip()
    youtube_id = _youtube_video_id(source_url) if source_url else None

    if example.stream_id and example.stream_id.strip():
        external_id = example.stream_id.strip()
    elif youtube_id:
        external_id = youtube_id
    elif source_url:
        external_id = source_url
    else:
        raise ValueError(
            "example has neither stream_id nor source_url; source identity is ambiguous"
        )

    platform = SourcePlatform.YOUTUBE if youtube_id else SourcePlatform.OTHER
    asset_id = make_source_asset_id(platform, external_id)
    return SourceAsset(
        id=asset_id,
        platform=platform,
        external_id=external_id,
        canonical_url=source_url or None,
        title=example.source_title,
        source_type=SourceType.STREAM,
        provenance={
            "legacy_kind": "human_clip_example",
            "legacy_store_path": str(source_path),
            "legacy_example_id": example.id,
            "clip_url": example.clip_url,
        },
    )


def migrate_human_example(
    repository: SQLiteCanonicalRepository,
    example: HumanClipExample,
    *,
    source_path: Path,
    report: MigrationReport,
) -> None:
    report.human_examples_seen += 1
    try:
        source_asset = _example_source_asset(example, source_path)
    except ValueError as exc:
        _diagnostic(
            report,
            level="error",
            source=source_path,
            message=f"example {example.id}: {exc}",
        )
        return

    repository.upsert_source_asset(source_asset)
    report.source_assets_processed += 1

    timeline = human_example_to_clip_timeline(
        example,
        source_asset_id=source_asset.id,
    )
    if timeline is None:
        _diagnostic(
            report,
            level="warning",
            source=source_path,
            message=(
                f"example {example.id}: source identity was migrated but no "
                "aligned source range exists yet"
            ),
        )
        return

    repository.upsert_clip_timeline(timeline)
    report.clip_timelines_processed += 1


def migrate_human_example_store(
    repository: SQLiteCanonicalRepository,
    store_path: Path,
    report: MigrationReport,
) -> None:
    if not store_path.exists():
        _diagnostic(
            report,
            level="warning",
            source=store_path,
            message="human example store does not exist; skipped",
        )
        return

    for line_number, raw_line in enumerate(
        store_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            example = HumanClipExample.model_validate_json(line)
        except ValueError as exc:
            _diagnostic(
                report,
                level="error",
                source=store_path,
                message=f"line {line_number}: invalid human example: {exc}",
            )
            continue
        migrate_human_example(
            repository,
            example,
            source_path=store_path,
            report=report,
        )


def migrate_legacy_data(
    repository: SQLiteCanonicalRepository,
    *,
    runs_dir: Path,
    examples_store: Path,
) -> MigrationReport:
    report = MigrationReport()

    if runs_dir.exists():
        for run_path in sorted(runs_dir.glob("*.json")):
            if run_path.is_file():
                migrate_evaluation_run(repository, run_path, report)
    else:
        _diagnostic(
            report,
            level="warning",
            source=runs_dir,
            message="evaluation runs directory does not exist; skipped",
        )

    migrate_human_example_store(
        repository,
        examples_store,
        report,
    )
    return report


def report_as_json(report: MigrationReport) -> str:
    payload = report.model_dump(mode="json")
    payload["warnings"] = report.warnings
    payload["errors"] = report.errors
    return json.dumps(payload, ensure_ascii=False, indent=2)
