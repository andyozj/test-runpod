"""Runs the shared guardrail corpus against the worker's own guardrail.

Package isolation forbids sharing the guardrail implementation with the
gateway, so this corpus is the only thing preventing the two tiers from
silently agreeing on some cases and diverging on others. Dropping a
confusable or invisible-char case from the corpus is a test-suite change,
not a contract change, so it cannot mask a real divergence.
"""

from __future__ import annotations

import json

import pytest

from worker.contracts import contract_path
from worker.guardrails import BlocklistPromptGuardrail

_CORPUS = json.loads(contract_path("guardrail-corpus.json").read_text())["cases"]
_GUARDRAIL = BlocklistPromptGuardrail()


@pytest.mark.parametrize(
    "case",
    _CORPUS,
    ids=[f"{i}-{case['note']}" for i, case in enumerate(_CORPUS)],
)
def test_corpus_case_matches_expected_verdict(case: dict[str, object]) -> None:
    verdict = _GUARDRAIL.check(str(case["input"]))

    assert verdict.blocked is case["expect_blocked"]
