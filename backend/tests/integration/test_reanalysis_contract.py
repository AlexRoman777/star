from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from star.db.models import ReviewReanalysisRun
from star.main import app
from star.services.review_prompts import (
    REANALYSIS_CHUNK_SIZE,
    _reanalysis_asset_statement,
    reanalysis_summary,
)


def test_reanalysis_summary_preserves_the_original_requested_count() -> None:
    run = ReviewReanalysisRun(
        id=uuid.uuid4(),
        program_id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        review_generation=17,
        scope="analyzed",
        selection={"_requested_photo_count": 10_000},
        request_digest="b" * 64,
        mode="complete_review",
        start_stage="source",
        expanded_stages=["primary", "publication"],
        reuse_policy="none",
        publication_policy="atomic",
        reason="requested_count_contract",
        state="running",
        selection_cutoff=datetime(2026, 8, 31, tzinfo=UTC),
        scanned_count=1,
        planned_task_count=1,
    )

    summary = reanalysis_summary(run)

    assert summary["requested"] == 10_000
    assert summary["scanned"] == 1
    assert "_requested_photo_count" not in summary["selection"]


async def test_220k_reanalysis_preview_keeps_exact_population_and_compute(
    authenticated_context,
) -> None:
    transport = ASGITransport(app=app)
    with (
        patch(
            "star.api.routers.reanalysis._affected_count",
            new=AsyncMock(return_value=220_000),
        ),
        patch(
            "star.api.routers.reanalysis._measured_compute_seconds",
            new=AsyncMock(return_value=30.0),
        ),
    ):
        async with AsyncClient(
            transport=transport,
            base_url="http://localhost",
            cookies={"star_session": authenticated_context["token"]},
        ) as client:
            response = await client.post(
                "/api/v1/reanalysis/preview",
                json={
                    "scope": "all",
                    "selection": {},
                    "mode": "complete_review",
                    "start_stage": "source",
                    "reuse_policy": "none",
                    "publication_policy": "atomic",
                    "reason": "scale_contract_test",
                },
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["affected_photos"] == 220_000
    assert payload["estimated_model_calls"] == 220_000
    assert payload["estimated_compute_seconds"] == 6_600_000
    assert payload["execution_prompt_version"].startswith("primary-r")


def test_220k_reanalysis_materialization_uses_bounded_keyset_chunks() -> None:
    run = ReviewReanalysisRun(
        id=uuid.uuid4(),
        program_id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        review_generation=2,
        scope="all",
        selection={},
        request_digest="a" * 64,
        mode="complete_review",
        start_stage="source",
        expanded_stages=["technical", "primary", "publication"],
        reuse_policy="none",
        publication_policy="atomic",
        reason="scale_contract_test",
        state="running",
        selection_cutoff=datetime(2026, 8, 16, tzinfo=UTC),
        cursor_created_at=datetime(2026, 8, 1, tzinfo=UTC),
        cursor_asset_id=uuid.uuid4(),
    )
    statement = _reanalysis_asset_statement(run).limit(REANALYSIS_CHUNK_SIZE)
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True})).upper()

    assert "LIMIT 250" in compiled
    assert " OFFSET " not in compiled
    assert "ASSETS.CREATED_AT >" in compiled
    assert (220_000 + REANALYSIS_CHUNK_SIZE - 1) // REANALYSIS_CHUNK_SIZE == 880


def test_retired_worker_completion_and_saved_filter_routes_are_not_registered() -> None:
    openapi_paths = set(app.openapi()["paths"])
    for suffix in {
        "complete-quality-specialist",
        "complete-portfolio-specialist",
        "complete-portrait-specialist",
        "complete-eye-state",
        "complete-subject-detection",
        "complete-synthesis",
    }:
        assert not any(path.endswith(suffix) for path in openapi_paths)
    assert not any("saved-filter" in path for path in openapi_paths)
    assert "/api/v1/review-prompts/reanalysis" not in openapi_paths
