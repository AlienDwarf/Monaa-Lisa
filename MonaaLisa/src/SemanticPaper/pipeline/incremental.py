"""
Abstract: Implements the incremental update pipeline that keeps the database
    in sync with arXiv.

    Flow
    ────
    1.  Query the DB for the most recent ``published`` date.
    2.  Fetch every paper published on arXiv after that date.
    3.  Filter out papers already in the DB (by entry_id).
    4.  Batch-request SemanticScholar for SPECTER v2 embeddings (768-D),
        citations, and references.
    5.  For papers with embeddings:
        -> PCA 768
        -> 128-D  
        -> UMAP 128
        -> 2-D  
        -> save to ``paper`` + ``embedding``.
    6.  Papers without SemanticScholar data 
        -> ``uncaught_paper`` table.
    7.  Periodically retry uncaught papers; after max retries they are kept
        ("parked") instead of deleted.

    Steps 4–6 run batch by batch (default 100 papers), so a crash loses at
    most one batch.
        The pipeline is designed to run as a scheduled job (default: monthly)
        via APScheduler inside ``main.py``.
"""

import os
import time
from datetime import datetime, timedelta

import arxiv as arx

from config import cfg
from util.logger import Logger
from SemanticPaper.api.semantic_scholar import SemanticScholarAPI
from SemanticPaper.api.arxiv import ArxivAPI
from SemanticPaper.pipeline.embedding_pipeline import EmbeddingPipeline
from Database.db import (
    UNKNOWN_TITLE,
    get_newest_paper_date,
    get_known_entry_ids,
    save_processed_papers,
    save_uncaught_papers,
    get_uncaught_papers_due,
    increment_uncaught_retries,
    delete_uncaught_papers,
    count_parked_uncaught,
    get_paper_count,
)

logger = Logger("IncrementalPipeline")


# ------------------------------------------------------------------
#  arXiv gap-fill fetcher
# ------------------------------------------------------------------


def _fetch_new_arxiv_papers(arxiv_client: ArxivAPI, since: datetime,
                            max_results: int | None = None) -> list:
    """
    Abstract: Fetches papers from arXiv submitted after ``since``.
        Returns a list of ``arxiv.Result`` objects (not our Paper dataclass)
        so we keep all metadata available.
    Args:
    - arxiv_client: ArxivAPI instance
    - since: datetime – fetch papers newer than this date
    - max_results: optional cap
    Returns: list[arxiv.Result]
    """
    # arXiv Suchdatum-Format: YYYYMMDDHHMMSS
    date_str = since.strftime("%Y%m%d%H%M%S")
    # Suche nach allen Papern, die nach dem übergebenen Datum eingereicht wurden
    query = f"submittedDate:[{date_str} TO 99991231235959]"

    logger.info(f"Querying arXiv for papers since {since.isoformat()} ...")

    search = arx.Search(
        query=query,
        max_results=max_results,
        sort_by=arx.SortCriterion.SubmittedDate,
        sort_order=arx.SortOrder.Ascending,
    )

    results = []
    try:
        client = arxiv_client.client
        for result in client.results(search):
            results.append(result)
            if max_results and len(results) >= max_results:
                break
    except Exception as e:
        # Teilergebnis behalten: wird aufsteigend nach Datum geholt, der nächste
        # Lauf setzt beim neuesten gespeicherten Paper wieder an.
        logger.error(
            f"Error fetching papers from arXiv after {len(results)} results: {e} – "
            f"continuing with the partial result"
        )

    logger.info(f"Fetched {len(results)} papers from arXiv since {since.isoformat()}")
    return results


def _normalize_entry_id(entry_id_or_url: str) -> str:
    """Strips the arxiv.org URL prefix and version suffix."""
    # Entferne die URL-Präfixe, um nur die reine ID zu erhalten
    eid = entry_id_or_url
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "https://arxiv.org/", "http://arxiv.org/"):
        eid = eid.replace(prefix, "")
    # Entferne Versions-Suffix (z.B. v1, v2)
    if "v" in eid.split("/")[-1]:
        parts = eid.rsplit("v", 1)
        if parts[-1].isdigit():
            eid = parts[0]
    return eid



def _categories_of(result) -> str | None:
    """All categories of an arxiv.Result, primary category first, space-separated."""
    if result is None:
        return None
    primary = getattr(result, "primary_category", None)
    others = getattr(result, "categories", None)
    cats: list[str] = []
    if isinstance(primary, str) and primary:
        cats.append(primary)
    if isinstance(others, (list, tuple)):
        cats.extend(c for c in others if isinstance(c, str) and c and c not in cats)
    return " ".join(cats) if cats else None


