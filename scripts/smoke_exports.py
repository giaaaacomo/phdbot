"""Read-only end-to-end smoke test for every portable export format."""

from __future__ import annotations

import asyncio

from phd_searcher.dependency import container
from phd_searcher.service.export_service import ExportService
from phd_searcher.typedef.search import ExportBody, ExportFormat, SearchBody


async def smoke() -> None:
    service = container.get(ExportService)
    signatures: dict[ExportFormat, bytes] = {
        "html": b"<!doctype html>",
        "pdf": b"%PDF",
        "csv": b"\xef\xbb\xbf",
        "json": b"{",
    }
    for format_name, signature in signatures.items():
        content, content_type, filename = await service.render(
            ExportBody(
                search=SearchBody(query="research", min_score=0.8, limit=1),
                format=format_name,
                title="PHDBOT export smoke",
            )
        )
        assert content.startswith(signature), (format_name, content[:20])
        print(format_name, len(content), content_type, filename)


if __name__ == "__main__":
    asyncio.run(smoke())
