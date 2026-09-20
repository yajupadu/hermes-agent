"""Fail-closed per-task routing and capability contracts for delegated children."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional


_REASONING_VALUES = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_READ_ONLY_FILE_TOOLS = frozenset({"read_file", "search_files"})


@dataclass(frozen=True)
class DelegationDispatchConfig:
    """Normalized authority for exactly one task/candidate pairing."""

    task_index: int
    goal: str
    context: Optional[str]
    model: Optional[str]
    reasoning: Optional[str]
    reasoning_config: Optional[Mapping[str, Any]]
    allowed_toolsets: Optional[tuple[str, ...]]
    allowed_tools: Optional[frozenset[str]]
    fingerprint: str

    @property
    def restricted(self) -> bool:
        return self.model is not None or self.reasoning is not None or self.allowed_toolsets is not None


def normalize_reasoning(value: Any) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Canonical public reasoning value and corresponding ``AIAgent`` config."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise ValueError("reasoning must be one of: none, minimal, low, medium, high, xhigh, max, ultra.")
    normalized = value.strip().lower()
    if normalized not in _REASONING_VALUES:
        raise ValueError("reasoning must be one of: none, minimal, low, medium, high, xhigh, max, ultra.")
    from hermes_constants import parse_reasoning_effort

    parsed = parse_reasoning_effort(normalized)
    if parsed is None:
        raise ValueError(f"reasoning {value!r} is not supported.")
    return normalized, dict(parsed)


def normalize_allowed_toolsets(value: Any) -> Optional[tuple[str, ...]]:
    """Validate the public narrowing list without silently dropping entries."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("allowed_toolsets must be a non-empty array of toolset names.")
    if any(not isinstance(name, str) or not name.strip() for name in value):
        raise ValueError("allowed_toolsets must contain only non-empty toolset names.")
    names = tuple(dict.fromkeys(name.strip() for name in value))
    from toolsets import validate_toolset

    unknown = [name for name in names if not validate_toolset(name)]
    if unknown:
        raise ValueError(f"Unknown allowed_toolsets: {', '.join(sorted(unknown))}.")
    return names


def _tools_for_allowed_toolsets(names: tuple[str, ...]) -> frozenset[str]:
    from toolsets import resolve_toolset

    tools: set[str] = set()
    for name in names:
        resolved = set(resolve_toolset(name))
        # Explicit file scope is the inspection-only surface. Legacy inherited
        # file scope keeps write_file/patch unchanged.
        if name == "file":
            resolved &= _READ_ONLY_FILE_TOOLS
        tools.update(resolved)
    return frozenset(tools)


def _parent_tool_names(parent: Any) -> Optional[set[str]]:
    valid = getattr(parent, "valid_tool_names", None)
    if isinstance(valid, (set, frozenset, list, tuple)):
        return {str(name) for name in valid}
    enabled = getattr(parent, "enabled_toolsets", None)
    if enabled is None:
        return None
    if not isinstance(enabled, (list, tuple, set, frozenset)):
        return set()
    from toolsets import resolve_toolset

    tools = {tool for name in enabled for tool in resolve_toolset(str(name))}
    disabled = getattr(parent, "disabled_toolsets", None)
    if isinstance(disabled, (list, tuple, set, frozenset)):
        tools.difference_update(tool for name in disabled for tool in resolve_toolset(str(name)))
    return tools


def _validate_model(model: str, parent: Any, credentials: Mapping[str, Any]) -> None:
    inherited = str(credentials.get("model") or getattr(parent, "model", "") or "").strip()
    if model == inherited:
        return
    from hermes_cli.models_validate import validate_requested_model

    verdict = validate_requested_model(
        model,
        credentials.get("provider") or getattr(parent, "provider", None),
        api_key=credentials.get("api_key") or getattr(parent, "api_key", None),
        base_url=credentials.get("base_url") or getattr(parent, "base_url", None),
        api_mode=credentials.get("api_mode") or getattr(parent, "api_mode", None),
    )
    if not verdict.get("accepted") or not verdict.get("recognized") or verdict.get("corrected_model"):
        detail = str(verdict.get("message") or "model is not in the active provider catalog")
        raise ValueError(f"Unsupported delegation model {model!r}: {detail}")


