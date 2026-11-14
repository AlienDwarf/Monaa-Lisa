"""
Abstract: Entry point of the SemanticPaper application (refactored).

New flow:
    1. First run -> download dataset + models from Google Drive -> import into DB
    2. Every run  -> ensure pre-trained models (PCA + UMAP) are present
    3. Load the EmbeddingPipeline (768 ->128 ->2D)
    4. Run an immediate incremental update (arXiv OAI-PMH harvest since last run)
    5. Schedule monthly incremental updates and bi-weekly uncaught-paper retries
    6. Wait for shutdown signal
"""

import os
import sys
import signal
import faulthandler
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from config import cfg
from util.logger import Logger, capture_library_logger
from Database.db import (
    DATASET_IMPORT_COMPLETE,
    DATASET_IMPORT_KEY,
    engine,
    get_app_state,
    is_database_empty,
    is_dataset_import_complete,
    set_app_state,
)
from SemanticPaper.api.semantic_scholar import SemanticScholarAPI
from SemanticPaper.api.arxiv_oai import ArxivOAIClient
from SemanticPaper.data.downloader import (
    ensure_all_downloaded,
    ensure_models_exist,
    get_dataset_path,
    cleanup_dataset,
)
from SemanticPaper.data.importer import run_import
from SemanticPaper.pipeline.embedding_pipeline import EmbeddingPipeline
from SemanticPaper.pipeline.incremental import (
    UNCAUGHT_RETRY_LAST_RUN,
    run_incremental_update,
    retry_uncaught_papers,
)

logger = Logger("Main")
# APScheduler logs job exceptions on its own logger - route them to console + logs/apscheduler.log
capture_library_logger("apscheduler")


def _next_uncaught_retry(interval_days: int) -> datetime:
    """
    The scheduler is in-memory: a plain interval job would first run
    ``interval_days`` after every restart, i.e. practically never on a host
    that restarts more often. The last successful run is stored in app_state
    instead; an overdue (or never run) retry starts right away.
    """
    now = datetime.now()
    try:
        last = get_app_state(UNCAUGHT_RETRY_LAST_RUN)
        last_run = datetime.fromisoformat(last) if last else None
    except Exception as e:
        logger.warning(f"Could not read last uncaught retry run ({e}) - running it now")
        last_run = None
    if last_run is None:
        return now
    if last_run.tzinfo is not None:
        last_run = last_run.astimezone().replace(tzinfo=None)   # local naive, like datetime.now()
    return max(now, last_run + timedelta(days=interval_days))

load_dotenv()

try:
    faulthandler.enable()
except Exception:
    pass


