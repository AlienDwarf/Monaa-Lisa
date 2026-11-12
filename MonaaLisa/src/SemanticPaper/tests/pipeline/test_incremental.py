import sys
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

# Database.db creates a SQLAlchemy engine at module level, which requires
# DATABASE_URL.  We inject a MagicMock into sys.modules BEFORE importing
# incremental.py so that the real module is never loaded during tests.
sys.modules.setdefault("Database.db", MagicMock())

from MonaaLisa.src.SemanticPaper.pipeline.incremental import (
    _categories_of,
    _fetch_new_arxiv_papers,
    _normalize_entry_id,
    run_incremental_update,
    retry_uncaught_papers,
)

MODULE = "MonaaLisa.src.SemanticPaper.pipeline.incremental"


# because arxiv.Result is a complex object with many fields, we create a helper function

def _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00001v1",
                       title="Test Paper", authors=None, summary="Abstract La La La Wait till I...",
                       primary_category="cs.AI", published=None,
                       pdf_url="http://arxiv.org/pdf/2401.00001v1"):
    """Creates a Mock that mimics an arxiv.Result object with all fields the pipeline accesses."""
    r = Mock()
    r.entry_id = entry_id
    r.title = title
    r.authors = authors or [Mock(__str__=lambda s: "Author A")]
    r.summary = summary
    r.primary_category = primary_category
    r.published = published or datetime(2024, 1, 1)
    r.updated = datetime(2024, 1, 2)
    r.doi = "10.1234/test"
    r.journal_ref = None
    r.pdf_url = pdf_url
    return r


class TestNormalizeEntryId:
    """Tests the URL-stripping and version-suffix logic in _normalize_entry_id()."""

    def test_strips_abs_url_prefix(self):
        """
        Tests: eid = entry_id_or_url.replace("http://arxiv.org/abs/", "")

        The raw entry_id from arXiv is a full URL.  Without stripping,
        DB lookups would fail because we store bare IDs.
        """
        result = _normalize_entry_id("http://arxiv.org/abs/2401.00001v1")

        # The URL prefix must be gone; only the bare ID (possibly with version) remains
        assert "http://arxiv.org" not in result

    def test_strips_plain_url_prefix(self):
        """
        Tests: .replace("http://arxiv.org/", "")

        Some entry_ids use the non-/abs/ URL variant.
        """
        result = _normalize_entry_id("http://arxiv.org/2401.00001v1")

        assert "http://arxiv.org" not in result

    def test_removes_version_suffix(self):
        """
        Tests: if "v" in eid ... parts[-1].isdigit() → strip version

        Version suffixes must be removed so that v1 and v2 of the same
        paper map to the same ID in the database.
        """
        result = _normalize_entry_id("2401.00001v2")

        assert result == "2401.00001"

    def test_preserves_id_without_version(self):
        """
        Tests the False branch: parts[-1].isdigit() is False → keep eid unchanged.

        Not every ID has a version suffix.  A bare ID like "2401.00001"
        contains no 'v' followed by digits, so it must pass through as-is.
        """
        result = _normalize_entry_id("2401.00001")

        assert result == "2401.00001"


