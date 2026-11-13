import sys
from datetime import date, datetime
from unittest.mock import Mock, MagicMock, patch

import pytest

# Database.db creates a SQLAlchemy engine at module level, which requires
# DATABASE_URL.  We inject a MagicMock into sys.modules BEFORE importing
# incremental.py so that the real module is never loaded during tests.
sys.modules.setdefault("Database.db", MagicMock())

from MonaaLisa.src.SemanticPaper.pipeline import incremental as incremental_module
from MonaaLisa.src.SemanticPaper.pipeline.incremental import (
    INCREMENTAL_STATE,
    UNCAUGHT_RETRY_LAST_RUN,
    SemanticScholarUnavailable,
    backfill_missing_papers,
    harvest,
    run_incremental_update,
    retry_uncaught_papers,
)

MODULE = "MonaaLisa.src.SemanticPaper.pipeline.incremental"

# Use the classes exactly as incremental.py imported them (the package is
# importable under two paths in tests, which would create two distinct classes).
OAIPage = incremental_module.OAIPage
BadResumptionToken = incremental_module.BadResumptionToken


def _rec(entry_id, **extra):
    """One parsed OAI record as produced by arxiv_oai.parse_record()."""
    r = {"entry_id": entry_id, "title": f"Title {entry_id}", "authors": "A, B",
         "abstract": "abs", "categories": "cs.LG stat.ML",
         "published": datetime(2026, 10, 1), "updated": datetime(2026, 10, 1),
         "doi": None, "journal_ref": None, "license": None,
         "url": f"https://arxiv.org/pdf/{entry_id}"}
    r.update(extra)
    return r


class FakeOAI:
    """Stands in for ArxivOAIClient; records the arguments of iter_pages()."""

    def __init__(self, pages, fail_with_token=False):
        self.pages = pages
        self.calls = []
        self.fail_with_token = fail_with_token

    def iter_pages(self, from_date=None, until_date=None, set_spec=None, resumption_token=None):
        self.calls.append((from_date, until_date, set_spec, resumption_token))
        if resumption_token and self.fail_with_token:
            raise BadResumptionToken("badResumptionToken", "expired")
        yield from self.pages


@pytest.fixture
def state():
    """In-memory replacement for the app_state table."""
    store = {}
    with patch(f"{MODULE}.get_app_state", side_effect=lambda k: store.get(k)), \
         patch(f"{MODULE}.set_app_state", side_effect=lambda k, v: store.__setitem__(k, v)), \
         patch(f"{MODULE}._today_utc", return_value=date(2026, 11, 12)):
        yield store


def _s2_found(arxiv_id="2401.00001", **extra):
    """One 'found' item as returned by SemanticScholarAPI.fetch_batch_detailed()."""
    item = {"arxiv_id": arxiv_id, "embedding_768d": [0.1] * 768,
            "s2_id": "s2_123", "non_arxiv_citation_count": 5,
            "non_arxiv_reference_count": 3,
            "citation_arxiv_ids": [], "reference_arxiv_ids": []}
    item.update(extra)
    return item


def _uncaught(entry_id, **fields):
    """One row as yielded by iter_uncaught_papers_due() (plain dict, selected columns)."""
    return {
        "id": fields.get("id", 1),
        "entry_id": entry_id,
        "title": fields.get("title", "Test"),
        "authors": fields.get("authors", "Author"),
        "abstract": fields.get("abstract", "Abstract"),
        "categories": fields.get("categories", "cs.AI"),
        "published": fields.get("published", datetime(2024, 1, 1)),
        "url": fields.get("url", f"http://arxiv.org/pdf/{entry_id}"),
    }


def _pages(*pages):
    """side_effect for iter_uncaught_papers_due: yields the given pages."""
    return lambda **kw: iter(pages)