def build_dispatch_configs(
    tasks: list[dict[str, Any]], parent: Any, credentials: Mapping[str, Any]
) -> list[DelegationDispatchConfig]:
    """Validate and freeze each normalized task's candidate and authority."""
    parent_tools = _parent_tool_names(parent)
    configs: list[DelegationDispatchConfig] = []
    validated_models: set[str] = set()
    for index, task in enumerate(tasks):
        raw_model = task.get("model")
        if raw_model is not None and (not isinstance(raw_model, str) or not raw_model.strip()):
            raise ValueError("model must be a non-empty string when provided.")
        model = raw_model.strip() if isinstance(raw_model, str) else None
        if model and model not in validated_models:
            _validate_model(model, parent, credentials)
            validated_models.add(model)
        reasoning, reasoning_config = normalize_reasoning(task.get("reasoning"))
        allowed_toolsets = normalize_allowed_toolsets(task.get("allowed_toolsets"))
        allowed_tools = _tools_for_allowed_toolsets(allowed_toolsets) if allowed_toolsets is not None else None
        if allowed_tools is not None and parent_tools is not None:
            broadened = allowed_tools - parent_tools
            if broadened:
                raise ValueError(
                    "allowed_toolsets would broaden parent permissions with tools: "
                    + ", ".join(sorted(broadened))
                    + "."
                )
        bound = {
            "task_index": index,
            "goal": task["goal"],
            "context": task.get("context"),
            "model": model,
            "reasoning": reasoning,
            "allowed_toolsets": allowed_toolsets,
        }
        fingerprint = hashlib.sha256(
            json.dumps(bound, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        configs.append(
            DelegationDispatchConfig(
                index, task["goal"], task.get("context"), model, reasoning,
                MappingProxyType(reasoning_config) if reasoning_config is not None else None,
                allowed_toolsets, allowed_tools, fingerprint,
            )
        )
    return configs


def apply_and_attest_child(
    child: Any, parent: Any, config: DelegationDispatchConfig, *, effective_model: Optional[str]
) -> Mapping[str, Any]:
    """Apply the frozen capability ceiling and attest the actual child."""
    if not config.restricted:
        return MappingProxyType({})

    subagent_id = str(getattr(child, "_subagent_id", "") or "")
    parent_subagent_id = str(getattr(parent, "_subagent_id", "") or "")
    if not subagent_id or (parent_subagent_id and subagent_id == parent_subagent_id):
        raise ValueError("Delegated child did not receive a distinct subagent identity.")
    parent_session_id = str(getattr(parent, "session_id", "") or "")
    child_session_id = str(getattr(child, "session_id", "") or "")
    if parent_session_id and (not child_session_id or child_session_id == parent_session_id):
        raise ValueError("Delegated child did not receive a distinct session identity.")

    expected_model = str(config.model or effective_model or getattr(parent, "model", "") or "")
    actual_model = str(getattr(child, "model", "") or "")
    if expected_model and actual_model != expected_model:
        raise ValueError(
            f"Delegated child model mismatch: requested {expected_model!r}, constructed {actual_model!r}."
        )
    if config.reasoning_config is not None:
        actual_reasoning = getattr(child, "reasoning_config", None)
        if actual_reasoning != dict(config.reasoning_config):
            raise ValueError("Delegated child reasoning does not match the requested reasoning policy.")

    if config.allowed_tools is not None:
        allowed = set(config.allowed_tools)
        actual_names: set[str] = {str(name) for name in (getattr(child, "valid_tool_names", set()) or ())}
        raw_tools = getattr(child, "tools", None)
        if isinstance(raw_tools, list):
            for schema in raw_tools:
                if isinstance(schema, dict):
                    name = (schema.get("function") or {}).get("name")
                    if isinstance(name, str):
                        actual_names.add(name)
        actual_names.discard(None)
        outside = actual_names - allowed
        if outside:
            raise ValueError(
                "Delegated child enabled capabilities outside the request: " + ", ".join(sorted(outside)) + "."
            )
        tools = getattr(child, "tools", None)
        if isinstance(tools, list):
            child.tools = [
                schema for schema in tools
                if isinstance(schema, dict) and (schema.get("function") or {}).get("name") in allowed
            ]
        child.valid_tool_names = {
            name for name in getattr(child, "valid_tool_names", set()) if name in allowed
        }
        child._delegate_allowed_tools = frozenset(allowed)
        child._skip_mcp_refresh = True

    attestation = MappingProxyType({
        "task_index": config.task_index,
        "task_fingerprint": config.fingerprint,
        "subagent_id": subagent_id,
        "session_id": child_session_id,
        "provider": str(getattr(child, "provider", "") or "") or None,
        "model": actual_model or None,
        "reasoning": config.reasoning,
        "allowed_toolsets": config.allowed_toolsets,
        "allowed_tools": tuple(sorted(config.allowed_tools)) if config.allowed_tools is not None else None,
    })
    child._delegate_attestation = attestation
    return attestation
