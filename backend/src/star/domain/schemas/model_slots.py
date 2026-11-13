"""Per-machine model slots: who holds the accelerator, since when, and what is next."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from star.domain.schemas._base import ApiSchema

ModelSlotRole = Literal["primary", "highlights", "advanced", "similarity", "staging"]
ModelSlotState = Literal["active", "resident", "waiting", "idle", "unavailable", "paused"]
ModelSlotExclusiveGroup = Literal["accelerator", "cpu"]
MachineKind = Literal["nvidia", "apple", "other"]


class ModelSlotHandover(ApiSchema):
    """When and why this slot's turn on its exclusive lane changes."""

    reason: str = Field(max_length=256)
    remaining_tasks: int | None = Field(default=None, ge=0)
    estimated_seconds: int | None = Field(default=None, ge=0)
    mean_task_seconds: float | None = Field(default=None, ge=0)
    next_model_key: str | None = Field(default=None, max_length=512)


class ModelSlotResponse(ApiSchema):
    model_key: str = Field(max_length=512)
    label: str = Field(max_length=128)
    role: ModelSlotRole
    runtime: str | None = Field(default=None, max_length=128)
    exclusive_group: ModelSlotExclusiveGroup
    state: ModelSlotState
    worker_ids: list[uuid.UUID] = Field(default_factory=list)
    worker_phase: str | None = Field(default=None, max_length=32)
    lanes: int = Field(ge=1)
    active_tasks: int = Field(ge=0)
    since: datetime | None = None
    active_seconds: int | None = Field(default=None, ge=0)
    queued_tasks: int = Field(ge=0)
    completed_in_block: int = Field(ge=0)
    handover: ModelSlotHandover | None = None


class MachineSlotsResponse(ApiSchema):
    machine_group: str = Field(max_length=128)
    kind: MachineKind
    label: str = Field(max_length=128)
    online: bool
    paused: bool
    worker_ids: list[uuid.UUID] = Field(default_factory=list)
    slots: list[ModelSlotResponse] = Field(default_factory=list)


class ModelSlotsResponse(ApiSchema):
    observed_at: datetime
    machines: list[MachineSlotsResponse] = Field(default_factory=list)
