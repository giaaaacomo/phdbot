"""CLI degli stadi pipeline: `phd <stage>` (o `uv run phd <stage>`)."""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

from phd_searcher.pipeline import ingest
from phd_searcher.pipeline.runner import STAGES, StageFn

_STAGES: dict[str, StageFn] = {
    **STAGES,
    "ingest": ingest.run,
}  # batch per fama, ricerca disponibile durante il caricamento


def main() -> None:
    parser = argparse.ArgumentParser(prog="phd", description="PHDBOT pipeline stages")
    parser.add_argument("stage", choices=[*_STAGES, "all"])
    parser.add_argument("--limit", type=int, default=None, help="max items to process in this run")
    for stage in (
        "universities",
        "discovery",
        "schema",
        "scrape",
        "quality",
        "review",
        "evidence",
        "review2",
        "enrich",
        "institutions",
        "index",
    ):
        parser.add_argument(
            f"--{stage}-limit",
            type=int,
            default=None,
            help=f"all only: max items for {stage}",
        )
    parser.add_argument("--max-pages", type=int, default=None, help="scrape only: max pages per paginated source")
    parser.add_argument("--name", default=None, help="only universities whose name matches (ILIKE)")
    args = parser.parse_args()

    load_dotenv()  # run da host: porta .env in os.environ (litellm legge es. AZURE_API_VERSION da lì)
    from phd_searcher.dependency import container  # import qui: Settings legge l'env solo a runtime

    names = [s for s in _STAGES if s != "ingest"] if args.stage == "all" else [args.stage]
    for name in names:
        print(f"--- stage: {name}")
        stage_limit = getattr(args, f"{name}_limit", None) if args.stage == "all" else args.limit
        kwargs = {"limit": stage_limit, "name_like": args.name}
        if name == "scrape":
            kwargs["max_pages"] = args.max_pages
        asyncio.run(_STAGES[name](container, **kwargs))


if __name__ == "__main__":  # nel container il progetto non è installato: python -m phd_searcher.pipeline.cli
    main()
