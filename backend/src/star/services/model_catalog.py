"""Static build facts for the models Star runs and the alternatives worth benchmarking.

Everything here is vendor or quantizer documentation gathered in September
2026 (plan A4), keyed by the model key workers advertise. Sizes are the
published download sizes; perplexities are the quantizer's own numbers. None
of it changes production behaviour: the owner-editable production settings
live on the library program and are surfaced next to these facts so the two
can be compared on the Models page and in the benchmark.
"""

from __future__ import annotations

from typing import Final

from star.domain.schemas import (
    InferenceSettings,
    InferenceStageSettings,
    ModelBuildAlternative,
    ModelBuildDetails,
    VendorRecommendedSettings,
)
from star.domain.schemas.library import InferenceStage
from star.services.pipeline_contract import PRODUCTION_MODEL_KEYS

MINISTRAL_3_8B_KEY: Final = "ministral-3:8b-instruct-2512-q4_K_M"
QWEN3_VL_8B_KEY: Final = "qwen3-vl:8b-instruct-q4_K_M"
QWEN3_8_27B_KEY: Final = "qwen3.8:27b"

_UNSLOTH_MINISTRAL: Final = "https://huggingface.co/unsloth/Ministral-3-8B-Instruct-2512-GGUF"
_UNSLOTH_QWEN3_VL: Final = "https://huggingface.co/unsloth/Qwen3-VL-8B-Instruct-GGUF"
_MLX_QWEN38_4BIT: Final = "https://huggingface.co/mlx-community/Qwen3.8-27B-4bit"
_MLX_QWEN38_8BIT: Final = "https://huggingface.co/mlx-community/Qwen3.8-27B-8bit"
_BONSAI_QWEN38_GGUF: Final = "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf"

MODEL_CATALOG: Final[dict[str, ModelBuildDetails]] = {
    MINISTRAL_3_8B_KEY: ModelBuildDetails(
        model_key=MINISTRAL_3_8B_KEY,
        display_name="Ministral 3 8B Instruct 2512",
        family="Mistral Ministral 3",
        parameters="8B",
        quantization="Q4_K_M",
        size_gb=6.0,
        runtime="ollama",
        ollama_tag="ministral-3:8b-instruct-2512-q4_K_M",
        huggingface_url="https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512",
        vendor_recommended=VendorRecommendedSettings(
            temperature=0.15,
            top_p=None,
            top_k=None,
            presence_penalty=None,
            reasoning_note=(
                "Instruct build has no thinking mode; Star runs it with reasoning off."
            ),
            image_note=(
                "Mistral and Unsloth recommend roughly 1:1 aspect inputs; Ollama's Mistral 3 "
                "runner resizes every photo to about 1,000 image tokens (about 1024 px), so "
                "larger inputs buy nothing."
            ),
        ),
        alternatives=[
            ModelBuildAlternative(
                label="Ollama q8_0",
                tag="ministral-3:8b-instruct-2512-q8_0",
                quantization="Q8_0",
                size_gb=9.9,
                runtime="ollama",
                note="Fits the 48 GB apple, not the 8 GB NVIDIA GPU.",
            ),
            ModelBuildAlternative(
                label="Ollama fp16",
                tag="ministral-3:8b-instruct-2512-fp16",
                quantization="F16",
                size_gb=18.0,
                runtime="ollama",
                note="Reference precision; Apple only.",
            ),
            ModelBuildAlternative(
                label="Unsloth Dynamic 2.0 UD-Q4_K_XL",
                tag="hf.co/unsloth/Ministral-3-8B-Instruct-2512-GGUF:UD-Q4_K_XL",
                quantization="UD-Q4_K_XL",
                size_gb=5.29,
                runtime="ollama",
                source_url=_UNSLOTH_MINISTRAL,
                note=(
                    "Claims better accuracy than Q4_K_M at the same size; the vision projector "
                    "import must be verified before it can rate photos."
                ),
            ),
        ],
        notes=[
            "Primary review model on the NVIDIA worker and the Apple worker helper lanes.",
            "Production sampling (temperature 0.2) sits above the vendor's 0.15.",
        ],
    ),
    QWEN3_VL_8B_KEY: ModelBuildDetails(
        model_key=QWEN3_VL_8B_KEY,
        display_name="Qwen3-VL 8B Instruct",
        family="Qwen3-VL",
        parameters="8B",
        quantization="Q4_K_M",
        size_gb=6.1,
        runtime="ollama",
        ollama_tag="qwen3-vl:8b-instruct-q4_K_M",
        huggingface_url="https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct",
        vendor_recommended=VendorRecommendedSettings(
            temperature=0.7,
            top_p=0.8,
            top_k=20,
            presence_penalty=1.5,
            reasoning_note="Instruct build has no thinking mode; Star runs it with reasoning off.",
            image_note=(
                "Qwen recommends 262K to 1.3M input pixels per image; a 1344 px contact sheet "
                "is about 1.8M pixels and a 720 px thumbnail about 0.5M."
            ),
        ),
        alternatives=[
            ModelBuildAlternative(
                label="Ollama q8_0",
                tag="qwen3-vl:8b-instruct-q8_0",
                quantization="Q8_0",
                size_gb=9.8,
                runtime="ollama",
                note="Apple only.",
            ),
            ModelBuildAlternative(
                label="Unsloth Dynamic 2.0 UD-Q4_K_XL",
                tag="hf.co/unsloth/Qwen3-VL-8B-Instruct-GGUF:UD-Q4_K_XL",
                quantization="UD-Q4_K_XL",
                size_gb=None,
                runtime="ollama",
                source_url=_UNSLOTH_QWEN3_VL,
                note="Same-size dynamic quantization worth a ranking-sheet comparison.",
            ),
        ],
        notes=[
            "Quick group ordering and the Highlight League comparison model.",
            "Production sampling (0.2 / 0.70 / 10, no presence penalty) is far from the "
            "vendor's 0.7 / 0.8 / 20 / presence 1.5; the benchmark compares both.",
        ],
    ),
    QWEN3_8_27B_KEY: ModelBuildDetails(
        model_key=QWEN3_8_27B_KEY,
        display_name="Qwen3.8 27B",
        family="Qwen3.8",
        parameters="27B",
        quantization="MLX 4-bit",
        size_gb=16.1,
        runtime="mlx",
        ollama_tag="qwen3.8:27b",
        huggingface_url=_MLX_QWEN38_4BIT,
        vendor_recommended=VendorRecommendedSettings(
            temperature=1.0,
            top_p=0.95,
            top_k=20,
            presence_penalty=None,
            reasoning_note=(
                "Qwen: thinking temperature 1.0 / top_p 0.95 / top_k 20; non-thinking 0.7 / "
                "0.8 / 20 with presence penalty 1.5. Greedy decoding with thinking on causes "
                "loops. Star never requests an effort above medium."
            ),
            image_note="Star sends a 1440 px input for the advanced review.",
        ),
        alternatives=[
            ModelBuildAlternative(
                label="Ternary Bonsai 2 27B (nvidia)",
                tag="prism-ml/Ternary-Bonsai-2-27B-gguf · PTQ1_0",
                quantization="ternary PTQ1_0",
                size_gb=6.6,
                runtime="llama.cpp-prism",
                source_url=_BONSAI_QWEN38_GGUF,
                note=(
                    "PrismML's ternary build of the same Qwen3.8 27B, 5.95 GB plus a 0.63 GB "
                    "vision projector, served by their llama.cpp fork on an 8 GB NVIDIA GPU. "
                    "On nine owner photos its stars matched the Mac 4-bit build on eight; "
                    "22-24 s per Advanced review."
                ),
            ),
            ModelBuildAlternative(
                label="mlx-community 8-bit",
                tag="mlx-community/Qwen3.8-27B-8bit",
                quantization="MLX 8-bit",
                size_gb=29.0,
                runtime="mlx",
                source_url=_MLX_QWEN38_8BIT,
                note=(
                    "Perplexity 6.94, the same as bf16 (4-bit: 7.09); near-lossless and "
                    "affordable on the Mac, where the Advanced review runs exclusively."
                ),
            ),
            ModelBuildAlternative(
                label="Ollama package",
                tag="qwen3.8:27b",
                quantization="Q4_K_M",
                size_gb=None,
                runtime="ollama",
                note="Fallback only when the native MLX runtime is not configured.",
            ),
        ],
        notes=[
            "Advanced (senior) review model on both review machines: the MLX 4-bit build on "
            "the Mac and the ternary Bonsai build on the NVIDIA GPU. Either machine takes "
            "an Advanced review; the owner's order of work applies to both.",
            "4-bit perplexity 7.09 versus 6.94 for 8-bit and bf16.",
            "Production sampling (0.2 / 0.80 / 10) is near-greedy; with thinking on the "
            "vendor warns of loops, so the benchmark tries vendor sampling with effort "
            "off, low, and medium.",
        ],
    ),
}

