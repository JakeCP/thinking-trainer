"""uv run python -m app.ingest [content/curriculum.json] [--lens 3 --lens 17] [--refetch] [--re-extract] [--originals-dir DIR]"""

import argparse
import logging
import os
import sys
from pathlib import Path

from app.config import load_settings
from app.db import make_engine, make_sessionmaker
from app.ingest.pipeline import run
from app.llm import Llm
from app.migrate import upgrade


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="python -m app.ingest", description="Fetch and extract the problem bank.")
    parser.add_argument("curriculum", nargs="?", type=Path, default=settings.content_dir / "curriculum.json")
    parser.add_argument("--lens", type=int, action="append", help="only these lens ids (repeatable)")
    parser.add_argument("--refetch", action="store_true", help="re-download documents even if cached")
    parser.add_argument("--re-extract", action="store_true", help="re-run extraction on documents already extracted (reviewed problems are kept)")
    parser.add_argument("--originals-dir", type=Path, help="directory holding substitute_exercises.html")
    args = parser.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # Without this, every document would be fetched and then fail extraction.
        sys.exit("ANTHROPIC_API_KEY isn't set. Add it to .env (or the environment) and run again.")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade(settings.database_url)
    db = make_sessionmaker(make_engine(settings.database_url))()
    report = run(
        db,
        Llm(settings.prompts_dir),
        settings,
        args.curriculum,
        lens_ids=set(args.lens) if args.lens else None,
        refetch=args.refetch,
        re_extract=args.re_extract,
        originals_dir=args.originals_dir,
    )
    print("\n".join(report.lines()))
    print("Review flagged problems at /review.")


if __name__ == "__main__":
    main()
