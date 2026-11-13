from __future__ import annotations

import uuid

from pydantic import ConfigDict, Field, model_validator

from star.domain.schemas._base import ApiSchema


class FamilyRosterMemberInput(ApiSchema):
    """Owner-supplied roster entry; names stay encrypted at rest."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern=r"^(person|pet)$")
    role: str = Field(pattern=r"^(parent|child|pet)$")
    aliases: list[str] = Field(min_length=1, max_length=8)
    species: str | None = Field(default=None, min_length=1, max_length=64)
    breed: str | None = Field(default=None, min_length=1, max_length=64)
    pronouns: str | None = Field(default=None, min_length=1, max_length=32)
    active: bool = True

    @model_validator(mode="after")
    def validate_kind_role(self) -> FamilyRosterMemberInput:
        if (self.kind == "pet") != (self.role == "pet"):
            raise ValueError("pets use role 'pet'; people use 'parent' or 'child'")
        if any(not alias.strip() or len(alias) > 64 for alias in self.aliases):
            raise ValueError("aliases must be non-empty and at most 64 characters")
        return self


class FamilyRosterMemberResponse(ApiSchema):
    id: uuid.UUID
    name: str
    kind: str
    role: str
    aliases: list[str]
    species: str | None
    breed: str | None
    pronouns: str | None
    active: bool


class FamilyRosterResponse(ApiSchema):
    members: list[FamilyRosterMemberResponse]
