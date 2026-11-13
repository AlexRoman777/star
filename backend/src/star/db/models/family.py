from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from star.core.ids import uuid7
from star.db.base import Base
from star.db.models._base import TimestampMixin


class FamilyRosterMember(Base, TimestampMixin):
    """One owner-configured core family member or pet.

    The roster only names people and pets the owner explicitly registers.
    Attribution to a photo still comes exclusively from Immich person/face
    data; the roster never authorizes visual identity inference. Names and
    aliases stay encrypted at rest like Immich person references.
    """

    __tablename__ = "family_roster_members"
    __table_args__ = (
        CheckConstraint("kind IN ('person', 'pet')", name="ck_family_roster_kind"),
        CheckConstraint("role IN ('parent', 'child', 'pet')", name="ck_family_roster_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted_canonical_name: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Encrypted JSON array of normalized alias strings.
    encrypted_aliases: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    species: Mapped[str | None] = mapped_column(String(64))
    breed: Mapped[str | None] = mapped_column(String(64))
    pronouns: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