def _metadata_from_result(result) -> dict:
    """Paper metadata from an arxiv.Result (or placeholders if unknown)."""
    if result is None:
        return {"title": UNKNOWN_TITLE}
    return {
        "title": result.title,
        "authors": ", ".join(str(a) for a in result.authors),
        "abstract": result.summary,
        "categories": _categories_of(result),
        "published": result.published,
        "updated": getattr(result, "updated", None),
        "doi": getattr(result, "doi", None),
        "journal_ref": getattr(result, "journal_ref", None),
        "url": result.pdf_url,
    }


def _metadata_from_uncaught(uncaught) -> dict:
    if uncaught is None:
        return {"title": UNKNOWN_TITLE}
    return {
        "title": uncaught.title or UNKNOWN_TITLE,
        "authors": uncaught.authors,
        "abstract": uncaught.abstract,
        "categories": uncaught.categories,
        "published": uncaught.published,
        "url": uncaught.url,
    }


def _s2_batch_size() -> int:
    return cfg.get_int("semanticpaper", "s2_batch_size", int(os.getenv("S2_BATCH_SIZE", "100")))


def _process_in_batches(arxiv_ids: list[str], metadata: dict[str, dict],
                        s2_client: SemanticScholarAPI, pipeline: EmbeddingPipeline,
                        on_batch_done=None) -> dict:
    """
    Abstract: Streams the IDs through SemanticScholar -> PCA/UMAP -> DB in
        batches. Each batch is saved before the next one is requested, so a
        crash loses at most one batch and memory stays bounded.
    Args:
    - arxiv_ids: IDs to process
    - metadata: entry_id -> paper metadata dict (title, authors, ...)
    - on_batch_done: optional callback(saved: set, not_found: list, failed: list)
    Returns: dict with lists ``saved``, ``not_found``, ``failed``
    """
    batch_size = max(1, _s2_batch_size())
    totals = {"saved": [], "not_found": [], "failed": []}

    for start in range(0, len(arxiv_ids), batch_size):
        chunk = arxiv_ids[start:start + batch_size]
        found, not_found, failed = s2_client.fetch_batch_detailed(chunk, batch_size=batch_size)

        saved: set[str] = set()
        if found:
            batch_results = pipeline.batch_process([item["embedding_768d"] for item in found])
            records = []
            for item, (emb_128_list, (x, y)) in zip(found, batch_results):
                arxiv_id = item["arxiv_id"]
                records.append({
                    **metadata.get(arxiv_id, {"title": UNKNOWN_TITLE}),
                    "entry_id": arxiv_id,
                    "s2_id": item.get("s2_id"),
                    "non_arxiv_citation_count": item.get("non_arxiv_citation_count"),
                    "non_arxiv_reference_count": item.get("non_arxiv_reference_count"),
                    "embedding_128d": emb_128_list,
                    "tsne_x": x,
                    "tsne_y": y,
                    "citation_ids": item.get("citation_arxiv_ids"),
                    "reference_ids": item.get("reference_arxiv_ids"),
                })
            saved = save_processed_papers(records)
            unsaved = [r["entry_id"] for r in records if r["entry_id"] not in saved]
            if unsaved:
                logger.warning(f"Failed to save {len(unsaved)} papers of this batch")
                # keep them for a later attempt instead of losing them
                failed = list(failed) + unsaved

        if on_batch_done:
            on_batch_done(saved, not_found, failed)

        totals["saved"].extend(saved)
        totals["not_found"].extend(not_found)
        totals["failed"].extend(failed)
        logger.info(
            f"Progress: {min(start + batch_size, len(arxiv_ids))}/{len(arxiv_ids)} "
            f"(saved {len(totals['saved'])}, not found {len(totals['not_found'])}, "
            f"failed {len(totals['failed'])})"
        )

    return totals


