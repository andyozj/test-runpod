"""Content guardrails for prompts and generated images.

`diffusers` FLUX pipelines ship no `safety_checker`, so whatever is not
implemented here does not exist. See docs/specs/04-guardrails.md.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "blocklist.json"

Action = Literal["allow", "flag", "block"]

_SEVERITY: dict[Action, int] = {"allow": 0, "flag": 1, "block": 2}

# Characters used to break up a term without changing how it reads: zero-width
# spaces, joiners, and the soft hyphen.
_INVISIBLE = re.compile(r"[­​‌‍⁠﻿]")
_SEPARATOR_CHARS = r"[\s\-_.*+~/\\|]"
_SEPARATORS = re.compile(rf"{_SEPARATOR_CHARS}+")
_SEPARATOR_RUN = rf"{_SEPARATOR_CHARS}*"

_CONFUSABLES = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
    }
)


@dataclass(frozen=True)
class GuardrailVerdict:
    """The outcome of one guardrail check.

    Three actions rather than two: moderation has three honest answers, and a
    binary interface forces every unsure case to be collapsed at the moment of
    judgement, after which the information is gone. `flag` currently behaves as
    `allow` plus an audit record — no review queue consumes it yet.

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
            >>> GuardrailVerdict(action="flag").blocked
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


@dataclass
class ChainedPromptGuardrail:
    """Runs members in order and returns the most severe verdict.

    Attributes:
        members: The guardrails to apply.
    """

    members: list[PromptGuardrail]

    def check(self, prompt: str) -> GuardrailVerdict:
        """Apply every member and return the most severe outcome.

        A member raising is treated as a block: a safety control that disables
        itself when its dependency fails is worse than none, because the system
        still reports itself protected.

        Args:
            prompt: The raw user-supplied text.

        Returns:
            The most severe verdict produced by any member.
        """
        worst = GuardrailVerdict()
        for member in self.members:
            try:
                verdict = member.check(prompt)
            except Exception as exc:  # noqa: BLE001 - fail closed, deliberately
                return GuardrailVerdict(
                    action="block",
                    categories=("guardrail_error",),
                    reason=f"Guardrail failed: {type(exc).__name__}",
                )
            if _SEVERITY[verdict.action] > _SEVERITY[worst.action]:
                worst = verdict
        return worst