def main():
    log_level = cfg.get("semanticpaper", "log_level", os.getenv("LOG_LEVEL", "DEBUG"))
    logger.set_level(log_level)
    logger.info(f"SemanticPaper starting (log level={log_level})...")

    # ---- 1. Database Check (WAIT for Prisma) ----
    # WICHTIG: Wir erstellen keine Tabellen mehr selbst!
    # Wir warten stattdessen kurz, bis die DB erreichbar ist.
    logger.info("Checking database connection...")
    
    # Einfacher Retry-Loop, falls der DB-Container noch nicht bereit ist
    for i in range(10):
        try:
            with engine.connect() as connection:
                logger.info("Database connection established.")
                break
        except Exception as e:
            logger.warning(f"Database not ready yet, retrying in 2s... ({e})")
            time.sleep(2)
    else:
        logger.error("Could not connect to database after 20s. Exiting.")
        sys.exit(1)

    # ---- 2. First-run: download & import ----
    # Maßgeblich ist der gespeicherte Import-Status, nicht "Tabelle nicht leer":
    # Ein abgebrochener Import wird so beim nächsten Start fortgesetzt
    # (der Import ist idempotent – vorhandene Zeilen werden per Upsert überschrieben).
    # Ein Fehler bei der Prüfung beendet das Programm, statt fälschlich
    # einen kompletten Neu-Import auszulösen.
    try:
        import_complete = is_dataset_import_complete()
        db_empty = is_database_empty()
    except Exception as e:
        logger.error(f"Could not determine import state (did Prisma migrations run?): {e}")
        sys.exit(1)

    if not import_complete:
        if db_empty:
            logger.info("First run detected – database is empty.")
        else:
            logger.warning("Previous dataset import did not finish – resuming import.")
        logger.info("Downloading dataset and pre-trained models...")

        if not ensure_all_downloaded():
            logger.error("Failed to download required files. Exiting.")
            sys.exit(1)

        dataset_path = get_dataset_path()
        batch_size = cfg.get_int("semanticpaper", "import_batch_size", int(os.getenv("IMPORT_BATCH_SIZE", "20000")))
        logger.info(f"Importing dataset from {dataset_path} ...")

        if not run_import(dataset_path, batch_size):
            logger.error("Dataset import failed. Exiting.")
            sys.exit(1)

        set_app_state(DATASET_IMPORT_KEY, DATASET_IMPORT_COMPLETE)
        logger.info("Dataset imported successfully. Cleaning up raw file...")
        cleanup_dataset()
    else:
        logger.info("Existing data found in database.")
        logger.info("Ensuring pre-trained models are available...")
        if not ensure_models_exist():
            logger.error("Failed to ensure ML models are present. Exiting.")
            sys.exit(1)

    # ---- 3. Load the embedding pipeline ----
    logger.info("Loading EmbeddingPipeline (PCA + UMAP)...")
    try:
        pipeline = EmbeddingPipeline()
    except FileNotFoundError as e:
        logger.error(f"Cannot load pipeline models: {e}")
        sys.exit(1)
    logger.info("EmbeddingPipeline ready.")

    # ---- 4. Initialise API clients ----
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    s2_client = SemanticScholarAPI(api_key if api_key else None)
    # arXiv OAI-PMH (offizielle Schnittstelle für Massen-/Inkrementell-Abgleich)
    oai_client = ArxivOAIClient(
        delay_seconds=cfg.get_float("semanticpaper", "oai_delay_seconds", 3.0),
    )

    # ---- 5. Schedule periodic tasks ----
    update_interval_days = cfg.get_int(
        "semanticpaper", "incremental_update_interval_days",
        int(os.getenv("INCREMENTAL_UPDATE_INTERVAL_DAYS", "30"))
    )
    uncaught_interval_days = cfg.get_int(
        "semanticpaper", "uncaught_retry_interval_days",
        int(os.getenv("UNCAUGHT_RETRY_INTERVAL_DAYS", "14"))
    )

    scheduler = BackgroundScheduler()

    # Run incremental update immediately, then on schedule
    scheduler.add_job(
        run_incremental_update,
        "interval",
        days=update_interval_days,
        next_run_time=datetime.now(),
        args=[oai_client, s2_client, pipeline],
        id="incremental_update",
        max_instances=1,
    )
    logger.info(f"Scheduled incremental update: every {update_interval_days} days")

    # Uncaught paper retry (shares a lock with the incremental update, so it
    # waits instead of hitting SemanticScholar at the same time)
    retry_first_run = _next_uncaught_retry(uncaught_interval_days)
    scheduler.add_job(
        retry_uncaught_papers,
        "interval",
        days=uncaught_interval_days,
        next_run_time=retry_first_run,
        args=[s2_client, pipeline],
        id="uncaught_retry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    logger.info(
        f"Scheduled uncaught paper retry: every {uncaught_interval_days} days "
        f"(next run {retry_first_run:%Y-%m-%d %H:%M})"
    )

    scheduler.start()
    logger.info("Scheduler started. System is running.")

    # ---- 6. Wait for shutdown ----
    def shutdown(signum, frame):
        logger.info("Shutdown signal received...")
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Press Ctrl+C to stop.")
    try:
        signal.pause()
    except AttributeError:
        # signal.pause() not available on Windows
        import time
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()