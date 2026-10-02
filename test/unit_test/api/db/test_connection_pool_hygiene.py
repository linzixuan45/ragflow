"""Guard against sweeping the connection pool while connections are checked out.

peewee's ``PooledDatabase.close_stale`` closes connections that are in ``_in_use``
-- i.e. checked out by a worker thread -- without resetting that thread's
connection state. The owner's next query then hits pymysql's ``if not self._sock:
raise InterfaceError(0, "")``, which peewee rewrites to ``InterfaceError(0, '')``.

The API request teardown used to call a ``close_connection()`` helper that did
``DB.close_stale(age=30)`` on every request. Any thread that held a connection
for more than thirty seconds had it yanked once per thirty seconds, which is the
exact cadence of the ``Database connection issue (attempt 1/5): (0, '')`` lines
(336 occurrences in three hours, every inter-arrival gap >= 30.016s). The pool
does not need the sweep: ``stale_timeout`` discards idle connections on check-out
and ``PooledMySQLDatabase._is_closed`` pings before handing one out.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCANNED_ROOTS = ("api", "rag", "common")


def test_pool_sweep_entry_point_is_gone():
    from api.db import db_models

    assert not hasattr(db_models, "close_connection")


def test_no_module_sweeps_the_pool():
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for root in SCANNED_ROOTS
        for path in (REPO_ROOT / root).rglob("*.py")
        if "close_stale(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
