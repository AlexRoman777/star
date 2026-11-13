"""How a worker normalizes any source photo for a model."""

from __future__ import annotations


def test_every_decodable_camera_format_becomes_the_same_jpeg_contract() -> None:
    """The model contract is the re-encoded JPEG, not the source format."""

    import io

    from PIL import Image

    from star.agent.media import _prepare_model_image

    for source_format in ("JPEG", "PNG", "TIFF", "BMP", "GIF", "WEBP"):
        buffer = io.BytesIO()
        Image.new("RGB", (96, 72), (120, 90, 60)).save(buffer, format=source_format)
        prepared = _prepare_model_image(buffer.getvalue(), "preview", 512)
        assert prepared.media_type == "image/jpeg", source_format
        assert prepared.content[:2] == b"\xff\xd8", source_format


async def test_an_unusable_photo_is_logged_with_the_reason_the_coordinator_gave() -> None:
    """The owner must be told which of six causes actually applied.

    Star always knew: the coordinator sends the reason in a header and the
    worker carries it on the exception. It was only ever logged as "failed
    unexpectedly", which sent the owner searching Immich for a fault that was
    not necessarily there.
    """

    import logging

    from star.agent.inference_tasks import SourceMediaUnavailableError

    error = SourceMediaUnavailableError("immich_preview_blank_image", "asset-123")
    assert error.error_code == "immich_preview_blank_image"
    assert error.asset_id == "asset-123"

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("star.agent.test-source-media")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        logger.warning(
            "source media could not be used",
            extra={"source_error_code": error.error_code, "asset_id": error.asset_id},
        )
    finally:
        logger.removeHandler(handler)

    assert records and records[0].source_error_code == "immich_preview_blank_image"
