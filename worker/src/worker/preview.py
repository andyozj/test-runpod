"""Latent-to-RGB preview frames attached to progress updates.

FLUX latents arrive at the step callback packed: a 2x2-patchified sequence of
shape [B, (H/16)(W/16), 64]. `unpack_latents` is a numpy mirror of
`FluxPipeline._unpack_latents` (diffusers 0.39.0, the locked image version),
recovering the spatial [B, 16, H/8, W/8] layout. `latents_to_rgb` then applies
the fixed linear projection ComfyUI ships for FLUX and the same normalisation
its previewer uses: rgb = clamp((x @ F + b + 1) / 2).

Numpy rather than torch so the whole transform is unit-testable in the
torch-free dev environment; the only torch contact is the duck-typed
tensor-to-ndarray conversion in `_to_numpy`.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

LATENT_CHANNELS = 16
PATCH = 2
PACKED_CHANNELS = LATENT_CHANNELS * PATCH * PATCH
VAE_SCALE_FACTOR = 8

MAX_PREVIEW_SIDE = 192
PREVIEW_JPEG_QUALITY = 60
PREVIEW_FORMAT = "jpeg"

# FLUX latent->RGB projection and bias from ComfyUI, comfy/latent_formats.py,
# class Flux (latent_rgb_factors / latent_rgb_factors_bias):
# https://github.com/comfyanonymous/ComfyUI/blob/master/comfy/latent_formats.py
# Rows are the 16 latent channels, columns R, G, B.
_RGB_FACTORS: NDArray[np.float32] = np.array(
    [
        [-0.0346, 0.0244, 0.0681],
        [0.0034, 0.0210, 0.0687],
        [0.0275, -0.0668, -0.0433],
        [-0.0174, 0.0160, 0.0617],
        [0.0859, 0.0721, 0.0329],
        [0.0004, 0.0383, 0.0115],
        [0.0405, 0.0861, 0.0915],
        [-0.0236, -0.0185, -0.0259],
        [-0.0245, 0.0250, 0.1180],
        [0.1008, 0.0755, -0.0421],
        [-0.0515, 0.0201, 0.0011],
        [0.0428, -0.0012, -0.0036],
        [0.0817, 0.0765, 0.0749],
        [-0.1264, -0.0522, -0.1103],
        [-0.0280, -0.0881, -0.0499],
        [-0.1262, -0.0982, -0.0778],
    ],
    dtype=np.float32,
)
_RGB_BIAS: NDArray[np.float32] = np.array([-0.0329, -0.0718, -0.0851], dtype=np.float32)


def unpack_latents(
    latents: NDArray[np.float32], height: int, width: int
) -> NDArray[np.float32]:
    """Undo FLUX's 2x2 patch packing back to spatial latents.

    Mirrors `FluxPipeline._unpack_latents` with `vae_scale_factor=8`:
    [B, (H/16)(W/16), 64] -> [B, 16, H/8, W/8].

    Args:
        latents: Packed latents as reported to the step callback.
        height: Target image height in pixels.
        width: Target image width in pixels.

    Returns:
        Spatial latents, channels second.

    Raises:
        ValueError: The array does not hold `height * width` worth of patches.
    """
    batch, _patches, channels = latents.shape
    latent_height = PATCH * (height // (VAE_SCALE_FACTOR * PATCH))
    latent_width = PATCH * (width // (VAE_SCALE_FACTOR * PATCH))
    spatial = latents.reshape(
        batch,
        latent_height // PATCH,
        latent_width // PATCH,
        channels // (PATCH * PATCH),
        PATCH,
        PATCH,
    )
    spatial = spatial.transpose(0, 3, 1, 4, 2, 5)
    return spatial.reshape(
        batch, channels // (PATCH * PATCH), latent_height, latent_width
    )


def latents_to_rgb(spatial: NDArray[np.float32]) -> Image.Image:
    """Project spatial latents to an RGB image via the fixed FLUX matrix.

    Applied exactly as ComfyUI's `Latent2RGBPreviewer`: a channels-last
    matmul with the bias, then `(x + 1) / 2` clamped to [0, 1].

    Args:
        spatial: Latents shaped [B, 16, H/8, W/8]; only the first batch
            element is rendered.

    Returns:
        An RGB image at latent resolution (H/8 x W/8).
    """
    channels_last = spatial[0].transpose(1, 2, 0)
    rgb = channels_last @ _RGB_FACTORS + _RGB_BIAS
    rgb = np.clip((rgb + 1.0) / 2.0, 0.0, 1.0)
    return Image.fromarray((rgb * 255).astype(np.uint8))


def encode_preview(image: Image.Image) -> bytes:
    """Bound the frame to `MAX_PREVIEW_SIDE` and encode it as JPEG.

    Args:
        image: The projected RGB frame.

    Returns:
        JPEG bytes at quality `PREVIEW_JPEG_QUALITY`. `thumbnail` never
        upscales, so frames below the cap keep their latent resolution.
    """
    image.thumbnail((MAX_PREVIEW_SIDE, MAX_PREVIEW_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
    return buffer.getvalue()


def render_preview(latents: Any, height: int, width: int) -> str:
    """Render packed FLUX latents as a base64 JPEG preview frame.

    Args:
        latents: `Any`: a bf16 CUDA tensor in production, an ndarray in
            tests; `_to_numpy` owns the narrowing.
        height: Effective image height in pixels.
        width: Effective image width in pixels.

    Returns:
        Base64-encoded JPEG bytes.
    """
    array = _to_numpy(latents)
    image = latents_to_rgb(unpack_latents(array, height, width))
    return base64.b64encode(encode_preview(image)).decode("ascii")


def _to_numpy(latents: Any) -> NDArray[np.float32]:
    """Convert callback latents to a float32 ndarray.

    Args:
        latents: `Any`: torch tensor or array-like. Duck-typed on `detach`
            so this module never imports torch.

    Returns:
        A float32 copy on the CPU.
    """
    if hasattr(latents, "detach"):
        # bf16 has no numpy dtype; widen on the GPU before the copy.
        return np.asarray(latents.detach().float().cpu().numpy(), dtype=np.float32)
    return np.asarray(latents, dtype=np.float32)
