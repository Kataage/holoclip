from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from holoclip.domain import (
    CanonicalTranscriptSegment,
    ClipTimeline,
    EditorialIdea,
    EvidenceItem,
    ReviewRecord,
    SourceAsset,
    SourceMoment,
)

SCHEMA_VERSION = 1

TModel = TypeVar("TModel", bound=BaseModel)


class RepositoryVersionError(RuntimeError):
    pass


class CanonicalRepository(Protocol):
    def upsert_source_asset(self, item: SourceAsset) -> None: ...
    def get_source_asset(self, item_id: str) -> SourceAsset | None: ...

    def upsert_transcript_segment(self, item: CanonicalTranscriptSegment) -> None: ...
    def get_transcript_segment(
        self,
        item_id: str,
    ) -> CanonicalTranscriptSegment | None: ...

    def upsert_evidence(self, item: EvidenceItem) -> None: ...
    def get_evidence(self, item_id: str) -> EvidenceItem | None: ...

    def upsert_source_moment(self, item: SourceMoment) -> None: ...
    def get_source_moment(self, item_id: str) -> SourceMoment | None: ...

    def upsert_clip_timeline(self, item: ClipTimeline) -> None: ...
    def get_clip_timeline(self, item_id: str) -> ClipTimeline | None: ...

    def upsert_editorial_idea(self, item: EditorialIdea) -> None: ...
    def get_editorial_idea(self, item_id: str) -> EditorialIdea | None: ...

    def upsert_review_record(self, item: ReviewRecord) -> None: ...
    def get_review_record(self, item_id: str) -> ReviewRecord | None: ...


