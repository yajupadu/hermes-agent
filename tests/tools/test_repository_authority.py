"""Regression tests for explicit cross-repository delegation authority."""
from pathlib import Path
import subprocess

import pytest

from tools.repository_authority import (
    REPOSITORY_AUTHORITY_VERSION,
    RepositoryAuthorityError,
    require_repository_authority,
    resolve_repository_authority,
)
from tools.subagent_worktree import create_subagent_worktree


def _repo(path: Path, filename: str) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / filename).write_text("fixture\n")
    subprocess.run(["git", "-C", str(path), "add", filename], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_missing_target_fails_closed():
    with pytest.raises(RepositoryAuthorityError, match="TARGET_REPOSITORY_REQUIRED"):
        resolve_repository_authority({"task_mode": "repo"})


def test_sha_is_verified_in_requested_repo(tmp_path):
    controller = tmp_path / "controller"
    target = tmp_path / "target"
    controller_sha = _repo(controller, "controller.txt")
    target_sha = _repo(target, "target.txt")
    authority = resolve_repository_authority({
        "task_mode": "repo", "target_repo_path": str(target), "target_base_sha": target_sha,
    })
    assert authority is not None
    assert authority["resolved_git_toplevel"] == str(target.resolve())
    assert authority["base_sha"] == target_sha
    with pytest.raises(RepositoryAuthorityError, match="TARGET_BASE_SHA_NOT_FOUND"):
        resolve_repository_authority({
            "task_mode": "repo", "target_repo_path": str(target), "target_base_sha": controller_sha,
        })


def test_worktree_comes_from_target_and_exact_head(tmp_path):
    controller = tmp_path / "controller"
    target = tmp_path / "target"
    _repo(controller, "controller.txt")
    target_sha = _repo(target, "target.txt")
    authority = resolve_repository_authority({
        "task_mode": "repo", "target_repo_path": str(target), "target_base_sha": target_sha,
    })
    info = create_subagent_worktree(str(controller), "authority-test", authority=authority)
    assert info is not None
    root = subprocess.check_output(["git", "-C", info["path"], "rev-parse", "--show-toplevel"], text=True).strip()
    head = subprocess.check_output(["git", "-C", info["path"], "rev-parse", "HEAD"], text=True).strip()
    assert root == str(target.resolve()) + "/.worktrees/authority-test"
    assert head == target_sha


def test_authority_attests_controller_source_and_version():
    attestation = require_repository_authority()
    assert attestation["controller_source_root"].endswith("hermes-active-runtime-authority")
    assert attestation["controller_head"]
    assert attestation["repository_authority_version"] == REPOSITORY_AUTHORITY_VERSION == 1


def test_mixed_control_plane_sources_fail_closed(monkeypatch):
    import tools.subagent_worktree as subagent_worktree

    monkeypatch.setattr(subagent_worktree, "__file__", "/tmp/operator-hud/tools/subagent_worktree.py")
    with pytest.raises(RepositoryAuthorityError, match="CONTROL_PLANE_SOURCE_MISMATCH"):
        require_repository_authority()


def test_missing_authority_version_fails_closed(monkeypatch):
    import tools.repository_authority as authority

    monkeypatch.setattr(authority, "REPOSITORY_AUTHORITY_VERSION", 0)
    with pytest.raises(RepositoryAuthorityError, match="REPOSITORY_AUTHORITY_UNAVAILABLE"):
        authority.require_repository_authority()


def test_controller_cwd_cannot_replace_target(tmp_path):
    controller = tmp_path / "controller"
    target = tmp_path / "target"
    controller_sha = _repo(controller, "controller.txt")
    target_sha = _repo(target, "target.txt")
    with pytest.raises(RepositoryAuthorityError, match="TARGET_BASE_SHA_NOT_FOUND"):
        resolve_repository_authority({
            "task_mode": "repo", "target_repo_path": str(controller), "target_base_sha": target_sha,
        })
    assert controller_sha != target_sha
