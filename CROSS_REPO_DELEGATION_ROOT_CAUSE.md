# Cross-repository delegation root cause

## Reproduction

The delegation worktree path accepted a parent-derived workspace hint and used the
controller repository's current Git context when constructing a child worktree.
The task payload did not carry a required, structured target repository authority.
Consequently a task whose prose named `/Users/yajupadu/meticulous_trader` could
still resolve from the Hermes controller repository. Base SHA lookup and child
cwd then followed that workspace choice. A failed worktree setup was also
allowed to degrade to the shared workspace, which made the controller repository
a silent fallback.

The defect is reproducible with two temporary Git repositories: give the child a
SHA from repository B while the controller is in repository A. The old path had
no target-local identity or SHA assertion, so the controller-derived workspace
could be selected. The regression tests now exercise this exact split.

## Correction

Repo-scoped tasks now carry explicit `target_repo_path`, `target_base_sha`, and
`task_mode` authority. The target is canonicalized through Git, including
`--show-toplevel`, `--git-common-dir`, remote identity where available, and a
fingerprint. The requested commit is verified with `git -C TARGET_REPO
rev-parse --verify TARGET_BASE_SHA^{commit}` before worktree creation.

Worktree creation receives this authority directly and validates the resulting
worktree common Git directory and HEAD. Explicit-authority worktree failures
raise `WORKSPACE_BOOTSTRAP_FAILED`; they do not degrade to the controller or
shared workspace. Missing target authority fails closed with
`TARGET_REPOSITORY_REQUIRED`, and a target-local SHA miss returns
`TARGET_BASE_SHA_NOT_FOUND`.

The parent launch path stores authority on the child run, and the worktree
startup path asserts repository identity and exact base SHA before binding the
child cwd. Legacy non-repository tasks retain their prior behavior because they
have no repository authority to enforce.

## Scope

This is limited to repository authority and child workspace bootstrap. It does
not alter model routing, HUD behavior, rollover, or trading strategy logic.
