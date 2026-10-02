"""Operator HUD quota: compact remaining-% for the Claude Code accounts and Codex.

Values are cached and refreshed on a single daemon thread, so a render never blocks on a provider.
Failures keep the last good snapshot (shown stale) or show unknown; reasons are fixed short strings,
never exception text, so no credential can leak into the HUD or ``/usage``.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from agent.account_usage import AccountUsageSnapshot, remaining_percent
from agent.operator_quota import (
    OperatorQuotaUnavailable as _QuotaUnavailable,
    fetch_quota as _fetch_exact_quota,
    quota_accounts,
    set_pool_reader,
)

logger = logging.getLogger(__name__)

# Compatibility patch point for older integrations.  The service remains the owner of
# discovery; this is only a replaceable read function, never a mutable pool object.
# It is intentionally None by default so HUD does not import or read the auth store.
read_credential_pool = None


def fetch_quota(provider: str, account_id: str):
    """Fetch one exact persisted account; provider-wide resolution is forbidden here."""
    if not isinstance(account_id, str) or not account_id.strip():
        raise _QuotaUnavailable("unavailable")
    return _fetch_exact_quota(provider, account_id)


def _codex_quota_accounts():
    set_pool_reader(read_credential_pool)
    return tuple(account for account in quota_accounts() if account[1] == "openai-codex")


def account_visible(provider: str, account_id: Optional[str]) -> bool:
    if provider != "claude-code" or account_id is None:
        return True
    from pathlib import Path
    config_dirs = {"claude-default": ".claude", "claude-account2": ".claude-account2"}
    dirname = config_dirs.get(account_id)
    if dirname is not None:
        return (Path.home() / dirname).is_dir()
    return Path(account_id).expanduser().is_dir()

# Kept as a compatibility sentinel for callers that used the old default.  Actual accounts come
# from agent.operator_quota.quota_accounts(): Claude1/Claude2 (Claude Code logins) and CX1..n.
QUOTA_ACCOUNTS: tuple[tuple[str, str, Optional[str]], ...] = ()
TTL_SECONDS = 60.0
_SESSION_LABELS = ("Current session", "Session")
_WEEK_LABELS = ("Current week", "Weekly")


@dataclass(frozen=True)
class QuotaEntry:
    label: str
    snapshot: Optional[AccountUsageSnapshot] = None  # last GOOD snapshot
    reason: str = ""  # set when the latest check failed
    checked_at: float = 0.0
    provider: Optional[str] = None
    # Identity of the account this row fetches: the persisted credential id.
    account_id: Optional[str] = None

    @property
    def stale(self) -> bool:
        return bool(self.reason) and self.snapshot is not None


def _window_remaining(snapshot: AccountUsageSnapshot, labels: tuple[str, ...]) -> Optional[int]:
    for window in snapshot.windows:
        if window.label in labels and window.used_percent is not None:
            return remaining_percent(window.used_percent)
    return None


def _window_reset(snapshot: AccountUsageSnapshot, labels: tuple[str, ...]) -> Optional[str]:
    for window in snapshot.windows:
        if window.label in labels and window.reset_at is not None:
            seconds = max(0, int((window.reset_at - datetime.now(timezone.utc)).total_seconds()))
            days, remainder = divmod(seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes = remainder // 60
            if days:
                return f"{days}d{hours}h"
            return f"{hours}:{minutes:02d}"
    return None


def compact_quota(entry: QuotaEntry, *, active_id: Optional[tuple[str, str]] = None) -> tuple[str, Optional[int]]:
    """(``Claude1 5h 72% ↻2:10 W 61%``, lowest remaining %) — the % is None when unknown or stale.

    Only the windows the provider actually supplied are rendered, each carrying its own reset
    countdown, so a Codex account that reports one window does not print a phantom second one.
    ``active_id`` is always the provider-qualified identity exposed by the live session."""
    active = (isinstance(active_id, tuple) and len(active_id) == 2 and
              isinstance(entry.provider, str) and bool(entry.provider) and
              isinstance(entry.account_id, str) and bool(entry.account_id) and
              active_id == (entry.provider, entry.account_id))
    label = entry.label + ("*" if active else "")
    if entry.reason == "expired":
        return f"{label} exp", None
    if entry.reason == "refresh":
        return f"{label} refresh", None
    if entry.snapshot is None:
        return f"{label} ?", None
    mark = "~" if entry.stale else ""
    parts, known = [], []
    for name, labels in (("5h", _SESSION_LABELS), ("W", _WEEK_LABELS)):
        value = _window_remaining(entry.snapshot, labels)
        if value is None:
            continue
        known.append(value)
        reset = _window_reset(entry.snapshot, labels)
        parts.append(f"{name} {mark}{value}%" + (f" ↻{reset}" if reset else ""))
    if not parts:
        return f"{label} ?", None
    return f"{label} {' '.join(parts)}", (min(known) if not entry.stale else None)


class QuotaHud:
    """Per-account cache; ``entries()`` is render-safe and kicks a background refresh when due."""

    def __init__(self, accounts=None, *, ttl: float = TTL_SECONDS,
                 fetch: Callable[[str, str], AccountUsageSnapshot] = fetch_quota,
                 on_update: Optional[Callable[[], None]] = None):
        if accounts is None or accounts == QUOTA_ACCOUNTS:
            set_pool_reader(read_credential_pool)
            accounts = quota_accounts()  # discovery already omits missing Claude config dirs
        else:
            accounts = tuple(a for a in accounts if account_visible(a[1], a[2]))
        self._accounts = tuple(accounts)
        self._ttl, self._fetch, self._on_update = ttl, fetch, on_update
        self._entries = {(provider, account_id): QuotaEntry(label, provider=provider, account_id=account_id)
                         for label, provider, account_id in self._accounts}
        self._thread: Optional[threading.Thread] = None

    def entries(self) -> list[QuotaEntry]:
        self.refresh_if_due()
        return [self._entries[(provider, account_id)] for label, provider, account_id in self._accounts]

    def refresh_if_due(self) -> None:
        now = time.time()
        if not any(now - e.checked_at >= self._ttl for e in self._entries.values()):
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.refresh_now, name="quota-hud", daemon=True)
        self._thread.start()

    def refresh_now(self) -> None:
        for label, provider, config_dir in self._accounts:
            identity = (provider, config_dir)
            old = self._entries[identity]
            if time.time() - old.checked_at < self._ttl:
                continue
            try:
                new = QuotaEntry(label, self._fetch(provider, config_dir), "", time.time(), provider, old.account_id)
            except _QuotaUnavailable as exc:
                new = QuotaEntry(label, old.snapshot, str(exc), time.time(), provider, old.account_id)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.debug("quota hud ▸ %s fetch failed (%s)", label, type(exc).__name__)
                new = QuotaEntry(label, old.snapshot, f"HTTP {status}" if status else "error", time.time(), provider, old.account_id)
            self._entries[identity] = new
        if self._on_update is not None:
            try:
                self._on_update()
            except Exception:
                pass

    def usage_lines(self) -> list[str]:
        """Expanded view for ``/usage``: per-account windows with reset times (cached, no network)."""
        from agent.account_usage import render_account_usage_lines
        lines: list[str] = []
        for entry in (self._entries[(provider, account_id)] for label, provider, account_id in self._accounts):
            if entry.reason == "expired":
                lines.append(f"📈 {entry.label} limits")
                lines.append(f"Unknown: {entry.reason}")
            elif entry.snapshot is not None:
                lines.extend(render_account_usage_lines(dataclasses.replace(entry.snapshot, title=f"{entry.label} limits")))
                if entry.reason:
                    lines.append(f"Stale: {entry.reason}")
            else:
                lines.append(f"📈 {entry.label} limits")
                if entry.reason:
                    lines.append(f"Unknown: {entry.reason}")
        return lines
