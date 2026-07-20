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
    parser = argparse.ArgumentParser(prog="phd", description="PhD searcher pipeline stages")
    parser.add_argument("stage", choices=[*_STAGES, "all"])
    parser.add_argument("--limit", type=int, default=None, help="max items to process in this run")
    parser.add_argument("--name", default=None, help="only universities whose name matches (ILIKE)")
    args = parser.parse_args()

    load_dotenv()  # run da host: porta .env in os.environ (litellm legge es. AZURE_API_VERSION da lì)
    from phd_searcher.dependency import container  # import qui: Settings legge l'env solo a runtime

    names = [s for s in _STAGES if s != "ingest"] if args.stage == "all" else [args.stage]
    for name in names:
        print(f"--- stage: {name}")
        asyncio.run(_STAGES[name](container, limit=args.limit, name_like=args.name))


if __name__ == "__main__":  # nel container il progetto non è installato: python -m phd_searcher.pipeline.cli
    main()
