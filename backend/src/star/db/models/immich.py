from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import JSON_TYPE, TimestampMixin
from star.domain.enums import (
    AccountPurpose,
    CredentialKind,
    HealthStatus,
)


class ImmichInstance(Base, TimestampMixin):
    __tablename__ = "immich_instances"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    stable_instance_id: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    server_version: Mapped[str | None] = mapped_column(String(64))
    tls_fingerprint: Mapped[str | None] = mapped_column(String(128))
    health: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, native_enum=False), default=HealthStatus.UNKNOWN, nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImmichConnection(Base, TimestampMixin):
    __tablename__ = "immich_connections"
    __table_args__ = (
        UniqueConstraint("instance_id", "immich_user_id"),
        Index("ix_connection_health_last_sync", "health", "last_sync_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_instances.id", ondelete="CASCADE"), nullable=False
    )
    immich_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_label: Mapped[str] = mapped_column(String(256), nullable=False)
    purpose: Mapped[AccountPurpose] = mapped_column(
        Enum(AccountPurpose, native_enum=False), nullable=False
    )
    permission_snapshot: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    read_policy: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    write_policy: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    processing_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    model_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    sync_lease_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, native_enum=False), default=HealthStatus.UNKNOWN, nullable=False
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiCredential(Base, TimestampMixin):
    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("connection_id", "kind", "disabled_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("immich_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[CredentialKind] = mapped_column(
        Enum(CredentialKind, native_enum=False), nullable=False
    )
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    permission_snapshot: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
