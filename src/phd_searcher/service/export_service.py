"""Portable reports generated from exactly the same search request as the GUI."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from injector import inject
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phd_searcher.config.export import ExportConfig
from phd_searcher.database.models.position import Position
from phd_searcher.service.search_service import SearchService
from phd_searcher.typedef.search import ExportBody, ExportFormat, SearchBody

_SLUG = re.compile(r"[^a-zA-Z0-9_-]+")


class ExportService:
    @inject
    def __init__(
        self,
        search: SearchService,
        session_maker: async_sessionmaker[AsyncSession],
        config: ExportConfig,
    ) -> None:
        self._search = search
        self._session_maker = session_maker
        self._root = config.root

    async def _payload(self, search_body: SearchBody, title: str | None) -> dict[str, object]:
        result = await self._search.search(search_body)
        ids = [hit.position_id for hit in result.hits]
        details: dict[int, str] = {}
        if ids:
            async with self._session_maker() as session:
                rows = (
                    await session.execute(
                        select(Position.id, Position.full_description, Position.description).where(Position.id.in_(ids))
                    )
                ).all()
            details = {position_id: full_description or description for position_id, full_description, description in rows}
        return {
            "title": title or f"PHDBOT search — {search_body.query}",
            "generated_at": datetime.now(UTC).isoformat(),
            "search": search_body.model_dump(mode="json"),
            "total": result.total,
            "hits": [
                {**hit.model_dump(mode="json"), "description": details.get(hit.position_id, "")}
                for hit in result.hits
            ],
            "institutions": [item.model_dump(mode="json") for item in result.institutions],
        }

    @staticmethod
    def _json(payload: dict[str, object]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode()

    @staticmethod
    def _csv(payload: dict[str, object]) -> bytes:
        output = io.StringIO()
        hits = payload["hits"]
        fieldnames = [
            "number",
            "score",
            "title",
            "university",
            "country",
            "position_type",
            "opportunity_kind",
            "verification_status",
            "uncertainty_percent",
            "uncertainty_flags",
            "source_family_signal",
            "source_family_samples",
            "published_at",
            "first_seen_at",
            "last_seen_at",
            "deadline",
            "duration",
            "compensation",
            "url",
            "description",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for number, raw in enumerate(hits if isinstance(hits, list) else [], 1):
            hit = raw if isinstance(raw, dict) else {}
            writer.writerow({"number": number, **{key: hit.get(key) for key in fieldnames if key != "number"}})
        return output.getvalue().encode("utf-8-sig")

    @staticmethod
    def _html(payload: dict[str, object]) -> bytes:
        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        title = escape(str(payload["title"]))
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>
:root{{--bg:#f5f7fa;--card:#fff;--text:#18202b;--muted:#657080;--line:#dfe4ea;--accent:#245fd3}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12161c;--card:#1b2028;--text:#edf0f4;--muted:#a1a9b5;--line:#303743;--accent:#70a0ff}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:24px}} h1{{font-size:24px;margin:0}} .meta{{color:var(--muted);font-size:12px}}
.bar,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:15px;margin:14px 0}}
.bar{{display:flex;gap:10px;flex-wrap:wrap;position:sticky;top:0;z-index:2}} input,select{{padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text)}}
input{{flex:1;min-width:220px}} a{{color:var(--accent);text-decoration:none}} .score{{float:right;color:var(--muted)}}
summary{{cursor:pointer;margin-top:8px}} .description{{white-space:pre-wrap;max-height:320px;overflow:auto;background:var(--bg);padding:12px;border-radius:8px;margin-top:8px}}
.uncertain{{display:inline-block;color:#9a6700;border:1px solid currentColor;border-radius:12px;padding:1px 7px;margin-left:6px;font-size:11px;font-weight:700}}
.hidden{{display:none}} @media print{{.bar{{display:none}}.description{{max-height:none}}}}
</style></head><body><main><h1>{title}</h1><p class="meta" id="summary"></p>
<div class="bar"><input id="filter" placeholder="Filter exported results"><select id="sort">
<option value="original">relevance</option><option value="deadline">deadline</option>
<option value="published_at">publication date</option><option value="country">country</option>
<option value="compensation_max">compensation</option></select></div>
<section id="institutions"></section><section id="results"></section>
<script>const report={data};
const escText=v=>String(v??""); const safeUrl=v=>/^https?:\\/\\//i.test(v||"")?v:"#";
document.querySelector("#summary").textContent=`Generated ${{new Date(report.generated_at).toLocaleString()}} · ${{report.hits.length}} of ${{report.total}} results · query: ${{report.search.query}}`;
function renderInstitutions(){{const root=document.querySelector("#institutions");if(!report.institutions.length)return;
root.innerHTML="<h2>Related institutions</h2>";for(const x of report.institutions){{const d=document.createElement("div");d.className="card";
const a=document.createElement("a");a.href=safeUrl(x.url);a.target="_blank";a.rel="noopener";a.textContent=x.name;d.append(a);
const m=document.createElement("div");m.className="meta";m.textContent=`${{x.kind}} · ${{x.university}} · ${{x.country}} · ${{x.active_positions}} active positions`;d.append(m);root.append(d)}}}}
function render(){{const needle=document.querySelector("#filter").value.toLowerCase();const sort=document.querySelector("#sort").value;
let rows=report.hits.map((x,i)=>({{...x,number:i+1}})).filter(x=>!needle||JSON.stringify(x).toLowerCase().includes(needle));
if(sort!=="original")rows.sort((a,b)=>String(a[sort]??"").localeCompare(String(b[sort]??""),undefined,{{numeric:true}}));
const root=document.querySelector("#results");root.replaceChildren();for(const x of rows){{const d=document.createElement("article");d.className="card";
const score=document.createElement("span");score.className="score";score.textContent=Number(x.score).toFixed(3);d.append(score);
const a=document.createElement("a");a.href=safeUrl(x.url);a.target="_blank";a.rel="noopener";a.textContent=`${{x.number}}. ${{x.title}}`;d.append(a);
if(Number(x.uncertainty_percent)>0){{const u=document.createElement("span");u.className="uncertain";u.textContent=`Uncertainty ${{x.uncertainty_percent}}%`;u.title=(x.uncertainty_flags||[]).join(", ")||"Automatic verdict not final";d.append(u)}}
if(x.source_family_signal){{const f=document.createElement("span");f.className="uncertain";f.textContent=x.source_family_signal==="supports_opportunity"?"Opportunity route":"Disputed route";f.title=`URL-family prior from ${{x.source_family_samples||0}} sibling labels; not evidence about this exact item`;d.append(f)}}
const m=document.createElement("div");m.className="meta";m.textContent=`${{x.university}} · ${{x.country}} · ${{x.position_type}} · ${{x.opportunity_kind||"unclassified"}} · published ${{x.published_at||"—"}} · first pulled by PHDBOT ${{x.first_seen_at ? new Date(x.first_seen_at).toLocaleString() : "—"}} · last checked ${{x.last_seen_at ? new Date(x.last_seen_at).toLocaleString() : "—"}} · deadline ${{x.deadline||"—"}} · duration ${{x.duration||"—"}} · compensation ${{x.compensation||"—"}}`;d.append(m);
const details=document.createElement("details");const summary=document.createElement("summary");summary.textContent="Show details";const body=document.createElement("div");body.className="description";body.textContent=x.description||"No additional description.";details.append(summary,body);d.append(details);root.append(d)}}}}
document.querySelector("#filter").addEventListener("input",render);document.querySelector("#sort").addEventListener("change",render);renderInstitutions();render();
</script></main></body></html>"""
        return document.encode()

    async def render(self, body: ExportBody) -> tuple[bytes, str, str]:
        payload = await self._payload(body.search, body.title)
        title_slug = _SLUG.sub("-", str(payload["title"])).strip("-")[:80] or "phdbot-results"
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        filename = f"{title_slug}-{stamp}.{body.format}"
        if body.format == "json":
            return self._json(payload), "application/json", filename
        if body.format == "csv":
            return self._csv(payload), "text/csv; charset=utf-8", filename
        html = self._html(payload)
        if body.format == "html":
            return html, "text/html; charset=utf-8", filename
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html.decode(), wait_until="load")
            pdf = await page.pdf(format="A4", print_background=True)
            await browser.close()
        return pdf, "application/pdf", filename

    async def save(
        self,
        search: SearchBody,
        formats: list[ExportFormat],
        *,
        title: str,
        destination: str,
    ) -> list[str]:
        relative = Path(destination or ".")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("macro destination must be relative to the configured export directory")
        directory = (self._root / relative).resolve()
        root = self._root.resolve()
        if directory != root and root not in directory.parents:
            raise ValueError("macro destination escapes the configured export directory")
        directory.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        for format_name in formats:
            content, _content_type, filename = await self.render(
                ExportBody(search=search, format=format_name, title=title)
            )
            path = directory / filename
            path.write_bytes(content)
            outputs.append(str(path.relative_to(root)))
        return outputs
