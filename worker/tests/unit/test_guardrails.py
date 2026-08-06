"""Blocklist normalisation, false positives and verdict semantics."""

from __future__ import annotations

import pytest

from worker.guardrails import (
    BlocklistPromptGuardrail,
    GuardrailVerdict,
    NoopImageGuardrail,
    load_terms,
    normalise,
)

SYNTHETIC = {"test": ("zzqblockedqz",)}


@pytest.fixture
def guardrail() -> BlocklistPromptGuardrail:
    return BlocklistPromptGuardrail(terms=SYNTHETIC)


@pytest.mark.parametrize(
    "evasion",
    [
        "zzqblockedqz",
        "ZZQBLOCKEDQZ",
        "ZzQbLoCkEdQz",
        "zzq blocked qz",
        "zzq-blocked-qz",
        "zzq_blocked_qz",
        "zzq.blocked.qz",
        "zzq*blocked*qz",
        "zzqbl0ckedqz",
        "zzqbl0ck3dqz",
        "zzqblöckedqz",
        "zzq​blocked​qz",
        "zzq­blocked­qz",
        "  zzqblockedqz  ",
    ],
)
def test_normalisation_defeats_evasions(
    guardrail: BlocklistPromptGuardrail, evasion: str
) -> None:
    assert guardrail.check(f"a painting of {evasion}").action == "block"


@pytest.mark.parametrize(
    "innocent",
    ["gorgeous scenery", "categorically fine", "a concatenation of ideas", "scatter"],
)
def test_word_boundaries_prevent_false_positives(innocent: str) -> None:
    guardrail = BlocklistPromptGuardrail(terms={"test": ("gore", "cat")})

    assert guardrail.check(innocent).action == "allow"


def test_separator_tolerance_does_not_match_across_words() -> None:
    """A split term must match; letters spanning unrelated words must not."""
    guardrail = BlocklistPromptGuardrail(terms={"test": ("cat",)})

    assert guardrail.check("a cat sitting").action == "block"
    assert guardrail.check("music attracts").action == "allow"


def test_blocked_verdict_names_the_category(
    guardrail: BlocklistPromptGuardrail,
) -> None:
    verdict = guardrail.check("zzqblockedqz")

    assert verdict.categories == ("test",)
    assert verdict.reason is not None


def test_contract_terms_load_and_are_normalised() -> None:
    terms = load_terms()

    assert "graphic_violence" in terms
    assert all(term == normalise(term) for group in terms.values() for term in group)


def test_noop_image_guardrail_allows() -> None:
    assert NoopImageGuardrail().check(b"\x89PNG").action == "allow"


def test_allow_is_not_blocked() -> None:
    assert GuardrailVerdict().blocked is False
    assert GuardrailVerdict(action="block").blocked is True
