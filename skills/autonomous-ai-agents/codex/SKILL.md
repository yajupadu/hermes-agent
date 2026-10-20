---
name: codex
description: "Delegate coding to OpenAI Codex CLI (features, PRs)."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI

Delegate coding tasks to [Codex](https://github.com/openai/codex) via the Hermes terminal. Codex is OpenAI's autonomous coding agent CLI.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing

Requires the codex CLI and a git repository.

## Prerequisites

- Codex installed: `npm install -g @openai/codex`
- OpenAI auth configured: either `OPENAI_API_KEY` or Codex OAuth credentials
  from the Codex CLI login flow
- **Must run inside a git repository** — Codex refuses to run outside one
- Use `pty=true` in terminal calls — Codex is an interactive terminal app

For Hermes itself, `model.provider: openai-codex` uses Hermes-managed Codex
OAuth from `~/.hermes/auth.json` after `hermes auth add openai-codex`. For the
standalone Codex CLI, a valid CLI OAuth session may live under
`~/.codex/auth.json`; do not treat a missing `OPENAI_API_KEY` alone as proof
that Codex auth is missing.

## One-Shot Tasks

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project", pty=true)
```

For scratch work (Codex needs a git repo):
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'", pty=true)
```

## Background Mode (Long Tasks)

```
# Start in background with PTY
terminal(command="codex exec --sandbox workspace-write 'Refactor the auth module'", workdir="~/project", background=true, pty=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send input if Codex asks a question
process(action="submit", session_id="<id>", data="yes")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Key Flags

| Flag | Effect |
|------|--------|
| `exec "prompt"` | One-shot execution, exits when done |
| `--sandbox workspace-write` (`-s`) | Sandboxed but auto-approves file changes in the workspace (the recommended auto-build mode) |
| `--dangerously-bypass-approvals-and-sandbox` | No sandbox, no approvals (fastest, most dangerous; `--yolo` still works as a hidden alias) |
| `--sandbox danger-full-access` | No Codex sandbox; useful when the host service context breaks bubblewrap |

> **Deprecated:** `--full-auto` still works but the live CLI warns to use `--sandbox workspace-write` instead.

## Hermes Gateway Caveat

When invoking the Codex CLI from a Hermes gateway/service context (for example,
Telegram-driven agent sessions), Codex `workspace-write` sandboxing may fail even
when the same command works in the user's interactive shell. A typical symptom is
bubblewrap/user-namespace errors such as `setting up uid map: Permission denied`
or `loopback: Failed RTM_NEWADDR: Operation not permitted`.

In that context, prefer:

```
codex exec --sandbox danger-full-access "<task>"
```

Use process boundaries as the safety layer instead: explicit `workdir`, clean git
status before launch, narrow task prompts, `git diff` review, targeted tests, and
human/agent confirmation before committing broad changes.

## PR Reviews

Do not use a full clone for PR review. From an existing local checkout of the
same repository, fetch the PR head and base into unique temporary refs, resolve
both to commit OIDs, and create a unique detached linked worktree at the pinned
head OID. Run the reviewer there and compare against the pinned base OID. Remove
the worktree and temporary refs when finished. If no local object store is
available, stop and ask an operator to provision a bounded review source; do not
fall back to `git clone`.

Run this as a single `terminal` call (`pty=true`) in its own `bash` process
(save it to a scratch script and run `bash <script>`, so `set -eu`, `cd` and the
cleanup trap do not leak into the session shell). Set `REPO`, `PR` and `BASE_BRANCH` first; `REPO` must already
be a checkout of the same repository (the fetch only transfers objects it is
missing, and uses GitHub's `refs/pull/<n>/head`).

```bash
set -eu
REPO="$HOME/project"   # existing local checkout of the SAME repository
PR=42
BASE_BRANCH=main
REMOTE=origin

# No local object store -> stop here; do not clone.
git -C "$REPO" rev-parse --git-dir >/dev/null || {
  echo "no local object store: ask an operator to provision a bounded review source" >&2
  exit 2
}
echo "fetching from: $(git -C "$REPO" remote get-url "$REMOTE")"

# Unique worktree directory and unique temporary refs (never refs/heads, refs/remotes, refs/pull).
WT=$(mktemp -d "${TMPDIR:?set TMPDIR to the Hermes scratch dir}/pr-review.XXXXXX")
ID=${WT##*/}
HEAD_REF=refs/hermes-review/$ID/head
BASE_REF=refs/hermes-review/$ID/base

# Refuse collisions and compare-and-delete so cleanup cannot remove another run’s refs.
HEAD_REF_OID=
BASE_REF_OID=
cleanup() {
  # No --force: git refuses to remove a dirty/untracked worktree, so reviewer output is never destroyed.
  git -C "$REPO" worktree remove "$WT" 2>/dev/null || { [ ! -e "$WT" ] || echo "preserved dirty review worktree: $WT" >&2; }
  rmdir "$WT" 2>/dev/null || true
  [ -z "$HEAD_REF_OID" ] || git -C "$REPO" update-ref -d "$HEAD_REF" "$HEAD_REF_OID" 2>/dev/null || true
  [ -z "$BASE_REF_OID" ] || git -C "$REPO" update-ref -d "$BASE_REF" "$BASE_REF_OID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if git -C "$REPO" show-ref --verify --quiet "$HEAD_REF" ||
   git -C "$REPO" show-ref --verify --quiet "$BASE_REF"; then
  echo "temporary review ref already exists; choose a fresh scratch path and retry" >&2
  exit 2
fi

git -C "$REPO" -c gc.auto=0 -c maintenance.auto=false fetch --atomic --no-tags \
  --no-write-fetch-head --no-recurse-submodules "$REMOTE" \
  "refs/pull/$PR/head:$HEAD_REF" "refs/heads/$BASE_BRANCH:$BASE_REF"

# Record each fetched ref's raw OID as soon as it exists, so cleanup can
# compare-and-delete exactly the refs this run created even if validation fails.
HEAD_REF_OID=$(git -C "$REPO" rev-parse --verify "$HEAD_REF")
BASE_REF_OID=$(git -C "$REPO" rev-parse --verify "$BASE_REF")
# Pin: peel each to a commit OID; worktree and diff use only these commit OIDs.
HEAD_OID=$(git -C "$REPO" rev-parse --verify --quiet "$HEAD_REF_OID^{commit}") ||
  { echo "PR head $HEAD_REF_OID is not a commit; nothing was reviewed" >&2; exit 2; }
BASE_OID=$(git -C "$REPO" rev-parse --verify --quiet "$BASE_REF_OID^{commit}") ||
  { echo "base $BASE_REF_OID is not a commit; nothing was reviewed" >&2; exit 2; }

git -C "$REPO" worktree add --detach "$WT" "$HEAD_OID"
cd "$WT"
codex review --base "$BASE_OID"
```

Afterwards, `git -C "$REPO" worktree list` and `git -C "$REPO" for-each-ref
refs/hermes-review/` should no longer show this run's worktree or refs.

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 ~/.hermes/cache/scratch/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 ~/.hermes/cache/scratch/issue-99 main", workdir="~/project")

# Launch Codex in each
terminal(command="codex --sandbox workspace-write exec 'Fix issue #78: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-78", background=true, pty=true)
terminal(command="codex --sandbox workspace-write exec 'Fix issue #99: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-99", background=true, pty=true)

# Monitor
process(action="list")

# After completion, push and create PRs
terminal(command="cd ~/.hermes/cache/scratch/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove ~/.hermes/cache/scratch/issue-78", workdir="~/project")
```

## Batch PR Reviews

```
# Fetch all PR refs
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# Review multiple PRs in parallel
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true, pty=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true, pty=true)

# Post results
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## Rules

1. **Always use `pty=true`** — Codex is an interactive terminal app and hangs without a PTY
2. **Git repo required** — Codex won't run outside a git directory. Use `mktemp -d && git init` for scratch
3. **Use `exec` for one-shots** — `codex exec "prompt"` runs and exits cleanly
4. **`--sandbox workspace-write` for building** — auto-approves changes within the sandbox (`--full-auto` is deprecated for this)
5. **Background for long tasks** — use `background=true` and monitor with `process` tool
6. **Don't interfere** — monitor with `poll`/`log`, be patient with long-running tasks
7. **Parallel is fine** — run multiple Codex processes at once for batch work
