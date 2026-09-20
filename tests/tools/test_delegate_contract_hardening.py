from types import SimpleNamespace

import pytest

from tools.delegate_tool_contract import (
    _validate_model,
    apply_and_attest_child,
    build_dispatch_configs,
)


def _parent(**overrides):
    values = {
        "model": "parent-model",
        "provider": "anthropic",
        "valid_tool_names": {"read_file", "search_files"},
        "_subagent_id": "parent-agent",
        "session_id": "parent-session",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_exact_model_reasoning_and_toolset_are_frozen_in_dispatch():
    config = build_dispatch_configs(
        [{
            "goal": "bounded review",
            "model": "parent-model",
            "reasoning": "low",
            "allowed_toolsets": ["file"],
        }],
        _parent(),
        {"model": "parent-model", "provider": "anthropic"},
    )[0]

    assert config.model == "parent-model"
    assert config.reasoning == "low"
    assert config.allowed_toolsets == ("file",)
    assert config.allowed_tools == frozenset({"read_file", "search_files"})


@pytest.mark.parametrize("reasoning", ["invalid", "HIGHER", 3, []])
def test_invalid_reasoning_fails_closed(reasoning):
    with pytest.raises(ValueError):
        build_dispatch_configs(
            [{"goal": "x", "reasoning": reasoning}],
            _parent(),
            {"model": "parent-model", "provider": "anthropic"},
        )


def test_provider_model_mismatch_fails_before_dispatch():
    with pytest.raises(ValueError, match="Unsupported delegation model"):
        _validate_model(
            "gpt-5.6-sol",
            _parent(model="parent-model", provider="anthropic"),
            {"provider": "anthropic"},
        )


def test_attestation_contains_provider_and_exact_capability_ceiling():
    config = build_dispatch_configs(
        [{"goal": "x", "model": "parent-model", "reasoning": "medium", "allowed_toolsets": ["file"]}],
        _parent(),
        {"model": "parent-model", "provider": "anthropic"},
    )[0]
    child = SimpleNamespace(
        _subagent_id="child-agent",
        session_id="child-session",
        provider="anthropic",
        model="parent-model",
        reasoning_config=dict(config.reasoning_config or {}),
        valid_tool_names={"read_file", "search_files"},
        tools=[],
    )

    attestation = apply_and_attest_child(child, _parent(), config, effective_model="parent-model")

    assert attestation["provider"] == "anthropic"
    assert attestation["model"] == "parent-model"
    assert attestation["reasoning"] == "medium"
    assert attestation["allowed_toolsets"] == ("file",)
    assert "terminal" not in attestation["allowed_tools"]


def test_missing_child_identity_fails_closed():
    config = build_dispatch_configs(
        [{"goal": "x", "model": "parent-model", "allowed_toolsets": ["file"]}],
        _parent(),
        {"model": "parent-model", "provider": "anthropic"},
    )[0]
    child = SimpleNamespace(model="parent-model", session_id="child-session", provider="anthropic")

    with pytest.raises(ValueError, match="distinct subagent identity"):
        apply_and_attest_child(child, _parent(), config, effective_model="parent-model")


def test_implementation_and_independent_review_are_distinct_dispatches(monkeypatch):
    monkeypatch.setattr("tools.delegate_tool_contract._validate_model", lambda *args: None)

    configs = build_dispatch_configs(
        [
            {"goal": "implement", "model": "claude-sonnet-5", "reasoning": "medium"},
            {"goal": "independent review", "model": "gpt-5.6-sol", "reasoning": "high"},
        ],
        _parent(),
        {"provider": "anthropic"},
    )

    assert configs[0].model != configs[1].model
    assert configs[0].fingerprint != configs[1].fingerprint
    assert configs[1].reasoning == "high"