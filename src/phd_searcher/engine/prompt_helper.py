"""Render Jinja prompt templates bundled under resources/prompt/<version>/."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "resources" / "prompt"
_env = Environment(loader=FileSystemLoader(str(_PROMPT_DIR)), autoescape=False)


def render_prompt(name: str, *, version: str = "v1", **params: object) -> str:
    return _env.get_template(f"{version}/{name}").render(**params)
