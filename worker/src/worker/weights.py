"""Locate model weights across all three delivery mechanisms.

The worker is identical whether weights are baked into the image, mounted from
a network volume, or pre-staged by RunPod's model cache. Only where they are
found differs, and that is resolved here rather than in the pipeline.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from worker.settings import Settings

logger = structlog.get_logger()


class WeightsNotFoundError(RuntimeError):
    """Raised when no usable weights exist at startup.

    Failing fast matters more than it looks: without it a misconfigured mount
    falls through to downloading ~33GB from HuggingFace on every cold start,
    which presents as "slow" rather than "broken" and can survive an entire
    benchmark run undetected.
    """


def snapshot_dir(cache_root: Path, model_id: str, revision: str) -> Path:
    """Return the cache path for one model revision.

    Args:
        cache_root: The HuggingFace cache hub directory.
        model_id: Repository in `org/name` form.
        revision: Commit SHA.

    Returns:
        The snapshot directory, which may not exist.

    Example:
        >>> snapshot_dir(Path("/c"), "black-forest-labs/FLUX.1-dev", "abc123")
        PosixPath('/c/models--black-forest-labs--FLUX.1-dev/snapshots/abc123')
    """
    org, _, name = model_id.partition("/")
    return cache_root / f"models--{org}--{name}" / "snapshots" / revision


def resolve(settings: Settings) -> Path:
    """Find the weights directory, trying each delivery mechanism in order.

    Order is deliberate: an explicitly configured path always wins, because a
    deployment that sets one has made a decision the cache must not override.

    Args:
        settings: Runtime configuration.

    Returns:
        A directory containing the diffusers layout.

    Raises:
        WeightsNotFoundError: Nothing usable was found, or the cache holds a
            revision other than the pinned one.
    """
    if settings.weights_path.exists():
        logger.info("weights_resolved", source="path", path=str(settings.weights_path))
        return settings.weights_path

    cache_root = settings.model_cache_root
    if not cache_root.exists():
        msg = (
            f"No weights at {settings.weights_path} and no model cache at "
            f"{cache_root}. For the baked image check WEIGHTS_PATH; for a "
            f"network volume check it is mounted and co-located with the "
            f"endpoint; for cached models check the endpoint's Model field."
        )
        raise WeightsNotFoundError(msg)

    pinned = snapshot_dir(cache_root, settings.model_id, settings.model_revision)
    if pinned.exists():
        logger.info("weights_resolved", source="cache", path=str(pinned))
        return pinned

    raise WeightsNotFoundError(_mismatch_message(cache_root, settings, pinned))


def _mismatch_message(cache_root: Path, settings: Settings, pinned: Path) -> str:
    """Explain what the cache holds instead of the pinned revision.

    The HuggingFace cache layout allows several snapshots to coexist. Picking
    an arbitrary one — as the platform's own example does by sorting and taking
    the first — would run a model the response then misreports as the pinned
    revision, and would quietly invalidate any comparison between endpoints.
    Refusing to start is the correct behaviour.

    Args:
        cache_root: The cache hub directory.
        settings: Runtime configuration.
        pinned: The snapshot path that was expected.

    Returns:
        A message naming what is present.
    """
    org, _, name = settings.model_id.partition("/")
    snapshots = cache_root / f"models--{org}--{name}" / "snapshots"
    present = (
        sorted(p.name for p in snapshots.iterdir() if p.is_dir())
        if snapshots.is_dir()
        else []
    )
    return (
        f"Model cache does not contain the pinned revision "
        f"{settings.model_revision} at {pinned}. "
        f"Present: {', '.join(present) or 'nothing'}. "
        "Refusing to start rather than run a different model: the response "
        "reports model_version from the pinned revision, so a mismatch would "
        "misattribute every image. Update contracts/model-revision.txt or "
        "re-stage the endpoint's cached model."
    )
