from __future__ import annotations

import io
from dataclasses import dataclass

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

FINGERPRINT_MAX_EDGE = 2048
FINGERPRINT_VERSIONS: tuple[tuple[str, str, str, int], ...] = (
    # (fingerprint field, algorithm, algorithm_version, bit length)
    ("perceptual_hash", "phash", "imagehash-4.3.2-hash8", 64),
    ("perceptual_hash_256", "phash", "imagehash-4.3.2-hash16", 256),
    ("difference_hash_256", "dhash", "imagehash-4.3.2-hash16", 256),
    ("color_signature", "color-grid", "rgb-4x4-v1", 384),
    ("ssim_luma_signature", "ssim-grid", "luma-16x16-v1", 2048),
)


class UnsafeImageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VisualFingerprints:
    """Deterministic similarity fingerprints for burst and series grouping.

    These used to travel with the Technical preview; since v209 round 3 the
    similarity-embedding task computes them so the visible burst/series
    grouping keeps working without a separate evidence stage. They describe
    pixel layout only and never vote on a star.
    """

    perceptual_hash: str
    perceptual_hash_256: str
    difference_hash_256: str
    color_signature: str
    ssim_luma_signature: str

    def payload(self) -> dict[str, str]:
        return {
            "perceptual_hash": self.perceptual_hash,
            "perceptual_hash_256": self.perceptual_hash_256,
            "difference_hash_256": self.difference_hash_256,
            "color_signature": self.color_signature,
            "ssim_luma_signature": self.ssim_luma_signature,
        }


def compute_visual_fingerprints(
    content: bytes,
    *,
    max_bytes: int = 50 * 1024 * 1024,
) -> VisualFingerprints:
    if not content or len(content) > max_bytes:
        raise UnsafeImageError("image is empty or exceeds the configured byte limit")
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise UnsafeImageError("image could not be decoded safely") from exc
    width, height = image.size
    if width < 16 or height < 16:
        raise UnsafeImageError("image dimensions are too small for a reliable fingerprint")
    image.thumbnail((FINGERPRINT_MAX_EDGE, FINGERPRINT_MAX_EDGE), Image.Resampling.LANCZOS)
    gray = image.convert("L")
    return VisualFingerprints(
        perceptual_hash=str(imagehash.phash(gray, hash_size=8)),
        perceptual_hash_256=str(imagehash.phash(gray, hash_size=16)),
        difference_hash_256=str(imagehash.dhash(gray, hash_size=16)),
        color_signature=image.resize((4, 4), Image.Resampling.BILINEAR).tobytes().hex(),
        ssim_luma_signature=gray.resize((16, 16), Image.Resampling.LANCZOS).tobytes().hex(),
    )
