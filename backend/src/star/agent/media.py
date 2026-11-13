from __future__ import annotations

import hashlib
import io
import math
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from star.inference.provider import LocalMediaInput
from star.services.media_validation import image_is_blank

DEFAULT_GROUP_CONTACT_SHEET_EDGE = 512
# Production groups hold at most eight candidates; the in-app benchmark may
# rank a sheet of up to twenty-four.
MAX_CONTACT_SHEET_CANDIDATES = 24
MODEL_JPEG_QUALITY = 80
MODEL_INPUT_SIZE_LIMIT = 16 * 1024 * 1024
# Every one of these is re-encoded to the same oriented JPEG q80 before a model
# sees it, so this list is about what the decoder can open, not what the model
# accepts. A library shot on several cameras reaches Immich in more shapes than
# three, and refusing the rest threw away photographs that decode perfectly.
_SUPPORTED_SOURCE_FORMATS = {
    "JPEG": "image/jpeg",
    "JPEG2000": "image/jp2",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "AVIF": "image/avif",
    "TIFF": "image/tiff",
    "MPO": "image/jpeg",
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "HEIF": "image/heif",
    "HEIC": "image/heic",
}


class MediaDecodeError(ValueError):
    """The delivered source derivative is permanently unusable as model input.

    Carries a ``source_error_code`` from ``PERMANENT_SOURCE_ERROR_CODES`` so a
    worker-detected blank or corrupt derivative feeds the same source-failure
    quarantine counter as a coordinator-detected one, instead of dead-ending as
    a generic worker failure.
    """

    def __init__(self, message: str, *, source_error_code: str) -> None:
        super().__init__(message)
        self.source_error_code = source_error_code


def _encode_model_jpeg(image: Image.Image) -> bytes:
    """Encode a worker-generated model input in the production JPEG contract."""

    encoded = io.BytesIO()
    image.convert("RGB").save(
        encoded,
        format="JPEG",
        quality=MODEL_JPEG_QUALITY,
        optimize=True,
    )
    return encoded.getvalue()


# The worker, relay, and durable cache share one blank definition.
_image_is_blank = image_is_blank


