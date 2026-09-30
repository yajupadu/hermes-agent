"""Read-only operator quota service.

This is the only boundary between persisted Hermes credential rows, provider quota
requests, and the operator HUD.  It deliberately rehydrates rows instead of
loading a CredentialPool: inspection must never select, refresh, exhaust, or
persist credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent import account_usage
from agent import anthropic_credentials
from agent.account_usage import AccountUsageSnapshot
from hermes_cli.auth import read_credential_pool

_pool_reader = read_credential_pool
_CLAUDE_DEFAULT_ID = "claude-default"
_CLAUDE_ACCOUNT2_ID = "claude-account2"


def set_pool_reader(reader) -> None:
    """Test seam for the persisted read boundary; never a mutation seam."""
    global _pool_reader
    _pool_reader = reader or read_credential_pool


@dataclass(frozen=True)
class OperatorQuotaAccount:
    """Sanitized identity used by the operator surface."""

    provider: str
    credential_id: str
    slot_name: str
    priority: int
    canonical_status: Optional[str] = None


@dataclass(frozen=True)
class OperatorQuotaSnapshot:
    """Renderer-safe result for one account."""

    slot_name: str
    snapshot: Optional[AccountUsageSnapshot] = None
    reason: str = ""
    stale: bool = False
    active: bool = False
    provider: Optional[str] = None
    credential_id: Optional[str] = None


class OperatorQuotaUnavailable(Exception):
    """A non-sensitive, stable reason for an unavailable quota."""


def _rows(provider: str) -> list[object]:
    from agent.credential_pool import PooledCredential

    payload = _pool_reader(provider)
    if isinstance(payload, dict):
        rows = payload.get("entries", payload.get("credentials", []))
        if not isinstance(rows, list) and isinstance(payload.get(provider), dict):
            nested = payload[provider]
            rows = nested.get("entries", nested.get("credentials", []))
    else:
        rows = payload
    result = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            continue
        try:
            result.append(PooledCredential.from_dict(provider, row))
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda row: (getattr(row, "priority", 0), row.id))


def _canonical_status(row: object) -> Optional[str]:
    status = getattr(row, "last_status", None)
    return "exp" if isinstance(status, str) and status.lower() in {"dead", "expired", "exp"} else status


def discover_accounts(provider: str) -> tuple[OperatorQuotaAccount, ...]:
    """Discover read-only quota identities without invoking pool mutation."""
    if provider == "claude-code":
        accounts = [OperatorQuotaAccount(provider, _CLAUDE_DEFAULT_ID, "C1", 0)]
        if (Path.home() / ".claude-account2").is_dir():
            accounts.append(OperatorQuotaAccount(provider, _CLAUDE_ACCOUNT2_ID, "C2", 1))
        return tuple(accounts)
    prefix = "C" if provider == "anthropic" else "CX" if provider == "openai-codex" else None
    if prefix is None:
        return ()
    try:
        rows = _rows(provider)
    except Exception:
        return ()
    accounts = []
    for index, row in enumerate(rows, 1):
        token = getattr(row, "runtime_api_key", "") or ""
        status = _canonical_status(row)
        if not token and status != "exp":
            continue
        accounts.append(OperatorQuotaAccount(
            provider=provider,
            credential_id=row.id,
            slot_name=f"{prefix}{index}",
            priority=getattr(row, "priority", 0),
            canonical_status=status,
        ))
    return tuple(accounts)


def _account(provider: str, credential_id: str) -> OperatorQuotaAccount:
    for account in discover_accounts(provider):
        if account.credential_id == credential_id:
            return account
    raise OperatorQuotaUnavailable("unavailable")


def _exact_row(account: OperatorQuotaAccount) -> object:
    for row in _rows(account.provider):
        if getattr(row, "id", None) == account.credential_id:
            return row
    raise OperatorQuotaUnavailable("unavailable")


def _claude_credentials(account: OperatorQuotaAccount) -> dict:
    if account.credential_id == _CLAUDE_DEFAULT_ID:
        credentials = anthropic_credentials.read_claude_code_credentials()
    elif account.credential_id == _CLAUDE_ACCOUNT2_ID:
        credentials = anthropic_credentials.read_claude_code_credentials_for_config_dir(
            Path.home() / ".claude-account2"
        )
    else:
        credentials = None
    if not credentials or not anthropic_credentials.is_claude_code_token_valid(credentials):
        raise OperatorQuotaUnavailable("expired" if credentials else "unavailable")
    return credentials


def fetch_exact(account: OperatorQuotaAccount) -> AccountUsageSnapshot:
    """Fetch exactly this persisted account; no ambient resolver or refresh path."""
    if account.provider == "claude-code":
        credentials = _claude_credentials(account)
        result = account_usage._fetch_anthropic_account_usage(None, credentials["accessToken"])
        if result is None or result.unavailable_reason or not result.windows:
            raise OperatorQuotaUnavailable("unavailable")
        return result
    row = _exact_row(account)
    token = getattr(row, "runtime_api_key", "") or ""
    if _canonical_status(row) == "exp":
        raise OperatorQuotaUnavailable("expired")
    if not token:
        raise OperatorQuotaUnavailable("unavailable")
    base_url = getattr(row, "runtime_base_url", None)
    if account.provider == "anthropic":
        result = account_usage._fetch_anthropic_account_usage(
            base_url, token)
    elif account.provider == "openai-codex":
        result = account_usage._fetch_codex_account_usage(
            base_url, token, refresh_on_401=False)
    else:
        raise OperatorQuotaUnavailable("unavailable")
    if result is None or result.unavailable_reason or not result.windows:
        raise OperatorQuotaUnavailable("unavailable")
    return result


def fetch_quota(provider: str, credential_id: str) -> AccountUsageSnapshot:
    """Fetch one exact persisted account; ambient provider resolution is forbidden."""
    if not isinstance(credential_id, str) or not credential_id:
        raise OperatorQuotaUnavailable("unavailable")
    return fetch_exact(_account(provider, credential_id))


def account_visible(provider: str, config_dir: Optional[str]) -> bool:
    return provider in {"claude-code", "anthropic", "openai-codex"}


def quota_accounts() -> tuple[tuple[str, str, str], ...]:
    return tuple((a.slot_name, a.provider, a.credential_id)
                 for provider in ("claude-code", "anthropic", "openai-codex")
                 for a in discover_accounts(provider))


def active_identity(provider: Optional[str], credential_id: Optional[str]) -> Optional[tuple[str, str]]:
    """Return an active identity only when both provider and exact id are known."""
    if not isinstance(provider, str) or not isinstance(credential_id, str) or not credential_id:
        return None
    if any(a.provider == provider and a.credential_id == credential_id
           for a in discover_accounts(provider)):
        return provider, credential_id
    return None
