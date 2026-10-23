"""#116 regression: concurrent Kanban creators must not duplicate a logical task.

This uses an isolated temporary Kanban home and two independent SQLite
connections. No live board, gateway, cron, profile or source worktree is touched.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def isolated_kanban(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_concurrent_create_same_idempotency_key_returns_one_task(
    isolated_kanban, monkeypatch
):
    """Force both unlocked lookups to miss before either transaction inserts.

    Old behavior: two distinct IDs and two tasks.
    Correct behavior: second writer sees first row after BEGIN IMMEDIATE.
    """
    barrier = Barrier(2)
    original_new_task_id = kb._new_task_id

    def synchronized_id():
        task_id = original_new_task_id()
        barrier.wait(timeout=10)
        return task_id

    monkeypatch.setattr(kb, "_new_task_id", synchronized_id)

    def create():
        with kbc.connect_closing() as conn:
            return kb.create_task(
                conn,
                title="#116 approved slice",
                assignee="trader",
                idempotency_key="mt116-approved-slice-concurrency",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create)
        second = executor.submit(create)
        created_ids = [first.result(timeout=20), second.result(timeout=20)]

    assert created_ids[0] == created_ids[1]

    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived'",
            ("mt116-approved-slice-concurrency",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == created_ids[0]


def test_serial_create_same_key_is_idempotent(isolated_kanban):
    with kbc.connect_closing() as conn:
        first = kb.create_task(
            conn, title="first", idempotency_key="mt116-serial-key"
        )
        second = kb.create_task(
            conn, title="duplicate", idempotency_key="mt116-serial-key"
        )
        assert first == second
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            ("mt116-serial-key",),
        ).fetchone()[0] == 1
