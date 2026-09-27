"""Explicit repository authority for repo-scoped delegated work."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

class RepositoryAuthorityError(ValueError):
    pass


def _git(path: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)
    if p.returncode:
        raise RepositoryAuthorityError(p.stderr.strip() or "git command failed")
    return p.stdout.strip()


def resolve_repository_authority(task: dict[str, Any]) -> dict[str, Any] | None:
    mode = task.get("task_mode") or task.get("TASK_MODE")
    path = task.get("target_repo_path") or task.get("TARGET_REPO_PATH")
    sha = task.get("target_base_sha") or task.get("TARGET_BASE_SHA")
    if not mode and not path and not sha:
        return None
    if not path:
        raise RepositoryAuthorityError("TARGET_REPOSITORY_REQUIRED")
    if not sha:
        raise RepositoryAuthorityError("TARGET_BASE_SHA_NOT_FOUND")
    requested = os.path.abspath(os.path.expanduser(str(path)))
    if not Path(requested).is_dir():
        raise RepositoryAuthorityError("TARGET_REPOSITORY_REQUIRED")
    try:
        top = os.path.realpath(_git(requested, "rev-parse", "--show-toplevel"))
        common_raw = _git(requested, "rev-parse", "--git-common-dir")
        common = os.path.realpath(os.path.join(requested, common_raw)) if not os.path.isabs(common_raw) else os.path.realpath(common_raw)
        base = _git(top, "rev-parse", "--verify", f"{sha}^{{commit}}")
    except RepositoryAuthorityError as exc:
        if "TARGET_BASE_SHA_NOT_FOUND" in str(exc):
            raise
        if sha:
            raise RepositoryAuthorityError("TARGET_BASE_SHA_NOT_FOUND") from exc
        raise
    remote = ""
    try:
        remote = _git(top, "config", "--get", "remote.origin.url")
    except RepositoryAuthorityError:
        pass
    identity = hashlib.sha256(f"{top}\0{common}\0{remote}".encode()).hexdigest()
    return {"requested_path": requested, "resolved_git_toplevel": top,
            "git_common_dir": common, "remote_identity": remote,
            "base_sha": base, "repository_fingerprint": identity,
            "task_mode": mode or "repo"}


def assert_worktree_authority(info: dict[str, Any], authority: dict[str, Any]) -> None:
    wt_top = os.path.realpath(_git(info["path"], "rev-parse", "--show-toplevel"))
    common_raw = _git(info["path"], "rev-parse", "--git-common-dir")
    wt_common = os.path.realpath(os.path.join(info["path"], common_raw)) if not os.path.isabs(common_raw) else os.path.realpath(common_raw)
    wt_head = _git(wt_top, "rev-parse", "HEAD")
    if wt_common != authority["git_common_dir"] or wt_head != authority["base_sha"]:
        raise RepositoryAuthorityError("REPOSITORY_AUTHORITY_MISMATCH")
