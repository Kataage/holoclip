from holoclip.examples import HumanClipExample
from holoclip.structure_validation import (
    StructureWarningCode,
    validate_editorial_structure,
)


def structured_example(*, payoff: str, traits: list[str]) -> HumanClipExample:
    return HumanClipExample(
        id="structure",
        clip_url="https://example.invalid/clip",
        clip_title="clip",
        setup="話題を提示する",
        development="理由について話を広げる",
        payoff=payoff,
        why_selected="視聴者が続きを知りたくなる構造",
        traits=traits,
    )


def test_resolved_setup_payoff_is_valid() -> None:
    example = structured_example(
        payoff="最後に理由を明かして話が回収される",
        traits=["setup_payoff", "reveal"],
    )

    result = validate_editorial_structure(example)

    assert result.warnings == []


def test_unresolved_payoff_conflicts_with_setup_payoff_trait() -> None:
    example = structured_example(
        payoff="The actual reason is deliberately withheld and not revealed.",
        traits=["setup_payoff", "callback"],
    )

    result = validate_editorial_structure(example)

    assert [warning.code for warning in result.warnings] == [
        StructureWarningCode.UNRESOLVED_SETUP_PAYOFF
    ]


def test_incomplete_structure_is_reported_without_mutating_example() -> None:
    example = HumanClipExample(
        id="incomplete",
        clip_url="https://example.invalid/incomplete",
        clip_title="incomplete",
        setup="setup only",
    )
    before = example.model_dump(mode="json")

    result = validate_editorial_structure(example)

    assert result.warnings
    assert result.warnings[0].code is StructureWarningCode.INCOMPLETE
    assert example.model_dump(mode="json") == before
