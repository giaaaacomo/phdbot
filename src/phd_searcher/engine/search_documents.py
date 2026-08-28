"""Compact, candidate-specific documents for semantic retrieval."""

from __future__ import annotations

import html
import re

CANDIDATE_SEARCH_DOCUMENT_CONTRACT = "candidate-compact-v2"
INSTITUTION_SEARCH_DOCUMENT_CONTRACT = "institution-compact-v1"
SEARCH_INDEX_CONTRACT_PAYLOAD = "_phdbot_search_index_contract"
DEFAULT_DESCRIPTION_CHARS = 800
DEFAULT_INSTITUTION_CHARS = 2_400
_INSTITUTION_SNIPPET_CHARS = 400
_HTML_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][^>]{0,500}>", re.S)


def clean_search_text(value: str | None, *, max_chars: int | None = None) -> str:
    """Remove small HTML fragments, collapse whitespace and truncate on a word."""
    if not value:
        return ""
    cleaned = " ".join(html.unescape(_HTML_TAG.sub(" ", value)).split())
    if max_chars is None or max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned

    if max_chars == 1:
        return "…"
    content_limit = max_chars - 1
    shortened = cleaned[:content_limit]
    cut_inside_word = (
        not shortened[-1].isspace()
        and not cleaned[content_limit].isspace()
    )
    shortened = shortened.rstrip()
    if cut_inside_word and " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return f"{shortened}…"


def build_candidate_search_document(
    *,
    title: str,
    position_type: str,
    institution: str,
    description: str | None,
    description_chars: int = DEFAULT_DESCRIPTION_CHARS,
) -> str:
    """Build the compact opportunity document embedded into the position index.

    Only fields attributable to the candidate are included.  In particular,
    callers should pass the listing description rather than a complete detail
    page, whose navigation and related listings can dominate short queries.
    """
    fields = (
        ("Title", clean_search_text(title)),
        ("Position type", clean_search_text(position_type)),
        ("Institution", clean_search_text(institution)),
        (
            "Description",
            clean_search_text(description, max_chars=description_chars),
        ),
    )
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def build_institution_search_document(
    *,
    name: str,
    university: str,
    kind: str,
    text: str | None,
    max_chars: int = DEFAULT_INSTITUTION_CHARS,
) -> str:
    """Compact an institution entity while retaining its names and snippets."""
    header_fields = (
        ("Name", clean_search_text(name)),
        ("Institution", clean_search_text(university)),
        ("Entity type", clean_search_text(kind)),
    )
    lines = [f"{label}: {value}" for label, value in header_fields if value]
    seen = {value.casefold() for _, value in header_fields if value}

    for raw_line in (text or "").splitlines():
        current = "\n".join(lines)
        remaining = max_chars - len(current) - len("\nSnippet: ")
        if remaining <= 1:
            break
        snippet = clean_search_text(
            raw_line,
            max_chars=min(_INSTITUTION_SNIPPET_CHARS, remaining),
        )
        if not snippet or snippet.casefold() in seen:
            continue
        lines.append(f"Snippet: {snippet}")
        seen.add(snippet.casefold())

    return "\n".join(lines)
