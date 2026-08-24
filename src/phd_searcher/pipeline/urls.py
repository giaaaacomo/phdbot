"""Regole condivise per distinguere listing HTML da documenti scaricabili."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

_DOCUMENT_SUFFIXES = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".epub",
        ".json",
        ".ods",
        ".odt",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".txt",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)


def is_listing_page_url(url: str) -> bool:
    """False per file/documenti singoli che non possono essere listing HTML ripetute."""
    path = unquote(urlsplit(url).path).lower().rstrip("/")
    return not any(path.endswith(suffix) for suffix in _DOCUMENT_SUFFIXES)
