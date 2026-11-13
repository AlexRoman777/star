from __future__ import annotations

import json

from sqlalchemy import select

from star.db.models import Task, TaskFailureEvidence, Worker
from star.domain.enums import TaskState
from star.services.evidence_persistence import record_task_failure_evidence


async def test_failed_attempt_retains_only_sanitized_metadata_and_digests(session) -> None:
    worker = Worker(
        token_hash="failure-worker-token",
        hostname_label="failure-worker",
        architecture="test",
        operating_system="test",
        software_version="173.0.0",
        capabilities={},
        runtime_status={
            "software_version": "173.0.0",
            "build_git_sha": "a" * 40,
            "build_tree_state": "clean",
            "build_source_fingerprint": "b" * 64,
        },
        allowed_capabilities=["small-vlm"],
    )
    session.add(worker)
    await session.flush()
    task = Task(
        task_type="group.highlight_compare",
        routing_key="small-vlm",
        data_class="family-preview",
        target_ref={
            "model_key": "qwen-test",
            "prompt_version": "highlight-test-v1",
            "schema_version": "highlight-schema-v1",
            "context_tokens": 4096,
            "max_completion_tokens": 512,
        },
        requirements={"capabilities": ["small-vlm"]},
        idempotency_key="sanitized-failure-evidence",
        state=TaskState.RUNNING,
        leased_to_worker_id=worker.id,
        attempt_count=2,
    )
    session.add(task)
    await session.flush()
    private_reason = "provider rejected private structured output: family description"

    first = await record_task_failure_evidence(
        session,
        task=task,
        attempt_number=task.attempt_count,
        worker_id=worker.id,
        error_class="LocalInferenceSchemaError",
        retryable=True,
        reason=private_reason,
        input_digest="c" * 64,
        output_digest="d" * 64,
        duration_ms=1234,
        prompt_version=None,
        schema_version=None,
    )
    second = await record_task_failure_evidence(
        session,
        task=task,
        attempt_number=task.attempt_count,
        worker_id=worker.id,
        error_class="LocalInferenceSchemaError",
        retryable=True,
        reason=private_reason,
        input_digest="c" * 64,
        output_digest="d" * 64,
        duration_ms=1234,
        prompt_version=None,
        schema_version=None,
    )
    assert second.id == first.id
    await session.flush()
    session.expunge(first)
    retained = await session.scalar(
        select(TaskFailureEvidence).where(TaskFailureEvidence.task_id == task.id)
    )
    assert retained is not None
    assert retained.reason_digest is not None and len(retained.reason_digest) == 64
    assert retained.input_digest == "c" * 64
    assert retained.output_digest == "d" * 64
    assert retained.inference_profile["context_tokens"] == 4096
    assert retained.release_provenance["worker_release"]["build_git_sha"] == "a" * 40
    serialized = json.dumps(
        {
            "release_provenance": retained.release_provenance,
            "inference_profile": retained.inference_profile,
            "reason_digest": retained.reason_digest,
        }
    )
    assert private_reason not in serialized
