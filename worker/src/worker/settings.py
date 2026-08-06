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
        model_revision: The revision actually loaded. Discovered from the
            resolved snapshot or baked manifest at pipeline load — cached
            models offer no revision control, so this is reported, not
            pinned. `MODEL_REVISION` in the environment overrides discovery
            for layouts that carry no evidence.
        weights_path: Directory holding the diffusers layout, when weights are
            baked into the image or mounted from a network volume.
        model_cache_root: HuggingFace cache hub directory, where RunPod
            pre-stages a cached model. Used only when `weights_path` is absent,
            so one build serves every delivery mechanism.
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    model_id: str = "black-forest-labs/FLUX.1-dev"
    model_revision: str = Field(default="unknown", min_length=1)
    weights_path: Path = Path("/opt/weights")
    model_cache_root: Path = Path("/runpod-volume/huggingface-cache/hub")

    @property
    def model_version(self) -> str:
        """Return the identifier recorded on every generated image.

        Returns:
            Repository and loaded revision, e.g.
            `black-forest-labs/FLUX.1-dev@3de623f...`.
        """
        return f"{self.model_id}@{self.model_revision}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Returns:
        The cached `Settings` instance.
    """
    return Settings()
