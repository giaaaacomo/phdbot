"""Normalizza gli item estratti da Crawl4AI nello schema unico Position."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin

from pydantic import BaseModel

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# ponytail: solo formati comuni + mesi inglesi; aggiungere dateparser se le date localizzate contano
_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y")


class NormalizedPosition(BaseModel):
    title: str
    url: str
    description: str = ""
    area: str | None = None
    language: str | None = None
    deadline_raw: str | None = None
    deadline: date | None = None


def parse_deadline(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip()
    m = _ISO_RE.search(text)
    if m:
        try:
            return date.fromisoformat(m.group())
        except ValueError:
            pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_item(item: dict[str, object], *, base_url: str) -> NormalizedPosition | None:
    """Un item estratto -> NormalizedPosition; None se manca titolo o link (item spazzatura).

    Gli item senza href collasserebbero tutti sullo stesso url (UNIQUE) sovrascrivendosi.
    """
    title = _text(item.get("title"))
    href = _text(item.get("url"))
    if not title or not href:
        return None
    deadline_raw = _text(item.get("deadline"))[:256] or None
    return NormalizedPosition(
        title=title,
        url=urljoin(base_url, href),
        description=_text(item.get("description")),
        area=_text(item.get("area"))[:256] or None,
        language=_text(item.get("language"))[:8] or None,
        deadline_raw=deadline_raw,
        deadline=parse_deadline(deadline_raw),
    )
