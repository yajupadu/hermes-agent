---
title: "Grok — Delegate coding to xAI Grok Build CLI (features, PRs)"
sidebar_label: "Grok"
description: "Delegate coding to xAI Grok Build CLI (features, PRs)"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Grok

Delegate coding to xAI Grok Build CLI (features, PRs).

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/autonomous-ai-agents/grok` |
| Path | `optional-skills/autonomous-ai-agents/grok` |
| Version | `0.1.1` |
| Author | Matt Maximo (MattMaximo), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `Coding-Agent`, `Grok`, `xAI`, `Code-Review`, `Refactoring`, `Automation` |
| Related skills | [`codex`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-codex.md), [`claude-code`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code.md), [`hermes-agent`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Grok Build CLI — Hermes Orchestration Guide

Delegate coding tasks to [Grok Build](https://docs.x.ai/build/overview) (xAI's
autonomous coding agent CLI, the `grok` command) via the Hermes terminal. Grok
can read files, write code, run shell commands, spawn subagents, and manage git
workflows. It runs three ways: an interactive TUI, **headless** (`-p`), and as
an **ACP agent** over JSON-RPC.

This is the third sibling to `codex` and `claude-code`. The orchestration
pattern is nearly identical — **prefer headless `-p` for one-shots**, use a PTY
for interactive sessions.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing
- Any task where you'd otherwise reach for Codex / Claude Code but want Grok

## Prerequisites

- **Install (preferred):** `npm install -g @xai-official/grok`
  - The official installer `curl -fsSL https://x.ai/cli/install.sh | bash` also
    works, but the `x.ai` host is Cloudflare-walled in some environments. The
    npm path avoids that dependency entirely.
- **Auth — SuperGrok / X Premium+ subscription (primary path):**
  - Run `grok login` once → opens a browser for OAuth → token cached in
    `~/.grok/auth.json`. This uses your **SuperGrok or X Premium+** subscription
    (no per-token API billing).
  - Check sign-in state by looking for `~/.grok/auth.json`, or run a cheap
    headless smoke test: `grok --no-auto-update -p "Say ok."`
  - In the TUI, `/logout` signs out and `/login` (or relaunching) signs back in.
- **No git repo required** — unlike Codex, Grok runs fine outside a git
  directory (good for scratch/throwaway tasks).
- **Claude Code / AGENTS.md compatible with zero config** — Grok auto-reads
  `CLAUDE.md`, `.claude/` (skills, agents, MCPs, hooks, rules), and the
  `AGENTS.md` family. Existing project context just works.

> **API-key fallback (not the default for this user):** Grok also supports
> setting the `XAI_API_KEY` environment variable for pay-as-you-go billing
> via `api.x.ai`. Only use
> this if `grok login` / SuperGrok auth is unavailable. The subscription path
> (`grok login`) is the intended setup here.

## Two Orchestration Modes

### Mode 1: Headless (`-p`) — Non-Interactive (PREFERRED)

Runs a one-shot task, prints the result, and exits. No PTY, no interactive
dialogs to navigate. This is the cleanest integration path — the analog of
`claude -p` and `codex exec`.

```
terminal(command="grok --no-auto-update -p 'Add a dark mode toggle to settings'", workdir="/path/to/project", timeout=180)
```

Always pass `--no-auto-update` in automation to skip background update checks.

**When to use headless:**
- One-shot coding tasks (fix a bug, add a feature, refactor)
- CI/CD automation and scripting
- Structured output parsing with `--output-format json`
- Any task that doesn't need multi-turn conversation

### Mode 2: Interactive PTY — Multi-Turn TUI Sessions

The TUI is a fullscreen, mouse-interactive app. Drive it with `pty=true`. For
robust monitoring/input use tmux (same pattern as the `claude-code` skill).

```
# Launch in a tmux session for capture-pane monitoring
terminal(command="tmux new-session -d -s grok-work -x 140 -y 40")
terminal(command="tmux send-keys -t grok-work 'cd /path/to/project && grok' Enter")

# Wait for startup, then send a task
terminal(command="sleep 5 && tmux send-keys -t grok-work 'Refactor the auth module to use JWT' Enter")

# Monitor progress
terminal(command="sleep 15 && tmux capture-pane -t grok-work -p -S -50")

# Exit when done
terminal(command="tmux send-keys -t grok-work '/quit' Enter && sleep 1 && tmux kill-session -t grok-work")
```

**Tip for headless-but-inline output:** if you want TUI-style output without the
fullscreen alt-screen takeover (e.g. for cleaner logs), add `--no-alt-screen`.
For pure automation, headless `-p` is still cleaner than the TUI.

## Headless Deep Dive

### Common Flags

