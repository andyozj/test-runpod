"""Locate model weights and discover which revision was actually staged.

The worker is identical whether weights are baked into the image or
pre-staged by RunPod's model cache. Only where they are found differs, and
that is resolved here rather than in the pipeline.

The revision is discovered, not pinned: cached models are staged by the
platform from the console's Model field, which offers no revision control —
so the worker reports the revision it actually loaded rather than refusing
over a value nobody can set.
"""

from __future__ import annotations

import json
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


def discovered_revision(path: Path) -> str | None:
    """Return the revision the resolved directory actually holds.

    Cache snapshots are named by commit SHA; baked images carry the
    `MANIFEST.json` that `fetch_weights.py` writes at build time.

    Args:
        path: The resolved weights directory.

    Returns:
        The revision, or None when the layout carries no evidence.
    """
    if path.parent.name == "snapshots":
        return path.name
    manifest = path / "MANIFEST.json"
    if manifest.exists():
        rev = json.loads(manifest.read_text()).get("revision")
        return str(rev) if rev else None
    return None


def resolve(settings: Settings) -> Path:
    """Find the weights directory, trying each delivery mechanism in order.

    Order is deliberate: an explicitly configured path always wins, because a
    deployment that sets one has made a decision the cache must not override.

    In the cache, the staged snapshot is identified through `refs/main` when
    present, or by being the only snapshot. Several snapshots with no ref is
    the one case that still refuses: picking one arbitrarily — as the
    platform's own example does by sorting and taking the first — would run a
    model the response then misattributes.

    Args:
        settings: Runtime configuration.

    Returns:
        A directory containing the diffusers layout.

    Raises:
        WeightsNotFoundError: Nothing usable was found, or the cache holds
            several snapshots with no ref naming the staged one.
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

    org, _, name = settings.model_id.partition("/")
    repo_dir = cache_root / f"models--{org}--{name}"
    snapshots = repo_dir / "snapshots"
    present = (
        sorted(p for p in snapshots.iterdir() if p.is_dir())
        if snapshots.is_dir()
        else []
    )
    if not present:
        msg = (
            f"Model cache at {cache_root} holds no snapshot of "
            f"{settings.model_id}. Check the endpoint's Model field."
        )
        raise WeightsNotFoundError(msg)

    ref = repo_dir / "refs" / "main"
    if ref.exists():
        staged = snapshots / ref.read_text().strip()
        if staged.is_dir():
            logger.info("weights_resolved", source="cache-ref", path=str(staged))
            return staged

    if len(present) == 1:
        logger.info("weights_resolved", source="cache", path=str(present[0]))
        return present[0]

    msg = (
        f"Model cache holds {len(present)} snapshots of {settings.model_id} "
        f"({', '.join(p.name[:12] for p in present)}) and no ref names the "
        "staged one. Refusing to guess: an arbitrary pick would run a model "
        "the response then misattributes. Re-stage the endpoint's cached "
        "model, or set WEIGHTS_PATH to the intended snapshot."
    )
    raise WeightsNotFoundError(msg)
