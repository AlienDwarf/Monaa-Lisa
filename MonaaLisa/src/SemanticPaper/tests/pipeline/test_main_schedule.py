import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Database.db creates an engine at import time (needs DATABASE_URL)
sys.modules.setdefault("Database.db", MagicMock())

main = pytest.importorskip("main")


class TestNextUncaughtRetry:

    def test_never_run_starts_now(self):
        with patch.object(main, "get_app_state", return_value=None):
            assert main._next_uncaught_retry(14) <= datetime.now()

    def test_overdue_run_starts_now(self):
        last = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with patch.object(main, "get_app_state", return_value=last):
            assert main._next_uncaught_retry(14) <= datetime.now()

    def test_recent_run_waits_for_the_interval(self):
        last = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        with patch.object(main, "get_app_state", return_value=last):
            nxt = main._next_uncaught_retry(14)
        assert timedelta(days=9, hours=23) < nxt - datetime.now() < timedelta(days=10, hours=1)

    def test_unreadable_state_runs_now(self):
        with patch.object(main, "get_app_state", side_effect=RuntimeError("db")):
            assert main._next_uncaught_retry(14) <= datetime.now()
