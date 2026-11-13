"""
Abstract: One-off completeness check of the database against all of arXiv.

    Harvests every arXiv record via OAI-PMH and queues papers that are missing
    from the database into ``uncaught_paper``. The regular retry job (see
    main.py) then fetches their embeddings from SemanticScholar.

    The harvest is resumable: if it is interrupted, simply run it again.
    A full run over all of arXiv takes a few hours (arXiv asks for a pause
    of a few seconds between pages).

Usage (inside the app container):
    python MonaaLisa/src/backfill_arxiv.py --dry-run          # only count what is missing
    python MonaaLisa/src/backfill_arxiv.py                    # queue missing papers
    python MonaaLisa/src/backfill_arxiv.py --set cs           # only Computer Science
    python MonaaLisa/src/backfill_arxiv.py --set physics:hep-ph --update-metadata

    docker compose run --rm app python MonaaLisa/src/backfill_arxiv.py --dry-run
"""

import argparse
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

# arXiv's OAI repository starts here (Identify -> earliestDatestamp)
ARXIV_EARLIEST_DATESTAMP = date(2005, 9, 16)


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue arXiv papers missing from the database")
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat,
                        default=ARXIV_EARLIEST_DATESTAMP,
                        help="start date YYYY-MM-DD (default: %(default)s, start of arXiv's OAI data)")
    parser.add_argument("--set", dest="set_spec", default=None,
                        help='OAI set, e.g. "cs", "math", "physics:hep-ph" (default: all of arXiv)')
    parser.add_argument("--dry-run", action="store_true",
                        help="only count missing papers, write nothing")
    parser.add_argument("--update-metadata", action="store_true",
                        help="also refresh metadata of papers that are already in the DB")
    args = parser.parse_args()

    # Erst nach dem Parsen importieren: Database.db verbindet sich beim Import
    # (braucht DATABASE_URL) – so funktioniert --help auch ohne Datenbank.
    from config import cfg
    from SemanticPaper.api.arxiv_oai import ArxivOAIClient
    from SemanticPaper.pipeline.incremental import backfill_missing_papers

    client = ArxivOAIClient(delay_seconds=cfg.get_float("semanticpaper", "oai_delay_seconds", 3.0))
    totals = backfill_missing_papers(
        client,
        from_date=args.from_date,
        set_spec=args.set_spec,
        dry_run=args.dry_run,
        update_metadata=args.update_metadata,
    )
    print(
        f"\narXiv records: {totals['records']:,}\n"
        f"missing in DB: {totals['missing']:,}\n"
        f"queued:        {totals['queued']:,}\n"
        f"metadata upd.: {totals['updated']:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
