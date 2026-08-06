"""Weight resolution and revision discovery across delivery mechanisms."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker import weights
from worker.settings import Settings

MODEL = "black-forest-labs/FLUX.1-dev"
STAGED = "3de623fc3c33e44ffbe2bad470d0f45bccf2eb21"
OTHER = "1111111111111111111111111111111111111111"


def _settings(weights_path: Path, cache_root: Path) -> Settings:
    return Settings(
        model_id=MODEL,
        weights_path=weights_path,
        model_cache_root=cache_root,
    )


def test_an_existing_weights_path_wins(tmp_path: Path) -> None:
    baked = tmp_path / "opt" / "weights"
    baked.mkdir(parents=True)
    weights.snapshot_dir(tmp_path / "cache", MODEL, STAGED).mkdir(parents=True)

    resolved = weights.resolve(_settings(baked, tmp_path / "cache"))

    assert resolved == baked


def test_falls_back_to_the_only_cached_snapshot(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cached = weights.snapshot_dir(cache_root, MODEL, STAGED)
    cached.mkdir(parents=True)

    resolved = weights.resolve(_settings(tmp_path / "absent", cache_root))

    assert resolved == cached


def test_refs_main_names_the_staged_snapshot(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    weights.snapshot_dir(cache_root, MODEL, OTHER).mkdir(parents=True)
    staged = weights.snapshot_dir(cache_root, MODEL, STAGED)
    staged.mkdir(parents=True)
    ref = cache_root / "models--black-forest-labs--FLUX.1-dev" / "refs" / "main"
    ref.parent.mkdir(parents=True)
    ref.write_text(STAGED)

    resolved = weights.resolve(_settings(tmp_path / "absent", cache_root))

    assert resolved == staged


def test_several_snapshots_without_a_ref_refuse_to_guess(tmp_path: Path) -> None:
    """Picking one arbitrarily would misattribute every generated image."""
    cache_root = tmp_path / "cache"
    weights.snapshot_dir(cache_root, MODEL, STAGED).mkdir(parents=True)
    weights.snapshot_dir(cache_root, MODEL, OTHER).mkdir(parents=True)

    with pytest.raises(weights.WeightsNotFoundError, match="Refusing to guess"):
        weights.resolve(_settings(tmp_path / "absent", cache_root))


def test_no_weights_anywhere_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(weights.WeightsNotFoundError, match="no model cache"):
        weights.resolve(_settings(tmp_path / "absent", tmp_path / "no-cache"))


def test_error_names_all_three_mechanisms(tmp_path: Path) -> None:
    with pytest.raises(weights.WeightsNotFoundError) as exc:
        weights.resolve(_settings(tmp_path / "absent", tmp_path / "no-cache"))

    message = str(exc.value)
    assert "WEIGHTS_PATH" in message
    assert "network volume" in message
    assert "Model field" in message


def test_revision_discovered_from_a_snapshot_path(tmp_path: Path) -> None:
    snapshot = weights.snapshot_dir(tmp_path, MODEL, STAGED)
    snapshot.mkdir(parents=True)

    assert weights.discovered_revision(snapshot) == STAGED


def test_revision_discovered_from_a_baked_manifest(tmp_path: Path) -> None:
    baked = tmp_path / "opt" / "weights"
    baked.mkdir(parents=True)
    (baked / "MANIFEST.json").write_text(json.dumps({"revision": STAGED}))

    assert weights.discovered_revision(baked) == STAGED


def test_no_evidence_yields_no_revision(tmp_path: Path) -> None:
    bare = tmp_path / "weights"
    bare.mkdir()

    assert weights.discovered_revision(bare) is None


def test_snapshot_dir_matches_the_huggingface_layout() -> None:
    path = weights.snapshot_dir(Path("/c"), MODEL, STAGED)

    assert path.parts[-3:] == (
        "models--black-forest-labs--FLUX.1-dev",
        "snapshots",
        STAGED,
    )
