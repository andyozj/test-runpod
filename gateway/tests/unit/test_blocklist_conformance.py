"""Both tiers must agree on the same blocklist.

The worker implements the same matching over the same contract file. Package
isolation forbids importing it, so this asserts the shared corpus produces the
same verdicts here — the only thing preventing a silent divergence where the
gateway blocks a prompt the worker allows, or the reverse.
"""

from __future__ import annotations

import pytest

from gateway.adapters.guardrails import BlocklistGuardrail, normalise, term_pattern

SYNTHETIC = {"test": ("zzqblockedqz",)}


@pytest.fixture
def guardrail() -> BlocklistGuardrail:
    return BlocklistGuardrail(terms=SYNTHETIC)


@pytest.mark.parametrize(
    "evasion",
    [
        "zzqblockedqz",
        "ZZQBLOCKEDQZ",
        "zzq blocked qz",
        "zzq-blocked-qz",
        "zzqbl0ck3dqz",
        "zzq​blocked​qz",
    ],
)
def test_normalisation_defeats_evasions(
    guardrail: BlocklistGuardrail, evasion: str
) -> None:
    assert guardrail.check(f"a painting of {evasion}").blocked


@pytest.mark.parametrize("innocent", ["gorgeous scenery", "music attracts", "scatter"])
def test_no_false_positives_across_word_boundaries(innocent: str) -> None:
    guardrail = BlocklistGuardrail(terms={"test": ("gore", "cat")})

    assert not guardrail.check(innocent).blocked


def test_contract_loads_and_terms_are_normalised() -> None:
    guardrail = BlocklistGuardrail.from_contract()

    assert "graphic_violence" in guardrail.terms
    assert all(
        term == normalise(term) for terms in guardrail.terms.values() for term in terms
    )


def test_verdict_names_the_matched_categories(
    guardrail: BlocklistGuardrail,
) -> None:
    verdict = guardrail.check("zzqblockedqz")

    assert verdict.categories == ("test",)
    assert verdict.reason


def test_term_pattern_is_separator_tolerant() -> None:
    assert term_pattern("cat").search("a c-a-t sat")
    assert not term_pattern("cat").search("music attracts")


def test_confusable_table_matches_the_worker() -> None:
    """The `!`→`i` mapping is where the two tiers actually diverged once."""
    guardrail = BlocklistGuardrail(terms={"test": ("zzqindigoqz",)})

    assert guardrail.check("zzq!nd!goqz").blocked
    assert normalise("g!re") == "gire"
