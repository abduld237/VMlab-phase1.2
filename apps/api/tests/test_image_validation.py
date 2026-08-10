"""Image validation, exercised on synthetic cases and the real corpus fixtures.

The behaviour that matters most is the middle ground: a soft or dark photo must
be *flagged and processed*, not rejected. A shop floor is not a studio, and a
prototype that refuses real photographs fails the PRD's own §9 requirement to
analyse with reduced confidence instead.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vmlab.graph.nodes.validate import (  # noqa: E402
    ImageRejected,
    validate_and_normalise,
)

FIXTURES = Path(__file__).resolve().parents[3] / "data" / "fixtures"


def _photo(width: int = 1200, height: int = 900, brightness: int = 128) -> Image.Image:
    """A synthetic image with enough edge detail to read as sharp."""
    image = Image.new("RGB", (width, height), (brightness, brightness, brightness))
    draw = ImageDraw.Draw(image)
    for i in range(0, width, 23):
        draw.line([(i, 0), (i, height)], fill=(min(255, brightness + 90), 40, 90), width=3)
    for i in range(0, height, 19):
        draw.line([(0, i), (width, i)], fill=(20, min(255, brightness + 60), 140), width=2)
    return image


def _encode(image: Image.Image, fmt: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, quality=92)
    return buffer.getvalue()


def test_a_normal_photo_passes_unflagged():
    result = validate_and_normalise(_encode(_photo()))

    assert result.quality_flags == []
    assert result.low_confidence is False
    assert result.mime_type == "image/jpeg"


def test_large_images_are_resized_to_the_client_standard():
    result = validate_and_normalise(_encode(_photo(4000, 3000)), long_edge=1600)

    assert max(result.width, result.height) == 1600
    # Aspect ratio must survive the resize, or composition judgements shift.
    assert abs((result.width / result.height) - (4000 / 3000)) < 0.02


def test_small_images_are_not_upscaled():
    """Upscaling would invent detail the vision model then describes as real."""
    result = validate_and_normalise(_encode(_photo(800, 600)), long_edge=1600)

    assert (result.width, result.height) == (800, 600)


def test_a_blurred_photo_is_flagged_but_still_processed():
    blurred = _photo().filter(ImageFilter.GaussianBlur(radius=9))

    result = validate_and_normalise(_encode(blurred))

    assert result.low_confidence is True
    assert any("soft" in flag or "focus" in flag for flag in result.quality_flags)
    assert result.data, "a soft image must still be analysable"


def test_a_dark_photo_is_flagged_but_still_processed():
    result = validate_and_normalise(_encode(_photo(brightness=12)))

    assert result.low_confidence is True
    assert any("dark" in flag for flag in result.quality_flags)
    assert result.data


def test_a_washed_out_photo_is_flagged():
    result = validate_and_normalise(_encode(_photo(brightness=245)))

    assert any("bright" in flag or "washed" in flag for flag in result.quality_flags)


def test_tiny_images_are_rejected():
    with pytest.raises(ImageRejected, match="long edge"):
        validate_and_normalise(_encode(_photo(320, 240)))


def test_panoramas_are_rejected():
    with pytest.raises(ImageRejected, match="elongated"):
        validate_and_normalise(_encode(_photo(4000, 700)))


def test_non_images_are_rejected():
    with pytest.raises(ImageRejected, match="readable image"):
        validate_and_normalise(b"this is not an image at all")


def test_empty_upload_is_rejected():
    with pytest.raises(ImageRejected, match="empty"):
        validate_and_normalise(b"")


def test_oversized_uploads_are_rejected():
    with pytest.raises(ImageRejected, match="limit"):
        validate_and_normalise(_encode(_photo()), max_bytes=1024)


def test_png_input_is_normalised_to_jpeg():
    result = validate_and_normalise(_encode(_photo(), fmt="PNG"))

    assert result.mime_type == "image/jpeg"
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


@pytest.mark.skipif(not FIXTURES.exists(), reason="fixtures not extracted")
def test_real_corpus_fixtures_are_accepted():
    """The rendered planograms pulled from the client's own decks must pass."""
    images = sorted(FIXTURES.glob("*.jpg"))[:20]
    if not images:
        pytest.skip("no fixtures available")

    rejected = []
    for path in images:
        try:
            validate_and_normalise(path.read_bytes())
        except ImageRejected as exc:
            rejected.append((path.name, str(exc)))

    assert not rejected, f"real display artwork was rejected: {rejected}"
