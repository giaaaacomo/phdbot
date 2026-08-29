"""Controlli strutturali ad alta precisione sugli schemi di estrazione.

La validazione di Crawl4AI verifica che i selettori trovino elementi, non che
quegli elementi siano vacancy.  Questi controlli bloccano soltanto due classi
di errore inequivocabili osservate in produzione: usare ogni nodo del documento
come item e usare la navigazione del sito come lista di opportunita'.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy

SCHEMA_WILDCARD_BASE = "schema_wildcard_base"
SCHEMA_NAVIGATION_BASE = "schema_navigation_base"
SCHEMA_NAVIGATION_TITLE = "schema_navigation_title"

_WILDCARD_BASES = frozenset({"*", "html *", "body *", ":root *"})
_NAVIGATION_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:nav|navigation|menu|breadcrumb|header|footer)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_OPPORTUNITY_TOKEN_RE = re.compile(
    r"(?:job|vacanc|position|opening|opportunit|career|doctoral|phd|postdoc|stellen)",
    re.IGNORECASE,
)
_CSS_COMBINATOR_RE = re.compile(r"\s*(?:[>+~]|\s)\s*")


def _base_selector_targets_anchors(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    selectors = [part.strip() for part in value.split(",") if part.strip()]
    if not selectors:
        return False
    for selector in selectors:
        compounds = [part for part in _CSS_COMBINATOR_RE.split(selector) if part]
        if not compounds or re.match(r"^a(?:$|[#.:\[].*)", compounds[-1], re.I) is None:
            return False
    return True


def repair_base_anchor_url_schema(
    schema: Mapping[str, object],
) -> dict[str, object]:
    """Recover an anchor base element's own href without asking the LLM again.

    Crawl4AI field selectors search descendants only, so an ``a.card`` base
    cannot extract its own href through a normal field. ``baseFields`` is the
    exact schema primitive for attributes on the repeated element itself.
    """
    repaired = deepcopy(dict(schema))
    raw_fields = repaired.get("fields")
    raw_base_fields = repaired.get("baseFields", [])
    if not isinstance(raw_fields, list) or not isinstance(raw_base_fields, list):
        return repaired
    existing_names = {
        str(field.get("name", "")).casefold()
        for field in (*raw_fields, *raw_base_fields)
        if isinstance(field, Mapping)
    }
    if "url" in existing_names or not _base_selector_targets_anchors(
        repaired.get("baseSelector")
    ):
        return repaired
    repaired["baseFields"] = [
        *raw_base_fields,
        {"name": "url", "type": "attribute", "attribute": "href"},
    ]
    return repaired


def _unsafe_navigation_selector(value: object) -> bool:
    if not isinstance(value, str):
        return False
    selector = " ".join(value.split())
    return bool(_NAVIGATION_TOKEN_RE.search(selector)) and not bool(
        _OPPORTUNITY_TOKEN_RE.search(selector)
    )


def schema_quality_issues(schema: Mapping[str, object] | None) -> tuple[str, ...]:
    """Restituisce reason code stabili per strutture certamente pericolose.

    Non prova a decidere se un generico ``li`` sia corretto: quel caso richiede
    il quality gate sui risultati reali. In questo modo un sito con markup
    minimale non viene penalizzato da una sola euristica sul CSS.
    """
    if not schema:
        return ()

    reasons: list[str] = []
    raw_base = schema.get("baseSelector")
    base = " ".join(raw_base.split()).casefold() if isinstance(raw_base, str) else ""
    if base in _WILDCARD_BASES:
        reasons.append(SCHEMA_WILDCARD_BASE)
    if _unsafe_navigation_selector(raw_base):
        reasons.append(SCHEMA_NAVIGATION_BASE)

    raw_fields = schema.get("fields")
    if isinstance(raw_fields, list):
        for field in raw_fields:
            if not isinstance(field, Mapping) or str(field.get("name", "")).casefold() != "title":
                continue
            if _unsafe_navigation_selector(field.get("selector")):
                reasons.append(SCHEMA_NAVIGATION_TITLE)
            break

    return tuple(dict.fromkeys(reasons))
