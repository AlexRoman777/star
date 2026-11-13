"""One shared definition of a visually unusable (blank) derivative.

The worker normalizer, the media relay, and the durable normalized-derivative
cache must agree on what "blank" means so an unusable Immich derivative is
rejected at every boundary instead of being analyzed or durably cached at any
one of them.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError


def image_is_blank(image: Image.Image) -> bool:
    """Detect a near-uniform frame: all-black, all-white, or flat single tone."""

    probe = image.copy()
    probe.thumbnail((64, 64), Image.Resampling.BILINEAR)
    histogram = probe.convert("RGB").histogram()
    channel_values: list[int] = []
    for channel in range(3):
        occupied = [
            value
            for value, count in enumerate(histogram[channel * 256 : (channel + 1) * 256])
            if count
        ]
        if occupied and occupied[-1] - occupied[0] > 2:
            return False
        if occupied:
            channel_values.append((occupied[0] + occupied[-1]) // 2)
    return bool(channel_values) and (
        max(channel_values) <= 3
        or min(channel_values) >= 252
        or max(channel_values) - min(channel_values) <= 2
    )


def decoded_image_is_blank(content: bytes) -> bool:
    """Fully decode a bounded payload and report whether it is blank.

    Raises the PIL decode errors (``UnidentifiedImageError``/``OSError``) to the
    caller so each boundary can map them to its own failure vocabulary.
    """

    with Image.open(io.BytesIO(content)) as image:
        image.load()
        return image_is_blank(image)


__all__ = ["UnidentifiedImageError", "decoded_image_is_blank", "image_is_blank"]
