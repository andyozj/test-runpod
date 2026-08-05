"""Weight resolution across baked, volume and cached delivery."""

from __future__ import annotations

from pathlib import Path

import pytest

from worker import weights
from worker.settings import Settings

MODEL = "black-forest-labs/FLUX.1-dev"
PINNED = "0ef5fff789c832c5c7f4e127f94c8b54bbcced44"
OTHER = "1111111111111111111111111111111111111111"


def _settings(weights_path: Path, cache_root: Path) -> Settings:
    return Settings(
        model_id=MODEL,
        model_revision=PINNED,
        weights_path=weights_path,
        model_cache_root=cache_root,
    )


def test_an_existing_weights_path_wins(tmp_path: Path) -> None:
    baked = tmp_path / "opt" / "weights"
    baked.mkdir(parents=True)
    cached = weights.snapshot_dir(tmp_path / "cache", MODEL, PINNED)
    cached.mkdir(parents=True)

    resolved = weights.resolve(_settings(baked, tmp_path / "cache"))

    assert resolved == baked


def test_falls_back_to_the_cached_snapshot(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cached = weights.snapshot_dir(cache_root, MODEL, PINNED)
    cached.mkdir(parents=True)

    resolved = weights.resolve(_settings(tmp_path / "absent", cache_root))

    assert resolved == cached


def test_no_weights_anywhere_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(weights.WeightsNotFoundError, match="no model cache"):
        weights.resolve(_settings(tmp_path / "absent", tmp_path / "no-cache"))


def test_cache_holding_a_different_revision_refuses_to_start(
    tmp_path: Path,
) -> None:
    """The platform's own example sorts snapshots and takes the first.

    That would run a model the response then reports as the pinned revision,
    misattributing every image and invalidating any comparison between
    endpoints. Refusing is the correct behaviour.
    """
    cache_root = tmp_path / "cache"
    weights.snapshot_dir(cache_root, MODEL, OTHER).mkdir(parents=True)

    with pytest.raises(weights.WeightsNotFoundError) as exc:
        weights.resolve(_settings(tmp_path / "absent", cache_root))

    assert PINNED in str(exc.value)
    assert OTHER in str(exc.value)


def test_error_names_all_three_mechanisms(tmp_path: Path) -> None:
    with pytest.raises(weights.WeightsNotFoundError) as exc:
        weights.resolve(_settings(tmp_path / "absent", tmp_path / "no-cache"))

    message = str(exc.value)
    assert "WEIGHTS_PATH" in message
    assert "network volume" in message
    assert "Model field" in message


def test_snapshot_dir_matches_the_huggingface_layout() -> None:
    path = weights.snapshot_dir(Path("/c"), MODEL, PINNED)

    assert path.parts[-3:] == (
        "models--black-forest-labs--FLUX.1-dev",
        "snapshots",
        PINNED,
    )
