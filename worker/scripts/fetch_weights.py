"""Download FLUX.1-dev weights in the diffusers layout only.

The repository ships the sharded diffusers layout *and* the standalone
`flux1-dev.safetensors` (23.8GB) plus `ae.safetensors`. A naive
`snapshot_download` pulls both — roughly 56GB instead of 33GB, for weights that
are already present in another format.

`--check` verifies the filter against the real manifest using
`list_repo_files`, which downloads nothing. That makes both build-blockers
provable before a GPU or credits exist.

Usage:
    python scripts/fetch_weights.py --check
    python scripts/fetch_weights.py --dest /opt/weights
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

REPO_ID = "black-forest-labs/FLUX.1-dev"

# Single-file duplicates of weights already present in the diffusers layout.
IGNORE_PATTERNS = [
    "flux1-dev.safetensors",
    "ae.safetensors",
    "*.gguf",
    "*.onnx",
    "*.msgpack",
    "*.pt",
]

# Every prefix the diffusers layout needs. Anything outside these is either a
# duplicate or documentation.
REQUIRED_PREFIXES = (
    "model_index.json",
    "scheduler/",
    "text_encoder/",
    "text_encoder_2/",
    "tokenizer/",
    "tokenizer_2/",
    "transformer/",
    "vae/",
)


def revision() -> str:
    """Return the pinned model revision from the shared contract.

    Returns:
        The 40-character commit SHA.
    """
    path = Path(__file__).resolve().parents[2] / "contracts" / "model-revision.txt"
    return path.read_text().strip()


def _matches_ignore(name: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(name, pattern) for pattern in IGNORE_PATTERNS)


def check() -> int:
    """Verify the ignore filter against the live repository manifest.

    Downloads nothing. Confirms the gated repo is reachable with the current
    token, that every required prefix is present, and that the single-file
    duplicates are excluded.

    Returns:
        A process exit code.
    """
    api = HfApi()
    rev = revision()
    files = api.list_repo_files(REPO_ID, revision=rev)

    kept = [f for f in files if not _matches_ignore(f)]
    dropped = [f for f in files if _matches_ignore(f)]

    missing = [
        prefix
        for prefix in REQUIRED_PREFIXES
        if not any(f == prefix or f.startswith(prefix) for f in kept)
    ]

    print(f"repo      {REPO_ID}@{rev}")
    print(f"files     {len(files)} total, {len(kept)} kept, {len(dropped)} excluded")
    print(f"excluded  {', '.join(dropped) or 'none'}")

    if missing:
        print(f"FAIL missing required prefixes: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not dropped:
        print(
            "FAIL no files were excluded; the duplicate-weights filter is not working",
            file=sys.stderr,
        )
        return 1
    print("OK diffusers layout complete, single-file duplicates excluded")
    return 0


def download(dest: Path) -> int:
    """Download the diffusers layout at the pinned revision.

    Args:
        dest: Directory to populate.

    Returns:
        A process exit code.
    """
    rev = revision()
    print(f"downloading {REPO_ID}@{rev} -> {dest}")
    snapshot_download(
        repo_id=REPO_ID,
        revision=rev,
        local_dir=dest,
        ignore_patterns=IGNORE_PATTERNS,
        max_workers=8,
    )
    (dest / "MANIFEST.json").write_text(_manifest(dest, rev))
    print("done")
    return 0


def _manifest(dest: Path, rev: str) -> str:
    import json

    files = [p for p in dest.rglob("*") if p.is_file()]
    return json.dumps(
        {
            "repo": REPO_ID,
            "revision": rev,
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
        },
        indent=2,
    )


def main() -> int:
    """Parse arguments and dispatch.

    Returns:
        A process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the filter against the manifest without downloading",
    )
    parser.add_argument("--dest", type=Path, default=Path("/opt/weights"))
    args = parser.parse_args()

    if args.check:
        return check()
    return download(args.dest)


if __name__ == "__main__":
    raise SystemExit(main())
