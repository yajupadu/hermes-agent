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
from pathlib import Path
from typing import Callable, Optional

from agent.account_usage import AccountUsageSnapshot, fetch_account_usage, fetch_claude_oauth_usage, remaining_percent

logger = logging.getLogger(__name__)

# (label, provider, Claude config dir). C1 is Claude Code's default login (no CLAUDE_CONFIG_DIR, ~/.claude);
# C2 is the isolated CLAUDE_CONFIG_DIR login. An account whose config dir does not exist is not shown.
# ponytail: fixed account list, move to display config when someone needs a different layout.
QUOTA_ACCOUNTS: tuple[tuple[str, str, Optional[str]], ...] = (
    ("C1", "claude-code", None),
    ("C2", "claude-code", "~/.claude-account2"),
    ("CX", "openai-codex", None),
)
TTL_SECONDS = 300.0
_SESSION_LABELS = ("Current session", "Session")
_WEEK_LABELS = ("Current week", "Weekly")


@dataclass(frozen=True)
class QuotaEntry:
    label: str
    snapshot: Optional[AccountUsageSnapshot] = None  # last GOOD snapshot
    reason: str = ""  # set when the latest check failed
    checked_at: float = 0.0

    @property
    def stale(self) -> bool:
        return bool(self.reason) and self.snapshot is not None


def _window_remaining(snapshot: AccountUsageSnapshot, labels: tuple[str, ...]) -> Optional[int]:
    for window in snapshot.windows:
        if window.label in labels and window.used_percent is not None:
            return remaining_percent(window.used_percent)
    return None


def compact_quota(entry: QuotaEntry) -> tuple[str, Optional[int]]:
    """(``C1 5h 72% W 61%``, lowest remaining %) — the % is None when unknown or stale."""
    if entry.snapshot is None:
        return f"{entry.label} {'exp' if entry.reason == 'expired' else '?'}", None
    session = _window_remaining(entry.snapshot, _SESSION_LABELS)
    week = _window_remaining(entry.snapshot, _WEEK_LABELS)
    mark = "~" if entry.stale else ""
    text = " ".join(
        f"{name} {mark}{value}%" if value is not None else f"{name} --" for name, value in (("5h", session), ("W", week)))
    known = [v for v in (session, week) if v is not None]
    return f"{entry.label} {text}", (min(known) if known and not entry.stale else None)


def _fetch_claude(config_dir: Optional[str]) -> AccountUsageSnapshot:
    from agent.anthropic_credentials import (
        _read_claude_code_credentials_from_keychain, is_claude_code_token_valid,
        read_claude_code_credentials_for_config_dir)
    if config_dir is None:
        # Default login only (keychain entry without suffix). Deliberately not resolve_anthropic_token(): that
        # prefers env/Hermes-pool credentials, which would mix the accounts.
        creds = _read_claude_code_credentials_from_keychain()
        if creds is None:
            from agent.anthropic_credentials import _read_claude_code_credentials_from_file
            creds = _read_claude_code_credentials_from_file()
    else:
        creds = read_claude_code_credentials_for_config_dir(Path(config_dir).expanduser())
    if not creds:
        raise _QuotaUnavailable("not logged in")
    if not is_claude_code_token_valid(creds):
        raise _QuotaUnavailable("expired")
    return fetch_claude_oauth_usage(creds["accessToken"], timeout=8.0)


def _fetch_codex() -> AccountUsageSnapshot:
    snapshot = fetch_account_usage("openai-codex")  # never raises; None on any failure
    if snapshot is None:
        raise _QuotaUnavailable("unavailable")
    return snapshot


class _QuotaUnavailable(Exception):
    pass


def account_visible(provider: str, config_dir: Optional[str]) -> bool:
    return provider != "claude-code" or config_dir is None or Path(config_dir).expanduser().is_dir()


def fetch_quota(provider: str, config_dir: Optional[str]) -> AccountUsageSnapshot:
    snapshot = _fetch_claude(config_dir) if provider == "claude-code" else _fetch_codex()
    if snapshot.unavailable_reason or not snapshot.windows:
        raise _QuotaUnavailable("unavailable")
    return snapshot


class QuotaHud:
    """Per-account cache; ``entries()`` is render-safe and kicks a background refresh when due."""

    def __init__(self, accounts=QUOTA_ACCOUNTS, *, ttl: float = TTL_SECONDS,
                 fetch: Callable[[str, Optional[str]], AccountUsageSnapshot] = fetch_quota,
                 on_update: Optional[Callable[[], None]] = None):
        self._accounts = tuple(a for a in accounts if account_visible(a[1], a[2]))
        self._ttl, self._fetch, self._on_update = ttl, fetch, on_update
        self._entries = {label: QuotaEntry(label) for label, _, _ in self._accounts}
        self._thread: Optional[threading.Thread] = None

    def entries(self) -> list[QuotaEntry]:
        self.refresh_if_due()
        return [self._entries[label] for label, _, _ in self._accounts]

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
            old = self._entries[label]
            if time.time() - old.checked_at < self._ttl:
                continue
            try:
                new = QuotaEntry(label, self._fetch(provider, config_dir), "", time.time())
            except _QuotaUnavailable as exc:
                new = QuotaEntry(label, old.snapshot, str(exc), time.time())
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.debug("quota hud ▸ %s fetch failed (%s)", label, type(exc).__name__)
                new = QuotaEntry(label, old.snapshot, f"HTTP {status}" if status else "error", time.time())
            self._entries[label] = new
        if self._on_update is not None:
            try:
                self._on_update()
            except Exception:
                pass

    def usage_lines(self) -> list[str]:
        """Expanded view for ``/usage``: per-account windows with reset times (cached, no network)."""
        from agent.account_usage import render_account_usage_lines
        lines: list[str] = []
        for entry in (self._entries[label] for label, _, _ in self._accounts):
            if entry.snapshot is not None:
                lines.extend(render_account_usage_lines(dataclasses.replace(entry.snapshot, title=f"{entry.label} limits")))
            else:
                lines.append(f"📈 {entry.label} limits")
            if entry.reason:
                lines.append(f"{'Stale' if entry.snapshot else 'Unknown'}: {entry.reason}")
        return lines
