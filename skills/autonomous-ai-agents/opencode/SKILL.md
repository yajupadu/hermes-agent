---
name: opencode
description: "Delegate coding to OpenCode CLI (features, PR review)."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, OpenCode, Autonomous, Refactoring, Code-Review]
    related_skills: [claude-code, codex, hermes-agent]
---

# OpenCode CLI

Use [OpenCode](https://opencode.ai) as an autonomous coding worker orchestrated by Hermes terminal/process tools. OpenCode is a provider-agnostic, open-source AI coding agent with a TUI and CLI.

## When to Use

- User explicitly asks to use OpenCode
- You want an external coding agent to implement/refactor/review code
- You need long-running coding sessions with progress checks
- You want parallel task execution in isolated workdirs/worktrees

## Prerequisites

- OpenCode installed: `npm i -g opencode-ai@latest` or `brew install anomalyco/tap/opencode`
- Auth configured: `opencode auth login` or set provider env vars (OPENROUTER_API_KEY, etc.)
- Verify: `opencode auth list` should show at least one provider
- Git repository for code tasks (recommended)
- `pty=true` for interactive TUI sessions

## Binary Resolution (Important)

Shell environments may resolve different OpenCode binaries. If behavior differs between your terminal and Hermes, check:

```
terminal(command="which -a opencode")
terminal(command="opencode --version")
```

If needed, pin an explicit binary path:

```
terminal(command="$HOME/.opencode/bin/opencode run '...'", workdir="~/project", pty=true)
```

## One-Shot Tasks

Use `opencode run` for bounded, non-interactive tasks:

```
terminal(command="opencode run 'Add retry logic to API calls and update tests'", workdir="~/project")
```

Attach context files with `-f`:

```
terminal(command="opencode run 'Review this config for security issues' -f config.yaml -f .env.example", workdir="~/project")
```

Show model thinking with `--thinking`:

```
terminal(command="opencode run 'Debug why tests fail in CI' --thinking", workdir="~/project")
```

Force a specific model:

```
terminal(command="opencode run 'Refactor auth module' --model openrouter/anthropic/claude-sonnet-4", workdir="~/project")
```

## Interactive Sessions (Background)

For iterative work requiring multiple exchanges, start the TUI in background:

```
terminal(command="opencode", workdir="~/project", background=true, pty=true)
# Returns session_id

# Send a prompt
process(action="submit", session_id="<id>", data="Implement OAuth refresh flow and add tests")

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send follow-up input
process(action="submit", session_id="<id>", data="Now add error handling for token expiry")

# Exit cleanly — Ctrl+C
process(action="write", session_id="<id>", data="\x03")
# Or just kill the process
process(action="kill", session_id="<id>")
```

**Important:** Do NOT use `/exit` — it is not a valid OpenCode command and will open an agent selector dialog instead. Use Ctrl+C (`\x03`) or `process(action="kill")` to exit.

### TUI Keybindings

| Key | Action |
|-----|--------|
| `Enter` | Submit message (press twice if needed) |
| `Tab` | Switch between agents (build/plan) |
| `Ctrl+P` | Open command palette |
| `Ctrl+X L` | Switch session |
| `Ctrl+X M` | Switch model |
| `Ctrl+X N` | New session |
| `Ctrl+X E` | Open editor |
| `Ctrl+C` | Exit OpenCode |

### Resuming Sessions

After exiting, OpenCode prints a session ID. Resume with:

```
terminal(command="opencode -c", workdir="~/project", background=true, pty=true)  # Continue last session
terminal(command="opencode -s ses_abc123", workdir="~/project", background=true, pty=true)  # Specific session
```

## Common Flags

| Flag | Use |
|------|-----|
| `run 'prompt'` | One-shot execution and exit |
| `--continue` / `-c` | Continue the last OpenCode session |
| `--session <id>` / `-s` | Continue a specific session |
| `--agent <name>` | Choose OpenCode agent (build or plan) |
| `--model provider/model` | Force specific model |
| `--format json` | Machine-readable output/events |
| `--file <path>` / `-f` | Attach file(s) to the message |
| `--thinking` | Show model thinking blocks |
| `--variant <level>` | Reasoning effort (high, max, minimal) |
| `--title <name>` | Name the session |
| `--attach <url>` | Connect to a running opencode server |

## Procedure

1. Verify tool readiness:
   - `terminal(command="opencode --version")`
   - `terminal(command="opencode auth list")`
2. For bounded tasks, use `opencode run '...'` (no pty needed).
3. For iterative tasks, start `opencode` with `background=true, pty=true`.
4. Monitor long tasks with `process(action="poll"|"log")`.
5. If OpenCode asks for input, respond via `process(action="submit", ...)`.
6. Exit with `process(action="write", data="\x03")` or `process(action="kill")`.
7. Summarize file changes, test results, and next steps back to user.

## PR Review Workflow

OpenCode has a built-in PR command:

```
terminal(command="opencode pr 42", workdir="~/project", pty=true)
```

Do not use a full clone for PR review. From an existing local checkout of the
same repository, fetch the PR head and base into unique temporary refs, resolve
both to commit OIDs, and create a unique detached linked worktree at the pinned
head OID. Run the reviewer there against the pinned base OID, then explicitly
remove the worktree and temporary refs. If no local object store is available,
stop and ask an operator to provision a bounded review source; do not clone.

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

# Fail closed on the file bound: a positive integer of at most 500, default 20.
MAX_FILES=${MAX_FILES:-20}
if ! [[ "$MAX_FILES" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$MAX_FILES" -gt 500 ]; then
  echo "MAX_FILES must be an integer from 1 to 500 (got: $MAX_FILES)" >&2
  exit 2
fi

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
# Fail now if the diff cannot be computed (a failure inside <(...) below would be silent).
git diff --name-only -z "$BASE_OID...$HEAD_OID" >/dev/null
FILE_ARGS=()
file_count=0
while IFS= read -r -d '' file; do
  if [ "$file_count" -ge "$MAX_FILES" ]; then
    echo "PR changes more than $MAX_FILES files; refusing a partial review (raise MAX_FILES or review in chunks)" >&2
    exit 2
  fi
  FILE_ARGS+=(-f "$file")
  file_count=$((file_count + 1))
done < <(git diff --name-only -z "$BASE_OID...$HEAD_OID")
opencode run "Review this PR against $BASE_OID (git diff $BASE_OID...$HEAD_OID). Report bugs, security risks, test gaps, and style issues." ${FILE_ARGS[@]+"${FILE_ARGS[@]}"}
```

Afterwards, `git -C "$REPO" worktree list` and `git -C "$REPO" for-each-ref
refs/hermes-review/` should no longer show this run's worktree or refs.

## Parallel Work Pattern

Use separate workdirs/worktrees to avoid collisions:

```
terminal(command="opencode run 'Fix issue #101 and commit'", workdir="~/.hermes/cache/scratch/issue-101", background=true, pty=true)
terminal(command="opencode run 'Add parser regression tests and commit'", workdir="~/.hermes/cache/scratch/issue-102", background=true, pty=true)
process(action="list")
```

## Session & Cost Management

List past sessions:

```
terminal(command="opencode session list")
```

Check token usage and costs:

```
terminal(command="opencode stats")
terminal(command="opencode stats --days 7 --models anthropic/claude-sonnet-4")
```

## Pitfalls

- Interactive `opencode` (TUI) sessions require `pty=true`. The `opencode run` command does NOT need pty.
- `/exit` is NOT a valid command — it opens an agent selector. Use Ctrl+C to exit the TUI.
- PATH mismatch can select the wrong OpenCode binary/model config.
- If OpenCode appears stuck, inspect logs before killing:
  - `process(action="log", session_id="<id>")`
- Avoid sharing one working directory across parallel OpenCode sessions.
- Enter may need to be pressed twice to submit in the TUI (once to finalize text, once to send).

## Verification

Smoke test:

```
terminal(command="opencode run 'Respond with exactly: OPENCODE_SMOKE_OK'")
```

Success criteria:
- Output includes `OPENCODE_SMOKE_OK`
- Command exits without provider/model errors
- For code tasks: expected files changed and tests pass

## Rules

1. Prefer `opencode run` for one-shot automation — it's simpler and doesn't need pty.
2. Use interactive background mode only when iteration is needed.
3. Always scope OpenCode sessions to a single repo/workdir.
4. For long tasks, provide progress updates from `process` logs.
5. Report concrete outcomes (files changed, tests, remaining risks).
6. Exit interactive sessions with Ctrl+C or kill, never `/exit`.