# The production stage each model key serves; the owner-editable settings for
# that stage are the ones the worker applies to it.
_ROLE_STAGES: Final[dict[str, InferenceStage]] = {
    "primary_rating": "primary",
    "senior_rating": "advanced",
    "provisional_group_rating": "group",
    "relative_comparison": "group",
}


def model_build_details(model_key: str, runtime: str | None = None) -> ModelBuildDetails | None:
    """Static build facts for a known model key, or ``None``.

    A registration on another runtime than the catalog's main build (the
    nvidia's Bonsai registration of the senior key) gets that alternative's
    facts as its build, with the main build listed among the alternatives.
    """

    details = MODEL_CATALOG.get(model_key)
    if details is None or runtime is None or runtime == details.runtime:
        return details
    match = next((item for item in details.alternatives if item.runtime == runtime), None)
    if match is None:
        return details
    main_as_alternative = ModelBuildAlternative(
        label=f"{details.display_name} ({details.quantization})",
        tag=details.ollama_tag or details.model_key,
        quantization=details.quantization,
        size_gb=details.size_gb,
        runtime=details.runtime,
        source_url=details.huggingface_url,
        note="The build the catalog documents first.",
    )
    return details.model_copy(
        update={
            "display_name": match.label,
            "quantization": match.quantization,
            "size_gb": match.size_gb,
            "runtime": match.runtime,
            "ollama_tag": match.tag,
            "huggingface_url": match.source_url,
            "alternatives": [main_as_alternative]
            + [item for item in details.alternatives if item is not match],
        }
    )


def model_stage(model_key: str) -> InferenceStage | None:
    """The production stage a model key serves, if it is a production model."""

    # A key may serve two roles (Qwen3-VL: quick group and Highlight League);
    # both map to the group stage, so the first match is the only answer.
    for role, key in PRODUCTION_MODEL_KEYS.items():
        if key == model_key and role in _ROLE_STAGES:
            return _ROLE_STAGES[role]
    return None


def active_settings_for_model(
    model_key: str, settings: InferenceSettings
) -> tuple[InferenceStage | None, InferenceStageSettings | None]:
    """Pair a model with the stage settings currently applied to it."""

    stage = model_stage(model_key)
    if stage is None:
        return None, None
    return stage, settings.stage(stage)


__all__ = [
    "MINISTRAL_3_8B_KEY",
    "MODEL_CATALOG",
    "QWEN3_8_27B_KEY",
    "QWEN3_VL_8B_KEY",
    "active_settings_for_model",
    "model_build_details",
    "model_stage",
]