| Flag | Effect |
|------|--------|
| `-p, --single <PROMPT>` | Send one prompt, run headless, exit |
| `-m, --model <MODEL>` | Choose a model |
| `-s, --session-id <UUID>` | Assign a **NEW** valid UUID to a fresh conversation (must not already exist). Does **not** resume — use `--resume`/`--continue` for that. Only valid with `--resume`/`--continue` when paired with `--fork-session` |
| `-r, --resume [<UUID>]` | Resume an existing session by its UUID (or the most recent if omitted) |
| `-c, --continue` | Continue the most recent session in the current directory |
| `--fork-session` | When resuming, create a new session ID instead of reusing the original |
| `--max-turns <N>` | Cap the maximum number of agent turns |
| `--cwd <PATH>` | Set the working directory |
| `--output-format <FMT>` | `plain` (default), `json`, or `streaming-json` |
| `--always-approve` | Auto-approve all tool executions (the `--full-auto` / `--yolo` equivalent) |
| `--no-alt-screen` | Run inline, no fullscreen TUI takeover |
| `--no-auto-update` | Skip background update checks (use in all automation; hidden from `--help` but still works) |

### Output Formats

- `plain` — human-readable text (default)
- `json` — one JSON object at the end of the run (parse the result cleanly)
- `streaming-json` — newline-delimited JSON events as they arrive

```
# Structured result for parsing
terminal(command="grok --no-auto-update -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the database layer and run the tests'", workdir="/project", timeout=300)
```

### Background Mode (Long Tasks)

```
# Start headless in background
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the auth module'", workdir="/project", background=true, notify_on_complete=true)
# Returns session_id

# Monitor
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

For an interactive (TUI) background session, use `pty=true` + tmux and monitor
with `tmux capture-pane`, exactly like the `claude-code` / `codex` skills.

### Session Continuation

Sessions are keyed by **UUID**, not by name. `--session-id` assigns a *new* UUID
to a fresh run (it does **not** resume); `--resume` takes an existing session's
UUID (or omit the value to resume the most recent).

```
# Start a session with a self-assigned UUID (must be a valid, unused UUID)
SID=$(uuidgen)
terminal(command="grok --no-auto-update -s $SID -p 'Start refactoring the database layer' --always-approve", workdir="/project", timeout=240)

# Resume that exact session later by its UUID
terminal(command="grok --no-auto-update -r $SID -p 'Now add connection pooling' --always-approve", workdir="/project", timeout=180)

# Or just continue the most recent session in this directory (no UUID needed)
terminal(command="grok --no-auto-update -c -p 'What did you change last time?'", workdir="/project", timeout=60)
```

## Read-Only Audit → Markdown Note Pattern

To have Grok review local artifacts and return a clean markdown note (for
Obsidian or a repo) without mutating anything:

1. Prepare stable input files first with Hermes tools (`read_file`,
   `write_file`). Snapshot only the relevant context into a temp file rather
   than dumping raw paths.
2. Run Grok headless **without** `--always-approve` so it cannot auto-write, and
   demand `markdown only, no preamble`.
3. Save Grok's stdout straight into the destination note with `write_file()`.

```
grok --no-auto-update -p "Read ~/.hermes/cache/scratch/current.md and ~/.hermes/cache/scratch/inventory.md. Produce markdown only, no preamble. Output a clean note titled 'Cleanup Review'." --output-format plain
```

**Pitfall (same as Claude Code):** for document rewrites, a loose "rewrite this"
prompt may return a change summary instead of the full file. Instead: pipe the
file in, and demand `Return ONLY the full revised markdown document. No intro,
no explanation, no code fences. Start immediately with '# Title'.` Verify the
first lines with `read_file()` before overwriting the destination.

## PR Review Patterns

### Quick Review (Headless)

```
terminal(command="cd /path/to/repo && git diff main...feature-branch | grok --no-auto-update -p 'Review this diff for bugs, security issues, and style problems. Be thorough.'", timeout=120)
```

### Isolated PR Review (no full clone)

Use an existing local checkout of the same repository: fetch the PR head and
base into unique temporary refs, pin both resolved commit OIDs, and create a
unique detached linked worktree at the pinned head. Run Grok there against the
pinned base OID, then explicitly remove the worktree and temporary refs. If no
local object store is available, stop and ask an operator to provision a
bounded review source; never fall back to a full clone.

Run this as a single `terminal` call (`pty=true`, `timeout=300`) in its own
`bash` process (save it to a scratch script and run `bash <script>`, so
`set -eu`, `cd` and the cleanup trap do not leak into the session shell). Set
`REPO`, `PR` and `BASE_BRANCH` first; `REPO` must already be a checkout of the
same repository (the fetch only transfers objects it is missing, and uses
GitHub's `refs/pull/<n>/head`).

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
grok --no-auto-update -p "Review the changes against $BASE_OID (git diff $BASE_OID...$HEAD_OID). Check bugs, security, race conditions, missing tests."
```

