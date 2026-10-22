"""#116: native exact-ID READY dispatch and fail-closed unattended REVIEW regression.

All DB writes occur on a temporary Kanban board. No installed Hermes,
broker, protected review card, cron, or user-owned worktree is touched.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


def _review(conn, title):
    tid = kb.create_task(conn, title=title, assignee="trader")
    assert kb.request_review(conn, tid, reviewer="trader")
    return tid


@pytest.mark.parametrize("slots", [1, 3])
def test_protected_review_never_reserved_or_claimed(home, all_assignees_spawnable, slots):
    spawned = []
    with kbc.connect() as conn:
        protected = [_review(conn, "#58 human approval"), _review(conn, "#87 owner review")]
        ready = kb.create_task(conn, title="#116 authorized scratch", assignee="trader")
        # With one slot, protected REVIEW must NOT reserve the capacity.
        # The unrestricted tick may still dispatch an independently READY task.
        initial = kbd.dispatch_once(conn, max_spawn=slots, dry_run=True)
        assert [tid for tid, _a, _w in initial.spawned] == [ready]
        assert all(kb.get_task(conn, tid).status == "review" for tid in protected)
        res = kbd.dispatch_once(
            conn, max_spawn=slots, spawn_fn=lambda task, _ws: spawned.append(task.id),
            ready_task_ids=[ready],
        )
        assert [t for t, _assignee, _ws in res.spawned] == [ready]
        assert spawned == [ready]
        assert all(kb.get_task(conn, tid).status == "review" for tid in protected)


def test_default_deny_grant_and_revoke(home, all_assignees_spawnable):
    with kbc.connect() as conn:
        protected = _review(conn, "#58 protected")
        automatic = _review(conn, "#116 explicitly approved automatic reviewer")
        assert kb.review_auto_authorized(conn, protected) is False
        assert kb.claim_review_task(conn, protected, automated=True) is None
        assert kb.set_review_auto_authorization(
            conn, automatic, enabled=True, authorized_by="operator",
            reason="approved isolated offline automatic review",
        )
        assert kb.review_auto_authorized(conn, automatic) is True
        res = kbd.dispatch_once(
            conn, max_spawn=3, spawn_fn=lambda _task, _ws: None,
        )
        assert [tid for tid, _a, _w in res.spawned] == [automatic]
        assert kb.get_task(conn, protected).status == "review"
        assert kb.set_review_auto_authorization(
            conn, protected, enabled=True, authorized_by="operator",
            reason="temporary authorization for test",
        )
        assert kb.set_review_auto_authorization(
            conn, protected, enabled=False, authorized_by="operator",
            reason="human gate restored",
        )
        assert kb.claim_review_task(conn, protected, automated=True) is None


def test_grant_revocation_race_revalidated_at_claim(home, all_assignees_spawnable, monkeypatch):
    with kbc.connect() as conn:
        candidate = _review(conn, "auto-review revoked during selection")
        kb.set_review_auto_authorization(
            conn, candidate, enabled=True, authorized_by="operator", reason="test",
        )
        original = kb.claim_review_task

        def revoke_then_claim(c, tid, **kw):
            kb.set_review_auto_authorization(
                c, tid, enabled=False, authorized_by="operator",
                reason="revoked before claim",
            )
            return original(c, tid, **kw)

        monkeypatch.setattr(kb, "claim_review_task", revoke_then_claim)
        res = kbd.dispatch_once(conn, max_spawn=1, spawn_fn=lambda _t, _w: None)
        assert res.spawned == []
        assert kb.get_task(conn, candidate).status == "review"


def test_reassignment_snapshot_revalidated_at_claim(home, all_assignees_spawnable, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready reassignment race", assignee="trader")
        original = kb.claim_task

        def reassign_then_claim(c, task_id, **kw):
            with kb.write_txn(c):
                c.execute("UPDATE tasks SET assignee='other' WHERE id=?", (task_id,))
            return original(c, task_id, **kw)

        monkeypatch.setattr(kb, "claim_task", reassign_then_claim)
        res = kbd.dispatch_once(conn, ready_task_ids=[tid],
                                max_spawn=1, spawn_fn=lambda _t, _w: None)
        assert res.spawned == []
        assert kb.get_task(conn, tid).status == "ready"


def test_empty_allowlist_claims_nothing(home, all_assignees_spawnable):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unlisted READY", assignee="trader")
        res = kbd.dispatch_once(conn, ready_task_ids=[], max_spawn=3,
                                spawn_fn=lambda _t, _w: None)
        assert res.spawned == []
        assert kb.get_task(conn, tid).status == "ready"
