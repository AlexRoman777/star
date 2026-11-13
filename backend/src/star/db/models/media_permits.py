from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from star.db.base import Base


class MediaPermit(Base):
    __tablename__ = "media_permits"

    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
