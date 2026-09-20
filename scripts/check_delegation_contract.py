"""Deterministic, non-live gate for the child delegation contract."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTEST_TARGETS = [
    "tests/tools/test_delegate_dispatch_contract.py",
    "tests/tools/test_delegate_contract_hardening.py",
    "tests/agent/test_subagent_lifecycle_dispatch_contract.py",
]
RUFF_TARGETS = [
    "tools/delegate_tool_contract.py",
    *PYTEST_TARGETS,
    "scripts/check_delegation_contract.py",
]
COMPILE_TARGETS = ["tools/delegate_tool_contract.py", "agent/subagent_lifecycle.py"]


def run(label: str, args: list[str]) -> None:
    print(f"[delegation] {label}")
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def check_static_invariants() -> None:
    """Keep the gate sensitive to removal of the fail-closed contract."""
    source = (ROOT / "tools/delegate_tool_contract.py").read_text()
    tree = ast.parse(source)
    validate = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_model"
    )
    required = {"recognized", "corrected_model", "accepted"}
    validate_source = ast.get_source_segment(source, validate) or ""
    missing = {name for name in required if f'get("{name}")' not in validate_source}
    if missing:
        raise SystemExit("delegation static invariant failed: model validation lost " + ", ".join(sorted(missing)))
    if '"provider"' not in source or '"session_id"' not in source:
        raise SystemExit("delegation static invariant failed: attestation identity fields are missing")


def main() -> int:
    check_static_invariants()
    run("focused tests", ["uv", "run", "pytest", "-q", *PYTEST_TARGETS])
    run("ruff", ["uv", "run", "ruff", "check", *RUFF_TARGETS])
    run("compileall", ["uv", "run", "python", "-m", "compileall", "-q", *COMPILE_TARGETS])
    run("diff check", ["git", "diff", "--check"])
    print("delegation contract gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())