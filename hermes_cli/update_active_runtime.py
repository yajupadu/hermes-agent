"""Active-runtime worktree preflight for ``hermes update``.

Before the canonical checkout is mutated (git merge/reset), any active-runtime
worktrees must be validated: valid git worktree, anchor ancestry intact, clean
working tree.  After fetch, a clean active runtime is reconciled by merging the
upstream target INTO the worktree's history — never by replacing it with
canonical HEAD.

Fail-closed: dirty, missing, stale, anchor-absent, or conflicting authority
aborts the update before any file mutation.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes_cli.update_cmd")

_GIT_TEXT_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class ActiveRuntimeState:
    """Snapshot of one active-runtime worktree's git state."""

    path: str
    sha: Optional[str]
    branch: Optional[str]
    merge_base: Optional[str]
    dirty: bool
    valid: bool
    error: Optional[str] = None


def _git_stdout(args: list[str], cwd: str, timeout: float = 10) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, timeout=timeout, **_GIT_TEXT_KW,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _git_ok(args: list[str], cwd: str, timeout: float = 10) -> bool:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, timeout=timeout,
            capture_output=True, text=True,
        ).returncode == 0
    except Exception:
        return False


def collect_worktree_state(
    project_root: Path, worktree_path: str,
) -> ActiveRuntimeState:
    """Collect SHA/branch/merge-base/dirty for one worktree. Never raises."""
    wt = str(worktree_path)
    try:
        if not Path(wt).is_dir():
            return ActiveRuntimeState(
                path=wt, sha=None, branch=None, merge_base=None,
                dirty=True, valid=False, error="worktree directory missing",
            )
        sha = _git_stdout(["rev-parse", "HEAD"], wt)
        if sha is None:
            return ActiveRuntimeState(
                path=wt, sha=None, branch=None, merge_base=None,
                dirty=True, valid=False, error="cannot resolve HEAD",
            )
        branch = _git_stdout(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        if branch == "HEAD":
            branch = None

        merge_base = _git_stdout(
            ["merge-base", "HEAD", f"origin/{_default_branch(project_root)}"], wt,
        )
        status = _git_stdout(["status", "--porcelain"], wt)
        dirty = status is None or bool(status)

        return ActiveRuntimeState(
            path=wt, sha=sha, branch=branch, merge_base=merge_base,
            dirty=dirty, valid=True,
        )
    except Exception as exc:
        return ActiveRuntimeState(
            path=wt, sha=None, branch=None, merge_base=None,
            dirty=True, valid=False, error=str(exc),
        )


def _default_branch(project_root: Path) -> str:
    b = _git_stdout(["rev-parse", "--abbrev-ref", "origin/HEAD"], str(project_root))
    if b and b.startswith("origin/"):
        return b[len("origin/"):]
    return "main"


def list_active_runtime_worktrees(
    project_root: Path, configured_paths: Optional[list[str]] = None,
) -> list[str]:
    """Return configured runtimes, or linked worktrees during migration."""
    if configured_paths is not None:
        return [str(Path(path).expanduser()) for path in configured_paths]
    output = _git_stdout(["worktree", "list", "--porcelain"], str(project_root))
    if output is None:
        return []

    result = []
    canonical = str(project_root.resolve())
    for block in output.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("worktree "):
                wt_path = line[len("worktree "):]
                if str(Path(wt_path).resolve()) != canonical:
                    result.append(wt_path)
    return result


def preflight_active_runtimes(
    project_root: Path, anchor_sha: str, configured_paths: Optional[list[str]] = None,
) -> tuple[bool, list[ActiveRuntimeState], list[str]]:
    """Validate all active-runtime worktrees before canonical checkout mutation.

    Returns ``(ok, states, errors)``.  ``ok`` is True only when every worktree
    passes: valid, clean, and ``anchor_sha`` is an ancestor of (or equal to)
    the worktree HEAD.

    Fail-closed: any invalid/dirty/missing/stale/anchor-absent worktree makes
    ``ok`` False.
    """
    paths = list_active_runtime_worktrees(project_root, configured_paths)
    if not paths:
        return True, [], []

    states: list[ActiveRuntimeState] = []
    errors: list[str] = []

    for wt_path in paths:
        state = collect_worktree_state(project_root, wt_path)
        states.append(state)

        if not state.valid:
            errors.append(f"{wt_path}: invalid — {state.error}")
            continue
        if state.dirty:
            errors.append(f"{wt_path}: dirty working tree")
            continue

        # Anchor ancestry: anchor must be ancestor of worktree HEAD, or exact match.
        if state.sha == anchor_sha:
            continue
        ancestor = _git_stdout(
            ["merge-base", "--is-ancestor", anchor_sha, state.sha],
            wt_path,
        )
        # --is-ancestor exits 0 if true, 1 if not; _git_ok wraps this.
        is_ancestor = _git_ok(
            ["merge-base", "--is-ancestor", anchor_sha, state.sha],
            wt_path,
        )
        if not is_ancestor:
            errors.append(
                f"{wt_path}: anchor {anchor_sha[:12]} is not an ancestor of HEAD {state.sha[:12]}"
            )

    return len(errors) == 0, states, errors


def reconcile_active_runtime(
    worktree_path: str, target_ref: str, anchor_sha: str,
) -> tuple[bool, str]:
    """Merge upstream target into an active-runtime worktree's history.

    Does NOT replace the worktree with canonical HEAD — it merges the target
    into the worktree's own branch so anchor ancestry is preserved.

    Returns ``(ok, message)``.  Rolls back on merge failure.
    """
    wt = str(worktree_path)
    pre_sha = _git_stdout(["rev-parse", "HEAD"], wt)
    if pre_sha is None:
        return False, "cannot resolve HEAD before reconcile"

    r = subprocess.run(
        ["git", "merge", "--ff", "--no-edit", target_ref],
        cwd=wt, **_GIT_TEXT_KW,
    )
    if r.returncode != 0:
        subprocess.run(
            ["git", "merge", "--abort"], cwd=wt, **_GIT_TEXT_KW,
        )
        return False, f"merge failed: {r.stderr.strip()}"

    post_sha = _git_stdout(["rev-parse", "HEAD"], wt)
    # Verify anchor ancestry survived the merge.
    if not _git_ok(["merge-base", "--is-ancestor", anchor_sha, post_sha or ""], wt):
        subprocess.run(
            ["git", "reset", "--hard", pre_sha], cwd=wt, **_GIT_TEXT_KW,
        )
        return False, "anchor ancestry lost after merge — rolled back"

    return True, f"reconciled {pre_sha[:12]} → {post_sha[:12] if post_sha else '?'}"


def reconcile_all_active_runtimes(
    project_root: Path, target_ref: str, anchor_sha: str,
    configured_paths: Optional[list[str]] = None,
) -> tuple[bool, list[str]]:
    """Reconcile every active-runtime worktree with ``target_ref``.

    Returns ``(all_ok, messages)``.  Stops at first failure (fail closed).
    """
    paths = list_active_runtime_worktrees(project_root, configured_paths)
    if not paths:
        return True, []

    messages: list[str] = []
    for wt_path in paths:
        ok, msg = reconcile_active_runtime(wt_path, target_ref, anchor_sha)
        messages.append(f"{wt_path}: {msg}")
        if not ok:
            return False, messages
    return True, messages


def render_update_launcher(hermes_executable: str = "hermes") -> str:
    """Return the reconstructable launcher; updater policy stays in Python."""
    return "#!/usr/bin/env bash\nset -euo pipefail\nexec %s update \"$@\"\n" % hermes_executable


def install_update_launcher(path: Path, hermes_executable: str = "hermes") -> None:
    """Install the thin launcher generated from this version-controlled module."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_update_launcher(hermes_executable), encoding="utf-8")
    path.chmod(0o755)
