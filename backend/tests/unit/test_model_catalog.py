from __future__ import annotations

from star.domain.schemas import InferenceSettings, ModelBuildDetails
from star.services.model_catalog import (
    MINISTRAL_3_8B_KEY,
    MODEL_CATALOG,
    QWEN3_8_27B_KEY,
    QWEN3_VL_8B_KEY,
    active_settings_for_model,
    model_build_details,
    model_stage,
)
from star.services.pipeline_contract import PRODUCTION_MODEL_KEYS


def test_catalog_covers_every_production_model_with_vendor_guidance() -> None:
    assert set(PRODUCTION_MODEL_KEYS.values()) <= set(MODEL_CATALOG)
    for key, build in MODEL_CATALOG.items():
        assert isinstance(build, ModelBuildDetails)
        assert build.model_key == key
        assert build.huggingface_url and build.huggingface_url.startswith("https://huggingface.co/")
        assert build.vendor_recommended.reasoning_note
        assert build.vendor_recommended.image_note
        assert build.alternatives, key
        assert all(alternative.tag for alternative in build.alternatives)

    ministral = MODEL_CATALOG[MINISTRAL_3_8B_KEY]
    assert (ministral.quantization, ministral.size_gb) == ("Q4_K_M", 6.0)
    assert ministral.runtime == "ollama"
    assert ministral.vendor_recommended.temperature == 0.15
    assert {alt.quantization: alt.size_gb for alt in ministral.alternatives} == {
        "Q8_0": 9.9,
        "F16": 18.0,
        "UD-Q4_K_XL": 5.29,
    }
    assert any(
        alt.tag == "hf.co/unsloth/Ministral-3-8B-Instruct-2512-GGUF:UD-Q4_K_XL"
        for alt in ministral.alternatives
    )

    qwen_vl = MODEL_CATALOG[QWEN3_VL_8B_KEY]
    assert (qwen_vl.size_gb, qwen_vl.runtime) == (6.1, "ollama")
    recommended = qwen_vl.vendor_recommended
    assert (recommended.temperature, recommended.top_p, recommended.top_k) == (0.7, 0.8, 20)
    assert recommended.presence_penalty == 1.5
    assert "262K" in recommended.image_note

    qwen38 = MODEL_CATALOG[QWEN3_8_27B_KEY]
    assert (qwen38.runtime, qwen38.size_gb) == ("mlx", 16.1)
    assert qwen38.huggingface_url == "https://huggingface.co/mlx-community/Qwen3.8-27B-4bit"
    assert "never requests an effort above medium" in qwen38.vendor_recommended.reasoning_note
    eight_bit = next(alt for alt in qwen38.alternatives if alt.quantization == "MLX 8-bit")
    assert eight_bit.size_gb == 29.0
    assert "6.94" in eight_bit.note and "7.09" in eight_bit.note
    # Nothing in the catalog ever suggests an effort above medium.
    for build in MODEL_CATALOG.values():
        text = " ".join([build.vendor_recommended.reasoning_note, *build.notes]).lower()
        assert "xhigh" not in text


def test_models_map_to_the_stage_whose_settings_they_receive() -> None:
    settings = InferenceSettings.defaults()
    assert model_stage(MINISTRAL_3_8B_KEY) == "primary"
    assert model_stage(QWEN3_8_27B_KEY) == "advanced"
    assert model_stage(QWEN3_VL_8B_KEY) == "group"
    assert model_stage("gemma4-e4b-vision:q4_0") is None
    assert model_build_details("gemma4-e4b-vision:q4_0") is None

    stage, active = active_settings_for_model(QWEN3_8_27B_KEY, settings)
    assert stage == "advanced"
    assert active == settings.advanced
    assert active is not None and active.reasoning_effort == "off"
    assert active_settings_for_model("facebook/dinov2-small", settings) == (None, None)
