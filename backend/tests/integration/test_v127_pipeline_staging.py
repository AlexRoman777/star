from __future__ import annotations

import io
from pathlib import Path

import yaml
from PIL import Image

from star.agent.media import (
    MODEL_JPEG_QUALITY,
    _build_group_contact_sheet,
    _prepare_model_image,
)


def _candidate(color: tuple[int, int, int]):
    source = io.BytesIO()
    Image.new("RGB", (1600, 1000), color).save(source, format="WEBP", quality=80)
    return _prepare_model_image(source.getvalue(), "thumbnail", max_edge=720)


def test_released_qwen_sheet_is_a_decodable_jpeg_q80() -> None:
    sheet = _build_group_contact_sheet(
        [
            _candidate((180, 20, 20)),
            _candidate((20, 180, 20)),
            _candidate((20, 20, 180)),
            _candidate((180, 180, 20)),
        ],
        sheet_edge=1440,
    )

    assert sheet.media_type == "image/jpeg"
    assert sheet.encoding_quality == MODEL_JPEG_QUALITY == 80
    assert sheet.processing_mode == "labelled_contact_sheet_jpeg_q80"
    assert sheet.content[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(sheet.content)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (1440, 1440)
        decoded.verify()


def test_release_keeps_a_continuous_100_photo_admission_runway() -> None:
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "Deployment/coordinator/compose.yaml").read_text())
    environment = compose["x-star-environment"]

    assert int(environment["STAR_LIBRARY_METADATA_ADMISSION_BATCH_SIZE"]) >= 100
    assert int(environment["STAR_SCHEDULER_HOT_ADMISSION_BATCH_SIZE"]) >= 100
