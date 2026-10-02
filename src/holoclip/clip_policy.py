from __future__ import annotations

import json
import unicodedata
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from holoclip.models import TranscriptSegment
from holoclip.providers.source.base import SourceMetadata


POLICY_VERSION = "clip-policy-v1"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class PolicyOperation(StrEnum):
    LEARN = "learn"
    DISCOVER = "discover"
    RENDER = "render"
    PUBLISH = "publish"


class ClipPolicyRule(BaseModel):
    id: str
    decision: PolicyDecision
    reason: str
    operations: list[PolicyOperation] = Field(
        default_factory=lambda: list(PolicyOperation)
    )
    source_id: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    evidence_url: str | None = None
    evidence_text: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "ClipPolicyRule":
        if self.start is not None or self.end is not None:
            if self.start is None or self.end is None:
                raise ValueError("range policy rules require both start and end")
            if self.end <= self.start:
                raise ValueError("range policy rule end must be greater than start")
            if self.source_id is None:
                raise ValueError("range policy rules require source_id")
        if (
            self.source_id is None
            and self.channel_id is None
            and self.channel_name is None
        ):
            raise ValueError(
                "policy rule requires source_id, channel_id, or channel_name"
            )
        return self

    @property
    def is_range_rule(self) -> bool:
        return self.start is not None and self.end is not None


class PolicyFinding(BaseModel):
    decision: PolicyDecision
    reason: str
    rule_id: str | None = None
    source: str
    start: float | None = None
    end: float | None = None
    evidence_url: str | None = None
    evidence_text: str | None = None


class ClipPolicyEvaluation(BaseModel):
    version: str = POLICY_VERSION
    decision: PolicyDecision
    findings: list[PolicyFinding] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision is PolicyDecision.BLOCK

    @property
    def review_required(self) -> bool:
        return self.decision is PolicyDecision.REVIEW


_EXPLICIT_NO_CLIP_PHRASES = (
    "切り抜き禁止",
    "切り抜きng",
    "切り抜き不可",
    "切り抜きはご遠慮",
    "切り抜きご遠慮",
    "切り抜かないで",
    "切り抜きしないで",
    "do not clip",
    "don't clip",
    "no clipping",
    "clips prohibited",
)

_SPOKEN_RESTRICTION_PHRASES = (
    "切り抜き禁止",
    "切り抜かないで",
    "切り抜きしないで",
    "切り抜きはやめて",
    "切り抜きやめて",
    "ここは切り抜かないで",
    "この部分は切り抜かないで",
    "do not clip",
    "don't clip",
    "no clipping",
)

_PAID_OR_RESTRICTED_AVAILABILITY = {
    "subscriber_only",
    "premium_only",
    "needs_auth",
    "private",
}


def _normalized(text: str | None) -> str:
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).casefold()


def _contains_any(text: str | None, phrases: tuple[str, ...]) -> str | None:
    normalized = _normalized(text)
    for phrase in phrases:
        if _normalized(phrase) in normalized:
            return phrase
    return None


def _decision(findings: list[PolicyFinding]) -> PolicyDecision:
    if any(item.decision is PolicyDecision.BLOCK for item in findings):
        return PolicyDecision.BLOCK
    if any(item.decision is PolicyDecision.REVIEW for item in findings):
        return PolicyDecision.REVIEW
    return PolicyDecision.ALLOW


def load_policy_rules(path: Path) -> list[ClipPolicyRule]:
    if not path.exists():
        return []

    rules: list[ClipPolicyRule] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
            rules.append(ClipPolicyRule.model_validate(payload))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"invalid clip policy rule at {path}:{line_number}: {exc}"
            ) from exc
    return rules


def append_policy_rule(path: Path, rule: ClipPolicyRule) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                rule.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )


def _rule_matches_source(rule: ClipPolicyRule, metadata: SourceMetadata) -> bool:
    if rule.source_id is not None and rule.source_id != metadata.external_id:
        return False
    if rule.channel_id is not None and rule.channel_id != metadata.channel_id:
        return False
    if rule.channel_name is not None:
        if _normalized(rule.channel_name) != _normalized(metadata.channel):
            return False
    return True


def _overlaps(
    start: float,
    end: float,
    rule_start: float | None,
    rule_end: float | None,
) -> bool:
    if rule_start is None or rule_end is None:
        return True
    return end > rule_start and start < rule_end


