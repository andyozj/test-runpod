"""Matcher unit tests over synthetic terms.

These do not prevent divergence from the worker and never did: the terms are
invented here, so the worker never sees them. Cross-tier agreement is asserted
by `test_guardrail_corpus.py` (the shared corpus, run by both tiers) and
`test_normalisation_contract.py` (the shared normalisation tables). What is
left here is what those cannot cover — the matcher's own behaviour on terms
chosen to isolate one rule at a time.
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


def test_a_confusable_is_applied_inside_a_term() -> None:
    """The `!`→`i` mapping is where the two tiers actually diverged once."""
    guardrail = BlocklistGuardrail(terms={"test": ("zzqindigoqz",)})

    assert guardrail.check("zzq!nd!goqz").blocked
    assert normalise("g!re") == "gire"