class TestHarvest:
    """Resumable page loop on top of the OAI client."""

    def test_stores_token_after_each_page(self, state):
        oai = FakeOAI([OAIPage([_rec("1")], [], "tok1"), OAIPage([_rec("2")], ["9"], None)])
        seen_tokens = []

        stats = harvest(oai, "x", date(2026, 10, 1),
                        on_page=lambda p: seen_tokens.append(state.get("x_token")))

        assert stats["pages"] == 2 and stats["records"] == 2 and stats["deleted"] == 1
        assert seen_tokens == [None, "tok1"]          # token of page 1 stored before page 2
        assert state["x_token"] == ""                 # finished
        assert oai.calls[0] == (date(2026, 10, 1), date(2026, 11, 12), None, None)

    def test_resumes_with_stored_token_and_original_until(self, state):
        state["x_token"] = "tok-old"
        state["x_until"] = "2026-11-01"
        oai = FakeOAI([OAIPage([_rec("1")], [], None)])

        stats = harvest(oai, "x", date(2026, 10, 1), on_page=lambda p: None)

        assert oai.calls[0][3] == "tok-old"
        assert stats["until"] == date(2026, 11, 1)    # not today: the token belongs to the old range

    def test_expired_token_restarts_same_range(self, state):
        state["x_token"] = "tok-old"
        state["x_until"] = "2026-11-01"
        oai = FakeOAI([OAIPage([_rec("1")], [], None)], fail_with_token=True)

        stats = harvest(oai, "x", date(2026, 10, 1), on_page=lambda p: None)

        assert [c[3] for c in oai.calls] == ["tok-old", None]
        assert stats["pages"] == 1

    def test_dry_run_does_not_touch_state(self, state):
        state["x_token"] = "tok-old"
        oai = FakeOAI([OAIPage([_rec("1")], [], "tok1"), OAIPage([], [], None)])

        harvest(oai, "x", date(2026, 10, 1), on_page=lambda p: None, persist=False)

        assert oai.calls[0][3] is None
        assert state == {"x_token": "tok-old"}


class TestRunIncrementalUpdate:

    @patch(f"{MODULE}.get_paper_count")
    @patch(f"{MODULE}.get_newest_paper_date", return_value=None)
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_db_is_empty(self, mock_logger, mock_newest, mock_count, state):
        oai = FakeOAI([])
        run_incremental_update(oai, Mock(), Mock())
        mock_logger.warning.assert_called_once()
        assert oai.calls == []

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", side_effect=lambda recs: {r["entry_id"] for r in recs})
    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", return_value={"old.1"})
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2026, 10, 5, 12))
    @patch(f"{MODULE}.logger")
    def test_new_papers_processed_known_papers_updated(self, mock_logger, mock_newest, mock_count,
                                                       mock_cfg, mock_known, mock_update,
                                                       mock_save, mock_uncaught, state):
        mock_cfg.get_int.return_value = 100
        oai = FakeOAI([OAIPage([_rec("old.1", title="Revised"), _rec("2610.1")], [], None)])
        s2 = Mock()
        s2.fetch_batch_detailed.return_value = ([_s2_found("2610.1")], [], [])
        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.0] * 128, (1.0, 2.0))]

        run_incremental_update(oai, s2, pipeline)

        # first run starts one day before the newest paper
        assert oai.calls[0][0] == date(2026, 10, 4)
        mock_update.assert_called_once()
        assert [r["entry_id"] for r in mock_update.call_args[0][0]] == ["old.1"]
        assert s2.fetch_batch_detailed.call_args[0][0] == ["2610.1"]
        saved = mock_save.call_args[0][0][0]
        assert saved["title"] == "Title 2610.1" and saved["categories"] == "cs.LG stat.ML"
        assert (saved["tsne_x"], saved["tsne_y"]) == (1.0, 2.0)
        # next run continues at the date harvested up to
        assert state[f"{INCREMENTAL_STATE}_from"] == "2026-11-12"
        mock_uncaught.assert_not_called()

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_continues_from_stored_date_and_queues_unprocessed(self, mock_logger, mock_count, mock_cfg,
                                                               mock_known, mock_update, mock_uncaught, state):
        mock_cfg.get_int.return_value = 4
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("2610.1"), _rec("2610.2")], [], None)])
        s2 = Mock()
        s2.fetch_batch_detailed.return_value = ([], ["2610.1"], ["2610.2"])
        pipeline = Mock()

        run_incremental_update(oai, s2, pipeline)

        assert oai.calls[0][0] == date(2026, 10, 20)
        rows = {r["entry_id"]: r for r in mock_uncaught.call_args[0][0]}
        assert set(rows) == {"2610.1", "2610.2"}
        assert rows["2610.1"]["title"] == "Title 2610.1"   # metadata kept for the retry job
        assert rows["2610.2"]["max_retries"] == 4
        pipeline.batch_process.assert_not_called()

    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", side_effect=lambda ids: set(ids))
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_known_papers_never_reach_semantic_scholar(self, mock_logger, mock_count,
                                                       mock_known, mock_update, state):
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("a"), _rec("b")], [], None)])
        s2 = Mock()

        run_incremental_update(oai, s2, Mock())

        s2.fetch_batch_detailed.assert_not_called()
        mock_known.assert_called_once()                 # one DB query per page, no N+1

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", side_effect=lambda recs: {r["entry_id"] for r in recs})
    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_processes_batch_by_batch(self, mock_logger, mock_count, mock_cfg, mock_known,
                                      mock_update, mock_save, mock_uncaught, state):
        """With batch size 2 and 3 new papers, S2 is asked twice and each batch is saved separately."""
        mock_cfg.get_int.return_value = 2
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("1"), _rec("2"), _rec("3")], [], None)])
        s2 = Mock()
        s2.fetch_batch_detailed.side_effect = lambda ids, batch_size: ([_s2_found(i) for i in ids], [], [])
        pipeline = Mock()
        pipeline.batch_process.side_effect = lambda vecs: [([0.0] * 128, (0.0, 0.0))] * len(vecs)

        run_incremental_update(oai, s2, pipeline)

        assert s2.fetch_batch_detailed.call_count == 2
        assert [len(c[0][0]) for c in mock_save.call_args_list] == [2, 1]


