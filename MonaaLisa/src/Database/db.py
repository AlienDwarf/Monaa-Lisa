import os
import re
import sys
from datetime import datetime, timedelta, timezone
from sqlalchemy import bindparam, case, create_engine, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from Database.db_models import (
    DBPaper, DBEmbedding, DBUncaughtPaper, DBPaperCitation, DBPaperReference,
)
from util.logger import Logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

load_dotenv()

logger = Logger("Database")

# Placeholder title for papers whose arXiv metadata is unknown
UNKNOWN_TITLE = "[Unknown]"

# DATABASE_URL remains env-driven for Docker; do not move into config.ini
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_pre_ping=True,
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


"""
21-September-2025 - Basti
Abstract: Converts a datetime to naive UTC (if no timezone info)
Args: dt: datetime 
Returns: datetime in UTC without tzinfo or None if input was None
"""
def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return dt


# ---------------------------------------------------------------------------
# New functions for the refactored pipeline – feb 2026 – Basti
# ---------------------------------------------------------------------------


"""
Abstract: Checks whether the paper table is empty.
Returns: bool -> True if no papers exist in the DB.
Raises on DB errors – a failed check must NOT be mistaken for "empty",
otherwise a temporary DB problem would trigger a full re-download/re-import.
"""
def is_database_empty() -> bool:
    session = SessionLocal()
    try:
        return session.query(DBPaper.id).first() is None
    finally:
        session.close()


DATASET_IMPORT_KEY = "dataset_import"
DATASET_IMPORT_COMPLETE = "complete"


