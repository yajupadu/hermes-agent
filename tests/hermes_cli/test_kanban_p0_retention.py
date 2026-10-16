"""P0 regression: a DONE/archived task must not destroy undeclared source.

This suite covers the immediate non-destructive retention layer. A separate
artifact-before-DONE contract is tracked in meticulous_trader#114.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def test_done_retains_undeclared_source_and_records_recovery_path(kanban_home):
    """Undeclared source changes survive post-DONE workspace cleanup."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="source owner")
        workspace = kbw.resolve_workspace(kb.get_task(conn, task_id))
        kbw.set_workspace_path(conn, task_id, workspace)
        repo = workspace / "src"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "new_test.py").write_text("assert True\n", encoding="utf-8")
        (workspace / "unattached.patch").write_text("unique patch bytes\n", encoding="utf-8")

        assert kb.complete_task(conn, task_id, result="tests passed")
        assert kb.get_task(conn, task_id).status == "done"
        retained = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "scratch_workspace_retained"
        ]

    assert not workspace.exists()
    assert len(retained) == 1
    archived = Path(retained[0].payload["path"])
    assert archived.parent == kanban_home / "kanban" / "retained-workspaces"
    assert (archived / "src" / "new_test.py").read_text() == "assert True\n"
    assert (archived / "unattached.patch").read_text() == "unique patch bytes\n"


def test_retention_failure_leaves_original_source_untouched(kanban_home, monkeypatch):
    """A storage/rename failure must never fall back to rmtree."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="cannot rename")
        workspace = kbw.resolve_workspace(kb.get_task(conn, task_id))
        kbw.set_workspace_path(conn, task_id, workspace)
        (workspace / "only.patch").write_text("preserve", encoding="utf-8")

        original = Path.rename

        def fail_workspace_rename(self, target):
            if self == workspace:
                raise OSError("simulated disk/rename failure")
            return original(self, target)

        monkeypatch.setattr(Path, "rename", fail_workspace_rename)
        assert kb.complete_task(conn, task_id, result="done")

    assert (workspace / "only.patch").read_text() == "preserve"


def test_empty_scratch_still_cleans_up_normally(kanban_home):
    """Normal no-output tasks do not accumulate empty retained directories."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="no source")
        workspace = kbw.resolve_workspace(kb.get_task(conn, task_id))
        kbw.set_workspace_path(conn, task_id, workspace)
        assert kb.complete_task(conn, task_id, result="done")
    assert not workspace.exists()
    root = kanban_home / "kanban" / "retained-workspaces"
    assert not root.exists() or not any(root.iterdir())
