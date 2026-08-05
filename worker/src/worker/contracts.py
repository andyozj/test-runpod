"""Locate shared contract files at both repo and image depths."""

from __future__ import annotations

from pathlib import Path


def contract_path(name: str) -> Path:
    """Return the path to a file in the nearest `contracts/` directory.

    Searches upward because the package sits at different depths in the repo
    (`worker/src/worker/`) and in the image (`/app/src/worker/`, contracts at
    `/app/contracts/`), so a fixed `parents[n]` resolves correctly in one and
    breaks in the other.

    Args:
        name: Filename inside the contracts directory.

    Returns:
        The resolved path.

    Raises:
        FileNotFoundError: No parent directory contains `contracts/{name}`.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / name
        if candidate.exists():
            return candidate
    msg = f"contracts/{name} not found in any parent directory"
    raise FileNotFoundError(msg)
