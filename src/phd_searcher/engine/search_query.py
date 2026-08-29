"""Parsing helpers for user-composed semantic searches."""

from __future__ import annotations

MAX_COMBINED_QUERIES = 8


def split_combined_query(value: str) -> list[str]:
    """Split ``a+b`` OR searches, preserving ``C++`` and escaped ``\\+``."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value) and value[index + 1] == "+":
            current.append("+")
            index += 2
            continue
        if (
            char == "+"
            and (index == 0 or value[index - 1] != "+")
            and (index + 1 == len(value) or value[index + 1] != "+")
        ):
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(char)
        index += 1
    final = "".join(current).strip()
    if final:
        parts.append(final)
    if not parts:
        raise ValueError("enter at least one semantic query")
    if len(parts) > MAX_COMBINED_QUERIES:
        raise ValueError(
            f"combine at most {MAX_COMBINED_QUERIES} semantic queries with +"
        )
    return parts
