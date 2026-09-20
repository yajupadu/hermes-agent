#!/usr/bin/env python3
"""Deterministic operator-quota contract gate."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FOCUSED = [
    "tests/agent/test_operator_quota.py",
    "tests/agent/test_account_usage.py",
    "tests/agent/test_quota_hud.py",
    "tests/hermes_cli/test_operator_hud.py",
]
FORBIDDEN = ("load_pool(", "select(", "reclaim(", "try_refresh_matching(",
             "mark_exhausted(", "write_credential_pool(")

def run(*args):
    return subprocess.run(args, cwd=ROOT, check=True).returncode

def main():
    if subprocess.run(("git", "branch", "--show-current"), cwd=ROOT,
                      check=True, capture_output=True, text=True).stdout.strip() != "operator-hud":
        raise SystemExit("wrong branch")
    hud = (ROOT / "agent/quota_hud.py").read_text()
    if any(token in hud for token in ("hermes_cli.auth", "CredentialPool", *FORBIDDEN)):
        raise SystemExit("operator HUD crossed the credential mutation boundary")
    run("uv", "run", "pytest", *FOCUSED, "-q")
    run("uv", "run", "ruff", "check", "agent/operator_quota.py", "agent/quota_hud.py", *FOCUSED)
    run(sys.executable, "-m", "compileall", "-q", "agent", "hermes_cli")
    run("git", "diff", "--check")
    print("operator quota gate: PASS")

if __name__ == "__main__":
    main()