def _prepare_model_image(
    content: bytes,
    input_variant: str,
    max_edge: int = 2048,
    label: str | None = None,
) -> LocalMediaInput:
    """Normalize every production visual input to one oriented JPEG q80 contract."""

    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            source_format = str(source.format or "").upper()
            source_media_type = _SUPPORTED_SOURCE_FORMATS.get(source_format)
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MediaDecodeError(
            "model input could not be decoded safely",
            source_error_code="invalid_image",
        ) from exc
    if _image_is_blank(image):
        raise MediaDecodeError(
            "model input is blank or visually unusable",
            source_error_code="blank_image",
        )
    if not 128 <= max_edge <= 2048:
        raise ValueError("model input edge is outside the worker safety contract")
    requires_resize = max(image.size) > max_edge
    source_label = {
        "thumbnail": "immich-720p-thumbnail",
        "preview": "immich-4k-preview",
        "fullsize": "immich-full-dimension-webp",
    }.get(input_variant)
    if source_label is None:
        raise ValueError("model input variant is outside the Immich derivative contract")
    if source_media_type is None:
        raise MediaDecodeError(
            "model input format is outside the supported image contract",
            source_error_code="unsupported_media_type",
        )

    if requires_resize:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if label:
        badge_size = max(44, min(image.size) // 10)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (12, 12, 12 + badge_size, 12 + badge_size),
            radius=max(8, badge_size // 6),
            fill=(0, 0, 0),
        )
        try:
            font = ImageFont.load_default(size=max(24, badge_size // 2))
        except TypeError:
            font = ImageFont.load_default()
        draw.text(
            (12 + badge_size / 2, 12 + badge_size / 2),
            label,
            fill=(255, 255, 255),
            font=font,
            anchor="mm",
        )
    value = _encode_model_jpeg(image)
    encoding_quality = MODEL_JPEG_QUALITY
    if not value or len(value) > MODEL_INPUT_SIZE_LIMIT:
        raise ValueError("normalized model input exceeds the safety limit")
    return LocalMediaInput(
        content=value,
        media_type="image/jpeg",
        source=source_label,
        asset_reference="asset",
        processing_mode=("labelled_jpeg_q80" if label else "bounded_jpeg_q80"),
        orientation_policy="exif_transpose",
        max_edge=max_edge,
        encoding_quality=encoding_quality,
    )


def contact_sheet_columns(candidate_count: int) -> int:
    """Grid width shared by the sheet builder and the per-cell fetch edge."""

    if candidate_count <= 4:
        return 2
    if candidate_count <= 8:
        return 3
    return math.ceil(math.sqrt(candidate_count))


def _build_group_contact_sheet(
    media: list[LocalMediaInput],
    *,
    sheet_edge: int = DEFAULT_GROUP_CONTACT_SHEET_EDGE,
) -> LocalMediaInput:
    """Pack labelled candidates into one bounded image for a predictable VLM token budget.

    Up to four candidates use two columns and up to eight use three, exactly as
    the released group tasks always have. Larger benchmark sheets use a
    square-ish grid (``ceil(sqrt(n))`` columns) so every cell keeps the same
    aspect budget and the number badges stay readable.
    """

    if not 2 <= len(media) <= MAX_CONTACT_SHEET_CANDIDATES:
        raise ValueError("contact sheet requires between two and twenty-four candidates")
    if not 512 <= sheet_edge <= 4096:
        raise ValueError("contact sheet edge is outside the worker safety contract")
    columns = contact_sheet_columns(len(media))
    rows = math.ceil(len(media) / columns)
    cell_width = sheet_edge // columns
    cell_height = sheet_edge // rows
    # Dense benchmark grids get a proportionally larger badge so two-digit
    # labels stay legible after the runtime's own downscale.
    badge_divisor = 8 if len(media) <= 8 else 6
    sheet = Image.new("RGB", (sheet_edge, sheet_edge), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for index, item in enumerate(media):
        try:
            with Image.open(io.BytesIO(item.content)) as source:
                candidate = source.convert("RGB")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("contact sheet candidate could not be decoded safely") from exc
        candidate.thumbnail((cell_width, cell_height), Image.Resampling.LANCZOS)
        column = index % columns
        row = index // columns
        left = column * cell_width + (cell_width - candidate.width) // 2
        top = row * cell_height + (cell_height - candidate.height) // 2
        sheet.paste(candidate, (left, top))
        badge_size = max(44, min(cell_width, cell_height) // badge_divisor)
        badge_left = column * cell_width + 12
        badge_top = row * cell_height + 12
        draw.rounded_rectangle(
            (badge_left, badge_top, badge_left + badge_size, badge_top + badge_size),
            radius=max(8, badge_size // 6),
            fill=(0, 0, 0),
        )
        try:
            font = ImageFont.load_default(size=max(24, badge_size // 2))
        except TypeError:
            font = ImageFont.load_default()
        draw.text(
            (badge_left + badge_size / 2, badge_top + badge_size / 2),
            str(index + 1),
            fill=(255, 255, 255),
            font=font,
            anchor="mm",
        )
    value = _encode_model_jpeg(sheet)
    encoding_quality = MODEL_JPEG_QUALITY
    if not value or len(value) > MODEL_INPUT_SIZE_LIMIT:
        raise ValueError("contact sheet exceeds the model input safety limit")
    try:
        with Image.open(io.BytesIO(value)) as encoded_sheet:
            if encoded_sheet.format != "JPEG" or encoded_sheet.size != (sheet_edge, sheet_edge):
                raise ValueError("contact sheet did not preserve its JPEG contract")
            encoded_sheet.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("contact sheet JPEG could not be decoded safely") from exc
    return LocalMediaInput(
        content=value,
        media_type="image/jpeg",
        source="star-labelled-contact-sheet",
        asset_reference="candidate-contact-sheet",
        processing_mode="labelled_contact_sheet_jpeg_q80",
        orientation_policy="provider_native",
        max_edge=sheet_edge,
        encoding_quality=encoding_quality,
    )


def _media_provenance(
    response: httpx.Response,
    *,
    asset_reference: str,
    processing_content: bytes,
    processing_media_type: str,
    processing_width: int,
    processing_height: int,
    processing_mode: str,
    orientation_policy: str,
    max_edge: int | None = None,
    encoding_quality: int | None = None,
) -> dict[str, Any]:
    delivered_variant = response.headers.get("x-star-input-variant", "preview")
    roles = {
        "thumbnail": "720p_thumbnail",
        "preview": "4k_preview",
        "fullsize": "full_dimension_webp",
    }
    delivered_role = roles.get(delivered_variant)
    if delivered_role is None:
        raise ValueError("media response has an invalid derivative variant")
    source_media_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
    if source_media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("media response has an invalid image type")
    computed_source_digest = hashlib.sha256(response.content).hexdigest()
    reported_source_digest = response.headers.get("x-star-source-digest")
    if reported_source_digest is not None and reported_source_digest != computed_source_digest:
        raise ValueError("media response digest does not match its provenance header")
    dimension_header = response.headers.get("x-star-source-dimensions", "")
    try:
        source_width, source_height = (int(value) for value in dimension_header.split("x", 1))
    except TypeError, ValueError:
        with Image.open(io.BytesIO(response.content)) as source:
            source_width, source_height = source.size
    return {
        "contract_version": response.headers.get(
            "x-star-media-contract",
            "immich-720p-thumbnail__4k-preview__full-dimension-webp-v1",
        ),
        "requested_variant": ("thumbnail" if delivered_variant == "thumbnail" else "fullsize"),
        "delivered_variant": delivered_variant,
        "delivered_role": delivered_role,
        "source_media_type": source_media_type,
        "source_digest": computed_source_digest,
        "source_width": source_width,
        "source_height": source_height,
        "visual_revision": response.headers.get("x-star-preview-version", "unreported"),
        "fallback_reason": response.headers.get("x-star-fallback-reason"),
        "contract_conformant": (
            delivered_variant != "fullsize" or source_media_type == "image/webp"
        ),
        "asset_reference": asset_reference,
        "processing": {
            "mode": processing_mode,
            "media_type": processing_media_type,
            "digest": hashlib.sha256(processing_content).hexdigest(),
            "width": processing_width,
            "height": processing_height,
            "max_edge": max_edge,
            "encoding_quality": encoding_quality,
            "orientation_policy": orientation_policy,
            "cropped": False,
            "upscaled": False,
        },
    }


def _validated_group_asset_ids(value: object, *, highlight_comparison: bool) -> list[str]:
    if not isinstance(value, list) or not 2 <= len(value) <= 8:
        raise ValueError("group VLM task has an invalid asset list")
    asset_ids = [str(asset_id) for asset_id in value]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("group VLM task contains duplicate assets")
    return asset_ids
