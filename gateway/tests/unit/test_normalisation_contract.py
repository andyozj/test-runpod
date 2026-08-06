"""The gateway's normalisation tables must come from contracts/normalisation.json.

Package isolation forbids sharing the loader with the worker, so this asserts
the gateway's loaded regexes and translation table behave the way the contract
file declares, and that they are read from the file rather than transcribed
into the module — a transcription drifts silently, and the two tiers then
disagree about what counts as an evasion.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.adapters.guardrails import (
    _CONFUSABLES,
    _INVISIBLE,
    _SEPARATORS,
    _load_normalisation,
)
from gateway.contracts import contract_path


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


def test_the_tables_are_read_from_the_file(tmp_path: Path) -> None:
    """A transcribed copy would ignore this file entirely and still pass above."""
    path = tmp_path / "normalisation.json"
    path.write_text(
        json.dumps(
            {
                "invisible_chars": ["Q"],
                "separator_chars": ["§"],
                "separator_includes_unicode_whitespace": False,
                "confusables": {"9": "n"},
            }
        )
    )

    invisible, separators, separator_body, confusables = _load_normalisation(path)

    assert invisible.fullmatch("Q")
    assert separators.fullmatch("§")
    assert not separators.fullmatch(" ")
    assert "9".translate(confusables) == "n"
    assert separator_body == "§"