class SQLiteCanonicalRepository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteCanonicalRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connection:
                yield self._connection
        except Exception:
            raise

    def _schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if row is None:
            return 0

        version_row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version_row is None:
            return 0
        return int(version_row["value"])

    def _migrate(self) -> None:
        current = self._schema_version()
        if current > SCHEMA_VERSION:
            raise RepositoryVersionError(
                f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
            )

        if current == 0:
            with self.transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS source_assets (
                        id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_source_assets_external
                        ON source_assets(platform, external_id);

                    CREATE TABLE IF NOT EXISTS transcript_segments (
                        id TEXT PRIMARY KEY,
                        source_asset_id TEXT NOT NULL,
                        start REAL NOT NULL,
                        end REAL NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(source_asset_id) REFERENCES source_assets(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_transcript_source_time
                        ON transcript_segments(source_asset_id, start, end);

                    CREATE TABLE IF NOT EXISTS evidence_items (
                        id TEXT PRIMARY KEY,
                        source_asset_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        start REAL NOT NULL,
                        end REAL NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(source_asset_id) REFERENCES source_assets(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_evidence_source_time
                        ON evidence_items(source_asset_id, start, end);

                    CREATE TABLE IF NOT EXISTS source_moments (
                        id TEXT PRIMARY KEY,
                        source_asset_id TEXT NOT NULL,
                        start REAL NOT NULL,
                        end REAL NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(source_asset_id) REFERENCES source_assets(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_moments_source_time
                        ON source_moments(source_asset_id, start, end);

                    CREATE TABLE IF NOT EXISTS clip_timelines (
                        id TEXT PRIMARY KEY,
                        timeline_version INTEGER NOT NULL,
                        estimated_duration REAL NOT NULL,
                        payload TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS timeline_segments (
                        timeline_id TEXT NOT NULL,
                        segment_id TEXT NOT NULL,
                        source_asset_id TEXT NOT NULL,
                        output_order INTEGER NOT NULL,
                        source_start REAL NOT NULL,
                        source_end REAL NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(timeline_id, segment_id),
                        UNIQUE(timeline_id, output_order),
                        FOREIGN KEY(timeline_id) REFERENCES clip_timelines(id)
                            ON DELETE CASCADE,
                        FOREIGN KEY(source_asset_id) REFERENCES source_assets(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_timeline_source
                        ON timeline_segments(source_asset_id, source_start, source_end);

                    CREATE TABLE IF NOT EXISTS editorial_ideas (
                        id TEXT PRIMARY KEY,
                        archetype TEXT NOT NULL,
                        generation_horizon TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS editorial_idea_sources (
                        idea_id TEXT NOT NULL,
                        source_asset_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        PRIMARY KEY(idea_id, source_asset_id, role),
                        FOREIGN KEY(idea_id) REFERENCES editorial_ideas(id)
                            ON DELETE CASCADE,
                        FOREIGN KEY(source_asset_id) REFERENCES source_assets(id)
                    );

                    CREATE TABLE IF NOT EXISTS editorial_idea_moments (
                        idea_id TEXT NOT NULL,
                        source_moment_id TEXT NOT NULL,
                        PRIMARY KEY(idea_id, source_moment_id),
                        FOREIGN KEY(idea_id) REFERENCES editorial_ideas(id)
                            ON DELETE CASCADE,
                        FOREIGN KEY(source_moment_id) REFERENCES source_moments(id)
                    );

                    CREATE TABLE IF NOT EXISTS review_records (
                        id TEXT PRIMARY KEY,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_review_target
                        ON review_records(target_type, target_id, created_at);
                    """
                )
                connection.execute(
                    """
                    INSERT INTO schema_meta(key, value)
                    VALUES('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(SCHEMA_VERSION),),
                )

        final = self._schema_version()
        if final != SCHEMA_VERSION:
            raise RepositoryVersionError(
                f"database schema version {final} does not match supported {SCHEMA_VERSION}"
            )

    @staticmethod
    def _payload(item: BaseModel) -> str:
        return json.dumps(
            item.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(model: type[TModel], row: sqlite3.Row | None) -> TModel | None:
        if row is None:
            return None
        return model.model_validate_json(row["payload"])

    def upsert_source_asset(self, item: SourceAsset) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_assets(id, platform, external_id, payload)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    platform=excluded.platform,
                    external_id=excluded.external_id,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.platform.value,
                    item.external_id,
                    self._payload(item),
                ),
            )

    def get_source_asset(self, item_id: str) -> SourceAsset | None:
        row = self._connection.execute(
            "SELECT payload FROM source_assets WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(SourceAsset, row)

    def upsert_transcript_segment(self, item: CanonicalTranscriptSegment) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO transcript_segments(id, source_asset_id, start, end, payload)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_asset_id=excluded.source_asset_id,
                    start=excluded.start,
                    end=excluded.end,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.source_asset_id,
                    item.start,
                    item.end,
                    self._payload(item),
                ),
            )

    def get_transcript_segment(
        self,
        item_id: str,
    ) -> CanonicalTranscriptSegment | None:
        row = self._connection.execute(
            "SELECT payload FROM transcript_segments WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(CanonicalTranscriptSegment, row)

    def upsert_evidence(self, item: EvidenceItem) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO evidence_items(
                    id, source_asset_id, kind, start, end, payload
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_asset_id=excluded.source_asset_id,
                    kind=excluded.kind,
                    start=excluded.start,
                    end=excluded.end,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.source_asset_id,
                    item.kind.value,
                    item.start,
                    item.end,
                    self._payload(item),
                ),
            )

    def get_evidence(self, item_id: str) -> EvidenceItem | None:
        row = self._connection.execute(
            "SELECT payload FROM evidence_items WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(EvidenceItem, row)

    def upsert_source_moment(self, item: SourceMoment) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_moments(id, source_asset_id, start, end, payload)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_asset_id=excluded.source_asset_id,
                    start=excluded.start,
                    end=excluded.end,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.source_asset_id,
                    item.start,
                    item.end,
                    self._payload(item),
                ),
            )

    def get_source_moment(self, item_id: str) -> SourceMoment | None:
        row = self._connection.execute(
            "SELECT payload FROM source_moments WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(SourceMoment, row)

    def upsert_clip_timeline(self, item: ClipTimeline) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO clip_timelines(
                    id, timeline_version, estimated_duration, payload
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    timeline_version=excluded.timeline_version,
                    estimated_duration=excluded.estimated_duration,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.timeline_version,
                    item.estimated_duration,
                    self._payload(item),
                ),
            )
            connection.execute(
                "DELETE FROM timeline_segments WHERE timeline_id=?",
                (item.id,),
            )
            connection.executemany(
                """
                INSERT INTO timeline_segments(
                    timeline_id,
                    segment_id,
                    source_asset_id,
                    output_order,
                    source_start,
                    source_end,
                    payload
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        segment.id,
                        segment.source_asset_id,
                        segment.output_order,
                        segment.source_start,
                        segment.source_end,
                        self._payload(segment),
                    )
                    for segment in item.segments
                ],
            )

    def get_clip_timeline(self, item_id: str) -> ClipTimeline | None:
        row = self._connection.execute(
            "SELECT payload FROM clip_timelines WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(ClipTimeline, row)

    def upsert_editorial_idea(self, item: EditorialIdea) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO editorial_ideas(
                    id, archetype, generation_horizon, status, payload
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    archetype=excluded.archetype,
                    generation_horizon=excluded.generation_horizon,
                    status=excluded.status,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.archetype.value,
                    item.generation_horizon.value,
                    item.status.value,
                    self._payload(item),
                ),
            )
            connection.execute(
                "DELETE FROM editorial_idea_sources WHERE idea_id=?",
                (item.id,),
            )
            source_rows = [
                (item.id, source_id, "required")
                for source_id in item.required_source_asset_ids
            ]
            source_rows.extend(
                (item.id, source_id, "optional")
                for source_id in item.optional_source_asset_ids
            )
            connection.executemany(
                """
                INSERT INTO editorial_idea_sources(idea_id, source_asset_id, role)
                VALUES(?, ?, ?)
                """,
                source_rows,
            )

            connection.execute(
                "DELETE FROM editorial_idea_moments WHERE idea_id=?",
                (item.id,),
            )
            connection.executemany(
                """
                INSERT INTO editorial_idea_moments(idea_id, source_moment_id)
                VALUES(?, ?)
                """,
                [
                    (item.id, moment_id)
                    for moment_id in item.source_moment_ids
                ],
            )

    def get_editorial_idea(self, item_id: str) -> EditorialIdea | None:
        row = self._connection.execute(
            "SELECT payload FROM editorial_ideas WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(EditorialIdea, row)

    def upsert_review_record(self, item: ReviewRecord) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO review_records(
                    id, target_type, target_id, created_at, payload
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target_type=excluded.target_type,
                    target_id=excluded.target_id,
                    created_at=excluded.created_at,
                    payload=excluded.payload
                """,
                (
                    item.id,
                    item.target_type.value,
                    item.target_id,
                    item.created_at,
                    self._payload(item),
                ),
            )

    def get_review_record(self, item_id: str) -> ReviewRecord | None:
        row = self._connection.execute(
            "SELECT payload FROM review_records WHERE id=?",
            (item_id,),
        ).fetchone()
        return self._decode(ReviewRecord, row)

    def timeline_ids_for_source(self, source_asset_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT timeline_id
            FROM timeline_segments
            WHERE source_asset_id=?
            ORDER BY timeline_id
            """,
            (source_asset_id,),
        ).fetchall()
        return [str(row["timeline_id"]) for row in rows]

    def idea_ids_for_source(self, source_asset_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT idea_id
            FROM editorial_idea_sources
            WHERE source_asset_id=?
            ORDER BY idea_id
            """,
            (source_asset_id,),
        ).fetchall()
        return [str(row["idea_id"]) for row in rows]

    def export_jsonl(self, entity: str, output: Path | str) -> int:
        model_tables = {
            "source_assets": "source_assets",
            "transcript_segments": "transcript_segments",
            "evidence_items": "evidence_items",
            "source_moments": "source_moments",
            "clip_timelines": "clip_timelines",
            "editorial_ideas": "editorial_ideas",
            "review_records": "review_records",
        }
        table = model_tables.get(entity)
        if table is None:
            raise ValueError(f"unsupported export entity: {entity}")

        rows = self._connection.execute(
            f"SELECT payload FROM {table} ORDER BY id"  # noqa: S608
        ).fetchall()
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                payload = json.loads(str(row["payload"]))
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        return len(rows)
