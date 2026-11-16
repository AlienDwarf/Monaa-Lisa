import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import import_dataset as imp


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class TestParseRecord:

    def test_null_title_and_numeric_id_do_not_crash(self):
        paper_row, emb, cits, refs = imp.parse_record({"id": 1234, "title": None, "citations": [1, None, " 2101.1 "]})
        assert paper_row[0] == "1234" and paper_row[1] == ""
        assert emb is None
        assert cits == ["1", "2101.1"] and refs == []

    def test_record_without_id_is_skipped(self):
        assert imp.parse_record({"title": "x"}) is None

    def test_derived_columns(self):
        paper_row, _, _, _ = imp.parse_record({
            "entry_id": "1", "categories": "cs.AI, cs.LG  stat.ML",
            "citations": ["a", "b", "a", " ", None],
        })
        row = dict(zip(imp.PAPER_COLS, paper_row))
        assert row["category_list"] == '{"cs.AI","cs.LG","stat.ML"}'
        assert row["primary_category"] == "cs.AI"
        assert row["arxiv_citation_count"] == 2      # duplicates collapse like the primary key

    def test_derived_columns_without_categories(self):
        paper_row, _, _, _ = imp.parse_record({"entry_id": "1", "categories": None})
        row = dict(zip(imp.PAPER_COLS, paper_row))
        assert row["category_list"] == "{}"
        assert row["primary_category"] is None
        assert row["arxiv_citation_count"] == 0

    def test_array_literal_escapes_quotes_and_backslashes(self):
        assert imp.pg_text_array(['a"b', "c\\d"]) == '{"a\\"b","c\\\\d"}'


class TestDedupe:

    def test_last_row_per_entry_id_wins_and_duplicate_s2_id_is_nulled(self):
        def row(eid, title, s2):
            r = [None] * len(imp.PAPER_COLS)
            r[0], r[1], r[imp._S2_IDX] = eid, title, s2
            return tuple(r)

        out = imp._dedupe_papers([row("a", "old", "S1"), row("b", "t", "S1"), row("a", "new", "S2")])

        by_id = {r[0]: r for r in out}
        assert len(out) == 2
        assert by_id["a"][1] == "new" and by_id["a"][imp._S2_IDX] == "S2"
        assert by_id["b"][imp._S2_IDX] == "S1"


