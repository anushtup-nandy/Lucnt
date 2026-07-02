"""Company ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lucnt.database import Base

if TYPE_CHECKING:
    from lucnt.models.entity import Entity
    from lucnt.models.user import User


class Company(Base):
    """Company (tenant) model."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    plan: Mapped[str] = mapped_column(String(100), default="free", nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default={})
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # relationships
    users: Mapped[list[User]] = relationship("User", back_populates="company")
    entities: Mapped[list[Entity]] = relationship("Entity", back_populates="company")
