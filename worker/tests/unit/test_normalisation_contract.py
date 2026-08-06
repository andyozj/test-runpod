"""The worker's normalisation tables must match contracts/normalisation.json.

Package isolation forbids sharing the loader with the gateway, so this asserts
the worker's loaded regexes and translation table actually behave the way the
contract file declares — the only thing preventing the two tiers from
silently diverging on what counts as an evasion.
"""

from __future__ import annotations

import json

from worker.contracts import contract_path
from worker.guardrails import _CONFUSABLES, _INVISIBLE, _SEPARATORS


def _contract() -> dict[str, object]:
    return json.loads(contract_path("normalisation.json").read_text())


def test_every_contract_invisible_char_is_stripped() -> None:
    data = _contract()
    for ch in data["invisible_chars"]:  # type: ignore[union-attr]
        assert _INVISIBLE.fullmatch(ch)


def test_every_contract_separator_char_is_collapsed() -> None:
    data = _contract()
    for ch in data["separator_chars"]:  # type: ignore[union-attr]
        assert _SEPARATORS.fullmatch(ch)
    if data["separator_includes_unicode_whitespace"]:
        assert _SEPARATORS.fullmatch(" ")
        assert _SEPARATORS.fullmatch("\t")


def test_every_contract_confusable_maps_as_declared() -> None:
    data = _contract()
    for key, value in data["confusables"].items():  # type: ignore[union-attr]
        assert key.translate(_CONFUSABLES) == value
    assert "x".translate(_CONFUSABLES) == "x"
