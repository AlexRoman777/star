from __future__ import annotations

from fastapi import APIRouter, Depends

from star.api.dependencies import Principal, get_principal
from star.domain.schemas import PipelineContractResponse
from star.services.pipeline_contract import pipeline_contract_payload

router = APIRouter(prefix="/pipeline", tags=["Pipeline"])


@router.get("/contract", response_model=PipelineContractResponse)
async def pipeline_contract(
    _principal: Principal = Depends(get_principal),
) -> PipelineContractResponse:
    return PipelineContractResponse.model_validate(pipeline_contract_payload())
