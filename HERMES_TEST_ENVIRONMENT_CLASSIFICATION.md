Hermes test environment classification

AUTHORITATIVE_RUNTIME: /Users/yajupadu/.hermes/hermes-agent/.venv/bin/python (pytest available)
PYTHON_PATH: /Users/yajupadu/.hermes/hermes-agent/.venv/bin/python
PYTEST_SOURCE: interpreter module pytest
RUNNER: scripts/run_tests.sh

FOCUSED_INVOCATION: direct authoritative interpreter with tests/tools/test_repository_authority.py
FOCUSED_RESULT: 4 passed

RUN_TESTS_WRAPPER_FAILURE: The wrapper invocation from the contaminated operator-hud worktree exited 1 without test diagnostics. In the clean candidate, the repository focused tests pass under the discovered interpreter. This is classified as an environment/bootstrap/runner selection problem, not a repository-authority regression. The broader tests/tools collection under the available environment fails during collection because installed binary dependencies are incompatible/missing (pydantic_core, rpds, fal_client) and contains pre-existing unrelated import failures.

NO_GLOBAL_INSTALL: YES
