"""Read-only operator quota service.

This is the only boundary between credentials, provider quota requests, and the
operator HUD.  It deliberately rehydrates rows instead of loading a
CredentialPool, and reads Claude Code logins straight from their own stores:
inspection must never select, refresh, exhaust, or persist credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent import account_usage, anthropic_credentials
from agent.account_usage import AccountUsageSnapshot
from hermes_cli.auth import read_credential_pool

_pool_reader = read_credential_pool
# Claude Code's own logins, by synthetic id → config dir name under $HOME: the default login
# (unsuffixed Keychain item + ~/.claude) and the isolated CLAUDE_CONFIG_DIR=~/.claude-account2 one.
_CLAUDE_CODE_DIRS = {"claude-default": ".claude", "claude-account2": ".claude-account2"}


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
    """Discover quota identities without reading secrets or invoking pool mutation."""
    if provider == "claude-code":
        # A login whose config dir is missing is omitted; only is_dir() runs here, never a credential read.
        return tuple(OperatorQuotaAccount(provider, credential_id, f"Claude{index}", index - 1)
                     for index, (credential_id, dirname) in enumerate(_CLAUDE_CODE_DIRS.items(), 1)
                     if (Path.home() / dirname).is_dir())
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


def _claude_code_token(credential_id: str) -> str:
    """Access token of exactly this Claude Code login; never refreshed, never another login's."""
    from agent.credential_sources import adopt_external_logins_enabled
    if credential_id not in _CLAUDE_CODE_DIRS or not adopt_external_logins_enabled():
        raise OperatorQuotaUnavailable("unavailable")
    config_dir = Path.home() / _CLAUDE_CODE_DIRS[credential_id]
    if credential_id == "claude-default":
        # Pinned to ~/.claude: read_claude_code_credentials() follows CLAUDE_CONFIG_DIR, which may
        # point at the other login.  Same pick as there: a valid token first, then the later expiry.
        data = anthropic_credentials._load_json_if_exists(config_dir / ".credentials.json", ".credentials.json")
        found = (anthropic_credentials._read_claude_code_credentials_from_keychain(),
                 anthropic_credentials._claude_oauth_record(data, "claude_code_credentials_file")
                 if isinstance(data, dict) else None)
        creds = max((c for c in found if c), default=None, key=lambda c: (
            anthropic_credentials.is_claude_code_token_valid(c), c.get("expiresAt", 0) or 0))
    else:
        creds = anthropic_credentials.read_claude_code_credentials_for_config_dir(config_dir)
    if not creds:
        raise OperatorQuotaUnavailable("unavailable")
    if not anthropic_credentials.is_claude_code_token_valid(creds):
        raise OperatorQuotaUnavailable("expired")
    return creds["accessToken"]


def fetch_exact(account: OperatorQuotaAccount) -> AccountUsageSnapshot:
    """Fetch exactly this account; no ambient resolver or refresh path."""
    if account.provider == "claude-code":
        base_url, token = None, _claude_code_token(account.credential_id)
    else:
        row = _exact_row(account)
        token = getattr(row, "runtime_api_key", "") or ""
        if _canonical_status(row) == "exp":
            raise OperatorQuotaUnavailable("expired")
        if not token:
            raise OperatorQuotaUnavailable("unavailable")
        base_url = getattr(row, "runtime_base_url", None)
    if account.provider in {"claude-code", "anthropic"}:
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
    # Claude slots come from the Claude Code logins themselves; the anthropic pool usually holds
    # copies of those same logins, so listing it too would show (and poll) one account twice.
    return tuple((a.slot_name, a.provider, a.credential_id)
                 for provider in ("claude-code", "openai-codex")
                 for a in discover_accounts(provider))


def active_identity(provider: Optional[str], credential_id: Optional[str]) -> Optional[tuple[str, str]]:
    """Return an active identity only when both provider and exact id are known."""
    if not isinstance(provider, str) or not isinstance(credential_id, str) or not credential_id:
        return None
    if any(a.provider == provider and a.credential_id == credential_id
           for a in discover_accounts(provider)):
        return provider, credential_id
    return None
