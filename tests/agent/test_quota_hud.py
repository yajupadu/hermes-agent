"""Operator HUD quota: account isolation, remaining-% conversion, fail-open caching, no secret leakage."""

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone

import agent.anthropic_credentials as creds_mod
import agent.quota_hud as hud_mod
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, remaining_percent
from agent.quota_hud import QuotaEntry, QuotaHud, compact_quota, fetch_quota

_FAR_FUTURE_MS = int((time.time() + 3600) * 1000)


def _snap(session_used, week_used, provider="anthropic", labels=("Current session", "Current week")):
    return AccountUsageSnapshot(
        provider=provider, source="test", fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow(labels[0], session_used), AccountUsageWindow(labels[1], week_used)))


def test_used_percent_converts_to_remaining():
    assert remaining_percent(28) == 72
    assert remaining_percent(100.4) == 0
    assert remaining_percent(-3) == 100
    text, lowest = compact_quota(QuotaEntry("C1", _snap(28, 39)))
    assert text == "C1 5h 72% W 61%"
    assert lowest == 61


def test_claude_accounts_read_isolated_credentials(monkeypatch, tmp_path):
    services = []

    def fake_keychain(service="Claude Code-credentials"):
        services.append(service)
        token = "tok-account-one" if service == "Claude Code-credentials" else "tok-account-two"
        return {"accessToken": token, "expiresAt": _FAR_FUTURE_MS}

    used_tokens = []
    monkeypatch.setattr(creds_mod, "_read_claude_code_credentials_from_keychain", fake_keychain)
    monkeypatch.setattr(hud_mod, "fetch_claude_oauth_usage", lambda token, timeout: used_tokens.append(token) or _snap(0, 0))

    fetch_quota("claude-code", None)
    fetch_quota("claude-code", str(tmp_path))

    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:8]
    assert services == ["Claude Code-credentials", f"Claude Code-credentials-{suffix}"]
    assert used_tokens == ["tok-account-one", "tok-account-two"]


def test_expired_account_token_is_not_refreshed_and_shows_exp(monkeypatch, tmp_path):
    monkeypatch.setattr(creds_mod, "_read_claude_code_credentials_from_keychain",
                        lambda service="": {"accessToken": "tok", "refreshToken": "rt", "expiresAt": 1})
    monkeypatch.setattr(creds_mod, "_refresh_oauth_token", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh")))
    monkeypatch.setattr(hud_mod, "fetch_claude_oauth_usage", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    hud = QuotaHud((("C2", "claude-code", str(tmp_path)),), ttl=999)
    hud.refresh_now()
    assert compact_quota(hud.entries()[0]) == ("C2 exp", None)


def test_rate_limited_codex_shows_zero_and_unavailable_shows_unknown(monkeypatch):
    monkeypatch.setattr(hud_mod, "fetch_account_usage",
                        lambda provider: _snap(100, 100, "openai-codex", ("Session", "Weekly")))
    assert compact_quota(QuotaEntry("CX", fetch_quota("openai-codex", None))) == ("CX 5h 0% W 0%", 0)

    monkeypatch.setattr(hud_mod, "fetch_account_usage", lambda provider: None)
    hud = QuotaHud((("CX", "openai-codex", None),), ttl=999)
    hud.refresh_now()
    assert compact_quota(hud.entries()[0]) == ("CX ?", None)
    assert "Unknown: unavailable" in hud.usage_lines()


def test_failed_refresh_is_non_fatal_keeps_stale_value_and_leaks_nothing(caplog):
    secret = "sk-ant-oat01-SECRET-TOKEN"
    calls = []

    def fetch(provider, config_dir):
        calls.append(provider)
        if len(calls) == 1:
            return _snap(10, 20)
        raise RuntimeError(f"401 for Bearer {secret}")

    hud = QuotaHud((("C1", "claude-code", None),), ttl=0, fetch=fetch)
    with caplog.at_level(logging.DEBUG):
        hud.refresh_now()
        hud.refresh_now()  # must not raise
    entry = hud._entries["C1"]
    text, lowest = compact_quota(entry)
    assert text == "C1 5h ~90% W ~80%" and lowest is None
    rendered = "\n".join([text, *hud.usage_lines(), caplog.text])
    assert secret not in rendered and "Stale: error" in rendered


def test_entries_never_block_on_a_slow_provider_and_cache_within_ttl():
    release, calls = threading.Event(), []

    def slow_fetch(provider, config_dir):
        calls.append(provider)
        release.wait(5)
        return _snap(0, 0)

    hud = QuotaHud((("C1", "claude-code", None),), ttl=300, fetch=slow_fetch)
    started = time.monotonic()
    assert compact_quota(hud.entries()[0]) == ("C1 ?", None)
    hud.entries()
    assert time.monotonic() - started < 2.0
    release.set()
    assert hud._thread is not None
    hud._thread.join(5)
    hud.entries()
    hud.refresh_now()
    assert calls == ["claude-code"]


def test_missing_account_config_dir_is_hidden(tmp_path):
    hud = QuotaHud((("C1", "claude-code", None), ("C2", "claude-code", str(tmp_path / "nope"))), fetch=lambda *a: _snap(0, 0))
    assert [e.label for e in hud._entries.values()] == ["C1"]
