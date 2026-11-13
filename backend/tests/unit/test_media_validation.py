from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from star.services.media_validation import decoded_image_is_blank, image_is_blank


def _encoded(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (120, 80), color).save(output, format="JPEG", quality=95)
    return output.getvalue()


@pytest.mark.parametrize(
    ("color", "blank"),
    [
        ((0, 0, 0), True),
        ((255, 255, 255), True),
        ((128, 128, 128), True),
        # A uniform colored frame stays conservative: only black, white, and
        # flat gray count as blank so a legitimate monochrome scene survives.
        ((30, 80, 140), False),
    ],
)
def test_uniform_frames_are_blank(color: tuple[int, int, int], blank: bool) -> None:
    assert decoded_image_is_blank(_encoded(color)) is blank


def test_textured_frame_is_not_blank() -> None:
    image = Image.new("RGB", (120, 80), (30, 80, 140))
    for x in range(0, 120, 8):
        for y in range(0, 80, 8):
            image.putpixel((x, y), (200, 40, 90))
    assert image_is_blank(image) is False
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=95)
    assert decoded_image_is_blank(output.getvalue()) is False


def test_undecodable_payload_raises_to_caller() -> None:
    with pytest.raises((UnidentifiedImageError, OSError)):
        decoded_image_is_blank(b"not-an-image")
