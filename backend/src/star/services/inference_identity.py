from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import AnalysisNode, ModelVersion
from star.inference.provider import (
    COMPARISON_SAMPLING_V2,
    REASONING_SAMPLING_V2,
    STRUCTURED_SAMPLING_V2,
)

PRODUCTION_MODELS = {
    "primary": "ministral-3:8b-instruct-2512-q4_K_M",
    "qwen": "qwen/qwen3.6-27b",
    "comparison": "qwen3-vl:8b-instruct-q4_K_M",
}


async def _resolved_profile(
    session: AsyncSession,
    *,
    stage: str,
    task: str,
    model_key: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    model = await session.scalar(
        select(ModelVersion)
        .where(ModelVersion.model_id == model_key)
        .order_by(ModelVersion.enabled.desc(), ModelVersion.updated_at.desc())
        .limit(1)
    )
    if model is None:
        return {
            **base,
            "stage": stage,
            "model_key": model_key,
            "identity_status": "awaiting_worker_inventory",
            "preview_digest": None,
        }
    resource = model.resource_profile if isinstance(model.resource_profile, dict) else {}
    runtime_fingerprint = resource.get("runtime_fingerprint")
    artifact_identity = model.artifact_checksum or runtime_fingerprint
    return {
        **base,
        "stage": stage,
        "task": task,
        "model_key": model.model_id,
        "model_version": model.version,
        "runtime": model.runtime,
        "runtime_version": resource.get("runtime_version"),
        "runtime_format": resource.get("runtime_format"),
        "runtime_fingerprint": runtime_fingerprint,
        "model_artifact_checksum": model.artifact_checksum,
        "artifact_identity": artifact_identity,
        "artifact_identity_kind": (
            "artifact_checksum" if model.artifact_checksum else "runtime_fingerprint"
        ),
        "quantization": model.quantization,
        "seed_supported": resource.get("seed_supported"),
        "identity_status": "resolved" if artifact_identity else "incomplete_worker_inventory",
        "preview_digest": None,
    }


async def build_generation_inference_profiles(session: AsyncSession) -> dict[str, dict[str, Any]]:
    primary = await _resolved_profile(
        session,
        stage="primary",
        task="primary",
        model_key=PRODUCTION_MODELS["primary"],
        base=STRUCTURED_SAMPLING_V2.as_persisted(task="primary"),
    )
    qwen = await _resolved_profile(
        session,
        stage="qwen",
        task="qwen",
        model_key=PRODUCTION_MODELS["qwen"],
        base=REASONING_SAMPLING_V2.as_persisted(task="qwen"),
    )
    comparison = await _resolved_profile(
        session,
        stage="highlights",
        task="highlights",
        model_key=PRODUCTION_MODELS["comparison"],
        base=COMPARISON_SAMPLING_V2.as_persisted(task="highlights"),
    )
    finalizer = {
        **primary,
        "stage": "finalizer",
        "task": "json_finalizer",
        "name": "greedy-v1",
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 1,
        "reasoning_mode": "off",
        "media_sent": False,
    }
    return {"primary": primary, "qwen": qwen, "comparison": comparison, "finalizer": finalizer}


def attach_preview_digests(
    profiles: Mapping[str, Mapping[str, Any]], nodes: Mapping[str, AnalysisNode]
) -> dict[str, dict[str, Any]]:
    result = {key: dict(value) for key, value in profiles.items()}
    stage_map = {"primary": "primary", "qwen": "qwen", "comparison": "highlights"}
    for profile_key, node_key in stage_map.items():
        node = nodes.get(node_key)
        if node is not None and node.input_digest:
            result[profile_key]["preview_digest"] = node.input_digest
    if result.get("primary", {}).get("preview_digest"):
        result["finalizer"]["preview_digest"] = result["primary"]["preview_digest"]
    return result
