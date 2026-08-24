"""Saved refresh → search → export workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from phd_searcher.typedef.pipeline import PipelineStartBody
from phd_searcher.typedef.search import ExportFormat, SearchBody

MacroRunState = Literal["queued", "waiting_pipeline", "running_pipeline", "exporting", "done", "failed"]


def _default_export_formats() -> list[ExportFormat]:
    return ["html"]


class MacroCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2_000)
    refresh: bool = True
    pipeline: PipelineStartBody = Field(default_factory=PipelineStartBody)
    search: SearchBody
    export_formats: list[ExportFormat] = Field(default_factory=_default_export_formats, min_length=1)
    destination: str = Field(default="", max_length=512)

    @field_validator("export_formats")
    @classmethod
    def unique_formats(cls, value: list[ExportFormat]) -> list[ExportFormat]:
        return list(dict.fromkeys(value))

    @field_validator("destination")
    @classmethod
    def relative_destination(cls, value: str) -> str:
        normalized = value.strip().strip("/")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError("destination cannot contain '..'")
        return normalized


class MacroView(MacroCreate):
    id: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class MacroRunView(BaseModel):
    id: int
    macro_id: int
    scheduled_job_id: int | None = None
    state: MacroRunState
    current_step: str | None = None
    pipeline_run_id: int | None = None
    outputs: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