"""
Abstract: Reads a value from the app_state table (None if not set).
Raises on DB errors (the caller decides how to react).
"""
def get_app_state(key: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_state WHERE key = :k"), {"k": key}).first()
        return row[0] if row else None


"""
Abstract: Sets a value in the app_state table (insert or update).
"""
def set_app_state(key: str, value: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_state (key, value, updated_at) VALUES (:k, :v, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """),
            {"k": key, "v": value},
        )


"""
Abstract: True once the initial dataset import ran to the end.
"""
def is_dataset_import_complete() -> bool:
    return get_app_state(DATASET_IMPORT_KEY) == DATASET_IMPORT_COMPLETE


"""
Abstract: Returns the most recent published date across all papers.
Returns: datetime | None
"""
def get_newest_paper_date():
    from sqlalchemy import func as sa_func
    session = SessionLocal()
    try:
        result = session.query(sa_func.max(DBPaper.published)).scalar()
        return result
    except Exception as e:
        logger.error(f"Failed to get newest paper date: {e}")
        return None
    finally:
        session.close()


"""
Abstract: Returns the total number of papers in the DB.
Returns: int
"""
def get_paper_count() -> int:
    session = SessionLocal()
    try:
        return session.query(DBPaper.id).count()
    except Exception:
        return 0
    finally:
        session.close()


"""
Abstract: Returns uncaught papers whose last_checked is older than the given
          interval (or was never checked), limited to those below max_retries.
          Papers that reached max_retries stay in the table ("parked") and are
          no longer returned – they are kept on purpose instead of being deleted,
          so no paper silently disappears from the system.
Args:
- retry_interval_days: minimum days since last check
Returns: list[DBUncaughtPaper]
"""
def get_uncaught_papers_due(retry_interval_days: int = 14) -> list:
    session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retry_interval_days)
        rows = (
            session.query(DBUncaughtPaper)
            .filter(DBUncaughtPaper.retry_count < DBUncaughtPaper.max_retries)
            .filter(
                (DBUncaughtPaper.last_checked.is_(None)) | (DBUncaughtPaper.last_checked <= cutoff)
            )
            .all()
        )
        # Detach from session so caller can use them freely
        session.expunge_all()
        return list(rows)
    except Exception as e:
        logger.error(f"Failed to query uncaught papers: {e}")
        return []
    finally:
        session.close()


"""
Abstract: Counts uncaught papers that exhausted their retries (kept, not retried).
Returns: int
"""
def count_parked_uncaught() -> int:
    session = SessionLocal()
    try:
        return (
            session.query(DBUncaughtPaper.id)
            .filter(DBUncaughtPaper.retry_count >= DBUncaughtPaper.max_retries)
            .count()
        )
    except Exception as e:
        logger.error(f"Failed to count parked uncaught papers: {e}")
        return 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Batch operations for the incremental pipeline (Nov 2026)
#
# Previously every paper needed several round trips (exists-check, insert,
# one INSERT per citation link, ...). These functions handle a whole batch
# in a single transaction and use INSERT ... ON CONFLICT, which also makes
# concurrent jobs (incremental update + uncaught retry) safe.
# ---------------------------------------------------------------------------

_ID_CHUNK = 5000      # ids per IN/ANY query
_LINK_CHUNK = 5000    # rows per multi-row INSERT for link tables


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def split_categories(categories: str | None) -> list[str]:
    """Splits a category string ("cs.LG stat.ML" or "cs.LG, stat.ML") into a list."""
    if not categories:
        return []
    return [c for c in re.split(r"[\s,]+", categories.strip()) if c]


"""
Abstract: Returns the subset of the given entry_ids that already exist in the
          paper table or in the uncaught_paper table (one query per 5000 ids).
          Uncaught papers are handled by the retry job, so the incremental
          update must not send them to SemanticScholar again.
Args:
- entry_ids: list[str]
Returns: set[str]
"""
def get_known_entry_ids(entry_ids: list[str]) -> set[str]:
    known: set[str] = set()
    if not entry_ids:
        return known
    unique_ids = list(dict.fromkeys(entry_ids))
    with engine.connect() as conn:
        for chunk in _chunks(unique_ids, _ID_CHUNK):
            for table in (DBPaper.__table__, DBUncaughtPaper.__table__):
                rows = conn.execute(
                    select(table.c.entry_id).where(table.c.entry_id.in_(chunk))
                )
                known.update(r[0] for r in rows)
    return known


def _paper_upsert_stmt(rows: list[dict]):
    t = DBPaper.__table__
    stmt = pg_insert(t).values(rows)
    ex = stmt.excluded
    keep_if_null = [
        "authors", "abstract", "categories", "published", "updated", "doi",
        "journal_ref", "license", "url", "s2_id", "tsne", "primary_category",
    ]
    set_ = {col: func.coalesce(getattr(ex, col), t.c[col]) for col in keep_if_null}
    # Placeholder title must not overwrite a real one
    set_["title"] = case((ex.title == UNKNOWN_TITLE, t.c.title), else_=ex.title)
    set_["category_list"] = case(
        (func.cardinality(ex.category_list) > 0, ex.category_list),
        else_=t.c.category_list,
    )
    set_["non_arxiv_citation_count"] = ex.non_arxiv_citation_count
    set_["non_arxiv_reference_count"] = ex.non_arxiv_reference_count
    return stmt.on_conflict_do_update(index_elements=[t.c.entry_id], set_=set_)


def _insert_links(conn, table, rows: list[dict]):
    for chunk in _chunks(rows, _LINK_CHUNK):
        conn.execute(pg_insert(table).values(chunk).on_conflict_do_nothing())


def _refresh_citation_counts(conn, entry_ids: list[str]):
    for chunk in _chunks(list(dict.fromkeys(entry_ids)), _ID_CHUNK):
        conn.execute(
            text("""
                UPDATE paper p
                   SET arxiv_citation_count = sub.cnt
                  FROM (SELECT belonging_paper_entry_id AS eid, COUNT(*)::int AS cnt
                          FROM paper_citation
                         WHERE belonging_paper_entry_id = ANY(:ids)
                         GROUP BY belonging_paper_entry_id) sub
                 WHERE p.entry_id = sub.eid
            """),
            {"ids": chunk},
        )


def _save_processed_batch(conn, records: list[dict]):
    paper_rows = []
    embedding_rows = []
    citation_rows = []   # paper_citation(belonging = cited paper, cited_paper = citing paper)
    reference_rows = []  # paper_reference(belonging = citing paper, referenced = cited paper)
    touched_ids: list[str] = []

    for r in records:
        eid = r["entry_id"]
        cats = split_categories(r.get("categories"))
        tsne = None
        if r.get("tsne_x") is not None and r.get("tsne_y") is not None:
            # dict, NOT json.dumps: the JSON column serialises itself
            tsne = {"x": float(r["tsne_x"]), "y": float(r["tsne_y"]), "method": "umap"}
        paper_rows.append({
            "entry_id": eid,
            "title": r.get("title") or UNKNOWN_TITLE,
            "authors": r.get("authors"),
            "abstract": r.get("abstract"),
            "categories": " ".join(cats) if cats else None,
            "primary_category": cats[0] if cats else None,
            "category_list": cats,
            "published": _to_naive_utc(r.get("published")),
            "updated": _to_naive_utc(r.get("updated")),
            "doi": r.get("doi"),
            "journal_ref": r.get("journal_ref"),
            "license": r.get("license"),
            "url": r.get("url"),
            "s2_id": r.get("s2_id"),
            "non_arxiv_citation_count": r.get("non_arxiv_citation_count"),
            "non_arxiv_reference_count": r.get("non_arxiv_reference_count"),
            "tsne": tsne,
        })
        if r.get("embedding_128d") is not None:
            embedding_rows.append({
                "belonging_paper_entry_id": eid,
                "content": list(r["embedding_128d"]),
            })

        touched_ids.append(eid)
        for cid in dict.fromkeys(r.get("citation_ids") or []):
            # cid cites eid
            citation_rows.append({"belonging_paper_entry_id": eid, "cited_paper_entry_id": cid})
            reference_rows.append({"belonging_paper_entry_id": cid, "referenced_paper_entry_id": eid})
        for rid in dict.fromkeys(r.get("reference_ids") or []):
            # eid cites rid → rid gains a citation (keeps counts of older papers current)
            reference_rows.append({"belonging_paper_entry_id": eid, "referenced_paper_entry_id": rid})
            citation_rows.append({"belonging_paper_entry_id": rid, "cited_paper_entry_id": eid})
            touched_ids.append(rid)

    conn.execute(_paper_upsert_stmt(paper_rows))

    if embedding_rows:
        stmt = pg_insert(DBEmbedding.__table__).values(embedding_rows)
        conn.execute(stmt.on_conflict_do_update(
            index_elements=[DBEmbedding.__table__.c.belonging_paper_entry_id],
            set_={"content": stmt.excluded.content},
        ))

    _insert_links(conn, DBPaperCitation.__table__, citation_rows)
    _insert_links(conn, DBPaperReference.__table__, reference_rows)
    _refresh_citation_counts(conn, touched_ids)


"""
Abstract: Saves fully processed papers (metadata + 128-D embedding + 2-D
          coordinates + citation/reference links) in ONE transaction.
          If the batch fails (e.g. one malformed record), it falls back to
          saving paper by paper so a single bad record cannot sink the batch.
Args:
- records: list of dicts with keys entry_id, title, authors, abstract,
  categories, published, updated, doi, journal_ref, license, url, s2_id,
  non_arxiv_citation_count, non_arxiv_reference_count, embedding_128d,
  tsne_x, tsne_y, citation_ids, reference_ids
Returns: set[str] -> entry_ids that were saved
"""
def save_processed_papers(records: list[dict]) -> set[str]:
    if not records:
        return set()
    try:
        with engine.begin() as conn:
            _save_processed_batch(conn, records)
        return {r["entry_id"] for r in records}
    except Exception as e:
        logger.warning(f"Batch save of {len(records)} papers failed ({e}); retrying paper by paper",
                       exc_info=False)

    saved: set[str] = set()
    for r in records:
        try:
            with engine.begin() as conn:
                _save_processed_batch(conn, [r])
            saved.add(r["entry_id"])
        except Exception as e:
            logger.error(f"Failed to save processed paper {r.get('entry_id')}: {e}")
    return saved


"""
Abstract: Stores papers without SemanticScholar data for later retries.
          Already known entries are left untouched (ON CONFLICT DO NOTHING).
Args:
- rows: list of dicts with keys entry_id, title, authors, abstract,
  categories, published, url, max_retries
Returns: int -> number of rows passed in (0 on error)
"""
def save_uncaught_papers(rows: list[dict]) -> int:
    if not rows:
        return 0
    t = DBUncaughtPaper.__table__
    values = [{
        "entry_id": r["entry_id"],
        "title": r.get("title"),
        "authors": r.get("authors"),
        "abstract": r.get("abstract"),
        "categories": r.get("categories"),
        "published": _to_naive_utc(r.get("published")),
        "url": r.get("url"),
        "retry_count": 0,
        "max_retries": r.get("max_retries", 4),
    } for r in rows]
    try:
        with engine.begin() as conn:
            for chunk in _chunks(values, _LINK_CHUNK):
                conn.execute(pg_insert(t).values(chunk).on_conflict_do_nothing(index_elements=[t.c.entry_id]))
        return len(values)
    except Exception as e:
        logger.error(f"Failed to save {len(values)} uncaught papers: {e}")
        return 0


"""
Abstract: Increments retry_count and sets last_checked for the given uncaught papers.
Args:
- entry_ids: list[str]
Returns: bool
"""
def increment_uncaught_retries(entry_ids: list[str]) -> bool:
    if not entry_ids:
        return True
    t = DBUncaughtPaper.__table__
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with engine.begin() as conn:
            for chunk in _chunks(list(entry_ids), _ID_CHUNK):
                conn.execute(
                    update(t).where(t.c.entry_id.in_(chunk))
                    .values(retry_count=t.c.retry_count + 1, last_checked=now)
                )
        return True
    except Exception as e:
        logger.error(f"Failed to increment retries for {len(entry_ids)} uncaught papers: {e}")
        return False


"""
Abstract: Removes uncaught papers (after they were successfully processed).
Args:
- entry_ids: list[str] | set[str]
Returns: bool
"""
def delete_uncaught_papers(entry_ids) -> bool:
    entry_ids = list(entry_ids)
    if not entry_ids:
        return True
    t = DBUncaughtPaper.__table__
    try:
        with engine.begin() as conn:
            for chunk in _chunks(entry_ids, _ID_CHUNK):
                conn.execute(delete(t).where(t.c.entry_id.in_(chunk)))
        return True
    except Exception as e:
        logger.error(f"Failed to delete {len(entry_ids)} uncaught papers: {e}")
        return False


"""
Abstract: Updates metadata of papers that already exist (e.g. revised versions
          harvested via OAI-PMH): title, authors, abstract, categories, latest
          version date, DOI, journal-ref, license. Coordinates, embeddings and
          citation data are not touched. NULL values keep the stored value.
Args:
- records: list of dicts with entry_id and the metadata keys above
Returns: int -> number of records sent (0 on error)
"""
def update_paper_metadata(records: list[dict]) -> int:
    if not records:
        return 0
    t = DBPaper.__table__
    rows = []
    for r in records:
        cats = split_categories(r.get("categories"))
        rows.append({
            "b_entry_id": r["entry_id"],
            "b_title": r.get("title"),
            "b_authors": r.get("authors"),
            "b_abstract": r.get("abstract"),
            "b_categories": " ".join(cats) if cats else None,
            "b_primary_category": cats[0] if cats else None,
            "b_category_list": cats or None,
            "b_published": _to_naive_utc(r.get("published")),
            "b_updated": _to_naive_utc(r.get("updated")),
            "b_doi": r.get("doi"),
            "b_journal_ref": r.get("journal_ref"),
            "b_license": r.get("license"),
        })
    stmt = (
        update(t)
        .where(t.c.entry_id == bindparam("b_entry_id"))
        .values(
            title=func.coalesce(bindparam("b_title"), t.c.title),
            authors=func.coalesce(bindparam("b_authors"), t.c.authors),
            abstract=func.coalesce(bindparam("b_abstract"), t.c.abstract),
            categories=func.coalesce(bindparam("b_categories"), t.c.categories),
            primary_category=func.coalesce(bindparam("b_primary_category"), t.c.primary_category),
            category_list=func.coalesce(bindparam("b_category_list", type_=t.c.category_list.type), t.c.category_list),
            # Erstveröffentlichung nur setzen, wenn sie fehlt
            published=func.coalesce(t.c.published, bindparam("b_published")),
            updated=func.coalesce(bindparam("b_updated"), t.c.updated),
            doi=func.coalesce(bindparam("b_doi"), t.c.doi),
            journal_ref=func.coalesce(bindparam("b_journal_ref"), t.c.journal_ref),
            license=func.coalesce(bindparam("b_license"), t.c.license),
        )
    )
    try:
        with engine.begin() as conn:
            for chunk in _chunks(rows, _LINK_CHUNK):
                conn.execute(stmt, chunk)
        return len(rows)
    except Exception as e:
        logger.error(f"Failed to update metadata of {len(rows)} papers: {e}")
        return 0


# Wird beim Container-Start als Verbindungstest ausgeführt (docker-compose: python Database/db.py).
# Tabellen legt ausschließlich Prisma an (prisma migrate deploy).
if __name__ == "__main__":
    try:
        with engine.connect() as conn:
            logger.info("Connection to database successful.")
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        sys.exit(1)