def run_incremental_update(
    arxiv_client: ArxivAPI,
    s2_client: SemanticScholarAPI,
    pipeline: EmbeddingPipeline,
    max_papers: int | None = None,
):
    """
    Abstract: Executes one full incremental sync cycle.
    Args:
    - arxiv_client: ArxivAPI instance
    - s2_client: SemanticScholarAPI instance
    - pipeline: EmbeddingPipeline (PCA + UMAP)
    - max_papers: optional cap on how many arXiv papers to fetch per cycle
    """
    logger.info("=== Incremental update started ===")
    t0 = time.time()

    newest_date = get_newest_paper_date()
    if newest_date is None:
        # Wenn die Datenbank leer ist, brechen wir ab, da zuerst der initiale Import laufen muss
        logger.warning("No papers in database – skipping incremental update (run initial import first).")
        return

    logger.info(f"Newest paper in DB: {newest_date.isoformat()}")
    paper_count_before = get_paper_count()

    # 1. Hole neue Paper von arXiv
    raw_results = _fetch_new_arxiv_papers(arxiv_client, newest_date, max_results=max_papers)
    if not raw_results:
        logger.info("No new papers found on arXiv. Database is up to date.")
        return

    # 2. Duplikate entfernen: EINE Abfrage gegen paper + uncaught_paper statt
    #    einer Abfrage pro Paper. Uncaught papers übernimmt der Retry-Job.
    result_map: dict[str, arx.Result] = {}
    for result in raw_results:
        result_map.setdefault(_normalize_entry_id(result.entry_id), result)
    known = get_known_entry_ids(list(result_map))
    new_ids = [eid for eid in result_map if eid not in known]

    logger.info(f"{len(new_ids)} genuinely new papers after dedup (out of {len(raw_results)} fetched)")
    if not new_ids:
        logger.info("All fetched papers already exist in the DB.")
        return

    metadata = {eid: _metadata_from_result(result_map[eid]) for eid in new_ids}
    max_retries = cfg.get_int("semanticpaper", "uncaught_max_retries", int(os.getenv("UNCAUGHT_MAX_RETRIES", "4")))

    def store_unprocessed(saved, not_found, failed):
        # Ohne S2-Daten (noch nicht bekannt oder Request fehlgeschlagen) ->
        # uncaught_paper; der Retry-Job versucht es später erneut.
        missing = list(not_found) + list(failed)
        if missing:
            save_uncaught_papers([
                {**metadata.get(eid, {}), "entry_id": eid, "max_retries": max_retries}
                for eid in missing
            ])

    # 3.–5. S2 -> PCA/UMAP -> DB, Batch für Batch
    totals = _process_in_batches(new_ids, metadata, s2_client, pipeline, on_batch_done=store_unprocessed)

    elapsed = time.time() - t0
    paper_count_after = get_paper_count()
    logger.info(
        f"=== Incremental update complete in {elapsed:.1f}s === "
        f"Processed: {len(totals['saved'])}, "
        f"Uncaught: {len(totals['not_found']) + len(totals['failed'])} "
        f"(of which request failures: {len(totals['failed'])}), "
        f"DB total: {paper_count_before} -> {paper_count_after}"
    )


def retry_uncaught_papers(
    s2_client: SemanticScholarAPI,
    pipeline: EmbeddingPipeline,
):
    """
    Abstract: Asks SemanticScholar again for papers that were not available
        before. Found papers are saved and removed from uncaught_paper.
        Papers S2 still does not know get their retry counter increased.
        Request failures (rate limit, network) do NOT count as a retry.
        Papers that reached max_retries are kept ("parked") instead of deleted,
        so nothing silently disappears; they can be re-activated by resetting
        retry_count.
    """
    retry_interval = cfg.get_int(
        "semanticpaper", "uncaught_retry_interval_days",
        int(os.getenv("UNCAUGHT_RETRY_INTERVAL_DAYS", "14"))
    )

    logger.info(f"=== Uncaught paper retry (interval: {retry_interval}d) ===")

    due = get_uncaught_papers_due(retry_interval_days=retry_interval)
    if not due:
        logger.info("No uncaught papers due for retry.")
        return

    logger.info(f"{len(due)} uncaught papers due for retry")
    metadata = {p.entry_id: _metadata_from_uncaught(p) for p in due}

    def update_uncaught(saved, not_found, failed):
        delete_uncaught_papers(saved)
        increment_uncaught_retries(list(not_found))

    totals = _process_in_batches([p.entry_id for p in due], metadata, s2_client, pipeline,
                                 on_batch_done=update_uncaught)

    parked = count_parked_uncaught()
    logger.info(
        f"=== Uncaught retry complete: rescued={len(totals['saved'])}, "
        f"still missing={len(totals['not_found'])}, request failures={len(totals['failed'])}, "
        f"parked after max retries={parked} ==="
    )