class TestBackfillMissingPapers:

    @patch(f"{MODULE}.update_paper_metadata", return_value=1)
    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.get_known_entry_ids", return_value={"have.1"})
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_queues_missing_papers(self, mock_logger, mock_cfg, mock_known, mock_uncaught, mock_update, state):
        mock_cfg.get_int.return_value = 4
        oai = FakeOAI([OAIPage([_rec("have.1"), _rec("miss.1")], [], None)])

        totals = backfill_missing_papers(oai, set_spec="cs", update_metadata=True)

        assert totals == {"records": 2, "missing": 1, "queued": 1, "updated": 1}
        assert [r["entry_id"] for r in mock_uncaught.call_args[0][0]] == ["miss.1"]
        assert oai.calls[0][2] == "cs"

    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}.logger")
    def test_dry_run_writes_nothing(self, mock_logger, mock_known, mock_uncaught, mock_update, state):
        oai = FakeOAI([OAIPage([_rec("miss.1")], [], None)])

        totals = backfill_missing_papers(oai, dry_run=True, update_metadata=True)

        assert totals["missing"] == 1 and totals["queued"] == 0
        mock_uncaught.assert_not_called()
        mock_update.assert_not_called()
        assert state == {}


class TestRetryUncaughtPapers:
    """Tests branching logic inside retry_uncaught_papers()."""

    @patch(f"{MODULE}.iter_uncaught_papers_due", side_effect=_pages())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_no_papers_due(self, mock_logger, mock_cfg, mock_due, state):
        """Nothing due → SemanticScholar must not be called; the run is still recorded."""
        mock_cfg.get_int.return_value = 14

        s2_client = Mock()
        retry_uncaught_papers(s2_client, Mock())

        s2_client.fetch_batch_detailed.assert_not_called()
        assert UNCAUGHT_RETRY_LAST_RUN in state

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", return_value={"2401.00001"})
    @patch(f"{MODULE}.iter_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_rescues_found_papers_and_deletes_from_uncaught(self, mock_logger, mock_cfg,
                                                             mock_due, mock_save,
                                                             mock_delete, mock_increment,
                                                             mock_parked, state):
        """A paper S2 now knows is saved with its stored metadata and removed from uncaught."""
        mock_due.side_effect = _pages([_uncaught("2401.00001", title="Stored title")])
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([_s2_found()], [], [])

        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.1] * 128, (1.0, 2.0))]

        retry_uncaught_papers(s2_client, pipeline)

        mock_save.assert_called_once()
        assert mock_save.call_args[0][0][0]["title"] == "Stored title"
        mock_delete.assert_called_once_with({"2401.00001"})

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", return_value=set())
    @patch(f"{MODULE}.iter_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_save_failure_keeps_paper_and_counts_retry(self, mock_logger, mock_cfg,
                                                       mock_due, mock_save,
                                                       mock_delete, mock_increment,
                                                       mock_parked, state):
        """If saving fails, the paper stays in uncaught but the attempt counts as a retry
        (a paper that can never be saved must not be retried forever)."""
        mock_due.side_effect = _pages([_uncaught("2401.00001")])
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([_s2_found()], [], [])

        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.1] * 128, (1.0, 2.0))]

        retry_uncaught_papers(s2_client, pipeline)

        deleted = mock_delete.call_args[0][0] if mock_delete.called else set()
        assert "2401.00001" not in deleted
        mock_increment.assert_called_once_with(["2401.00001"])

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.iter_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_not_found_and_isolated_request_failures_count_as_retry(self, mock_logger, mock_cfg,
                                                                    mock_due, mock_increment,
                                                                    mock_delete, mock_parked, state):
        """
        Still unknown to S2 → retry counter +1.
        Request for a single paper failed after all HTTP retries → also +1,
        otherwise it would be retried forever.
        """
        mock_due.side_effect = _pages([_uncaught("2401.00001"), _uncaught("2401.00002")])
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([], ["2401.00001"], ["2401.00002"])

        retry_uncaught_papers(s2_client, Mock())

        mock_increment.assert_called_once_with(["2401.00001", "2401.00002"])

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.iter_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_s2_outage_aborts_without_counting_retries(self, mock_logger, mock_cfg, mock_due,
                                                       mock_increment, mock_delete, mock_parked, state):
        """3 batches in a row fail completely → abort, nobody loses a retry, run not recorded."""
        mock_due.side_effect = _pages([_uncaught(str(i), id=i) for i in range(10)])
        mock_cfg.get_int.side_effect = lambda section, key, default: {"s2_batch_size": 2}.get(key, 3)
        s2_client = Mock()
        s2_client.fetch_batch_detailed.side_effect = lambda ids, batch_size: ([], [], list(ids))

        with pytest.raises(SemanticScholarUnavailable):
            retry_uncaught_papers(s2_client, Mock())

        assert s2_client.fetch_batch_detailed.call_count == 3
        mock_increment.assert_not_called()
        assert UNCAUGHT_RETRY_LAST_RUN not in state

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.iter_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_processes_queue_page_by_page(self, mock_logger, mock_cfg, mock_due,
                                          mock_increment, mock_delete, mock_parked, state):
        """Each page from the keyset iterator is processed on its own (bounded memory)."""
        mock_due.side_effect = _pages([_uncaught("a", id=1), _uncaught("b", id=2)], [_uncaught("c", id=3)])
        mock_cfg.get_int.return_value = 100
        s2_client = Mock()
        s2_client.fetch_batch_detailed.side_effect = lambda ids, batch_size: ([], list(ids), [])

        totals = retry_uncaught_papers(s2_client, Mock())

        assert [c[0][0] for c in s2_client.fetch_batch_detailed.call_args_list] == [["a", "b"], ["c"]]
        assert totals["due"] == 3 and totals["not_found"] == 3


