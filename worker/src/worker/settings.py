"""Environment-backed configuration. The only module that reads the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the worker.

    Attributes:
        model_id: HuggingFace repository the weights came from.
        model_revision: Pinned commit SHA, recorded on every result.
        weights_path: Directory holding the diffusers layout, when weights are
            baked into the image or mounted from a network volume.
        model_cache_root: HuggingFace cache hub directory, where RunPod
            pre-stages a cached model. Used only when `weights_path` is absent,
            so one build serves all three delivery mechanisms.
        storage_enabled: When true, upload the image and return a key instead
            of inline base64.
        log_level: Root log level.
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    model_id: str = "black-forest-labs/FLUX.1-dev"
    model_revision: str = Field(default="main", min_length=1)
    weights_path: Path = Path("/opt/weights")
    model_cache_root: Path = Path("/runpod-volume/huggingface-cache/hub")
    storage_enabled: bool = False
    log_level: str = "INFO"

    @property
    def model_version(self) -> str:
        """Return the identifier recorded on every generated image.

        Returns:
            Repository and pinned revision, e.g.
            `black-forest-labs/FLUX.1-dev@0ef5fff`.
        """
        return f"{self.model_id}@{self.model_revision}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Returns:
        The cached `Settings` instance.
    """
    return Settings()
