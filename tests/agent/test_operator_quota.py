import dataclasses
from types import SimpleNamespace

import pytest

from agent import operator_quota


def _row(identifier, priority=0, *, label="same", token="token"):
    return {
        "id": identifier, "priority": priority, "label": label,
        "access_token": token, "auth_type": "api_key", "source": "manual",
    }


def test_discovery_is_read_only_sanitized_and_deterministic(monkeypatch):
    calls = []
    rows = [_row("z", 1), _row("b", 0, label="PII alice@example.com"),
            _row("a", 0, label="same")]
    monkeypatch.setattr(operator_quota, "_pool_reader",
                        lambda provider: calls.append(provider) or rows)
    accounts = operator_quota.discover_accounts("anthropic")
    assert [(a.slot_name, a.credential_id) for a in accounts] == [("C1", "a"), ("C2", "b"), ("C3", "z")]
    assert all("alice" not in repr(a) and "same" not in repr(a) for a in accounts)
    assert calls == ["anthropic"]


def test_dead_rows_are_expired_and_not_queried(monkeypatch):
    row = _row("dead", token="retained")
    row["last_status"] = "dead"
    monkeypatch.setattr(operator_quota, "_pool_reader", lambda provider: [row])
    account = operator_quota.discover_accounts("anthropic")[0]
    assert account.canonical_status == "exp"
    def fail(*args, **kwargs):
        raise AssertionError("dead account queried")
    monkeypatch.setattr(operator_quota.account_usage, "_fetch_anthropic_account_usage", fail)
    try:
        operator_quota.fetch_exact(account)
    except operator_quota.OperatorQuotaUnavailable as exc:
        assert str(exc) == "expired"
    else:
        raise AssertionError("expired account was queried")


def test_active_identity_requires_provider_and_exact_id(monkeypatch):
    monkeypatch.setattr(operator_quota, "_pool_reader",
                        lambda provider: [_row("one")] if provider == "anthropic" else [])
    assert operator_quota.active_identity("anthropic", "one") == ("anthropic", "one")
    assert operator_quota.active_identity("openai-codex", "one") is None
    assert operator_quota.active_identity("anthropic", None) is None


def test_account_object_contains_only_safe_metadata(monkeypatch):
    row = _row("safe", label="John Smith <john@example.com>", token="super-secret-token-123")
    monkeypatch.setattr(operator_quota, "_pool_reader", lambda provider: [row])
    account = operator_quota.discover_accounts("anthropic")[0]
    serialized = dataclasses.asdict(account)
    assert "super-secret-token-123" not in repr(account)
    assert "super-secret-token-123" not in serialized
    assert "John Smith <john@example.com>" not in serialized
    assert not {"token", "_token", "access_token", "refresh_token", "api_key"} & serialized.keys()
    account_dict = getattr(account, "__dict__", {})
    assert "super-secret-token-123" not in repr(account_dict)
    assert not {"token", "_token", "access_token", "refresh_token", "api_key"} & account_dict.keys()


def test_fetch_exact_reloads_exact_private_row(monkeypatch):
    rows = [_row("a1", label="same", token="token-a1"), _row("a2", label="same", token="token-a2")]
    monkeypatch.setattr(operator_quota, "_pool_reader", lambda provider: rows)
    accounts = operator_quota.discover_accounts("anthropic")
    calls = []
    result = SimpleNamespace(windows=[object()], unavailable_reason=None)
    def fetch(base_url, token):
        calls.append((base_url, token))
        return result
    monkeypatch.setattr(operator_quota.account_usage, "_fetch_anthropic_account_usage", fetch)
    assert operator_quota.fetch_exact(accounts[1]) is result
    assert calls == [(None, "token-a2")]


def test_fetch_exact_fails_closed_when_row_disappears(monkeypatch):
    row = _row("gone", token="token-gone")
    monkeypatch.setattr(operator_quota, "_pool_reader", lambda provider: [row])
    account = operator_quota.discover_accounts("anthropic")[0]
    monkeypatch.setattr(operator_quota, "_pool_reader", lambda provider: [])
    with pytest.raises(operator_quota.OperatorQuotaUnavailable):
        operator_quota.fetch_exact(account)


def test_claude_code_accounts_are_independent_and_read_only(monkeypatch, tmp_path):
    account2 = tmp_path / ".claude-account2"
    account2.mkdir()
    monkeypatch.setattr(operator_quota.Path, "home", lambda: tmp_path)
    credentials = {
        operator_quota._CLAUDE_DEFAULT_ID: {"accessToken": "c1", "expiresAt": 0},
        operator_quota._CLAUDE_ACCOUNT2_ID: {"accessToken": "c2", "expiresAt": 0},
    }
    monkeypatch.setattr(
        operator_quota.anthropic_credentials,
        "read_claude_code_credentials",
        lambda: credentials[operator_quota._CLAUDE_DEFAULT_ID],
    )
    monkeypatch.setattr(
        operator_quota.anthropic_credentials,
        "read_claude_code_credentials_for_config_dir",
        lambda path: credentials[operator_quota._CLAUDE_ACCOUNT2_ID],
    )
    monkeypatch.setattr(
        operator_quota.anthropic_credentials,
        "is_claude_code_token_valid",
        lambda value: True,
    )
    calls = []
    result = SimpleNamespace(windows=[object()], unavailable_reason=None)
    monkeypatch.setattr(
        operator_quota.account_usage,
        "_fetch_anthropic_account_usage",
        lambda base_url, token: calls.append(token) or result,
    )

    accounts = operator_quota.discover_accounts("claude-code")
    assert [(a.slot_name, a.credential_id) for a in accounts] == [
        ("C1", operator_quota._CLAUDE_DEFAULT_ID),
        ("C2", operator_quota._CLAUDE_ACCOUNT2_ID),
    ]
    assert operator_quota.fetch_exact(accounts[0]) is result
    assert operator_quota.fetch_exact(accounts[1]) is result
    assert calls == ["c1", "c2"]
