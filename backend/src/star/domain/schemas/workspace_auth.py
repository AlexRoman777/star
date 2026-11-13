from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from star.domain.enums import (
    Role,
)
from star.domain.schemas._base import ApiSchema

WorkspacePresetKind = Literal["analysis_run", "library_view", "write_selection"]


class WorkspacePresetCreate(ApiSchema):
    kind: WorkspacePresetKind
    name: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class WorkspacePresetUpdate(ApiSchema):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict[str, Any] | None = None
    is_default: bool | None = None


class WorkspacePresetResponse(ApiSchema):
    id: uuid.UUID
    kind: WorkspacePresetKind
    name: str
    payload: dict[str, Any]
    revision: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


class UserSummary(ApiSchema):
    id: uuid.UUID
    username: str
    display_name: str
    role: Role


class SessionResponse(ApiSchema):
    authenticated: bool
    user: UserSummary | None = None


class AuthenticationStatusResponse(ApiSchema):
    administrator_exists: bool
    initial_password_setup_available: bool
    password_login_enabled: bool = True
    local_password_setup_available: bool = False


class PasswordLoginRequest(ApiSchema):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class InitialPasswordSetupRequest(ApiSchema):
    username: str = Field(min_length=3, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=256)


class PasswordChangeRequest(ApiSchema):
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class RegistrationOptionsRequest(ApiSchema):
    username: str = Field(min_length=3, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=256)


class ChallengeResponse(ApiSchema):
    challenge_id: uuid.UUID
    options: dict[str, Any]


class PasskeyVerifyRequest(ApiSchema):
    challenge_id: uuid.UUID
    credential: dict[str, Any]


class LoginOptionsRequest(ApiSchema):
    username: str
