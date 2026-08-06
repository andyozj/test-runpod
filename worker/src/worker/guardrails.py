"""Content guardrails for prompts and generated images.

`diffusers` FLUX pipelines ship no `safety_checker`, so whatever is not
implemented here does not exist.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from worker.contracts import contract_path

_CONTRACT = contract_path("blocklist.json")
_NORMALISATION_CONTRACT = contract_path("normalisation.json")

Action = Literal["allow", "block"]


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
class GuardrailVerdict:
    """The outcome of one guardrail check.

    Attributes:
        action: What the caller should do.
        categories: Which categories matched.
        reason: Human-readable explanation, safe to log.
        score: Confidence, where the implementation produces one.
    """

    action: Action = "allow"
    categories: tuple[str, ...] = ()
    reason: str | None = None
    score: float | None = None

    @property
    def blocked(self) -> bool:
        """Whether the request must not proceed.

        Returns:
            True only for a `block` verdict.

        Example:
            >>> GuardrailVerdict().blocked
            False
            >>> GuardrailVerdict(action="block").blocked
            True
        """
        return self.action == "block"


@runtime_checkable
class PromptGuardrail(Protocol):
    """A check applied to a prompt before any GPU time is spent."""

    def check(self, prompt: str) -> GuardrailVerdict:
        """Classify a prompt.

        Args:
            prompt: The raw user-supplied text.

        Returns:
            The verdict for this prompt.
        """
        ...


@runtime_checkable
class ImageGuardrail(Protocol):
    """A check applied to generated image bytes before they are returned."""

    def check(self, image: bytes) -> GuardrailVerdict:
        """Classify a generated image.

        Args:
            image: The encoded image bytes.

        Returns:
            The verdict for this image.
        """
        ...


def normalise(text: str) -> str:
    """Reduce text to a form that defeats common blocklist evasions.

    Applies NFKC normalisation, strips combining marks and invisible
    characters, casefolds, maps confusable digits and symbols to letters, and
    collapses separator runs to single spaces.

    Args:
        text: Raw input.

    Returns:
        The normalised form, used only for matching and never returned to a
        caller or used for generation.

    Separators become spaces rather than disappearing; bridging them is
    `term_pattern`'s job, so that stripping them here cannot glue
    unrelated words together.

    Example:
        >>> normalise("GŌRE")
        'gore'
        >>> normalise("g0re")
        'gore'
        >>> normalise("G-O-R-E")
        'g o r e'
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

    Normalisation collapses separators to single spaces, which defeats casing
    and diacritics but not `z-z-q-b-l-o-c-k-e-d`, where the term is split
    across them. Stripping separators globally would catch that but glue
    adjacent words together, so `music attracts` would match `cat`.

    Allowing optional separators *between the term's own characters*, while
    still requiring word boundaries at each end, catches the split form without
    inventing matches across unrelated words.

    Args:
        term: An already-normalised blocked term.

    Returns:
        The compiled pattern.

    Example:
        >>> bool(term_pattern("cat").search("a cat sitting"))
        True
        >>> bool(term_pattern("cat").search("music attracts"))
        False
        >>> bool(term_pattern("cat").search("c-a-t"))
        True
    """
    body = _SEPARATOR_RUN.join(re.escape(char) for char in term)
    return re.compile(rf"(?<!\w){body}(?!\w)")


def load_terms(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Load blocked terms grouped by category from the shared contract.

    Args:
        path: Override for the contract location. Tests only.

    Returns:
        Category name mapped to its normalised terms.
    """
    data = json.loads((path or _CONTRACT).read_text())
    categories: dict[str, list[str]] = data["categories"]
    return {
        name: tuple(normalise(term) for term in terms)
        for name, terms in categories.items()
    }


@dataclass
class BlocklistPromptGuardrail:
    """Normalised term matching against a curated list.

    Not a content classifier and does not pretend to be. It stops naive cases
    and proves the hook is wired through both tiers.

    Attributes:
        terms: Category name mapped to normalised terms.
    """

    terms: dict[str, tuple[str, ...]] = field(default_factory=load_terms)

    def check(self, prompt: str) -> GuardrailVerdict:
        """Match a prompt against every category.

        Matching is on word boundaries over the normalised text, so a blocked
        term does not fire on an innocent word that merely contains it — the
        false positive that makes naive blocklists unusable.

        Args:
            prompt: The raw user-supplied text.

        Returns:
            A `block` verdict naming the matched categories, or `allow`.

        Example:
            >>> g = BlocklistPromptGuardrail(terms={"test": ("gore",)})
            >>> g.check("a study in gore").action
            'block'
            >>> g.check("gorgeous scenery").action
            'allow'
        """
        haystack = normalise(prompt)
        matched = [
            category
            for category, terms in self.terms.items()
            if any(_contains_term(haystack, term) for term in terms)
        ]
        if not matched:
            return GuardrailVerdict()
        return GuardrailVerdict(
            action="block",
            categories=tuple(sorted(matched)),
            reason="Prompt matched a blocked term.",
        )


def _contains_term(haystack: str, term: str) -> bool:
    if not term:
        return False
    return term_pattern(term).search(haystack) is not None


@dataclass(frozen=True)
class NoopImageGuardrail:
    """Registered no-op that exercises the post-generation hook.

    A registered no-op is worth more than an unimplemented interface: it proves
    the hook is called at the right point with the right data, so adding a real
    classifier changes one binding rather than discovering the extension point
    was the wrong shape.
    """

    def check(self, image: bytes) -> GuardrailVerdict:
        """Allow every image.

        Args:
            image: The encoded image bytes.

        Returns:
            Always an `allow` verdict.
        """
        return GuardrailVerdict()