def evaluate_source_metadata(
    metadata: SourceMetadata,
    *,
    rules: list[ClipPolicyRule] | None = None,
    operation: PolicyOperation = PolicyOperation.LEARN,
) -> ClipPolicyEvaluation:
    findings: list[PolicyFinding] = []

    availability = _normalized(metadata.availability)
    if availability in _PAID_OR_RESTRICTED_AVAILABILITY:
        findings.append(
            PolicyFinding(
                decision=PolicyDecision.BLOCK,
                reason=(
                    "source availability indicates paid/restricted or non-public "
                    f"content: {metadata.availability}"
                ),
                source="source_metadata.availability",
            )
        )

    title_phrase = _contains_any(metadata.title, _EXPLICIT_NO_CLIP_PHRASES)
    if title_phrase is not None:
        findings.append(
            PolicyFinding(
                decision=PolicyDecision.BLOCK,
                reason=f"source title contains an explicit no-clipping instruction: {title_phrase}",
                source="source_metadata.title",
                evidence_text=metadata.title,
            )
        )

    description_phrase = _contains_any(
        metadata.description,
        _EXPLICIT_NO_CLIP_PHRASES,
    )
    if description_phrase is not None:
        findings.append(
            PolicyFinding(
                decision=PolicyDecision.BLOCK,
                reason=(
                    "source description contains an explicit no-clipping instruction: "
                    f"{description_phrase}"
                ),
                source="source_metadata.description",
                evidence_text=description_phrase,
            )
        )

    if metadata.live_status in {"is_live", "is_upcoming"}:
        findings.append(
            PolicyFinding(
                decision=PolicyDecision.REVIEW,
                reason=(
                    "source is still live/upcoming; official clip guidance requires "
                    "the stream to end and the archive to be public before posting"
                ),
                source="source_metadata.live_status",
                evidence_text=metadata.live_status,
            )
        )

    for rule in rules or []:
        if operation not in rule.operations or rule.is_range_rule:
            continue
        if not _rule_matches_source(rule, metadata):
            continue
        findings.append(
            PolicyFinding(
                decision=rule.decision,
                reason=rule.reason,
                rule_id=rule.id,
                source="policy_registry",
                evidence_url=rule.evidence_url,
                evidence_text=rule.evidence_text,
            )
        )

    return ClipPolicyEvaluation(
        decision=_decision(findings),
        findings=findings,
    )


def evaluate_source_ranges(
    metadata: SourceMetadata,
    *,
    ranges: list[tuple[float, float]],
    transcript: list[TranscriptSegment] | None = None,
    rules: list[ClipPolicyRule] | None = None,
    operation: PolicyOperation = PolicyOperation.LEARN,
    spoken_marker_margin_seconds: float = 120.0,
) -> ClipPolicyEvaluation:
    findings: list[PolicyFinding] = []

    for rule in rules or []:
        if operation not in rule.operations or not rule.is_range_rule:
            continue
        if not _rule_matches_source(rule, metadata):
            continue
        if any(
            _overlaps(start, end, rule.start, rule.end)
            for start, end in ranges
        ):
            findings.append(
                PolicyFinding(
                    decision=rule.decision,
                    reason=rule.reason,
                    rule_id=rule.id,
                    source="policy_registry",
                    start=rule.start,
                    end=rule.end,
                    evidence_url=rule.evidence_url,
                    evidence_text=rule.evidence_text,
                )
            )

    for segment in transcript or []:
        phrase = _contains_any(segment.text, _SPOKEN_RESTRICTION_PHRASES)
        if phrase is None:
            continue

        marker_start = max(0.0, segment.start - spoken_marker_margin_seconds)
        marker_end = segment.end + spoken_marker_margin_seconds
        if not any(
            _overlaps(start, end, marker_start, marker_end)
            for start, end in ranges
        ):
            continue

        findings.append(
            PolicyFinding(
                decision=PolicyDecision.REVIEW,
                reason=(
                    "source transcript contains a possible spoken no-clipping "
                    f"instruction near the selected range: {phrase}"
                ),
                source="source_transcript",
                start=marker_start,
                end=marker_end,
                evidence_text=segment.text,
            )
        )

    return ClipPolicyEvaluation(
        decision=_decision(findings),
        findings=findings,
    )


def combine_policy_evaluations(
    *evaluations: ClipPolicyEvaluation,
) -> ClipPolicyEvaluation:
    findings = [
        finding
        for evaluation in evaluations
        for finding in evaluation.findings
    ]
    return ClipPolicyEvaluation(
        decision=_decision(findings),
        findings=findings,
    )
