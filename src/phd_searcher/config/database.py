"""Database connection config."""

from __future__ import annotations

from pydantic import BaseModel


class DatabaseConfig(BaseModel):
    url: str  # SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/db
    schema_name: str = "public"
