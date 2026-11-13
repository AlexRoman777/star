from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from star.main import app


async def test_pipeline_contract_exposes_one_canonical_production_order(
    authenticated_context,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.get("/api/v1/pipeline/contract")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == "progressive-review-v18"
    assert [stage["key"] for stage in payload["stages"]] == [
        "primary",
        "senior_review",
        "final",
        "highlights",
    ]
    senior_stage = next(stage for stage in payload["stages"] if stage["key"] == "senior_review")
    assert senior_stage["model_role"] == "senior_rating"
    assert senior_stage["required"] is False
    assert {model["role"]: model["model_key"] for model in payload["models"]} == {
        "primary_rating": "ministral-3:8b-instruct-2512-q4_K_M",
        "relative_comparison": "qwen3-vl:8b-instruct-q4_K_M",
        "senior_rating": "qwen3.8:27b",
    }
    tasks = {task["task_type"]: task for task in payload["tasks"]}
    assert tasks["asset.primary_review"]["completion_role"] == "required"
    assert tasks["asset.primary_review"]["queue_stage"] == "ministral"
    senior = tasks["asset.deep_review"]
    assert senior["completion_role"] == "conditional"
    assert senior["stage"] == "advisory"
    assert senior["queue_stage"] == "senior_review"
    assert senior["model_role"] == "senior_rating"
    assert senior["route"] == "medium-vlm"
    assert senior["capabilities"] == ["medium-vlm", "large-vlm", "mlx"]
    assert senior["machine"] == "Mac or NVIDIA GPU worker"
    assert senior["runtime"] == {
        "candidate_count": 1,
        "context_tokens": 16_384,
        "max_completion_tokens": 1024,
        "max_media_edge": 1440,
        "compute_budget_ms": 600_000,
    }
    assert tasks["group.highlight_compare"]["runtime"] == {
        "candidate_count": 4,
        "context_tokens": 16_384,
        "max_completion_tokens": 256,
        "max_media_edge": 2048,
        "contact_sheet_edge": 4096,
        "compute_budget_ms": 150_000,
    }
    assert "asset.fast_rating" not in tasks
    assert "asset.apple_order_check" not in tasks
    # v209 retired the per-photo work that never fed the star.
    assert "group.cohort_triage" not in tasks
    assert "asset.clip_embedding" not in tasks
    assert "asset.description_embedding" not in tasks
    assert "asset.deep_review_experiment" not in tasks
    assert "asset.vlm_review" not in tasks
