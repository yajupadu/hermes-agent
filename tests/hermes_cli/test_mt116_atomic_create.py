"""#116: concurrent task creators must deduplicate one logical work item.

Run without pytest: python tests/hermes_cli/test_mt116_atomic_create.py -v
An isolated temporary Hermes home holds all board state; the active board,
gateway, cron, profile, and source checkout are never modified.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class AtomicIdempotencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mt116-create-test-")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / ".hermes"
        home.mkdir()
        env_patch = patch.dict(os.environ, {"HERMES_HOME": str(home)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        home_patch = patch.object(Path, "home", return_value=Path(temporary.name))
        home_patch.start()
        self.addCleanup(home_patch.stop)
        kb.init_db()

    def test_concurrent_create_same_key_returns_one_task(self):
        """Both unlocked prechecks must miss before either writer enters its txn.

        Synchronize immediately BEFORE write_txn, not at _new_task_id: the
        latter is called with the SQLite write lock held and deadlocks GREEN.
        """
        barrier = Barrier(2)
        original_write_txn = kb.write_txn

        @contextmanager
        def synchronized_write_txn(conn, *, allow_nested=False):
            barrier.wait(timeout=10)
            with original_write_txn(conn, allow_nested=allow_nested) as txn:
                yield txn

        def create():
            with kbc.connect_closing() as conn:
                return kb.create_task(
                    conn,
                    title="#116 approved slice",
                    assignee="trader",
                    idempotency_key="mt116-approved-slice-concurrency",
                )

        with patch.object(kb, "write_txn", synchronized_write_txn):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(create)
                second = pool.submit(create)
                ids = [first.result(timeout=25), second.result(timeout=25)]

        self.assertEqual(ids[0], ids[1])
        with kbc.connect_closing() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? "
                "AND status != 'archived'",
                ("mt116-approved-slice-concurrency",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], ids[0])

    def test_serial_create_same_key_is_idempotent(self):
        with kbc.connect_closing() as conn:
            first = kb.create_task(
                conn, title="first", idempotency_key="mt116-serial-key"
            )
            second = kb.create_task(
                conn, title="duplicate", idempotency_key="mt116-serial-key"
            )
            self.assertEqual(first, second)
            count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                ("mt116-serial-key",),
            ).fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