class TestFetchNewArxivPapers:
    """Tests conditional logic inside _fetch_new_arxiv_papers()."""

    @patch(f"{MODULE}.arx.Search")
    @patch(f"{MODULE}.logger")
    def test_returns_empty_list_when_no_results(self, mock_logger, mock_search):
        """
        Tests: the for-loop body never executes → returns []

        If arXiv has nothing new, the caller must receive an empty list
        so that downstream ``if not raw_results`` triggers correctly.
        """
        # Mock the API to return an empty iterator, simulating no new papers on arXiv
        arxiv_client = Mock()
        arxiv_client.client.results.return_value = iter([])

        result = _fetch_new_arxiv_papers(arxiv_client, datetime(2024, 1, 1))

        assert result == []
        assert isinstance(result, list)

    @patch(f"{MODULE}.arx.Search")
    @patch(f"{MODULE}.logger")
    def test_respects_max_results_cap(self, mock_logger, mock_search):
        """
        Tests: if max_results and len(results) >= max_results: break

        Without this guard the fetcher would exhaust the entire arXiv
        iterator, consuming unbounded memory and API quota.
        """
        # 10 results available, but we only want 3
        papers = [Mock() for _ in range(10)]
        arxiv_client = Mock()
        arxiv_client.client.results.return_value = iter(papers)

        # We set max_results=3, so the function should stop after collecting 3 papers
        result = _fetch_new_arxiv_papers(arxiv_client, datetime(2024, 1, 1), max_results=3)

        assert len(result) == 3

    @patch(f"{MODULE}.arx.Search")
    @patch(f"{MODULE}.logger")
    def test_catches_exception_and_returns_partial_results(self, mock_logger, mock_search):
        """
        Tests: except Exception as e: logger.error(...)

        If the arXiv API raises mid-iteration, papers collected so far
        must still be returned instead of crashing the pipeline.
        """
        # Iterator that yields 2 valid results, then explodes – simulates a network timeout mid-stream
        def _exploding_iterator():
            yield Mock()
            yield Mock()
            raise ConnectionError("timeout")

        arxiv_client = Mock()
        arxiv_client.client.results.return_value = _exploding_iterator()

        result = _fetch_new_arxiv_papers(arxiv_client, datetime(2024, 1, 1))

        # 2 papers were collected before the exception; they must not be lost
        assert len(result) == 2
        # The error must be logged so operators can investigate
        mock_logger.error.assert_called_once()


def _s2_found(arxiv_id="2401.00001", **extra):
    """One 'found' item as returned by SemanticScholarAPI.fetch_batch_detailed()."""
    item = {"arxiv_id": arxiv_id, "embedding_768d": [0.1] * 768,
            "s2_id": "s2_123", "non_arxiv_citation_count": 5,
            "non_arxiv_reference_count": 3,
            "citation_arxiv_ids": [], "reference_arxiv_ids": []}
    item.update(extra)
    return item


def _uncaught(entry_id, **fields):
    u = Mock()
    u.entry_id = entry_id
    u.title = fields.get("title", "Test")
    u.authors = fields.get("authors", "Author")
    u.abstract = fields.get("abstract", "Abstract")
    u.categories = fields.get("categories", "cs.AI")
    u.published = fields.get("published", datetime(2024, 1, 1))
    u.url = fields.get("url", f"http://arxiv.org/pdf/{entry_id}")
    return u


class TestCategoriesOf:
    """All categories are stored, primary category first."""

    def test_primary_first_then_others_without_duplicates(self):
        r = _make_arxiv_result(primary_category="cs.LG")
        r.categories = ["stat.ML", "cs.LG", "cs.AI"]
        assert _categories_of(r) == "cs.LG stat.ML cs.AI"

    def test_only_primary(self):
        r = _make_arxiv_result(primary_category="hep-ph")
        r.categories = None
        assert _categories_of(r) == "hep-ph"

    def test_none_result(self):
        assert _categories_of(None) is None


