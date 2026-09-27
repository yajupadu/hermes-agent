ACTIVE TRADER RUNTIME MAP

Observed command: trader --help / trader --version

Shell launcher:
  /Users/yajupadu/.local/bin/trader
  ROOT before integration: /Users/yajupadu/.hermes/worktrees/hermes-agent-operator-hud
  ROOT after integration: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority
  PYTHON: /Users/yajupadu/.hermes/hermes-agent/venv/bin/python
  exec: "$PYTHON" "$ROOT/hermes" -p trader --in "$ROOT" --no-restore-cwd "$@"

Pre-fix proof:
  The launcher changed directory to operator-hud before executing Hermes.
  Therefore Python sys.path[0] was the operator-hud checkout directory.
  The active pre-fix checkout HEAD was 97dee724b5a24733d9b9dcff037edc6fb52c9b2b.
  The active child reported repository identity NousResearch/hermes-agent and that HEAD.
  This is why Phase B used operator-hud code rather than the accepted authority candidate.
  The cause was launcher-selected source root, not target-repository resolution.

Active post-integration proof:
  cwd: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority
  Python executable: /Users/yajupadu/.hermes/hermes-agent/venv/bin/python
  Hermes source root: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority
  Active HEAD at mapping time: 4831ec3732553d51ae29ff9cb3b33766aaa10e15
  tools.delegate_tool.__file__: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority/tools/delegate_tool.py
  tools.repository_authority.__file__: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority/tools/repository_authority.py
  tools.subagent_worktree.__file__: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority/tools/subagent_worktree.py
  tools.delegate_tool_child_run.__file__: /Users/yajupadu/.hermes/worktrees/hermes-active-runtime-authority/tools/delegate_tool_child_run.py
  sys.path[0]: empty-string entry for the active cwd, resolving to the source root above
  repository authority version: 1

Architecture classification:
  A. Port authority fix into operator-hud: viable, but permanently binds security behavior to a mutable WIP branch.
  B. HUD presentation from operator-hud with authority-enabled core: viable only with an explicit, tested import boundary; mixed imports are otherwise unsafe.
  C. Versioned user-owned overlay containing HUD customizations over authority-enabled core: best long-term separation, but requires a broader overlay/update mechanism not present here.
  D. Minimal supported architecture: a clean integration worktree branched from the exact operator-hud HEAD, with the bounded authority commit and attestation applied, and trader launcher pointed to that worktree. This preserves committed HUD behavior and isolates historical dirty WIP.

Ranking:
  Correctness: D > C > B > A
  Update safety: C > D > B > A
  HUD preservation: D >= B >= C > A
  Mixed-import risk: C < D < B < A
  Complexity: A < D < B < C
  Maintenance burden: C < D < B < A

Selected architecture: D, as the minimum safe permanent fix supported by the current repository and launcher model. The control plane is one source root; HUD code remains in the same integration checkout; historical operator-hud dirty WIP is not modified.
