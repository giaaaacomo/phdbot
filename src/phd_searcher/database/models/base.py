"""Declarative base for all ORM models. Define models in this package and import
them in `models/__init__.py` so Alembic autogenerate picks them up."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