class TestRunIncrementalUpdate:
    """Tests branching logic inside run_incremental_update()."""

    # get_paper_count is patched but should NOT be called – it sits after the early return
    @patch(f"{MODULE}.get_paper_count")
    # newest_date is None when the DB is empty (initial import hasn't run yet)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=None)
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_db_is_empty(self, mock_logger, mock_newest, mock_count):
        """An empty database means the initial import hasn't run yet."""
        run_incremental_update(Mock(), Mock(), Mock())

        mock_logger.warning.assert_called_once()
        mock_count.assert_not_called()

    @patch(f"{MODULE}._fetch_new_arxiv_papers", return_value=[])
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_no_new_arxiv_papers(self, mock_logger, mock_newest,
                                                     mock_count, mock_fetch):
        """When arXiv has nothing new, SemanticScholar must not be called."""
        s2_client = Mock()

        run_incremental_update(Mock(), s2_client, Mock())

        s2_client.fetch_batch_detailed.assert_not_called()

    @patch(f"{MODULE}.get_known_entry_ids", return_value={"2401.00001"})
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_all_papers_already_known(self, mock_logger, mock_newest,
                                                          mock_count, mock_fetch,
                                                          mock_known):
        """If every fetched paper is already known, SemanticScholar must not be called."""
        mock_fetch.return_value = [_make_arxiv_result()]

        s2_client = Mock()
        run_incremental_update(Mock(), s2_client, Mock())

        s2_client.fetch_batch_detailed.assert_not_called()

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", return_value={"2401.00001"})
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_processes_found_papers_through_pipeline(self, mock_logger, mock_newest,
                                                      mock_count, mock_fetch,
                                                      mock_known, mock_cfg,
                                                      mock_save, mock_save_uncaught):
        """Found papers go through PCA+UMAP and are saved with metadata and coordinates."""
        result = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00001v1",
                                    primary_category="cs.LG")
        result.categories = ["cs.LG", "stat.ML"]
        mock_fetch.return_value = [result]
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([_s2_found()], [], [])

        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.1] * 128, (1.0, 2.0))]

        run_incremental_update(Mock(), s2_client, pipeline)

        pipeline.batch_process.assert_called_once()
        mock_save.assert_called_once()
        records = mock_save.call_args[0][0]
        assert len(records) == 1
        rec = records[0]
        assert rec["entry_id"] == "2401.00001"
        assert rec["title"] == "Test Paper"
        assert rec["categories"] == "cs.LG stat.ML"
        assert (rec["tsne_x"], rec["tsne_y"]) == (1.0, 2.0)
        assert rec["s2_id"] == "s2_123"
        mock_save_uncaught.assert_not_called()

    @patch(f"{MODULE}.save_uncaught_papers")
    # save fails for the only paper → it must not get lost
    @patch(f"{MODULE}.save_processed_papers", return_value=set())
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_unsaved_papers_go_to_uncaught(self, mock_logger, mock_newest,
                                           mock_count, mock_fetch,
                                           mock_known, mock_cfg,
                                           mock_save, mock_save_uncaught):
        """A DB write failure is logged and the paper is kept in uncaught_paper."""
        mock_fetch.return_value = [_make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00001v1")]
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([_s2_found()], [], [])

        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.1] * 128, (1.0, 2.0))]

        run_incremental_update(Mock(), s2_client, pipeline)

        mock_logger.warning.assert_called()
        rows = mock_save_uncaught.call_args[0][0]
        assert [r["entry_id"] for r in rows] == ["2401.00001"]

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_saves_not_found_and_failed_papers_as_uncaught(self, mock_logger, mock_newest,
                                                           mock_count, mock_fetch,
                                                           mock_known, mock_cfg,
                                                           mock_save_uncaught):
        """Unknown to S2 and request failures both end up in uncaught_paper (one batch insert)."""
        r1 = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00001v1", title="One")
        r2 = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00002v1", title="Two")
        mock_fetch.return_value = [r1, r2]
        mock_cfg.get_int.return_value = 4

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([], ["2401.00001"], ["2401.00002"])

        pipeline = Mock()

        run_incremental_update(Mock(), s2_client, pipeline)

        mock_save_uncaught.assert_called_once()
        rows = {r["entry_id"]: r for r in mock_save_uncaught.call_args[0][0]}
        assert set(rows) == {"2401.00001", "2401.00002"}
        assert rows["2401.00001"]["title"] == "One"   # metadata is kept for later
        assert rows["2401.00002"]["max_retries"] == 4
        pipeline.batch_process.assert_not_called()

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.get_known_entry_ids", return_value={"2401.00002"})
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_dedup_filters_known_papers_with_one_query(self, mock_logger, mock_newest,
                                                       mock_count, mock_fetch,
                                                       mock_known, mock_save_uncaught):
        """Known papers are skipped, and the DB is asked once for all IDs (no N+1)."""
        r1 = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00001v1")
        r2 = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00002v1")
        r3 = _make_arxiv_result(entry_id="http://arxiv.org/abs/2401.00003v2")
        mock_fetch.return_value = [r1, r2, r3]

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([], [], [])

        with patch(f"{MODULE}.cfg") as mock_cfg:
            mock_cfg.get_int.return_value = 100
            run_incremental_update(Mock(), s2_client, Mock())

        mock_known.assert_called_once()
        assert sorted(mock_known.call_args[0][0]) == ["2401.00001", "2401.00002", "2401.00003"]
        called_ids = s2_client.fetch_batch_detailed.call_args[0][0]
        assert called_ids == ["2401.00001", "2401.00003"]

    @patch(f"{MODULE}.save_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", side_effect=lambda recs: {r["entry_id"] for r in recs})
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.get_known_entry_ids", return_value=set())
    @patch(f"{MODULE}._fetch_new_arxiv_papers")
    @patch(f"{MODULE}.get_paper_count", return_value=100)
    @patch(f"{MODULE}.get_newest_paper_date", return_value=datetime(2024, 1, 1))
    @patch(f"{MODULE}.logger")
    def test_processes_and_saves_batch_by_batch(self, mock_logger, mock_newest,
                                                mock_count, mock_fetch,
                                                mock_known, mock_cfg,
                                                mock_save, mock_save_uncaught):
        """With batch size 2 and 3 papers, S2 is asked twice and each batch is saved separately."""
        mock_fetch.return_value = [
            _make_arxiv_result(entry_id=f"http://arxiv.org/abs/2401.0000{i}v1") for i in (1, 2, 3)
        ]
        mock_cfg.get_int.return_value = 2

        s2_client = Mock()
        s2_client.fetch_batch_detailed.side_effect = lambda ids, batch_size: (
            [_s2_found(i) for i in ids], [], []
        )
        pipeline = Mock()
        pipeline.batch_process.side_effect = lambda vecs: [([0.0] * 128, (0.0, 0.0))] * len(vecs)

        run_incremental_update(Mock(), s2_client, pipeline)

        assert s2_client.fetch_batch_detailed.call_count == 2
        assert mock_save.call_count == 2
        assert [len(c[0][0]) for c in mock_save.call_args_list] == [2, 1]


