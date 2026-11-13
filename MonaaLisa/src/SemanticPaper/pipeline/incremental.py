"""
Abstract: Implements the incremental update pipeline that keeps the database
    in sync with arXiv.

    Flow
    ────
    1.  Harvest every arXiv record that changed since the last run via
        OAI-PMH (new papers AND revised versions of older papers). The
        position is stored in ``app_state`` so an interrupted run resumes.
    2.  Known papers: update their metadata (title, abstract, categories, ...).
    3.  Unknown papers (new, or older ones missing from the DB):
        batch-request SemanticScholar for SPECTER v2 embeddings (768-D),
        citations and references.
    4.  For papers with embeddings:
        -> PCA 768 -> 128-D
        -> UMAP 128 -> 2-D
        -> save to ``paper`` + ``embedding``.
    5.  Papers without SemanticScholar data -> ``uncaught_paper`` table.
    6.  Periodically retry uncaught papers; after max retries they are kept
        ("parked") instead of deleted.

    Steps 3–5 run batch by batch (default 100 papers), so a crash loses at
    most one batch. The pipeline runs as a scheduled job (default: monthly)
    via APScheduler inside ``main.py``.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

from config import cfg
from util.logger import Logger
from SemanticPaper.api.semantic_scholar import SemanticScholarAPI
from SemanticPaper.api.arxiv_oai import ArxivOAIClient, BadResumptionToken, OAIPage
from SemanticPaper.pipeline.embedding_pipeline import EmbeddingPipeline
from Database.db import (
    UNKNOWN_TITLE,
    get_app_state,
    set_app_state,
    get_newest_paper_date,
    get_known_entry_ids,
    update_paper_metadata,
    save_processed_papers,
    save_uncaught_papers,
    get_uncaught_papers_due,
    increment_uncaught_retries,
    delete_uncaught_papers,
    count_parked_uncaught,
    get_paper_count,
)

logger = Logger("IncrementalPipeline")

# app_state keys
INCREMENTAL_STATE = "oai_incremental"
BACKFILL_STATE = "oai_backfill"
# arXiv's OAI repository starts here (Identify -> earliestDatestamp)
ARXIV_EARLIEST_DATESTAMP = date(2005, 9, 16)


# ------------------------------------------------------------------
#  OAI-PMH harvesting with resumable state
# ------------------------------------------------------------------


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _max_retries() -> int:
    return cfg.get_int("semanticpaper", "uncaught_max_retries", int(os.getenv("UNCAUGHT_MAX_RETRIES", "4")))


def harvest(oai_client: ArxivOAIClient, state_prefix: str, from_date: date, on_page,
            set_spec: str | None = None, persist: bool = True) -> dict:
    """
    Abstract: Harvests all OAI records from ``from_date`` up to today, page by
        page, and calls ``on_page(page)`` for each. After every page the
        resumption token is stored (``<state_prefix>_token``) together with
        the target date (``<state_prefix>_until``), so a crashed or restarted
        run continues where it stopped instead of starting over.
    Returns: dict with ``pages``, ``records``, ``deleted``, ``until``
    """
    token_key, until_key = f"{state_prefix}_token", f"{state_prefix}_until"
    token = (get_app_state(token_key) or None) if persist else None
    until = None
    if token and persist:
        stored_until = get_app_state(until_key)
        until = date.fromisoformat(stored_until) if stored_until else None
        logger.info(f"Resuming interrupted harvest (until {until})")
    if until is None:
        until = _today_utc()
        token = None
        if persist:
            set_app_state(until_key, until.isoformat())
    from_date = min(from_date, until)

    stats = {"pages": 0, "records": 0, "deleted": 0, "until": until}
    try:
        for page in oai_client.iter_pages(from_date, until, set_spec, resumption_token=token):
            on_page(page)
            stats["pages"] += 1
            stats["records"] += len(page.records)
            stats["deleted"] += len(page.deleted_ids)
            if persist:
                set_app_state(token_key, page.resumption_token or "")
            if stats["pages"] % 10 == 0:
                logger.info(f"Harvest progress: {stats['pages']} pages, {stats['records']} records")
    except BadResumptionToken:
        if not token:
            raise
        # Token abgelaufen (gilt ca. 1 Tag): Lauf für denselben Zeitraum neu starten.
        # Bereits verarbeitete Paper werden dabei als bekannt erkannt und übersprungen.
        logger.warning("Stored resumption token expired – restarting harvest of the same date range")
        set_app_state(token_key, "")
        set_app_state(until_key, "")
        return harvest(oai_client, state_prefix, from_date, on_page, set_spec, persist)
    return stats


def _split_known(page: OAIPage) -> tuple[list[dict], list[dict]]:
    """Splits a page into (unknown records, known records) with ONE DB query."""
    known = get_known_entry_ids([r["entry_id"] for r in page.records])
    new, existing = [], []
    for r in page.records:
        (existing if r["entry_id"] in known else new).append(r)
    return new, existing


def _queue_as_uncaught(records: list[dict]):
    max_retries = _max_retries()
    save_uncaught_papers([{**r, "max_retries": max_retries} for r in records])


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
    oai_client: ArxivOAIClient,
    s2_client: SemanticScholarAPI,
    pipeline: EmbeddingPipeline,
):
    """
    Abstract: Executes one incremental sync cycle via OAI-PMH.
        Harvests every record changed since the last successful run, updates
        metadata of known papers and runs unknown papers through
        SemanticScholar -> PCA/UMAP -> DB. The next run starts at the date
        this run harvested up to (inclusive – overlap is deduplicated).
    Args:
    - oai_client: ArxivOAIClient
    - s2_client: SemanticScholarAPI instance
    - pipeline: EmbeddingPipeline (PCA + UMAP)
    """
    logger.info("=== Incremental update started ===")
    t0 = time.time()

    stored_from = get_app_state(f"{INCREMENTAL_STATE}_from")
    if stored_from:
        from_date = date.fromisoformat(stored_from)
    else:
        newest_date = get_newest_paper_date()
        if newest_date is None:
            # Wenn die Datenbank leer ist, brechen wir ab, da zuerst der initiale Import laufen muss
            logger.warning("No papers in database – skipping incremental update (run initial import first).")
            return
        # Erster Lauf: kurz vor dem neuesten Paper beginnen (Überlappung wird dedupliziert)
        from_date = newest_date.date() - timedelta(days=1)

    logger.info(f"Harvesting arXiv changes since {from_date.isoformat()} ...")
    paper_count_before = get_paper_count()
    totals = {"new": 0, "updated": 0, "saved": 0, "not_found": 0, "failed": 0}

    def store_unprocessed(saved, not_found, failed, metadata):
        # Ohne S2-Daten (noch nicht bekannt oder Request fehlgeschlagen) ->
        # uncaught_paper; der Retry-Job versucht es später erneut.
        missing = list(not_found) + list(failed)
        if missing:
            _queue_as_uncaught([{**metadata.get(eid, {}), "entry_id": eid} for eid in missing])

    def on_page(page: OAIPage):
        new, existing = _split_known(page)
        if existing:
            update_paper_metadata(existing)
            totals["updated"] += len(existing)
        if not new:
            return
        totals["new"] += len(new)
        metadata = {r["entry_id"]: r for r in new}
        result = _process_in_batches(
            list(metadata), metadata, s2_client, pipeline,
            on_batch_done=lambda s, nf, f: store_unprocessed(s, nf, f, metadata),
        )
        totals["saved"] += len(result["saved"])
        totals["not_found"] += len(result["not_found"])
        totals["failed"] += len(result["failed"])

    stats = harvest(oai_client, INCREMENTAL_STATE, from_date, on_page)
    # Erst nach vollständigem Durchlauf weiterrücken
    set_app_state(f"{INCREMENTAL_STATE}_from", stats["until"].isoformat())
    set_app_state(f"{INCREMENTAL_STATE}_token", "")
    set_app_state(f"{INCREMENTAL_STATE}_until", "")

    elapsed = time.time() - t0
    logger.info(
        f"=== Incremental update complete in {elapsed:.1f}s === "
        f"Harvested: {stats['records']} records ({stats['pages']} pages, {stats['deleted']} withdrawn), "
        f"already known: {totals['updated']} (metadata refreshed), new: {totals['new']} "
        f"(saved {totals['saved']}, uncaught {totals['not_found'] + totals['failed']}, "
        f"of which request failures {totals['failed']}), "
        f"DB total: {paper_count_before} -> {get_paper_count()}"
    )
    return totals


def backfill_missing_papers(
    oai_client: ArxivOAIClient,
    from_date: date = ARXIV_EARLIEST_DATESTAMP,
    set_spec: str | None = None,
    dry_run: bool = False,
    update_metadata: bool = False,
) -> dict:
    """
    Abstract: One-off completeness check against all of arXiv.
        Harvests every OAI record (optionally only one set, e.g. "cs" or
        "physics:hep-ph") and queues papers missing from the DB into
        uncaught_paper. The regular retry job then fetches their embeddings
        from SemanticScholar in batches. Resumable like the incremental run.
    Args:
    - from_date: start datestamp (default: beginning of arXiv's OAI repository)
    - set_spec: optional OAI set to limit the harvest
    - dry_run: only count, write nothing (state is not persisted either)
    - update_metadata: also refresh metadata of papers already in the DB
    Returns: dict with ``records``, ``missing``, ``queued``, ``updated``
    """
    totals = {"records": 0, "missing": 0, "queued": 0, "updated": 0}

    def on_page(page: OAIPage):
        new, existing = _split_known(page)
        totals["records"] += len(page.records)
        totals["missing"] += len(new)
        if dry_run:
            return
        if new:
            _queue_as_uncaught(new)
            totals["queued"] += len(new)
        if update_metadata and existing:
            totals["updated"] += update_paper_metadata(existing)

    logger.info(f"=== Backfill started (from {from_date}, set={set_spec or 'all'}, dry_run={dry_run}) ===")
    stats = harvest(oai_client, BACKFILL_STATE, from_date, on_page, set_spec=set_spec, persist=not dry_run)
    if not dry_run:
        set_app_state(f"{BACKFILL_STATE}_token", "")
        set_app_state(f"{BACKFILL_STATE}_until", "")
    logger.info(
        f"=== Backfill complete: {totals['records']} arXiv records, {totals['missing']} missing in DB, "
        f"{totals['queued']} queued for SemanticScholar, {totals['updated']} metadata updates, "
        f"{stats['deleted']} withdrawn ==="
    )
    return totals


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
