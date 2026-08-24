"""Safe local destination for generated reports."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class ExportConfig(BaseModel):
    root: Path = Path("/app/exports")