class TestHarvestFailureSafety:

    @patch(f"{MODULE}.save_uncaught_papers", side_effect=RuntimeError("db down"))
    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_failed_uncaught_save_does_not_advance_token(self, mock_logger, mock_count, mock_cfg,
                                                         mock_known, mock_update, mock_uncaught, state):
        mock_cfg.get_int.return_value = 4
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("1")], [], "tok1"), OAIPage([_rec("2")], [], None)])
        s2 = Mock()
        s2.fetch_batch_detailed.side_effect = lambda ids, batch_size: ([], list(ids), [])

        with pytest.raises(RuntimeError):
            run_incremental_update(oai, s2, Mock())

        assert not state.get(f"{INCREMENTAL_STATE}_token")        # page 1 not marked done
        assert state[f"{INCREMENTAL_STATE}_from"] == "2026-10-20"  # start date unchanged

    @patch(f"{MODULE}.update_paper_metadata", side_effect=RuntimeError("db down"))
    @patch(f"{MODULE}.get_known_entry_ids", side_effect=lambda ids: set(ids))
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_failed_metadata_update_does_not_advance_token(self, mock_logger, mock_count,
                                                           mock_known, mock_update, state):
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("a")], [], "tok1"), OAIPage([_rec("b")], [], None)])

        with pytest.raises(RuntimeError):
            run_incremental_update(oai, Mock(), Mock())

        assert not state.get(f"{INCREMENTAL_STATE}_token")

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.update_paper_metadata")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.logger")
    def test_s2_outage_stops_harvest_without_queueing(self, mock_logger, mock_count, mock_cfg,
                                                      mock_known, mock_update, mock_uncaught, state):
        mock_cfg.get_int.side_effect = lambda section, key, default: {"s2_batch_size": 1}.get(key, 3)
        state[f"{INCREMENTAL_STATE}_from"] = "2026-10-20"
        oai = FakeOAI([OAIPage([_rec("1"), _rec("2"), _rec("3"), _rec("4")], [], "tok1")])
        s2 = Mock()
        s2.fetch_batch_detailed.side_effect = lambda ids, batch_size: ([], [], list(ids))

        with pytest.raises(SemanticScholarUnavailable):
            run_incremental_update(oai, s2, Mock())

        mock_uncaught.assert_not_called()
        assert not state.get(f"{INCREMENTAL_STATE}_token")


class TestJobLock:

    def test_jobs_share_one_lock(self):
        import threading
        lock = incremental_module._S2_JOB_LOCK
        with patch(f"{MODULE}._retry_uncaught_papers") as inner:
            lock.acquire()          # "incremental update is running"
            try:
                t = threading.Thread(target=retry_uncaught_papers, args=(Mock(), Mock()))
                t.start()
                t.join(0.2)
                assert t.is_alive() and not inner.called   # waits for the running job
            finally:
                lock.release()
            t.join(2)
            assert inner.called
