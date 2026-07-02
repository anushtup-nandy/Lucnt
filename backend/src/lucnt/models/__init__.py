"""Import every ORM model so Base.metadata is fully populated for Alembic."""

from __future__ import annotations

from lucnt.models.company import Company
from lucnt.models.entity import Entity
from lucnt.models.user import User, UserRole

__all__ = ["Company", "Entity", "User", "UserRole"]
