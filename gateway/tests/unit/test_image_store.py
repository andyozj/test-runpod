"""Filesystem image store: save, thumbhash, byte-cap eviction, traversal."""

from __future__ import annotations

import base64
import io
import os
import uuid
from pathlib import Path

from PIL import Image

from gateway.adapters.images import FilesystemImageStore, thumbhash_of


def png_base64(width: int = 64, height: int = 48, color: str = "red") -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def store(root: Path, max_bytes: int = 10_000_000) -> FilesystemImageStore:
    return FilesystemImageStore(root=root, max_bytes=max_bytes)


async def test_save_writes_the_decoded_file_under_the_root(tmp_path: Path) -> None:
    job_id = uuid.uuid4()
    encoded = png_base64()

    stored = await store(tmp_path).save(job_id, encoded, "png")

    assert stored.path == f"{job_id}.png"
    assert (tmp_path / stored.path).read_bytes() == base64.b64decode(encoded)


async def test_save_returns_a_compact_base64_thumbhash(tmp_path: Path) -> None:
    stored = await store(tmp_path).save(uuid.uuid4(), png_base64(), "png")

    hash_bytes = base64.b64decode(stored.thumbhash)

    assert 5 <= len(hash_bytes) <= 25


async def test_load_returns_the_saved_bytes(tmp_path: Path) -> None:
    subject = store(tmp_path)
    stored = await subject.save(uuid.uuid4(), png_base64(), "png")

    assert await subject.load(stored.path) == base64.b64decode(png_base64())


async def test_load_after_eviction_returns_none(tmp_path: Path) -> None:
    subject = store(tmp_path)
    stored = await subject.save(uuid.uuid4(), png_base64(), "png")
    (tmp_path / stored.path).unlink()

    assert await subject.load(stored.path) is None


async def test_load_never_escapes_the_root(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("credentials")
    root = tmp_path / "images"
    root.mkdir()

    assert await store(root).load("../secret.txt") is None


async def test_the_oldest_images_are_evicted_once_the_cap_is_exceeded(
    tmp_path: Path,
) -> None:
    encoded = png_base64()
    size = len(base64.b64decode(encoded))
    subject = store(tmp_path, max_bytes=2 * size)
    first = await subject.save(uuid.uuid4(), encoded, "png")
    second = await subject.save(uuid.uuid4(), encoded, "png")
    _age(tmp_path / first.path, 30)
    _age(tmp_path / second.path, 20)

    third = await subject.save(uuid.uuid4(), encoded, "png")

    assert await subject.load(first.path) is None
    assert await subject.load(second.path) is not None
    assert await subject.load(third.path) is not None


async def test_an_image_larger_than_the_cap_still_lands(tmp_path: Path) -> None:
    """The newest file is exempt; the alternative is a job whose image never existed."""
    encoded = png_base64()
    size = len(base64.b64decode(encoded))
    subject = store(tmp_path, max_bytes=size // 2)
    first = await subject.save(uuid.uuid4(), encoded, "png")
    _age(tmp_path / first.path, 30)

    second = await subject.save(uuid.uuid4(), encoded, "png")

    assert await subject.load(first.path) is None
    assert await subject.load(second.path) is not None


async def test_saving_creates_the_root_directory(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "images"

    await store(root).save(uuid.uuid4(), png_base64(), "png")

    assert root.is_dir()


def test_thumbhash_is_deterministic_for_the_same_image() -> None:
    encoded = base64.b64decode(png_base64())

    assert thumbhash_of(encoded) == thumbhash_of(encoded)


def test_thumbhash_downscales_oversized_images() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 768), "blue").save(buffer, format="PNG")

    hash_bytes = base64.b64decode(thumbhash_of(buffer.getvalue()))

    assert 5 <= len(hash_bytes) <= 25


def _age(path: Path, seconds_ago: int) -> None:
    """Backdate a file's mtime so eviction order is deterministic."""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime - seconds_ago))
