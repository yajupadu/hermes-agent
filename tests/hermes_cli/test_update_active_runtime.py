"""Active-runtime worktree update-survival contract.

Tests exercise the real git plumbing against temporary repositories:
anchor ancestry, dirty/missing/conflict/idempotent/fast-forward, no canonical
mutation before preflight failure, and launcher continuity after failed update.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_GIT = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")

ANCHOR_SHA_PREFIX = "b0669dfcab53"


def _git(args: list[str], cwd: str, check: bool = True):
    r = subprocess.run(["git", *args], cwd=cwd, **_GIT)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def _commit(cwd: str, msg: str, filename: str = "f.txt", content: str | None = None):
    p = Path(cwd) / filename
    p.write_text(content or msg, encoding="utf-8")
    _git(["add", filename], cwd)
    _git(["commit", "-m", msg], cwd)
    return _git(["rev-parse", "HEAD"], cwd).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[str, str]:
    """Create a bare 'origin' and a clone with a .worktrees/ child.  Returns (clone_path, anchor_sha)."""
    origin = str(tmp_path / "origin.git")
    _git(["init", "--bare", origin], str(tmp_path))

    clone = str(tmp_path / "canonical")
    _git(["clone", origin, clone], str(tmp_path))

    _git(["config", "user.email", "test@test"], clone)
    _git(["config", "user.name", "Test"], clone)
    base_sha = _commit(clone, "base commit")
    anchor_sha = _commit(clone, "anchor commit")
    _git(["push", "origin", "main"], clone)

    return clone, anchor_sha


def _add_worktree(clone: str, name: str, branch: str | None = None) -> str:
    """Add a worktree under .worktrees/ in the clone."""
    wt_dir = Path(clone) / ".worktrees"
    wt_dir.mkdir(exist_ok=True)
    wt_path = str(wt_dir / name)
    branch = branch or f"wt-{name}"
    _git(["worktree", "add", "-b", branch, wt_path], clone)
    _git(["config", "user.email", "test@test"], wt_path)
    _git(["config", "user.name", "Test"], wt_path)
    return wt_path


# ── Preflight tests ──────────────────────────────────────────────────


def test_preflight_passes_clean_ancestor(tmp_path):
    """A clean worktree whose HEAD descends from the anchor passes."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    _commit(wt, "work on top of anchor")

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert ok, errors
    assert len(states) == 1
    assert states[0].valid
    assert not states[0].dirty


def test_preflight_passes_exact_anchor(tmp_path):
    """Worktree HEAD == anchor SHA passes."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    _git(["reset", "--hard", anchor], wt)

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert ok, errors


def test_preflight_fails_dirty_worktree(tmp_path):
    """A dirty worktree fails the preflight."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    (Path(wt) / "dirty.txt").write_text("uncommitted", encoding="utf-8")

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert not ok
    assert any("dirty" in e for e in errors)


def test_preflight_fails_missing_worktree(tmp_path):
    """A worktree whose directory was removed fails closed."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    import shutil
    shutil.rmtree(wt)

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert not ok
    assert any("missing" in e or "invalid" in e for e in errors)


def test_preflight_fails_missing_anchor_ancestry(tmp_path):
    """A worktree that does not descend from the anchor fails."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    # Create an orphan branch so the anchor is unreachable
    _git(["checkout", "--orphan", "orphan"], wt)
    (Path(wt) / "orphan.txt").write_text("orphan", encoding="utf-8")
    _git(["add", "orphan.txt"], wt)
    _git(["commit", "-m", "orphan commit"], wt)

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert not ok
    assert any("ancestor" in e for e in errors)


def test_preflight_no_worktrees_passes(tmp_path):
    """No .worktrees/ at all is a clean pass."""
    clone, anchor = _init_repo(tmp_path)

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(Path(clone), anchor)
    assert ok
    assert states == []


# ── Reconciliation tests ─────────────────────────────────────────────


def test_reconcile_fast_forward(tmp_path):
    """A clean worktree fast-forwards to include upstream changes."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")

    # Push a new commit to origin from canonical
    _commit(clone, "upstream work", filename="upstream.txt")
    _git(["push", "origin", "main"], clone)
    _git(["fetch", "origin"], wt)

    from hermes_cli.update_active_runtime import reconcile_active_runtime

    ok, msg = reconcile_active_runtime(wt, "origin/main", anchor)
    assert ok, msg
    # Anchor ancestry preserved
    r = _git(["merge-base", "--is-ancestor", anchor, "HEAD"], wt, check=False)
    assert r.returncode == 0


def test_reconcile_preserves_local_commits(tmp_path):
    """Reconciliation merges upstream WITHOUT discarding the worktree's own commits."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    local_sha = _commit(wt, "local work in worktree", filename="local.txt")

    _commit(clone, "upstream work", filename="upstream.txt")
    _git(["push", "origin", "main"], clone)
    _git(["fetch", "origin"], wt)

    from hermes_cli.update_active_runtime import reconcile_active_runtime

    ok, msg = reconcile_active_runtime(wt, "origin/main", anchor)
    assert ok, msg
    # Local commit still reachable
    r = _git(["merge-base", "--is-ancestor", local_sha, "HEAD"], wt, check=False)
    assert r.returncode == 0
    # Anchor still ancestor
    r = _git(["merge-base", "--is-ancestor", anchor, "HEAD"], wt, check=False)
    assert r.returncode == 0


