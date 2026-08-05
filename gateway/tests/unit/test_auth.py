"""API key resolution."""

from __future__ import annotations

from gateway.settings import Settings


def test_valid_key_resolves_to_its_id(settings: Settings) -> None:
    assert settings.resolve_key("secret-key") == "demo"
    assert settings.resolve_key("other-key") == "other"


def test_unknown_key_resolves_to_none(settings: Settings) -> None:
    assert settings.resolve_key("wrong") is None
    assert settings.resolve_key("") is None


def test_prefix_of_a_valid_key_is_rejected(settings: Settings) -> None:
    assert settings.resolve_key("secret-ke") is None
    assert settings.resolve_key("secret-keyy") is None