class TestRetryUncaughtPapers:
    """Tests branching logic inside retry_uncaught_papers()."""

    @patch(f"{MODULE}.get_uncaught_papers_due", return_value=[])
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_returns_early_when_no_papers_due(self, mock_logger, mock_cfg, mock_due):
        """Nothing due → SemanticScholar must not be called."""
        mock_cfg.get_int.return_value = 14

        s2_client = Mock()
        retry_uncaught_papers(s2_client, Mock())

        s2_client.fetch_batch_detailed.assert_not_called()

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.save_processed_papers", return_value={"2401.00001"})
    @patch(f"{MODULE}.get_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_rescues_found_papers_and_deletes_from_uncaught(self, mock_logger, mock_cfg,
                                                             mock_due, mock_save,
                                                             mock_delete, mock_increment,
                                                             mock_parked):
        """A paper S2 now knows is saved with its stored metadata and removed from uncaught."""
        mock_due.return_value = [_uncaught("2401.00001", title="Stored title")]
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
    @patch(f"{MODULE}.get_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_does_not_delete_or_count_retry_when_save_fails(self, mock_logger, mock_cfg,
                                                            mock_due, mock_save,
                                                            mock_delete, mock_increment,
                                                            mock_parked):
        """If saving fails, the paper stays in uncaught and does not lose a retry."""
        mock_due.return_value = [_uncaught("2401.00001")]
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([_s2_found()], [], [])

        pipeline = Mock()
        pipeline.batch_process.return_value = [([0.1] * 128, (1.0, 2.0))]

        retry_uncaught_papers(s2_client, pipeline)

        deleted = mock_delete.call_args[0][0] if mock_delete.called else set()
        assert "2401.00001" not in deleted
        incremented = mock_increment.call_args[0][0] if mock_increment.called else []
        assert "2401.00001" not in incremented

    @patch(f"{MODULE}.count_parked_uncaught", return_value=0)
    @patch(f"{MODULE}.delete_uncaught_papers")
    @patch(f"{MODULE}.increment_uncaught_retries")
    @patch(f"{MODULE}.get_uncaught_papers_due")
    @patch(f"{MODULE}.cfg")
    @patch(f"{MODULE}.logger")
    def test_increments_retry_only_for_not_found(self, mock_logger, mock_cfg,
                                                 mock_due, mock_increment,
                                                 mock_delete, mock_parked):
        """
        Still unknown to S2 → retry counter +1.
        Request failed (rate limit / network) → NOT counted as a retry.
        """
        mock_due.return_value = [_uncaught("2401.00001"), _uncaught("2401.00002")]
        mock_cfg.get_int.return_value = 100

        s2_client = Mock()
        s2_client.fetch_batch_detailed.return_value = ([], ["2401.00001"], ["2401.00002"])

        retry_uncaught_papers(s2_client, Mock())

        mock_increment.assert_called_once_with(["2401.00001"])