def test_reconcile_conflict_rolls_back(tmp_path):
    """A merge conflict rolls back and reports failure."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    _commit(wt, "conflicting local", filename="conflict.txt", content="local side")

    _commit(clone, "conflicting upstream", filename="conflict.txt", content="upstream side")
    _git(["push", "origin", "main"], clone)
    _git(["fetch", "origin"], wt)

    pre_sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()

    from hermes_cli.update_active_runtime import reconcile_active_runtime

    ok, msg = reconcile_active_runtime(wt, "origin/main", anchor)
    assert not ok
    assert "merge failed" in msg or "rolled back" in msg
    # HEAD unchanged after rollback
    post_sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert post_sha == pre_sha


def test_reconcile_idempotent(tmp_path):
    """Running reconcile twice with no new upstream changes is a no-op."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")

    _commit(clone, "upstream work", filename="upstream.txt")
    _git(["push", "origin", "main"], clone)
    _git(["fetch", "origin"], wt)

    from hermes_cli.update_active_runtime import reconcile_active_runtime

    ok1, _ = reconcile_active_runtime(wt, "origin/main", anchor)
    assert ok1
    sha_after_first = _git(["rev-parse", "HEAD"], wt).stdout.strip()

    ok2, _ = reconcile_active_runtime(wt, "origin/main", anchor)
    assert ok2
    sha_after_second = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert sha_after_first == sha_after_second


# ── No canonical mutation before preflight failure ────────────────────


def test_no_canonical_mutation_on_preflight_failure(tmp_path):
    """The canonical checkout must not be mutated when preflight fails."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    (Path(wt) / "dirty.txt").write_text("uncommitted", encoding="utf-8")

    canonical_sha = _git(["rev-parse", "HEAD"], clone).stdout.strip()

    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, _, _ = preflight_active_runtimes(Path(clone), anchor)
    assert not ok

    # Canonical HEAD unchanged
    post_sha = _git(["rev-parse", "HEAD"], clone).stdout.strip()
    assert post_sha == canonical_sha


# ── Launcher continuity after failed update ───────────────────────────


def test_worktree_survives_failed_reconcile(tmp_path):
    """After a failed reconcile, the worktree is still a valid git worktree
    and its branch is intact — launcher continuity."""
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    wt_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
    _commit(wt, "conflicting local", filename="conflict.txt", content="local")

    _commit(clone, "conflicting upstream", filename="conflict.txt", content="upstream")
    _git(["push", "origin", "main"], clone)
    _git(["fetch", "origin"], wt)

    from hermes_cli.update_active_runtime import reconcile_active_runtime

    ok, _ = reconcile_active_runtime(wt, "origin/main", anchor)
    assert not ok

    # Worktree is still valid and on its branch
    r = _git(["rev-parse", "--is-inside-work-tree"], wt, check=False)
    assert r.returncode == 0
    assert r.stdout.strip() == "true"
    post_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip()
    assert post_branch == wt_branch


# ── collect_worktree_state ────────────────────────────────────────────


def test_collect_state_valid_worktree(tmp_path):
    clone, anchor = _init_repo(tmp_path)
    wt = _add_worktree(clone, "rt1")
    sha = _commit(wt, "work")

    from hermes_cli.update_active_runtime import collect_worktree_state

    state = collect_worktree_state(Path(clone), wt)
    assert state.valid
    assert state.sha == sha
    assert not state.dirty
    assert state.branch is not None


def test_collect_state_missing_dir(tmp_path):
    from hermes_cli.update_active_runtime import collect_worktree_state

    state = collect_worktree_state(Path(tmp_path), str(tmp_path / "nonexistent"))
    assert not state.valid
    assert state.error == "worktree directory missing"


def test_configured_runtime_path_is_used_outside_canonical_worktree(tmp_path):
    """The authority record may promote a runtime stored anywhere on disk."""
    clone, anchor = _init_repo(tmp_path)
    external = tmp_path / "external-runtime"
    _git(["worktree", "add", "-b", "external", str(external)], clone)
    from hermes_cli.update_active_runtime import preflight_active_runtimes

    ok, states, errors = preflight_active_runtimes(
        Path(clone), anchor, [str(external)])
    assert ok, errors
    assert [state.path for state in states] == [str(external)]


def test_launcher_is_reconstructable_and_contains_no_policy(tmp_path):
    from hermes_cli.update_active_runtime import (
        install_update_launcher,
        render_update_launcher,
    )

    launcher = tmp_path / "bin" / "hermes-update"
    install_update_launcher(launcher)
    assert launcher.read_text(encoding="utf-8") == render_update_launcher()
    assert "reconcile" not in launcher.read_text(encoding="utf-8")
    assert "b0669df" not in launcher.read_text(encoding="utf-8")
    assert "/Users/yajupadu/" not in launcher.read_text(encoding="utf-8")
