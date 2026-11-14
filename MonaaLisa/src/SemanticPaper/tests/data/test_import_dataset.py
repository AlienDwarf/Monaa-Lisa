import json
from unittest.mock import MagicMock, Mock, patch

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


@pytest.fixture
def fake_db():
    """Patches every DB touchpoint of import_dataset(); returns (state, flushed papers)."""
    state = {}
    flushed = []
    with patch.object(imp, "get_connection", return_value=Mock(dsn="test")), \
         patch.object(imp, "ensure_tables"), \
         patch.object(imp, "_has_app_state", return_value=True), \
         patch.object(imp, "get_state", side_effect=lambda conn, k: state.get(k)), \
         patch.object(imp, "set_state", side_effect=lambda conn, k, v: state.__setitem__(k, v)), \
         patch.object(imp, "flush_papers", side_effect=lambda conn, rows: flushed.extend(r[0] for r in rows)), \
         patch.object(imp, "flush_embeddings"), \
         patch.object(imp, "flush_links"), \
         patch.object(imp, "refresh_derived_columns"), \
         patch("builtins.print"):
        yield state, flushed


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
