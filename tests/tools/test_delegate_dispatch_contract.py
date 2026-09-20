"""Per-dispatch delegation routing, capability, and attestation contracts."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tool_executor import _parse_tool_call
from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_child_agent,
    _build_children,
    _normalize_task_list,
)
from tools.delegate_tool_contract import apply_and_attest_child, build_dispatch_configs
from tools.registry import registry


def _parent(**overrides):
    values = {
        "model": "test-model",
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "key",
        "api_mode": "chat_completions",
        "session_id": "parent-session",
        "enabled_toolsets": ["file", "terminal", "web"],
        "disabled_toolsets": [],
        "valid_tool_names": {
            "read_file", "write_file", "patch", "search_files",
            "terminal", "process_manage", "web_search", "web_extract",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _credentials(parent):
    return {
        key: getattr(parent, key)
        for key in ("model", "provider", "base_url", "api_key", "api_mode")
    }


def _child(**overrides):
    tools = [
        {"type": "function", "function": {"name": name}}
        for name in ("read_file", "search_files")
    ]
    values = {
        "_subagent_id": "sa-child",
        "session_id": "child-session",
        "model": "test-model",
        "reasoning_config": {"enabled": True, "effort": "high"},
        "tools": tools,
        "valid_tool_names": {item["function"]["name"] for item in tools},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_public_schema_and_registry_accept_all_dispatch_overrides():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_props = props["tasks"]["items"]["properties"]
    for field in ("model", "reasoning", "allowed_toolsets"):
        assert field in props
        assert field in task_props

    captured = {}
    with patch("tools.delegate_tool.delegate_task", lambda **kwargs: captured.update(kwargs) or "{}"):
        result = registry.dispatch(
            "delegate_task",
            {
                "tasks": [{"goal": "Inspect the candidate"}],
                "model": "test-model",
                "reasoning": "high",
                "allowed_toolsets": ["file"],
            },
            parent_agent=_parent(),
        )
    assert result == "{}"
    assert captured["model"] == "test-model"
    assert captured["reasoning"] == "high"
    assert captured["allowed_toolsets"] == ["file"]


def test_normalization_binds_defaults_to_exact_tasks_without_overwriting_candidates():
    tasks = [
        {"goal": "Inspect candidate alpha", "model": "alpha"},
        {"goal": "Inspect candidate beta", "reasoning": "low"},
    ]
    normalized, error = _normalize_task_list(
        None, None, tasks, None, "leaf", 4,
        model="default", reasoning="high", allowed_toolsets=["file"],
    )
    assert error is None
    assert normalized == [
        {
            "goal": "Inspect candidate alpha", "model": "alpha",
            "reasoning": "high", "allowed_toolsets": ["file"],
        },
        {
            "goal": "Inspect candidate beta", "model": "default",
            "reasoning": "low", "allowed_toolsets": ["file"],
        },
    ]


def test_child_construction_receives_each_exact_dispatch_config():
    parent = _parent()
    tasks = [
        {"goal": "Inspect candidate one", "reasoning": "high", "allowed_toolsets": ["file"]},
        {"goal": "Inspect candidate two", "reasoning": "low", "allowed_toolsets": ["web"]},
    ]
    configs = build_dispatch_configs(tasks, parent, _credentials(parent))
    seen = []

    def build(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(tool_progress_callback=None)

    with patch("tools.delegate_tool._build_child_preserving_parent_tools", build):
        children, error = _build_children(
            tasks, [None, None], _credentials(parent), configs,
            top_role="leaf", max_iterations=7, parent_agent=parent,
            routing_cfg={}, live_deleg_id=None, live_writers=[],
        )
    assert error is None
    assert len(children) == 2
    assert seen[0]["goal"] == "Inspect candidate one"
    assert seen[0]["toolsets"] == ["file"]
    assert seen[0]["dispatch_config"] is configs[0]
    assert seen[1]["goal"] == "Inspect candidate two"
    assert seen[1]["toolsets"] == ["web"]
    assert seen[1]["dispatch_config"] is configs[1]


def test_real_child_builder_applies_runtime_overrides_and_attests_constructed_child(monkeypatch):
    parent = _parent(
        _subagent_id=None,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
    )
    config = build_dispatch_configs(
        [{"goal": "Review candidate", "model": "test-model", "reasoning": "high", "allowed_toolsets": ["file"]}],
        parent,
        _credentials(parent),
    )[0]
    child = _child()
    constructed = {}

    def agent_factory(**kwargs):
        constructed.update(kwargs)
        child.model = kwargs["model"]
        child.reasoning_config = kwargs["reasoning_config"]
        return child

    monkeypatch.setattr("run_agent.AIAgent", agent_factory)
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: {})
    monkeypatch.setattr("tools.delegate_tool._open_child_session_db", lambda _parent: None)
    monkeypatch.setattr("tools.delegate_tool._resolve_child_credential_pool", lambda *_args: None)
    monkeypatch.setattr("tools.delegate_tool._safe_progress", lambda *_args, **_kwargs: None)

    built = _build_child_agent(
        task_index=0,
        goal="Review candidate",
        context=None,
        toolsets=["file"],
        model="test-model",
        max_iterations=7,
        task_count=1,
        parent_agent=parent,
        dispatch_config=config,
    )

    assert constructed["model"] == "test-model"
    assert constructed["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert constructed["enabled_toolsets"] == ["file"]
    assert built.valid_tool_names == {"read_file", "search_files"}
    assert built._delegate_attestation["task_fingerprint"] == config.fingerprint
    assert built._delegate_attestation["subagent_id"] == built._subagent_id


def test_file_only_child_is_read_only_and_cannot_dispatch_terminal_or_web():
    parent = _parent()
    config = build_dispatch_configs(
        [{"goal": "Review candidate", "reasoning": "high", "allowed_toolsets": ["file"]}],
        parent,
        _credentials(parent),
    )[0]
    child = _child()
    attestation = apply_and_attest_child(child, parent, config, effective_model="test-model")

    assert child.valid_tool_names == {"read_file", "search_files"}
    assert {item["function"]["name"] for item in child.tools} == {"read_file", "search_files"}
    assert child._skip_mcp_refresh is True
    assert attestation["allowed_toolsets"] == ("file",)
    assert attestation["allowed_tools"] == ("read_file", "search_files")

    for name in ("write_file", "patch", "terminal", "web_search", "tool_call"):
        call = SimpleNamespace(function=SimpleNamespace(name=name, arguments="{}"), id=f"call-{name}")
        parsed = _parse_tool_call(child, call)
        assert "outside this delegated child's allowed_toolsets" in parsed.scope_block


@pytest.mark.parametrize("extra_tool", ["terminal", "web_search"])
def test_file_only_child_with_extra_capability_fails_closed(extra_tool):
    parent = _parent()
    config = build_dispatch_configs(
        [{"goal": "Review candidate", "reasoning": "high", "allowed_toolsets": ["file"]}],
        parent,
        _credentials(parent),
    )[0]
    child = _child(
        tools=_child().tools + [{"type": "function", "function": {"name": extra_tool}}],
        valid_tool_names={"read_file", "search_files", extra_tool},
    )
    with pytest.raises(ValueError, match="outside the request"):
        apply_and_attest_child(child, parent, config, effective_model="test-model")


@pytest.mark.parametrize(
    ("task", "parent", "message"),
    [
        ({"goal": "x", "model": ""}, _parent(), "model must be"),
        ({"goal": "x", "reasoning": "extreme"}, _parent(), "reasoning must be"),
        ({"goal": "x", "allowed_toolsets": []}, _parent(), "non-empty array"),
        ({"goal": "x", "allowed_toolsets": ["missing"]}, _parent(), "Unknown allowed_toolsets"),
        (
            {"goal": "x", "allowed_toolsets": ["web"]},
            _parent(enabled_toolsets=["file"], valid_tool_names={"read_file", "search_files"}),
            "broaden parent permissions",
        ),
    ],
)
def test_invalid_dispatch_requests_fail_closed(task, parent, message):
    with pytest.raises(ValueError, match=message):
        build_dispatch_configs([task], parent, _credentials(parent))


def test_unsupported_model_fails_closed_without_constructing_a_child():
    parent = _parent()
    with patch(
        "hermes_cli.models_validate.validate_requested_model",
        return_value={"accepted": True, "recognized": False, "message": "not cataloged"},
    ):
        with pytest.raises(ValueError, match="Unsupported delegation model"):
            build_dispatch_configs(
                [{"goal": "Inspect candidate", "model": "invented-model"}],
                parent,
                _credentials(parent),
            )


@pytest.mark.parametrize(
    ("child", "message"),
    [
        (_child(model="different-model"), "model mismatch"),
        (_child(reasoning_config={"enabled": True, "effort": "low"}), "reasoning does not match"),
        (_child(_subagent_id="parent-agent"), "distinct subagent identity"),
        (_child(session_id="parent-session"), "distinct session identity"),
    ],
)
def test_child_identity_and_runtime_mismatches_fail_closed(child, message):
    parent = _parent(_subagent_id="parent-agent")
    config = build_dispatch_configs(
        [{"goal": "Review candidate", "model": "test-model", "reasoning": "high"}],
        parent,
        _credentials(parent),
    )[0]
    with pytest.raises(ValueError, match=message):
        apply_and_attest_child(child, parent, config, effective_model="test-model")


def test_attestation_binds_exact_task_and_candidate_and_children_are_distinct():
    parent = _parent()
    tasks = [
        {"goal": "Review alpha", "model": "test-model", "allowed_toolsets": ["file"]},
        {"goal": "Review beta", "model": "test-model", "allowed_toolsets": ["file"]},
    ]
    first, second = build_dispatch_configs(tasks, parent, _credentials(parent))
    child_one = _child(_subagent_id="sa-one", session_id="session-one")
    child_two = _child(_subagent_id="sa-two", session_id="session-two")
    attest_one = apply_and_attest_child(child_one, parent, first, effective_model="test-model")
    attest_two = apply_and_attest_child(child_two, parent, second, effective_model="test-model")

    assert attest_one["subagent_id"] != attest_two["subagent_id"]
    assert attest_one["session_id"] != attest_two["session_id"]
    assert attest_one["task_index"] == 0
    assert attest_two["task_index"] == 1
    assert attest_one["task_fingerprint"] != attest_two["task_fingerprint"]

    changed_candidate = build_dispatch_configs(
        [{"goal": "Review alpha", "model": "test-model", "reasoning": "low", "allowed_toolsets": ["file"]}],
        parent,
        _credentials(parent),
    )[0]
    assert changed_candidate.fingerprint != first.fingerprint
