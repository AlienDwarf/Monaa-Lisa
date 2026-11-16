#!/usr/bin/env python3
"""
Import jsonl into the Postgres database.

Handles the full dataset format including:
    - Paper metadata (entry_id, title, authors, abstract, ...)
    - 128-D embeddings (pre-reduced via PCA from 768-D SPECTER v2)
    - 2-D UMAP coordinates (tsne_x / tsne_y)
    - Citation and reference links (arXiv IDs)

Usage:
    python import_dataset.py [--file Full_Dataset_WithCoordsAndEmbeddings.jsonl] [--batch-size 5000]

Requirements:
    pip install psycopg2-binary  (already in requirements.txt)

Strategy:
    1. Read JSONL line-by-line (memory-safe for 10 GB+)
    2. Batch-insert papers via COPY (fastest Postgres bulk method)
    3. Batch-insert embeddings via COPY
    4. Collect citation / reference links and batch-insert via COPY
    5. Skip duplicates gracefully using a temp table + INSERT ... ON CONFLICT
    6. category_list / primary_category / arxiv_citation_count are computed per
       record, not by an UPDATE over the whole table afterwards
    7. First import into an empty database: non-unique indexes are dropped
       before and rebuilt once at the end (much faster than maintaining them
       row by row); the definitions are kept in app_state until the rebuild
       has finished, so an interrupted import still rebuilds them on resume
"""

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
from tqdm import tqdm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dt(value: str | None) -> str | None:
    """Return an ISO timestamp string or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.isoformat()
    except Exception:
        return None


def escape_copy(value) -> str:
    """Escape a value for Postgres COPY TSV format."""
    if value is None:
        return "\\N"
    s = str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace("\t", "\\t")
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    return s


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    """Create a psycopg2 connection from DATABASE_URL or individual env vars."""
    url = os.environ.get("DATABASE_URL")
    if url:
        url = url.replace("postgresql+psycopg2://", "postgresql://")
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "monaa"),
        password=os.environ.get("POSTGRES_PASSWORD", "monaa"),
        dbname=os.environ.get("POSTGRES_DB", "monaa"),
    )


def ensure_tables(conn):
    """Create core tables if they don't exist yet and drop link-table FKs."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper (
                id SERIAL PRIMARY KEY,
                entry_id VARCHAR UNIQUE NOT NULL,
                title VARCHAR NOT NULL,
                authors VARCHAR,
                abstract TEXT,
                categories VARCHAR,
                published TIMESTAMP,
                updated TIMESTAMP,
                doi VARCHAR,
                journal_ref VARCHAR,
                license VARCHAR,
                url VARCHAR,
                s2_id VARCHAR UNIQUE,
                non_arxiv_citation_count INTEGER,
                non_arxiv_reference_count INTEGER,
                related_arxiv_ids JSON,
                tsne JSON
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS embedding (
                id SERIAL PRIMARY KEY,
                belonging_paper_entry_id VARCHAR NOT NULL UNIQUE REFERENCES paper(entry_id),
                content JSON NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_citation (
                belonging_paper_entry_id VARCHAR NOT NULL,
                cited_paper_entry_id VARCHAR NOT NULL,
                PRIMARY KEY (belonging_paper_entry_id, cited_paper_entry_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_reference (
                belonging_paper_entry_id VARCHAR NOT NULL,
                referenced_paper_entry_id VARCHAR NOT NULL,
                PRIMARY KEY (belonging_paper_entry_id, referenced_paper_entry_id)
            );
        """)
        for constraint in [
            ("paper_citation", "paper_citation_belonging_paper_entry_id_fkey"),
            ("paper_citation", "paper_citation_cited_paper_entry_id_fkey"),
            ("paper_reference", "paper_reference_belonging_paper_entry_id_fkey"),
            ("paper_reference", "paper_reference_referenced_paper_entry_id_fkey"),
        ]:
            cur.execute(f"ALTER TABLE {constraint[0]} DROP CONSTRAINT IF EXISTS {constraint[1]};")
    conn.commit()
    print("[schema] Tables ensured, FK constraints on link tables dropped.")


# ---------------------------------------------------------------------------
# COPY-based bulk insert
# ---------------------------------------------------------------------------

PAPER_COLS = [
    "entry_id", "title", "authors", "abstract", "categories",
    "published", "updated", "doi", "journal_ref", "license",
    "url", "s2_id", "non_arxiv_citation_count", "non_arxiv_reference_count",
    "related_arxiv_ids", "tsne",
    "category_list", "primary_category", "arxiv_citation_count",
]

EMBEDDING_COLS = ["belonging_paper_entry_id", "content"]
PAPER_CITATION_COLS = ["belonging_paper_entry_id", "cited_paper_entry_id"]
PAPER_REFERENCE_COLS = ["belonging_paper_entry_id", "referenced_paper_entry_id"]


_PAPER_UPSERT_SET = """
    title     = EXCLUDED.title,
    authors   = EXCLUDED.authors,
    abstract  = EXCLUDED.abstract,
    categories = EXCLUDED.categories,
    published = EXCLUDED.published,
    updated   = EXCLUDED.updated,
    doi       = EXCLUDED.doi,
    journal_ref = EXCLUDED.journal_ref,
    license   = EXCLUDED.license,
    url       = EXCLUDED.url,
    s2_id     = EXCLUDED.s2_id,
    non_arxiv_citation_count  = EXCLUDED.non_arxiv_citation_count,
    non_arxiv_reference_count = EXCLUDED.non_arxiv_reference_count,
    related_arxiv_ids = EXCLUDED.related_arxiv_ids,
    tsne      = EXCLUDED.tsne,
    category_list    = EXCLUDED.category_list,
    primary_category = EXCLUDED.primary_category,
    arxiv_citation_count = EXCLUDED.arxiv_citation_count
"""
_S2_IDX = PAPER_COLS.index("s2_id")

_CATEGORY_SEP = re.compile(r"[\s,]+")


def derive_categories(categories) -> tuple[list[str], str | None]:
    """
    (category_list, primary_category) from the categories string - same rule
    as the migration category_list_and_citation_count and the backend's
    deriveCategoryColumns: split on whitespace/commas, first one is primary.
    """
    if isinstance(categories, list):
        categories = " ".join(str(c) for c in categories if c is not None)
    cats = [c for c in _CATEGORY_SEP.split(str(categories or "")) if c]
    return cats, (cats[0] if cats else None)


def pg_text_array(values: list[str]) -> str:
    """Postgres array literal for a TEXT[] column, e.g. {"cs.AI","cs.LG"}."""
    return "{" + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values) + "}"


def _dedupe_papers(rows: list[tuple]) -> list[tuple]:
    """
    One row per entry_id (last wins - ON CONFLICT DO UPDATE cannot touch a row
    twice in one statement) and one row per s2_id (later duplicates get NULL:
    paper.s2_id is UNIQUE). Without this a single duplicate would push the
    whole batch into the slow row-by-row fallback.
    """
    by_id = {row[0]: row for row in rows}
    seen_s2: set = set()
    out = []
    for row in by_id.values():
        sid = row[_S2_IDX]
        if sid is not None:
            if sid in seen_s2:
                row = row[:_S2_IDX] + (None,) + row[_S2_IDX + 1:]
            else:
                seen_s2.add(sid)
        out.append(row)
    return out


def flush_papers(conn, rows: list[tuple]):
    """Bulk-insert papers via temp table + ON CONFLICT."""
    if not rows:
        return
    rows = _dedupe_papers(rows)
    cols = ", ".join(PAPER_COLS)
    # s2_id already owned by ANOTHER paper -> store NULL instead of failing the batch
    # (EXISTS uses the unique index on paper.s2_id)
    select_cols = ", ".join(
        "CASE WHEN EXISTS (SELECT 1 FROM paper p WHERE p.s2_id = t.s2_id AND p.entry_id <> t.entry_id) "
        "THEN NULL ELSE t.s2_id END" if c == "s2_id" else f"t.{c}"
        for c in PAPER_COLS
    )
    try:
        with conn.cursor() as cur:
            # Only the columns we write; generated columns (point_x/point_y) are never touched
            cur.execute("""
                CREATE TEMP TABLE _tmp_paper (LIKE paper INCLUDING DEFAULTS)
                ON COMMIT DROP;
            """)
            cur.execute("ALTER TABLE _tmp_paper DROP COLUMN IF EXISTS id;")

            buf = io.StringIO()
            for row in rows:
                buf.write("\t".join(escape_copy(v) for v in row) + "\n")
            buf.seek(0)
            cur.copy_from(buf, "_tmp_paper", columns=PAPER_COLS)

            cur.execute(f"""
                INSERT INTO paper ({cols})
                SELECT {select_cols} FROM _tmp_paper t
                ON CONFLICT (entry_id) DO UPDATE SET {_PAPER_UPSERT_SET};
            """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[warn] Bulk paper insert failed ({e}), falling back to row-by-row...")
        _flush_papers_rowwise(conn, cols, rows)


def _flush_papers_rowwise(conn, cols: str, rows: list[tuple]):
    """Fallback: insert papers one by one (savepoint per row, ONE commit)."""
    placeholders = ", ".join(["%s"] * len(PAPER_COLS))
    skipped = 0
    try:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute("SAVEPOINT row_sp")
                try:
                    cur.execute(f"""
                        INSERT INTO paper ({cols}) VALUES ({placeholders})
                        ON CONFLICT (entry_id) DO UPDATE SET {_PAPER_UPSERT_SET};
                    """, row)
                    cur.execute("RELEASE SAVEPOINT row_sp")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                    skipped += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if skipped:
        print(f"[warn] Skipped {skipped} papers due to constraint violations.")


def flush_embeddings(conn, rows: list[tuple]):
    """Bulk-insert embeddings via temp table + ON CONFLICT."""
    if not rows:
        return
    rows = list({row[0]: row for row in rows}.values())   # one row per paper
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE _tmp_embedding (
                    belonging_paper_entry_id TEXT NOT NULL,
                    content TEXT NOT NULL
                ) ON COMMIT DROP;
            """)
            buf = io.StringIO()
            for row in rows:
                buf.write(f"{escape_copy(row[0])}\t{escape_copy(row[1])}\n")
            buf.seek(0)
            cur.copy_from(buf, "_tmp_embedding", columns=EMBEDDING_COLS)
            cur.execute("""
                INSERT INTO embedding (belonging_paper_entry_id, content)
                SELECT belonging_paper_entry_id, content::vector FROM _tmp_embedding
                ON CONFLICT (belonging_paper_entry_id) DO UPDATE SET
                    content = EXCLUDED.content;
            """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[warn] Bulk embedding insert failed ({e}), falling back to row-by-row...")
        skipped = 0
        try:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute("SAVEPOINT row_sp")
                    try:
                        cur.execute("""
                            INSERT INTO embedding (belonging_paper_entry_id, content)
                            VALUES (%s, %s::vector)
                            ON CONFLICT (belonging_paper_entry_id) DO UPDATE SET content = EXCLUDED.content;
                        """, row)
                        cur.execute("RELEASE SAVEPOINT row_sp")
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                        skipped += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if skipped:
            print(f"[warn] Skipped {skipped} embeddings due to errors.")


def flush_links(conn, table: str, cols: list[str], rows: list[tuple]):
    """Bulk-insert citation/reference links via temp table + ON CONFLICT."""
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            tmp = f"_tmp_{table}"
            cur.execute(f"""
                CREATE TEMP TABLE {tmp} (
                    {cols[0]} TEXT NOT NULL,
                    {cols[1]} TEXT NOT NULL
                ) ON COMMIT DROP;
            """)
            buf = io.StringIO()
            for row in rows:
                buf.write(f"{escape_copy(row[0])}\t{escape_copy(row[1])}\n")
            buf.seek(0)
            cur.copy_from(buf, tmp, columns=cols)
            col_list = ", ".join(cols)
            cur.execute(f"""
                INSERT INTO {table} ({col_list})
                SELECT {col_list} FROM {tmp}
                ON CONFLICT DO NOTHING;
            """)
        conn.commit()
    except Exception:
        # leave the connection usable; the import stops and resumes from the last checkpoint
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Import state (app_state table, created by the Prisma migrations)
# ---------------------------------------------------------------------------

DATASET_IMPORT_KEY = "dataset_import"          # same keys as Database/db.py
DATASET_IMPORT_COMPLETE = "complete"
DATASET_IMPORT_OFFSET_KEY = "dataset_import_offset"


def _has_app_state(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('app_state') IS NOT NULL")
        ok = cur.fetchone()[0]
    conn.commit()
    return bool(ok)


def get_state(conn, key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_state WHERE key = %s", (key,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def set_state(conn, key: str, value: str):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (key, value))
    conn.commit()


def _load_checkpoint(conn, filepath: str, file_size: int) -> tuple[int, int]:
    """(byte offset, line number) to resume from; (0, 0) if none or for another file."""
    raw = get_state(conn, DATASET_IMPORT_OFFSET_KEY)
    if not raw:
        return 0, 0
    try:
        cp = json.loads(raw)
    except ValueError:
        return 0, 0
    if cp.get("file") != os.path.basename(filepath) or cp.get("size") != file_size:
        print("[resume] Stored checkpoint belongs to a different file - starting from the beginning.")
        return 0, 0
    return int(cp.get("offset", 0)), int(cp.get("line", 0))


def _save_checkpoint(conn, filepath: str, file_size: int, offset: int, line_no: int):
    set_state(conn, DATASET_IMPORT_OFFSET_KEY, json.dumps({
        "file": os.path.basename(filepath), "size": file_size, "offset": offset, "line": line_no,
    }))


def require_derived_columns(conn):
    """
    The paper rows carry category_list / primary_category /
    arxiv_citation_count, which only exist after the Prisma migrations.
    Fail early with a clear message instead of on the first COPY.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'paper'
               AND column_name IN ('category_list', 'primary_category', 'arxiv_citation_count')
        """)
        ok = cur.fetchone()[0] == 3
    conn.commit()
    if not ok:
        raise RuntimeError("paper.category_list / primary_category / arxiv_citation_count missing - "
                           "run `prisma migrate deploy` before importing")


def reconcile_citation_counts(conn):
    """
    arxiv_citation_count is set per record. A paper that appears in several
    lines keeps only the last line's count, while paper_citation collects the
    links of all of them - fix just those papers (stored count is never too
    high, so only rows with links need checking).
    """
    print("[derive] Reconciling arxiv_citation_count with paper_citation ...")
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            WITH cc AS (
                SELECT belonging_paper_entry_id AS entry_id, COUNT(*)::int AS cnt
                  FROM paper_citation
                 GROUP BY belonging_paper_entry_id
            )
            UPDATE paper p
               SET arxiv_citation_count = cc.cnt
              FROM cc
             WHERE p.entry_id = cc.entry_id
               AND p.arxiv_citation_count <> cc.cnt
        """)
        print(f"[derive] {cur.rowcount:,} papers corrected in {time.time() - t0:.1f}s")
    conn.commit()


# ---------------------------------------------------------------------------
# Deferred indexes (first import into an empty database)
# ---------------------------------------------------------------------------

DEFERRED_INDEXES_KEY = "dataset_import_deferred_indexes"
INDEXED_TABLES = ["paper", "embedding", "paper_citation", "paper_reference"]


def _is_paper_table_empty(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT NOT EXISTS (SELECT 1 FROM paper)")
        empty = cur.fetchone()[0]
    conn.commit()
    return bool(empty)


def _secondary_indexes(conn) -> list[dict]:
    """
    Non-unique indexes of the import tables. Primary keys and unique indexes
    stay: ON CONFLICT (entry_id), the s2_id check and the link tables'
    duplicate handling depend on them.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ic.relname, pg_get_indexdef(x.indexrelid)
              FROM pg_index x
              JOIN pg_class ic ON ic.oid = x.indexrelid
              JOIN pg_class t  ON t.oid  = x.indrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = current_schema()
               AND t.relname = ANY(%s)
               AND NOT x.indisunique
               AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)
             ORDER BY t.relname, ic.relname
        """, (INDEXED_TABLES,))
        rows = cur.fetchall()
    conn.commit()
    return [{"name": name, "def": definition} for name, definition in rows]


def defer_secondary_indexes(conn):
    """
    Stores the index definitions in app_state FIRST, then drops the indexes.
    If the import dies at any point after this, the stored definitions make
    the resumed import rebuild them (rebuild_secondary_indexes).
    """
    indexes = _secondary_indexes(conn)
    if not indexes:
        return
    set_state(conn, DEFERRED_INDEXES_KEY, json.dumps(indexes))
    with conn.cursor() as cur:
        for idx in indexes:
            cur.execute(f'DROP INDEX IF EXISTS "{idx["name"]}"')
    conn.commit()
    print(f"[index] Dropped {len(indexes)} secondary indexes for the import "
          f"({', '.join(i['name'] for i in indexes)}); they are rebuilt at the end.")


def rebuild_secondary_indexes(conn):
    """
    Recreates every index stored by defer_secondary_indexes (skipping ones
    that already exist, e.g. after an interrupted rebuild), refreshes the
    planner statistics and only then clears the stored definitions.
    """
    raw = get_state(conn, DEFERRED_INDEXES_KEY)
    if not raw:
        return
    indexes = json.loads(raw)
    print(f"[index] Rebuilding {len(indexes)} secondary indexes ...")
    t_all = time.time()
    with conn.cursor() as cur:
        # Session-only: more memory and parallel workers for the index builds
        cur.execute("SET maintenance_work_mem = '1GB'")
        cur.execute("SET max_parallel_maintenance_workers = 4")
    conn.commit()
    for idx in indexes:
        if _index_exists(conn, idx["name"]):
            print(f"[index]   {idx['name']} already exists")
            continue
        t0 = time.time()
        with conn.cursor() as cur:
            cur.execute(idx["def"])
        conn.commit()
        print(f"[index]   {idx['name']} built in {time.time() - t0:.1f}s")

    missing = [i["name"] for i in indexes if not _index_exists(conn, i["name"])]
    if missing:
        raise RuntimeError(f"Indexes missing after rebuild: {', '.join(missing)}")

    with conn.cursor() as cur:
        for table in INDEXED_TABLES:
            cur.execute(f"ANALYZE {table}")
    conn.commit()
    set_state(conn, DEFERRED_INDEXES_KEY, "")
    print(f"[index] All secondary indexes rebuilt in {time.time() - t_all:.1f}s")


def _index_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f'"{name}"',))
        exists = cur.fetchone()[0]
    conn.commit()
    return bool(exists)


# ---------------------------------------------------------------------------
# Main import loop
# ---------------------------------------------------------------------------

def parse_record(rec: dict):
    """
    Converts one JSONL record into (paper_row, embedding_row | None,
    citation_ids, reference_ids), or None if the record has no id.
    Robust against null / non-string values (e.g. "title": null, numeric ids).
    """
    # Support both old ("id") and new ("entry_id") field name
    entry_id = str(rec.get("entry_id") or rec.get("id") or "").strip()
    if not entry_id:
        return None

    title = str(rec.get("title") or "").strip()

    # Related arXiv IDs (from Semantic Scholar)
    related_arxiv_ids = rec.get("related_arxiv_ids")
    related_arxiv_json = json.dumps(related_arxiv_ids) if isinstance(related_arxiv_ids, list) and related_arxiv_ids else None

    # Build tsne JSON from tsne_x / tsne_y if present
    tsne_x = rec.get("tsne_x")
    tsne_y = rec.get("tsne_y")
    tsne_json = None
    if tsne_x is not None and tsne_y is not None:
        tsne_json = json.dumps({"x": tsne_x, "y": tsne_y, "method": "umap"})

    citations = [c for c in (str(x).strip() for x in (rec.get("citations") or []) if x is not None) if c]
    references = [r for r in (str(x).strip() for x in (rec.get("references") or []) if x is not None) if r]

    category_list, primary_category = derive_categories(rec.get("categories"))

    paper_row = (
        entry_id, title, rec.get("authors"), rec.get("abstract"), rec.get("categories"),
        parse_dt(rec.get("published")), parse_dt(rec.get("updated")),
        rec.get("doi"), rec.get("journal_ref"), rec.get("license"), rec.get("url"),
        rec.get("s2_id") or None,
        rec.get("non_arxiv_citation_count"), rec.get("non_arxiv_reference_count"),
        related_arxiv_json, tsne_json,
        # arxiv_citation_count = number of paper_citation rows this record
        # creates (duplicates collapse via the primary key)
        pg_text_array(category_list), primary_category, len(set(citations)),
    )

    # Embedding (128-D vector)
    embedding_vec = rec.get("embedding")
    embedding_row = None
    if embedding_vec and isinstance(embedding_vec, list):
        embedding_row = (entry_id, json.dumps(embedding_vec))

    return paper_row, embedding_row, citations, references


def import_dataset(filepath: str, batch_size: int = 20000):
    """
    Streams the JSONL file into Postgres. Every ``batch_size`` papers all
    buffers are flushed and the byte offset is stored in app_state, so an
    interrupted import continues at the last checkpoint instead of starting
    over (re-imported rows are idempotent upserts). At the end the
    ``dataset_import=complete`` marker is set, so the app does not download
    and import the dataset again on its next start.
    """
    conn = get_connection()
    print(f"[db] Connected to {conn.dsn}")
    ensure_tables(conn)
    require_derived_columns(conn)

    use_state = _has_app_state(conn)
    if not use_state:
        print("[warn] app_state table missing (run `prisma migrate deploy`) - import is not resumable "
              "and will not be marked complete.")

    paper_batch: list[tuple] = []
    embedding_batch: list[tuple] = []
    citation_batch: list[tuple] = []
    reference_batch: list[tuple] = []

    total_papers = 0
    total_embeddings = 0
    total_citations = 0
    total_references = 0
    t0 = time.time()

    file_size = os.path.getsize(filepath)
    offset, line_no = _load_checkpoint(conn, filepath, file_size) if use_state else (0, 0)
    if offset:
        print(f"[resume] Continuing import at line {line_no + 1:,} (byte {offset:,} of {file_size:,})")

    # Only with app_state: the stored definitions are what guarantees the
    # rebuild, even if this run is interrupted. A resumed import finds them
    # there and keeps going without the indexes.
    if use_state and not offset and not get_state(conn, DEFERRED_INDEXES_KEY) and _is_paper_table_empty(conn):
        defer_secondary_indexes(conn)

    def flush_all():
        flush_papers(conn, paper_batch)
        paper_batch.clear()
        flush_embeddings(conn, embedding_batch)
        embedding_batch.clear()
        flush_links(conn, "paper_citation", PAPER_CITATION_COLS, citation_batch)
        citation_batch.clear()
        flush_links(conn, "paper_reference", PAPER_REFERENCE_COLS, reference_batch)
        reference_batch.clear()

    # Binary mode: the byte offset of every line is known exactly (text-mode
    # tell() is not usable while iterating), so the checkpoint can seek().
    with open(filepath, "rb") as fh:
        fh.seek(offset)
        pbar = tqdm(fh, total=None, unit="papers", mininterval=1.0, desc="Importing")

        for raw in pbar:
            offset += len(raw)
            line_no += 1
            line = raw.strip()
            if not line:
                continue

            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"[warn] Skipping line {line_no}: {e}")
                continue
            if not isinstance(rec, dict):
                print(f"[warn] Skipping line {line_no}: not a JSON object")
                continue

            parsed = parse_record(rec)
            if parsed is None:
                continue
            paper_row, embedding_row, citations, references = parsed
            entry_id = paper_row[0]

            paper_batch.append(paper_row)
            total_papers += 1
            if embedding_row is not None:
                embedding_batch.append(embedding_row)
                total_embeddings += 1
            # Citations (arXiv IDs this paper is cited by)
            for cit_id in citations:
                citation_batch.append((entry_id, cit_id))
            total_citations += len(citations)
            # References (arXiv IDs this paper references)
            for ref_id in references:
                reference_batch.append((entry_id, ref_id))
            total_references += len(references)

            # Checkpoint: flush everything, then remember where we are
            if len(paper_batch) >= batch_size:
                flush_all()
                if use_state:
                    _save_checkpoint(conn, filepath, file_size, offset, line_no)

            # Link buffers can grow much faster than the paper buffer
            if len(citation_batch) >= batch_size * 5:
                flush_links(conn, "paper_citation", PAPER_CITATION_COLS, citation_batch)
                citation_batch.clear()

            if len(reference_batch) >= batch_size * 5:
                flush_links(conn, "paper_reference", PAPER_REFERENCE_COLS, reference_batch)
                reference_batch.clear()

            # Update progress bar description with stats
            if line_no % 1000 == 0:
                pbar.set_postfix({
                    "papers": f"{total_papers:,}",
                    "cits": f"{total_citations:,}",
                    "refs": f"{total_references:,}"
                })

    # Flush remaining
    flush_all()
    if use_state:
        _save_checkpoint(conn, filepath, file_size, offset, line_no)

    reconcile_citation_counts(conn)

    if use_state:
        # Before the complete marker: an import is only done once its indexes exist
        rebuild_secondary_indexes(conn)
        set_state(conn, DATASET_IMPORT_KEY, DATASET_IMPORT_COMPLETE)
        set_state(conn, DATASET_IMPORT_OFFSET_KEY, "")

    elapsed = time.time() - t0
    print(
        f"\n[done] Imported {total_papers:,} papers, "
        f"{total_embeddings:,} embeddings, "
        f"{total_citations:,} citations, {total_references:,} references "
        f"in {elapsed:.1f}s"
    )
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import MonaaLisa JSONL dataset into Postgres")
    parser.add_argument(
        "--file", "-f",
        default="Full_Dataset_WithCoordsAndEmbeddings.jsonl",
        help="Path to the JSONL file (default: Full_Dataset_WithCoordsAndEmbeddings.jsonl)",
    )
    parser.add_argument(
        "--batch-size", "-b",
        type=int,
        default=20000,
        help="Number of papers per batch INSERT (default: 20000)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"[error] File not found: {args.file}")
        sys.exit(1)

    import_dataset(args.file, args.batch_size)
