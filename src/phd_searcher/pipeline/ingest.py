"""Ingestion progressiva: batch di atenei per notorietà, indicizzati subito.

A differenza di `all` (ogni stadio completa l'intero dataset prima del successivo),
qui ogni batch attraversa discovery → schema → scrape → index: le posizioni degli
atenei già processati sono cercabili mentre il resto è ancora in caricamento.
"""

from __future__ import annotations

from injector import Injector

from phd_searcher.pipeline import discovery, index, schema_gen, scrape

_BATCH = 5


async def run(container: Injector, *, limit: int | None = None, name_like: str | None = None) -> int:
    processed = 0
    while limit is None or processed < limit:
        n = _BATCH if limit is None else min(_BATCH, limit - processed)
        found = await discovery.run(container, limit=n, name_like=name_like)
        generated = await schema_gen.run(container, limit=n, name_like=name_like)
        await scrape.run(container, limit=n, name_like=name_like)
        await index.run(container)
        # found/generated contano il progresso (non i tentativi): 0+0 = frontiera esaurita
        if found == 0 and generated == 0:
            break
        processed += n
        print(f"ingest: batch completato ({processed} atenei avanzati)")
    return processed
