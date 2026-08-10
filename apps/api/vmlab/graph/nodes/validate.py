"""Stage 0 -- image validation and normalisation (FR-05).

Two jobs. Reject what cannot be analysed at all, and flag what can be analysed
but only with reduced confidence. The second matters more: the PRD (§9) is
explicit that a borderline image should still be processed, with the limitation
disclosed, rather than refused outright or -- worse -- analysed as though it were
fine.

Resizing to a 1600px long edge is not an arbitrary choice. It is the standard in
the client's own Photo Standards sheet, which is what his knowledge base was
written around, and it keeps the vision call cheap.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

MIN_LONG_EDGE = 640
# Below this the image is too soft for composition judgements to mean anything.
BLUR_FLOOR = 60.0
# Mean luminance outside this band is under- or over-exposed.
DARK_CEILING = 45.0
BRIGHT_FLOOR = 215.0
MAX_ASPECT = 3.0


class ImageRejected(ValueError):
    """The image cannot be analysed at all."""


@dataclass
class ValidatedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    quality_flags: list[str] = field(default_factory=list)

    @property
    def low_confidence(self) -> bool:
        return bool(self.quality_flags)


def _variance_of_laplacian(image: Image.Image) -> float:
    """Sharpness proxy: the spread of edge response across the image.

    A sharp photograph has strong, varied edges; a soft one has weak ones
    everywhere. Measured on a downscaled greyscale copy so the figure does not
    drift with resolution -- otherwise a large blurry image can outscore a small
    sharp one.
    """
    grey = ImageOps.grayscale(image)
    grey.thumbnail((640, 640))
    edges = grey.filter(
        ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
    )
    histogram = edges.histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    mean = sum(value * count for value, count in enumerate(histogram)) / total
    variance = sum(count * (value - mean) ** 2 for value, count in enumerate(histogram)) / total
    return variance


def _mean_luminance(image: Image.Image) -> float:
    grey = ImageOps.grayscale(image)
    grey.thumbnail((256, 256))
    histogram = grey.histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0
    return sum(value * count for value, count in enumerate(histogram)) / total


def validate_and_normalise(
    raw: bytes,
    *,
    long_edge: int = 1600,
    max_bytes: int = 15 * 1024 * 1024,
) -> ValidatedImage:
    if not raw:
        raise ImageRejected("the file is empty")
    if len(raw) > max_bytes:
        raise ImageRejected(
            f"the image is {len(raw) / 1e6:.1f} MB; the limit is {max_bytes / 1e6:.0f} MB"
        )

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise ImageRejected("the file is not a readable image") from exc

    # Phones write orientation as metadata rather than rotating pixels, so a
    # portrait photo arrives landscape unless this is applied. Composition
    # judgements depend on it.
    image = ImageOps.exif_transpose(image)

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")

    width, height = image.size
    if max(width, height) < MIN_LONG_EDGE:
        raise ImageRejected(
            f"the image is {width}x{height}; at least {MIN_LONG_EDGE}px "
            f"on the long edge is needed"
        )

    aspect = max(width, height) / max(1, min(width, height))
    if aspect > MAX_ASPECT:
        raise ImageRejected(
            f"the image is unusually elongated ({aspect:.1f}:1) and may be a "
            f"crop or a panorama rather than a single display"
        )

    flags: list[str] = []

    sharpness = _variance_of_laplacian(image)
    if sharpness < BLUR_FLOOR:
        flags.append("image appears soft or out of focus")

    luminance = _mean_luminance(image)
    if luminance < DARK_CEILING:
        flags.append("image is very dark")
    elif luminance > BRIGHT_FLOOR:
        flags.append("image is very bright or washed out")

    # Resize only downwards. Upscaling a small image invents detail the vision
    # model would then describe as if it were real.
    if max(width, height) > long_edge:
        image.thumbnail((long_edge, long_edge), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)

    logger.info(
        "validated image %dx%d -> %dx%d sharpness=%.0f luminance=%.0f flags=%s",
        width, height, image.size[0], image.size[1], sharpness, luminance, flags,
    )

    return ValidatedImage(
        data=buffer.getvalue(),
        mime_type="image/jpeg",
        width=image.size[0],
        height=image.size[1],
        quality_flags=flags,
    )


def estimate_vision_tokens(width: int, height: int) -> int:
    """Rough token cost of an image at this size, for budgeting."""
    return math.ceil((width * height) / 750)
