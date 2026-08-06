"""Settings: fail-closed credentials, log hygiene on malformed pairs."""

from __future__ import annotations

import pytest
import structlog.testing
from pydantic import ValidationError

from gateway.core.service import JobService
from gateway.settings import Settings

# Every setting the service also defaults. An unconfigured gateway and a
# bare `JobService()` must agree, or "the default" means two different things
# depending on which one you read.
SERVICE_DEFAULTED = [
    "job_deadline_s",
    "max_queue_wait_s",
    "avg_job_s",
    "submit_grace_s",
    "health_max_age_s",
    "max_active_jobs_per_key",
]


@pytest.mark.parametrize("name", SERVICE_DEFAULTED)
def test_settings_default_to_the_service_constants(name: str) -> None:
    settings = Settings(gateway_api_keys="demo:secret")

    assert getattr(settings, name) == JobService.__dataclass_fields__[name].default


def test_missing_gateway_api_keys_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_empty_gateway_api_keys_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)

    with pytest.raises(ValidationError):
        Settings(gateway_api_keys="")


def test_whitespace_only_gateway_api_keys_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)

    with pytest.raises(ValidationError):
        Settings(gateway_api_keys="   ")


def test_no_dev_credential_is_accepted_by_default() -> None:
    """The published dev key must not resolve unless explicitly configured."""
    settings = Settings(gateway_api_keys="prod:real-secret")

    assert settings.resolve_key("local-development-key") is None


def test_malformed_pair_is_logged_without_the_raw_value() -> None:
    settings = Settings(gateway_api_keys="demo:secret,not-a-pair-with-a-super-secret")

    with structlog.testing.capture_logs() as logs:
        settings.key_digests()

    warnings = [entry for entry in logs if entry["event"] == "malformed_api_key_pair"]
    assert len(warnings) == 1
    assert warnings[0]["index"] == 1
    rendered = repr(warnings[0])
    assert "not-a-pair-with-a-super-secret" not in rendered


def test_malformed_pair_is_skipped_but_valid_ones_still_resolve() -> None:
    settings = Settings(gateway_api_keys="bad-pair,demo:secret-key")

    assert settings.resolve_key("secret-key") == "demo"
