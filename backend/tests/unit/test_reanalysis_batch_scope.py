from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from star.db.base import utc_now
from star.db.models import ReviewReanalysisRun
from star.domain.schemas.review import ReviewReanalysisRequest
from star.services.review_prompts import _reanalysis_asset_statement


def test_batch_scope_requires_an_explicit_identity():
    for selection in ({}, {"run_id": "bad"}):
        with pytest.raises(ValidationError, match="valid run_id"):
            ReviewReanalysisRequest(scope="run", selection=selection)


def test_batch_scope_uses_membership_then_the_frozen_selection():
    source_id = uuid.uuid4()
    request = ReviewReanalysisRequest(scope="run", selection={"run_id": str(source_id)})
    run = ReviewReanalysisRun(
        id=uuid.uuid4(),
        scope=request.scope,
        selection=request.selection,
        selection_cutoff=utc_now(),
    )
    statement = _reanalysis_asset_statement(run).compile()
    assert "work_run_assets" in str(statement)
    assert source_id in statement.params.values()
    run.selection = {**run.selection, "_membership_frozen": True}
    frozen = _reanalysis_asset_statement(run).compile()
    assert run.id in frozen.params.values()
    assert source_id not in frozen.params.values()
