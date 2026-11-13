from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.db.models import AnalysisRun, InferenceRun, ModelVersion, Task
from star.domain.schemas import InferenceInvocationSubmission


async def record_inference_invocations(
    session: AsyncSession,
    *,
    task: Task,
    invocations: list[InferenceInvocationSubmission],
    model_key: str,
    analysis_run: AnalysisRun | None = None,
    model_version: ModelVersion | None = None,
) -> list[InferenceRun]:
    """Persist exact-idempotent, append-only model invocation accounting."""

    stored: list[InferenceRun] = []
    hourly_cost = None
    if model_version is not None:
        candidate = model_version.benchmark.get("cost_per_hour_microunits")
        if isinstance(candidate, int) and candidate >= 0:
            hourly_cost = candidate
    for index, invocation in enumerate(invocations, start=1):
        existing = await session.scalar(
            select(InferenceRun).where(
                InferenceRun.task_id == task.id,
                InferenceRun.invocation_index == index,
            )
        )
        if existing is not None:
            if (
                existing.input_digest != invocation.input_digest
                or existing.output_digest != invocation.output_digest
                or existing.phase != invocation.phase
            ):
                raise RuntimeError("completed inference-run evidence is immutable")
            stored.append(existing)
            continue
        estimated_cost = (
            (invocation.duration_ms * hourly_cost) // 3_600_000 if hourly_cost is not None else None
        )
        run = InferenceRun(
            task_id=task.id,
            analysis_run_id=analysis_run.id if analysis_run is not None else None,
            analysis_generation_id=task.analysis_generation_id,
            asset_id=task.asset_id,
            model_version_id=model_version.id if model_version is not None else None,
            invocation_index=index,
            phase=invocation.phase,
            model_key=model_key,
            model_reported_version=invocation.model_reported_version,
            runtime=invocation.runtime,
            prompt_version=invocation.prompt_version,
            schema_version=invocation.schema_version,
            input_digest=invocation.input_digest,
            output_digest=invocation.output_digest,
            image_count=invocation.image_count,
            request_count=invocation.request_count,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            duration_ms=invocation.duration_ms,
            estimated_cost_microunits=estimated_cost,
            cost_basis=(
                {
                    "method": "configured_runtime_hourly_rate",
                    "cost_per_hour_microunits": hourly_cost,
                }
                if hourly_cost is not None
                else {"method": "unconfigured_local_cost"}
            ),
            state="succeeded",
        )
        session.add(run)
        await session.flush()
        from star.services.performance import record_performance

        await record_performance(
            session, run=run, task=task, invocation=invocation, model=model_version
        )
        stored.append(run)
    await session.flush()
    return stored