Afterwards, `git -C "$REPO" worktree list` and `git -C "$REPO" for-each-ref
refs/hermes-review/` should no longer show this run's worktree or refs.

### Post the review

```
terminal(command="gh pr comment 42 --body '<review text>'", workdir="/path/to/repo")
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 ~/.hermes/cache/scratch/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 ~/.hermes/cache/scratch/issue-99 main", workdir="~/project")

# Launch Grok headless in each (background)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #78: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-78", background=true, notify_on_complete=true)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #99: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-99", background=true, notify_on_complete=true)

# Monitor
process(action="list")

# After completion: push and open PRs
terminal(command="cd ~/.hermes/cache/scratch/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove ~/.hermes/cache/scratch/issue-78", workdir="~/project")
```

## Useful Subcommands & TUI Commands

| Command | Purpose |
|---------|---------|
| `grok` | Start the interactive TUI |
| `grok -p "query"` | Headless one-shot |
| `grok login` / `grok logout` | Sign in / out (SuperGrok / X Premium+ OAuth) |
| `grok inspect` | Show what Grok discovered in cwd: config sources, instructions, skills, plugins, hooks, MCP servers |
| `grok agent stdio` | Run as an ACP agent over JSON-RPC (for IDE/tool integration) |
| `grok update` | Update the CLI (needs the `x.ai` host; skip in automation) |

TUI slash commands (interactive only): `/model <name>`, `/always-approve`,
`/plan`, `/context`, `/compact`, `/resume`, `/sessions`, `/fork`, `/usage`,
`/quit`. `Shift+Tab` cycles session modes (including Plan mode, which blocks
write tools except the session plan file).

## Config (`~/.grok/config.toml`)

```toml
[cli]
auto_update = false          # skip background update checks persistently

[ui]
permission_mode = "ask"      # or "always-approve" to skip tool prompts by default

[models]
default = "grok-build-0.1"
```

Put global preferences in `~/.grok/config.toml` (not project-scoped
`.grok/config.toml`). `permission_mode` supersedes the legacy `approval_mode` /
`yolo = true` keys.

## Pitfalls & Gotchas

1. **Auth is subscription-gated.** `grok login` requires a SuperGrok or X
   Premium+ subscription. If login fails or there's no `~/.grok/auth.json`,
   confirm the subscription is active before falling back to `XAI_API_KEY`.
2. **Don't conflate Hermes' xAI auth with the `grok` CLI's auth.** Hermes'
   `x_search` runs on its own xAI OAuth; the standalone `grok` CLI has a
   separate token in `~/.grok/auth.json`. A working `x_search` does NOT mean
   `grok` is logged in.
3. **Always pass `--no-auto-update` in automation** — otherwise Grok phones home
   for update checks (and `x.ai`/`storage.googleapis.com` may be unreachable).
4. **Prefer npm install over the curl installer** — `npm install -g
   @xai-official/grok` avoids the Cloudflare-walled `x.ai` host.
5. **`--always-approve` is the autonomous-build switch.** Without it, headless
   runs may stall waiting on tool-approval prompts. Omit it deliberately for
   read-only review/audit work so Grok can't mutate files.
6. **Headless `-p` skips TUI dialogs**; the TUI needs `pty=true` (+ tmux for
   monitoring), just like Claude Code.
7. **Use `--no-alt-screen`** if you run the TUI inline and the fullscreen
   alt-screen takeover garbles captured output.
8. **No git repo needed**, but for PR/commit workflows you still want one — use
   `mktemp -d && git init` for scratch commit tasks.
9. **Clean up tmux sessions** with `tmux kill-session -t <name>` when done.

## Rules for Hermes Agents

1. **Prefer headless `-p`** for single tasks — cleanest integration, structured
   output via `--output-format json`.
2. **Always set `workdir`** (or `--cwd`) so Grok targets the right project.
3. **Pass `--no-auto-update`** in every automated invocation.
4. **Use `--always-approve` only when Grok should write autonomously**; omit it
   for read-only reviews and audits.
5. **Background long tasks** with `background=true, notify_on_complete=true` and
   monitor via the `process` tool.
6. **Use tmux for multi-turn interactive work** and monitor with
   `tmux capture-pane -t <session> -p -S -50`.
7. **Verify auth before relying on it** — check `~/.grok/auth.json` or run a
   cheap `grok -p "Say ok."` smoke test; don't assume Hermes' xAI auth carries
   over.
8. **Report results to the user** — summarize what Grok changed and what's left.