class TestFlushLinks:

    def test_rolls_back_and_raises_on_error(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            imp.flush_links(conn, "paper_citation", imp.PAPER_CITATION_COLS, [("a", "b")])

        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


SECONDARY = [
    {"name": "paper_title_idx", "def": "CREATE INDEX paper_title_idx ON public.paper USING gin (title gin_trgm_ops)"},
    {"name": "paper_citation_cited_idx", "def": "CREATE INDEX paper_citation_cited_idx ON public.paper_citation USING btree (cited_paper_entry_id)"},
]


@pytest.fixture
def db():
    """
    Patches every DB touchpoint of import_dataset(). The connection's cursor
    tracks DROP/CREATE INDEX in `indexes`; `log` records state writes and
    index DDL in order.
    """
    t = SimpleNamespace(state={}, flushed=[], indexes={i["name"] for i in SECONDARY}, log=[], empty=True)

    def execute(sql, params=None):
        m = re.match(r'\s*DROP INDEX IF EXISTS "(\w+)"', sql)
        if m:
            t.indexes.discard(m.group(1))
            t.log.append(("drop", m.group(1)))
        m = re.match(r"\s*CREATE INDEX (\w+)", sql)
        if m:
            t.indexes.add(m.group(1))
            t.log.append(("create", m.group(1)))

    def set_state(conn, k, v):
        t.state[k] = v
        t.log.append(("state", k, v))

    conn = MagicMock(dsn="test")
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = execute

    with patch.object(imp, "get_connection", return_value=conn), \
         patch.object(imp, "ensure_tables"), \
         patch.object(imp, "require_derived_columns"), \
         patch.object(imp, "_has_app_state", return_value=True), \
         patch.object(imp, "get_state", side_effect=lambda conn, k: t.state.get(k)), \
         patch.object(imp, "set_state", side_effect=set_state), \
         patch.object(imp, "_is_paper_table_empty", side_effect=lambda conn: t.empty), \
         patch.object(imp, "_secondary_indexes", side_effect=lambda conn: [i for i in SECONDARY if i["name"] in t.indexes]), \
         patch.object(imp, "_index_exists", side_effect=lambda conn, name: name in t.indexes), \
         patch.object(imp, "flush_papers", side_effect=lambda conn, rows: t.flushed.extend(r[0] for r in rows)), \
         patch.object(imp, "flush_embeddings"), \
         patch.object(imp, "flush_links"), \
         patch.object(imp, "reconcile_citation_counts"), \
         patch("builtins.print"):
        yield t


@pytest.fixture
def fake_db(db):
    """(state, flushed papers) of the patched database."""
    return db.state, db.flushed


class TestImportDataset:

    def test_sets_complete_marker_at_the_end(self, fake_db, tmp_path):
        state, flushed = fake_db
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": "1", "title": "A"}, {"entry_id": "2", "title": None}])

        imp.import_dataset(str(f), batch_size=10)

        assert flushed == ["1", "2"]
        assert state[imp.DATASET_IMPORT_KEY] == imp.DATASET_IMPORT_COMPLETE
        assert state[imp.DATASET_IMPORT_OFFSET_KEY] == ""

    def test_resumes_after_last_checkpoint(self, fake_db, tmp_path):
        state, flushed = fake_db
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": str(i), "title": "t"} for i in range(5)])

        # first run crashes while flushing the 2nd batch (papers 2, 3)
        calls = {"n": 0}

        def flaky(conn, rows):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db gone")
            flushed.extend(r[0] for r in rows)

        with patch.object(imp, "flush_papers", side_effect=flaky):
            with pytest.raises(RuntimeError):
                imp.import_dataset(str(f), batch_size=2)
        assert flushed == ["0", "1"]
        assert json.loads(state[imp.DATASET_IMPORT_OFFSET_KEY])["line"] == 2
        assert imp.DATASET_IMPORT_KEY not in state

        imp.import_dataset(str(f), batch_size=2)

        assert flushed == ["0", "1", "2", "3", "4"]      # nothing before the checkpoint is re-read
        assert state[imp.DATASET_IMPORT_KEY] == imp.DATASET_IMPORT_COMPLETE

    def test_checkpoint_of_other_file_is_ignored(self, fake_db, tmp_path):
        state, flushed = fake_db
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": "1", "title": "t"}])
        state[imp.DATASET_IMPORT_OFFSET_KEY] = json.dumps({"file": "data.jsonl", "size": 1, "offset": 999, "line": 5})

        imp.import_dataset(str(f), batch_size=10)

        assert flushed == ["1"]


class TestDeferredIndexes:

    def _complete_at(self, log):
        return log.index(("state", imp.DATASET_IMPORT_KEY, imp.DATASET_IMPORT_COMPLETE))

    def test_fresh_import_drops_and_rebuilds_indexes_before_complete(self, db, tmp_path):
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": "1", "title": "t"}])

        imp.import_dataset(str(f), batch_size=10)

        names = {i["name"] for i in SECONDARY}
        assert db.indexes == names
        log = db.log
        stored = log.index(next(e for e in log if e[:2] == ("state", imp.DEFERRED_INDEXES_KEY) and e[2]))
        first_drop = log.index(next(e for e in log if e[0] == "drop"))
        assert stored < first_drop                       # definitions saved before anything is dropped
        created = [i for i, e in enumerate(log) if e[0] == "create"]
        assert len(created) == len(names)
        assert max(created) < self._complete_at(log)     # complete only after every index exists
        assert db.state[imp.DEFERRED_INDEXES_KEY] == ""

    def test_interrupted_import_rebuilds_indexes_on_resume(self, db, tmp_path):
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": str(i), "title": "t"} for i in range(4)])

        calls = {"n": 0}

        def flaky(conn, rows):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db gone")
            db.flushed.extend(r[0] for r in rows)
            db.empty = False

        with patch.object(imp, "flush_papers", side_effect=flaky):
            with pytest.raises(RuntimeError):
                imp.import_dataset(str(f), batch_size=2)
        assert db.indexes == set()                       # dropped and not yet rebuilt
        assert json.loads(db.state[imp.DEFERRED_INDEXES_KEY]) == SECONDARY

        imp.import_dataset(str(f), batch_size=2)

        assert db.indexes == {i["name"] for i in SECONDARY}
        assert db.state[imp.DATASET_IMPORT_KEY] == imp.DATASET_IMPORT_COMPLETE
        assert db.state[imp.DEFERRED_INDEXES_KEY] == ""

    def test_failed_rebuild_is_not_marked_complete(self, db, tmp_path):
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": "1", "title": "t"}])

        with patch.object(imp, "rebuild_secondary_indexes", side_effect=RuntimeError("out of memory")):
            with pytest.raises(RuntimeError):
                imp.import_dataset(str(f), batch_size=10)

        assert imp.DATASET_IMPORT_KEY not in db.state
        assert db.state[imp.DEFERRED_INDEXES_KEY]        # definitions kept for the next attempt

    def test_existing_data_keeps_indexes(self, db, tmp_path):
        db.empty = False
        f = tmp_path / "data.jsonl"
        _write_jsonl(f, [{"entry_id": "1", "title": "t"}])

        imp.import_dataset(str(f), batch_size=10)

        assert not any(e[0] in ("drop", "create") for e in db.log)
        assert db.state[imp.DATASET_IMPORT_KEY] == imp.DATASET_IMPORT_COMPLETE

    def test_rebuild_skips_existing_and_fails_if_one_is_missing(self, db):
        conn = imp.get_connection()
        db.state[imp.DEFERRED_INDEXES_KEY] = json.dumps(SECONDARY)
        db.indexes = {"paper_title_idx"}                  # rebuild was interrupted after the first one

        imp.rebuild_secondary_indexes(conn)
        assert [e for e in db.log if e[0] == "create"] == [("create", "paper_citation_cited_idx")]

        db.indexes.clear()
        db.state[imp.DEFERRED_INDEXES_KEY] = json.dumps(SECONDARY)
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = lambda sql, params=None: None
        with pytest.raises(RuntimeError, match="missing after rebuild"):
            imp.rebuild_secondary_indexes(conn)
        assert db.state[imp.DEFERRED_INDEXES_KEY]
