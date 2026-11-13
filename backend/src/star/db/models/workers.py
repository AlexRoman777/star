from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base, utc_now
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.domain.enums import (
    TaskState,
)


class Worker(Base, TimestampMixin):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    certificate_subject: Mapped[str | None] = mapped_column(String(512), unique=True)
    last_enrollment_nonce_hash: Mapped[str | None] = mapped_column(String(64))
    hostname_label: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    operating_system: Mapped[str] = mapped_column(String(128), nullable=False)
    software_version: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    runtime_status: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    affinity_routing_key: Mapped[str | None] = mapped_column(String(64))
    affinity_model_key: Mapped[str | None] = mapped_column(String(512))
    affinity_processing_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_batches.id", ondelete="SET NULL"), index=True
    )
    affinity_remaining: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    affinity_task_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    affinity_switch_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    affinity_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allowed_capabilities: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    desired_state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    # The Apple worker helps the NVIDIA worker with primary reviews whenever its senior
    # review queue is empty; the owner may switch help off.
    helper_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional window that limits WHEN an enabled helper leases NVIDIA work.
    # It never gates Apple Vision or owner-started Apple sessions; a null
    # policy keeps helper mode a pure manual toggle.
    helper_schedule_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    apple_review_filters: Mapped[list[str]] = mapped_column(
        JSON_TYPE,
        default=lambda: ["automatic"],
        nullable=False,
    )
    schedule_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        default=lambda: {
            "enabled": False,
            "timezone": "Europe/Stockholm",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "start_local": "00:00",
            "end_local": "00:00",
        },
        nullable=False,
    )
    control_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Coordinator-owned live limit for the worker's advertised model lanes.
    # The worker may expose six executors while the owner chooses to admit
    # only one through six concurrent Ministral leases.
    model_concurrency_limit: Mapped[int] = mapped_column(
        Integer, default=6, server_default="6", nullable=False
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    credential_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    service_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hostname_label: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="online", nullable=False)
    software_version: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class WorkerCredential(Base):
    __tablename__ = "worker_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    allowed_routing_keys: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    allowed_data_classes: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    allowed_connection_ids: Mapped[list[str]] = mapped_column(
        JSON_TYPE, default=list, nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grace_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("worker_credentials.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_state_type_generation", "state", "task_type", "analysis_generation_id"),
        Index(
            "ix_tasks_failed_recovery",
            "last_error_class",
            "updated_at",
            "id",
            postgresql_where=text("state = 'FAILED'"),
            sqlite_where=text("state = 'FAILED'"),
        ),
        Index(
            "ix_tasks_runnable_route",
            "routing_key",
            "data_class",
            "priority",
            "available_at",
            postgresql_where=text("state IN ('QUEUED', 'RETRY_WAITING')"),
        ),
        Index(
            "ix_tasks_expired_lease",
            "lease_expires_at",
            postgresql_where=text("state IN ('RUNNING', 'CANCEL_REQUESTED')"),
        ),
        Index("ix_tasks_state_updated_at", "state", "updated_at"),
        # The invariant sweep's two lease contradictions, each an index over
        # what should be an empty set, instead of a scan of every task.
        Index(
            "ix_tasks_lease_residue",
            "updated_at",
            "id",
            postgresql_where=text(
                "state NOT IN ('RUNNING', 'CANCEL_REQUESTED') AND "
                "(lease_id IS NOT NULL OR leased_to_worker_id IS NOT NULL "
                "OR lease_expires_at IS NOT NULL)"
            ),
            sqlite_where=text(
                "state NOT IN ('RUNNING', 'CANCEL_REQUESTED') AND "
                "(lease_id IS NOT NULL OR leased_to_worker_id IS NOT NULL "
                "OR lease_expires_at IS NOT NULL)"
            ),
        ),
        Index(
            "ix_tasks_lease_missing",
            "updated_at",
            "id",
            postgresql_where=text(
                "state IN ('RUNNING', 'CANCEL_REQUESTED') AND "
                "(lease_id IS NULL OR leased_to_worker_id IS NULL OR lease_expires_at IS NULL)"
            ),
            sqlite_where=text(
                "state IN ('RUNNING', 'CANCEL_REQUESTED') AND "
                "(lease_id IS NULL OR leased_to_worker_id IS NULL OR lease_expires_at IS NULL)"
            ),
        ),
        Index("ix_tasks_connection_state", "connection_id", "state"),
        Index("ix_tasks_batch_state", "processing_batch_id", "state"),
        Index("ix_tasks_asset_revision_state", "asset_id", "asset_revision_id", "state"),
        Index(
            "uq_tasks_active_primary_generation",
            "analysis_generation_id",
            unique=True,
            postgresql_where=text(
                "analysis_generation_id IS NOT NULL "
                "AND task_type = 'asset.primary_review' "
                "AND state IN ('PLANNED', 'QUEUED', 'RUNNING', "
                "'RETRY_WAITING', 'CANCEL_REQUESTED')"
            ),
            sqlite_where=text(
                "analysis_generation_id IS NOT NULL "
                "AND task_type = 'asset.primary_review' "
                "AND state IN ('PLANNED', 'QUEUED', 'RUNNING', "
                "'RETRY_WAITING', 'CANCEL_REQUESTED')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(256), unique=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cleanup_campaigns.id", ondelete="SET NULL"), index=True
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    processing_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_batches.id", ondelete="SET NULL"), index=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="SET NULL"), index=True
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    asset_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), index=True
    )
    analysis_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_generations.id", ondelete="SET NULL"), index=True
    )
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(64), default="metadata", nullable=False)
    data_class: Mapped[str] = mapped_column(String(32), default="metadata-only", nullable=False)
    estimated_work_class: Mapped[str] = mapped_column(String(32), default="short", nullable=False)
    completion_role: Mapped[str] = mapped_column(String(32), default="core", nullable=False)
    target_ref: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[TaskState] = mapped_column(
        Enum(TaskState, native_enum=False), default=TaskState.PLANNED, nullable=False
    )
    progress: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    leased_to_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(128))
