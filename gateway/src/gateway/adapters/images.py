"""Filesystem image store: decoded results on disk, byte-capped, oldest evicted."""

from __future__ import annotations

import asyncio
import base64
import io
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import structlog
from PIL import Image

from gateway.adapters.thumbhash import rgba_to_thumbhash
from gateway.core.protocols import StoredImage

logger = structlog.get_logger()

# ThumbHash rejects anything larger; downscaling further buys nothing.
THUMB_MAX_DIMENSION = 100


def thumbhash_of(image_bytes: bytes) -> str:
    """Compute the base64 ThumbHash of an encoded image.

    Args:
        image_bytes: A PNG or JPEG as bytes.

    Returns:
        The hash, base64-encoded (~25 bytes before encoding).
    """
    with Image.open(io.BytesIO(image_bytes)) as image:
        rgba = image.convert("RGBA")
        rgba.thumbnail((THUMB_MAX_DIMENSION, THUMB_MAX_DIMENSION))
        hash_bytes = rgba_to_thumbhash(rgba.width, rgba.height, rgba.tobytes())
    return base64.b64encode(hash_bytes).decode()


@dataclass
class FilesystemImageStore:
    """Images under one directory, capped by total bytes.

    File I/O and pixel work run in a worker thread: a multi-MB decode on the
    event loop would stall every concurrent request for its duration.

    Attributes:
        root: The image directory; created on first save.
        max_bytes: Cap on the directory's total size. When a save pushes the
            total over it, the oldest files are deleted first. The file just
            saved is never evicted, so one image larger than the cap still
            lands (and empties the rest of the directory).
    """

    root: Path
    max_bytes: int

    async def save(
        self, job_id: UUID, image_base64: str, image_format: str
    ) -> StoredImage:
        """Decode and persist one result image, then enforce the byte cap.

        Args:
            job_id: The owning job; names the file.
            image_base64: The worker's encoded image.
            image_format: `png` or `jpeg`; names the extension.

        Returns:
            The store-relative path and the image's ThumbHash.
        """
        return await asyncio.to_thread(self._save, job_id, image_base64, image_format)

    async def load(self, path: str) -> bytes | None:
        """Read a stored image back.

        Args:
            path: The store-relative path from `StoredImage.path`.

        Returns:
            The image bytes, or None when the file was evicted or `path`
            escapes the store directory.
        """
        return await asyncio.to_thread(self._load, path)

    def _save(self, job_id: UUID, image_base64: str, image_format: str) -> StoredImage:
        data = base64.b64decode(image_base64)
        thumbhash = thumbhash_of(data)
        self.root.mkdir(parents=True, exist_ok=True)
        name = f"{job_id}.{image_format}"
        (self.root / name).write_bytes(data)
        self._evict(keep=name)
        return StoredImage(path=name, thumbhash=thumbhash)

    def _evict(self, keep: str) -> None:
        """Delete oldest files until the directory fits the cap.

        Args:
            keep: The just-written file, exempt from eviction.
        """
        files = sorted(
            (entry for entry in self.root.iterdir() if entry.is_file()),
            key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        )
        total = sum(entry.stat().st_size for entry in files)
        for entry in files:
            if total <= self.max_bytes:
                return
            if entry.name == keep:
                continue
            total -= entry.stat().st_size
            entry.unlink(missing_ok=True)
            logger.info("image_evicted", path=entry.name)

    def _load(self, path: str) -> bytes | None:
        target = self.root / path
        # The stored paths are our own uuid-named files, but `load` takes a
        # string: resolve and re-anchor so a crafted row can never read
        # outside the store.
        if not target.resolve().is_relative_to(self.root.resolve()):
            return None
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
