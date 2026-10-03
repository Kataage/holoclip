from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from holoclip.examples import HumanClipExample


class StructureWarningCode(StrEnum):
    INCOMPLETE = "structure_incomplete"
    UNRESOLVED_SETUP_PAYOFF = "unresolved_setup_payoff"


class StructureWarning(BaseModel):
    code: StructureWarningCode
    message: str


class StructureValidationResult(BaseModel):
    warnings: list[StructureWarning] = Field(default_factory=list)


_UNRESOLVED_PAYOFF_MARKERS = (
    "withheld",
    "not revealed",
    "never revealed",
    "left unanswered",
    "unresolved",
    "not explained",
    "deliberately withheld",
    "明かされない",
    "明かさない",
    "明かさず",
    "伏せた",
    "伏せて",
    "未回収",
    "説明されない",
    "語られない",
)


def validate_editorial_structure(
    example: HumanClipExample,
) -> StructureValidationResult:
    warnings: list[StructureWarning] = []

    required = {
        "setup": example.setup,
        "development": example.development,
        "payoff": example.payoff,
        "why_selected": example.why_selected,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        warnings.append(
            StructureWarning(
                code=StructureWarningCode.INCOMPLETE,
                message=(
                    "editorial structure is incomplete: "
                    + ", ".join(missing)
                ),
            )
        )

    if "setup_payoff" in example.traits and example.payoff.strip():
        normalized_payoff = " ".join(example.payoff.lower().split())
        if any(
            marker in normalized_payoff
            for marker in _UNRESOLVED_PAYOFF_MARKERS
        ):
            warnings.append(
                StructureWarning(
                    code=StructureWarningCode.UNRESOLVED_SETUP_PAYOFF,
                    message=(
                        "trait setup_payoff conflicts with a payoff description "
                        "that explicitly says the promised information is unresolved"
                    ),
                )
            )

    return StructureValidationResult(warnings=warnings)
