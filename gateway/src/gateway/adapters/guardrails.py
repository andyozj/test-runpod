"""Prompt blocklist, loaded from the shared contracts.

Both the terms (`contracts/blocklist.json`) and the normalisation tables
(`contracts/normalisation.json`) come from files the worker reads too. Package
isolation forbids sharing the code, so `contracts/guardrail-corpus.json` is run
by both tiers — without it they would silently diverge, and the gateway's block
rate would stop describing what actually happens.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from gateway.contracts import contract_path

_NORMALISATION_CONTRACT = contract_path("normalisation.json")


def _load_normalisation(
    path: Path | None = None,
) -> tuple[re.Pattern[str], re.Pattern[str], str, dict[int, str]]:
    """Build the normalisation tables from the shared contract.

    Args:
        path: Override for the contract location. Tests only.

    Returns:
        A tuple of (invisible-char pattern, one-or-more-separators pattern,
        the separator character class body used to build `term_pattern`'s
        zero-or-more form, and the confusable translation table).
    """
    data = json.loads((path or _NORMALISATION_CONTRACT).read_text())
    invisible = re.compile(
        "[" + "".join(re.escape(ch) for ch in data["invisible_chars"]) + "]"
    )
    separator_body = "".join(re.escape(ch) for ch in data["separator_chars"])
    if data["separator_includes_unicode_whitespace"]:
        separator_body = r"\s" + separator_body
    separators = re.compile(rf"[{separator_body}]+")
    confusables = str.maketrans(data["confusables"])
    return invisible, separators, separator_body, confusables


_INVISIBLE, _SEPARATORS, _SEPARATOR_BODY, _CONFUSABLES = _load_normalisation()
_SEPARATOR_RUN = rf"[{_SEPARATOR_BODY}]*"


@dataclass(frozen=True)
class Verdict:
    """The outcome of a prompt check.

    Attributes:
        blocked: Whether the request must not proceed.
        categories: Which categories matched.
        reason: Human-readable explanation, safe to log.
    """

    blocked: bool = False
    categories: tuple[str, ...] = ()
    reason: str | None = None


def normalise(text: str) -> str:
    """Reduce text to a form that defeats common evasions.

    Args:
        text: Raw input.

    Returns:
        The normalised form, used only for matching.

    Example:
        >>> normalise("G0RE")
        'gore'
    """
    text = _INVISIBLE.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(_CONFUSABLES)
    return _SEPARATORS.sub(" ", text).strip()


@lru_cache(maxsize=512)
def term_pattern(term: str) -> re.Pattern[str]:
    """Compile a term into a separator-tolerant, boundary-anchored pattern.

    Args:
        term: An already-normalised blocked term.

    Returns:
        The compiled pattern.

    Example:
        >>> bool(term_pattern("cat").search("a c-a-t sat"))
        True
        >>> bool(term_pattern("cat").search("music attracts"))
        False
    """
    body = _SEPARATOR_RUN.join(re.escape(char) for char in term)
    return re.compile(rf"(?<!\w){body}(?!\w)")


@dataclass
class BlocklistGuardrail:
    """Matches prompts against the categories in `contracts/blocklist.json`.

    Attributes:
        terms: Category name mapped to normalised terms.
    """

    terms: dict[str, tuple[str, ...]]

    @classmethod
    def from_contract(cls, path: Path | None = None) -> BlocklistGuardrail:
        """Load the shared contract.

        Args:
            path: Override for the contract location. Tests only.

        Returns:
            A guardrail bound to the contract's terms.
        """
        data = json.loads((path or contract_path("blocklist.json")).read_text())
        return cls(
            terms={
                name: tuple(normalise(term) for term in terms)
                for name, terms in data["categories"].items()
            }
        )

    def check(self, prompt: str) -> Verdict:
        """Match a prompt against every category.

        Args:
            prompt: The raw user-supplied text.

        Returns:
            A blocking verdict naming the matched categories, or an allow.
        """
        haystack = normalise(prompt)
        matched = tuple(
            sorted(
                category
                for category, terms in self.terms.items()
                if any(term and term_pattern(term).search(haystack) for term in terms)
            )
        )
        if not matched:
            return Verdict()
        return Verdict(
            blocked=True,
            categories=matched,
            reason="Prompt matched a blocked term.",
        )
