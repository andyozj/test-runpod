"""Environment-backed configuration. The only module that reads the environment."""

from __future__ import annotations

import hmac
from functools import lru_cache
from hashlib import sha256

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the gateway.

    Attributes:
        runpod_api_key: Credential for the serverless endpoint.
        runpod_endpoint_id: The endpoint to call.
        gateway_api_keys: Caller credentials as `key_id:secret` pairs.
        reconcile_interval_s: Tick interval while work is outstanding.
        reconcile_idle_interval_s: Tick interval when nothing is unresolved.
        reconcile_batch: Jobs claimed per tick.
        job_deadline_s: Age at which an unresolved job is timed out. Must stay
            above the endpoint execution timeout, or jobs still running
            normally would be cut off and their results discarded.
        max_queue_wait_s: Estimated wait above which submissions are shed.
        avg_job_s: Expected job duration; replaced by a measured p50.
        version: Reported by the health endpoints.
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    gateway_api_keys: str = "demo:local-development-key"
    reconcile_interval_s: float = 2.0
    reconcile_idle_interval_s: float = 10.0
    reconcile_batch: int = 50
    job_deadline_s: int = 600
    max_queue_wait_s: float = 120.0
    avg_job_s: float = 22.0
    version: str = Field(default="0.1.0")

    def key_digests(self) -> dict[str, bytes]:
        """Return caller key digests, hashed once at startup.

        No table and no migration: this is a small fixed set of callers, and a
        database round trip per request buys nothing.

        Returns:
            Digest mapped to the `api_key_id` it identifies.
        """
        digests: dict[str, bytes] = {}
        for pair in self.gateway_api_keys.split(","):
            if ":" not in pair:
                continue
            key_id, _, secret = pair.strip().partition(":")
            digests[key_id] = sha256(secret.encode()).digest()
        return digests

    def resolve_key(self, presented: str) -> str | None:
        """Identify the caller behind a presented key.

        Comparison is constant-time. Because the stored value is a fixed-length
        digest the length-leak argument does not apply; the reason is the
        prefix leak — `==` short-circuits on the first differing byte, so
        comparison time correlates with how much of a guess is correct.

        Args:
            presented: The bearer token supplied by the caller.

        Returns:
            The `api_key_id`, or None if no key matches.
        """
        candidate = sha256(presented.encode()).digest()
        for key_id, digest in self.key_digests().items():
            if hmac.compare_digest(candidate, digest):
                return key_id
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Returns:
        The cached `Settings` instance.
    """
    return Settings()
