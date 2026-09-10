"""Audited department pages whose opportunities are absent from central boards.

Keep each advert's own evidence and canonical detail link. Do not turn the
surrounding admissions instructions, spontaneous invitations or archives into
vacancies. A changed layout fails visibly rather than silently emptying a feed.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from phd_searcher.pipeline.normalize import extract_deadline, extract_terms

STATML_PROJECTS_URL = "https://statml.io/admissions/specific-projects/"
DTU_MLSM_JOBS_URL = "https://mlsm.man.dtu.dk/jobs/"
DEPARTMENTAL_URLS = frozenset({STATML_PROJECTS_URL, DTU_MLSM_JOBS_URL})
_ROLE_TITLE = re.compile(r"^(?:(?:fully\s+)?funded\s+)?(?:Ph\.?D\.?|Postdoc(?:toral)?)\b", re.I)


def _item(title: str, blocks: list[Tag], *, portal: str) -> dict[str, object]:
    description = "\n\n".join(block.get_text(" ", strip=True) for block in blocks)
    # Read only the explicit deadline block, never the title's start date.
    deadline_blocks = [b.get_text(" ", strip=True) for b in blocks if "deadline:" in b.get_text().casefold()]
    if len(deadline_blocks) != 1:
        raise RuntimeError(f"{portal}: missing or ambiguous application deadline")
    raw_deadline, deadline = extract_deadline(deadline_blocks[0])
    if deadline is None:
        raise RuntimeError(f"{portal}: unparseable application deadline")
    links: set[str] = set()
    for block in blocks:
        for a in block.select("a[href]"):
            href = str(a["href"])
            parsed = urlsplit(href)
            if parsed.scheme != "https" or parsed.username or parsed.password:
                continue
            if portal == "StatML":
                valid = parsed.netloc == "statml.io" and re.fullmatch(
                    r"/wp-content/uploads/\d{4}/\d{2}/[^/]+\.pdf", parsed.path
                )
            else:
                valid = parsed.netloc == "efzu.fa.em2.oraclecloud.com" and re.fullmatch(
                    r"/hcmUI/CandidateExperience/en/sites/CX_2001/job/\d+", parsed.path
                )
            if valid:
                links.add(href)
    if len(links) != 1:
        raise RuntimeError(f"{portal}: missing or ambiguous official advert link")
    compensation, duration, _ = extract_terms(description)
    return {
        "title": title,
        "url": links.pop(),
        "description": description,
        "deadline": raw_deadline or deadline_blocks[0],
        "__phdbot_deadline_date": deadline.isoformat(),
        "compensation": compensation or "",
        "duration": duration or "",
        "language": "en",
    }


def departmental_items(html: str, source_url: str) -> list[dict[str, object]]:
    if source_url not in DEPARTMENTAL_URLS:
        raise RuntimeError("untrusted departmental source URL")
    soup = BeautifulSoup(html, "html.parser")
    if source_url == STATML_PROJECTS_URL:
        heading = soup.select_one("h1.et_pb_module_header")
        if heading is None or heading.get_text(strip=True) != "Specific projects":
            raise RuntimeError("StatML: specific projects layout missing")
        modules = soup.select(".entry-content .et_pb_text_inner")
        items: list[dict[str, object]] = []
        title: str | None = None
        blocks: list[Tag] = []
        for module in modules:
            text = module.get_text(" ", strip=True)
            if _ROLE_TITLE.match(text) and len(module.select("p")) == 1 and module.select_one("strong"):
                if title is not None:
                    items.append(_item(title, blocks, portal="StatML"))
                # The CDT is shared with Oxford: never attribute its vacancies
                # to Imperial merely because they share this website.
                if not re.search(r"\bat Imperial\b", text):
                    raise RuntimeError("StatML: institution needs verification")
                title, blocks = text, []
            elif title is not None:
                blocks.extend(module.select("p"))
        if title is not None:
            items.append(_item(title, blocks, portal="StatML"))
        if not items:
            raise RuntimeError("StatML: no auditable project blocks")
        return items

    content = soup.select_one(".entry-content .shapely-content")
    heading = None
    if content is not None:
        heading = next(
            (h for h in content.find_all("h5", recursive=False) if h.get_text(strip=True) == "Open opportunities:"),
            None,
        )
    if heading is None:
        raise RuntimeError("DTU MLSM: open opportunities section missing")
    # Only the first live section is authoritative. The page contains archived
    # copies of old 'Open opportunities' sections below 'Past opportunities'.
    blocks = []
    for sibling in heading.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if sibling.name in {"hr", "h5"}:
            break
        if sibling.name != "h6":
            raise RuntimeError("DTU MLSM: vacancy block layout changed")
        blocks.append(sibling)
    if not blocks or not blocks[0].select_one("strong"):
        raise RuntimeError("DTU MLSM: vacancy title missing")
    title = blocks[0].get_text(" ", strip=True)
    if not _ROLE_TITLE.match(title) or any(_ROLE_TITLE.match(b.get_text(" ", strip=True)) for b in blocks[1:]):
        raise RuntimeError("DTU MLSM: ambiguous vacancy boundaries")
    return [_item(title, blocks[1:], portal="DTU MLSM")]
