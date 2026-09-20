"""Lifecycle handles attest the same per-dispatch contract as delegate_task."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
)


def _parent():
    return SimpleNamespace(
        session_id="parent-lifecycle",
        model="test-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="key",
        api_mode="chat_completions",
        enabled_toolsets=["file", "terminal", "web"],
        disabled_toolsets=[],
        valid_tool_names={
            "read_file", "search_files", "write_file", "patch",
            "terminal", "process_manage", "web_search", "web_extract",
        },
    )


def _child():
    names = ("read_file", "search_files")
    return SimpleNamespace(
        _subagent_id="sa-lifecycle",
        _delegate_role="leaf",
        _delegate_depth=1,
        session_id="child-lifecycle",
        provider="openrouter",
        model="test-model",
        reasoning_config={"enabled": True, "effort": "high"},
        tools=[{"type": "function", "function": {"name": name}} for name in names],
        valid_tool_names=set(names),
    )


def test_lifecycle_forwards_and_attests_model_reasoning_and_read_only_scope(monkeypatch):
    parent = _parent()
    child = _child()
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return child

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "status": "completed", "summary": "reviewed", "api_calls": 1, "duration_seconds": 0.1,
        },
    )
    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(
        goal="Review exact candidate",
        model="test-model",
        reasoning="high",
        allowed_toolsets=("file",),
        correlation_id="candidate-1",
    ))

    assert captured["model"] == "test-model"
    assert captured["toolsets"] == ["file"]
    assert captured["dispatch_config"].reasoning == "high"
    assert handle.model == "test-model"
    assert handle.reasoning == "high"
    assert handle.allowed_toolsets == ("file",)
    assert handle.task_fingerprint == captured["dispatch_config"].fingerprint
    assert child.valid_tool_names == {"read_file", "search_files"}
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    forged_candidate = replace(handle, task_fingerprint="0" * 64)
    assert service.status(forged_candidate).state is SubagentState.UNKNOWN


@pytest.mark.parametrize(
    "launch_request",
    [
        SubagentLaunchRequest(goal="x", reasoning="extreme"),
        SubagentLaunchRequest(goal="x", allowed_toolsets=()),
        SubagentLaunchRequest(goal="x", allowed_toolsets=("missing",)),
    ],
)
def test_lifecycle_invalid_explicit_policy_fails_closed(launch_request):
    service = SubagentLifecycleService(_parent)
    with pytest.raises(SubagentLifecycleError):
        service.launch(launch_request)
