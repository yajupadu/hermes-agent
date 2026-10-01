"""Operator HUD quota: account isolation, remaining-% conversion, fail-open caching, no secret leakage."""

import dataclasses
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import agent.anthropic_credentials as creds_mod
import agent.account_usage as account_usage
import agent.quota_hud as hud_mod
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow, remaining_percent
from agent.quota_hud import QuotaEntry, QuotaHud, compact_quota, fetch_quota

_FAR_FUTURE_MS = int((time.time() + 3600) * 1000)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Claude Code logins are discovered under $HOME; no test may reach the real ones."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _claude_login(home, dirname, token, expires_ms=_FAR_FUTURE_MS):
    (home / dirname).mkdir()
    (home / dirname / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "refreshToken": f"refresh-{token}", "expiresAt": expires_ms}}))


def _pool_row(ident, priority=0, label="safe-label", token="fake-token", *, status=None):
    return {"id": ident, "label": label, "auth_type": "api_key", "source": "test",
            "access_token": token, "priority": priority, "last_status": status}


def test_hud_discovery_never_invokes_mutating_load_pool(monkeypatch):
    rows = {
        "anthropic": [_pool_row("a1", token="fake-anthropic")],
        "openai-codex": [_pool_row("x1", token="fake-codex")],
    }
    monkeypatch.setattr("agent.credential_pool.load_pool",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("load_pool")))
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: rows[provider])
    monkeypatch.setattr(account_usage, "_fetch_anthropic_account_usage", lambda *a, **k: _snap(1, 2))
    monkeypatch.setattr(account_usage, "_fetch_codex_account_usage",
                        lambda *a, **k: _snap(3, 4, "openai-codex", ("Session", "Weekly")))
    hud = QuotaHud(ttl=0)
    hud.refresh_now()
    # The anthropic pool mirrors the Claude Code logins, which are the Claude slots; never both.
    assert [entry.account_id for entry in hud.entries()] == ["x1"]


def test_codex_hud_uses_each_pool_entry_without_refresh_or_cross_account_fallthrough(monkeypatch):
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: [
        _pool_row("cx1", 0, token="cx1-token"), _pool_row("cx2", 1, token="cx2-token")
    ] if provider == "openai-codex" else [])
    calls = []
    payload = {
        "plan_type": "plus",
        "rate_limit": {"primary_window": {"used_percent": 21}, "secondary_window": {"used_percent": 4}},
        "credits": {"has_credits": False},
    }
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("GET", "https://chatgpt.com/backend-api/wham/usage")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("request failed", request=request, response=response)

        def json(self):
            return payload

    def client_factory(timeout):
        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, headers):
                token = headers["Authorization"]
                calls.append((url, token))
                return Response(401 if token == "Bearer cx1-token" else 200)

        return Client()

    monkeypatch.setattr(account_usage.httpx, "Client", client_factory)
    hud = QuotaHud(ttl=0)
    hud.refresh_now()
    assert calls == [
        ("https://chatgpt.com/backend-api/wham/usage", "Bearer cx1-token"),
        ("https://chatgpt.com/backend-api/wham/usage", "Bearer cx2-token"),
    ]
    assert [entry.snapshot is not None for entry in hud.entries()] == [False, True]
    assert [entry.reason for entry in hud.entries()] == ["unavailable", ""]


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


def test_compact_quota_separates_percentage_from_reset_arrow():
    reset_at = datetime.now(timezone.utc) + timedelta(hours=2, minutes=6)
    snapshot = AccountUsageSnapshot(
        provider="anthropic", source="test", fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow("Current session", 14, reset_at),
                 AccountUsageWindow("Current week", 55, reset_at + timedelta(days=2))),
    )
    text, _ = compact_quota(QuotaEntry("C1", snapshot))
    assert "% ↻" in text
    assert text.count("% ↻") == 2
    assert "%↻" not in text


def _forbid_credential_mutation(monkeypatch):
    def forbidden(name):
        return lambda *a, **k: (_ for _ in ()).throw(AssertionError(name))
    monkeypatch.setattr("agent.credential_pool.load_pool", forbidden("load_pool"))
    monkeypatch.setattr(creds_mod, "_refresh_oauth_token", forbidden("refresh"))
    monkeypatch.setattr(creds_mod, "_post_oauth_token", forbidden("refresh"))
    monkeypatch.setattr(creds_mod, "_write_claude_code_credentials", forbidden("write"))
    # The Keychain is host state; these tests use the per-login credential files.
    monkeypatch.setattr(creds_mod, "_read_claude_code_credentials_from_keychain", lambda *a, **k: None)


def _oauth_usage_client(monkeypatch, calls, status=lambda token: 200):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append((url, headers["Authorization"]))
            request = httpx.Request("GET", url)
            return httpx.Response(status(headers["Authorization"]), request=request, json={
                "five_hour": {"utilization": 12, "resets_at": "2030-01-01T00:00:00Z"},
                "seven_day": {"utilization": 34, "resets_at": "2030-01-02T00:00:00Z"}})

    monkeypatch.setattr(account_usage.httpx, "Client", lambda timeout: Client())


def test_claude_code_logins_flow_through_service_into_hud_each_with_its_own_token(_home, monkeypatch):
    """Integration: real discovery + credential files + usage parsing; only HTTP is faked."""
    _claude_login(_home, ".claude", "sk-ant-oat01-default-token")
    _claude_login(_home, ".claude-account2", "sk-ant-oat01-account2-token")
    # CLAUDE_CONFIG_DIR may point at the other login; Claude1 must still be the default login.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_home / ".claude-account2"))
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: [
        _pool_row("pool-anthropic", token="sk-ant-oat01-pool-token")] if provider == "anthropic" else [])
    _forbid_credential_mutation(monkeypatch)
    calls = []
    _oauth_usage_client(monkeypatch, calls)
    hud = QuotaHud(ttl=0)
    hud.refresh_now()
    url = "https://api.anthropic.com/api/oauth/usage"
    assert calls == [(url, "Bearer sk-ant-oat01-default-token"), (url, "Bearer sk-ant-oat01-account2-token")]
    assert [(e.label, e.provider, e.account_id) for e in hud.entries()] == [
        ("Claude1", "claude-code", "claude-default"), ("Claude2", "claude-code", "claude-account2")]
    assert [compact_quota(e)[0].split(" ↻")[0] for e in hud.entries()] == ["Claude1 5h 88%", "Claude2 5h 88%"]


def test_claude_code_login_failures_stay_on_their_own_slot(_home, monkeypatch, caplog):
    """Expired → exp with no request and no refresh; a 401 is a sanitized reason, never the other login."""
    _claude_login(_home, ".claude", "sk-ant-oat01-default-token")
    _claude_login(_home, ".claude-account2", "sk-ant-oat01-account2-token", expires_ms=1)
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: [])
    _forbid_credential_mutation(monkeypatch)
    calls = []
    _oauth_usage_client(monkeypatch, calls, status=lambda token: 401)
    hud = QuotaHud(ttl=0)
    with caplog.at_level(logging.DEBUG):
        hud.refresh_now()
    assert [token for _, token in calls] == ["Bearer sk-ant-oat01-default-token"]
    assert [compact_quota(e)[0] for e in hud.entries()] == ["Claude1 ?", "Claude2 exp"]
    shown = "\n".join([*hud.usage_lines(), caplog.text])
    assert "Unknown: HTTP 401" in shown and "Unknown: expired" in shown
    assert "sk-ant-" not in shown and "refresh-" not in shown


def test_missing_claude_login_dirs_are_omitted_and_labels_stay_stable(_home, monkeypatch):
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: [])
    assert QuotaHud()._accounts == ()
    (_home / ".claude-account2").mkdir()
    assert QuotaHud()._accounts == (("Claude2", "claude-code", "claude-account2"),)


def test_missing_exact_account_id_fails_closed_without_ambient_usage(monkeypatch):
    monkeypatch.setattr(account_usage, "fetch_account_usage",
                        lambda provider: (_ for _ in ()).throw(AssertionError("ambient fallback forbidden")))
    with pytest.raises(Exception):
        fetch_quota("openai-codex", None)
    with pytest.raises(Exception):
        fetch_quota("anthropic", "")
    hud = QuotaHud((("CX", "openai-codex", ""),), ttl=999)
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

    hud = QuotaHud((("C1", "anthropic", "c1"),), ttl=0, fetch=fetch)
    with caplog.at_level(logging.DEBUG):
        hud.refresh_now()
        hud.refresh_now()  # must not raise
    entry = hud._entries[("anthropic", "c1")]
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


def test_codex_pool_accounts_refresh_independently(monkeypatch):
    rows = [_pool_row("account-one", 0, token="token-account-one"),
            _pool_row("account-two", 1, token="token-account-two")]
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: rows if provider == "openai-codex" else [])
    seen = []
    monkeypatch.setattr("agent.account_usage._fetch_codex_account_usage",
                        lambda base, token, *, refresh_on_401=True: seen.append((token, refresh_on_401)) or _snap(10 if token.endswith("one") else 20, 30,
                                                                        "openai-codex", ("Session", "Weekly")))
    hud = QuotaHud(ttl=0)
    hud.refresh_now()
    assert seen == [("token-account-one", False), ("token-account-two", False)]
    assert compact_quota(hud._entries[("openai-codex", "account-one")])[0] == "CX1 5h 90% W 70%"
    assert compact_quota(hud._entries[("openai-codex", "account-two")])[0] == "CX2 5h 80% W 70%"


class _PoolEntry:
    def __init__(self, ident, priority, key="token"):
        self.id, self.priority = ident, priority
        self.key = key


def _patch_pool(monkeypatch, entries):
    rows = [_pool_row(entry.id, entry.priority, token=entry.key) for entry in entries]
    monkeypatch.setattr(hud_mod, "read_credential_pool", lambda provider: rows if provider == "openai-codex" else [])


def test_codex_labels_follow_pool_order_and_never_invent_an_account(monkeypatch):
    """CX1..CXn come from the pool sorted by (priority, id); an empty pool shows no CX row."""
    _patch_pool(monkeypatch, [_PoolEntry("zz", 1), _PoolEntry("bb", 0), _PoolEntry("aa", 0)])
    assert hud_mod._codex_quota_accounts() == (
        ("CX1", "openai-codex", "aa"), ("CX2", "openai-codex", "bb"), ("CX3", "openai-codex", "zz"))

    _patch_pool(monkeypatch, [])
    assert hud_mod._codex_quota_accounts() == ()
    assert not [label for label, _, _ in QuotaHud()._accounts if label.startswith("CX")]


def test_codex_entry_without_a_runtime_key_reports_unknown_instead_of_another_account(monkeypatch):
    """A keyless pool row must not fall through to the shared resolver and render someone else's quota."""
    _patch_pool(monkeypatch, [_PoolEntry("empty", 0, key="")])
    monkeypatch.setattr("agent.account_usage._fetch_codex_account_usage",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolver fallback")))
    hud = QuotaHud(ttl=0)
    hud.refresh_now()
    assert hud.entries() == []


def test_only_supplied_windows_render_each_with_its_own_reset():
    now = datetime.now(timezone.utc)
    weekly_only = AccountUsageSnapshot(
        provider="openai-codex", source="test", fetched_at=now,
        windows=(AccountUsageWindow("Weekly", 25, now + timedelta(days=3, hours=2, seconds=30)),))
    assert compact_quota(QuotaEntry("CX1", weekly_only)) == ("CX1 W 75% ↻3d2h", 75)

    session_only = AccountUsageSnapshot(
        provider="openai-codex", source="test", fetched_at=now,
        windows=(AccountUsageWindow("Session", 17, now + timedelta(hours=1, minutes=24, seconds=30)),))
    assert compact_quota(QuotaEntry("CX1", session_only)) == ("CX1 5h 83% ↻1:24", 83)

    neither = AccountUsageSnapshot(provider="openai-codex", source="test", fetched_at=now, windows=())
    assert compact_quota(QuotaEntry("CX1", neither)) == ("CX1 ?", None)

    both = AccountUsageSnapshot(
        provider="openai-codex", source="test", fetched_at=now,
        windows=(AccountUsageWindow("Session", 10, now + timedelta(hours=2, seconds=30)),
                 AccountUsageWindow("Weekly", 25, now + timedelta(days=3, hours=2, seconds=30))))
    assert compact_quota(QuotaEntry("CX1", both)) == ("CX1 5h 90% ↻2:00 W 75% ↻3d2h", 75)


def test_active_star_needs_a_positively_known_credential_id():
    entries = [QuotaEntry("CX1", _snap(10, 20, "openai-codex", ("Session", "Weekly")), provider="openai-codex", account_id="aa"),
               QuotaEntry("CX2", _snap(10, 20, "openai-codex", ("Session", "Weekly")), provider="openai-codex", account_id="bb")]
    assert [compact_quota(e, active_id=("openai-codex", "bb"))[0].split()[0] for e in entries] == ["CX1", "CX2*"]
    assert [compact_quota(e, active_id="bb")[0].split()[0] for e in entries] == ["CX1", "CX2"]
    assert [compact_quota(e, active_id=None)[0].split()[0] for e in entries] == ["CX1", "CX2"]
    assert compact_quota(QuotaEntry("CX2", None, "expired", provider="openai-codex", account_id="bb"),
                         active_id=("openai-codex", "bb")) == ("CX2* exp", None)


def test_active_identity_requires_provider_and_rejects_mismatch_or_providerless():
    snapshot = _snap(10, 20, "anthropic", ("Session", "Weekly"))
    entry = QuotaEntry("C1", snapshot, provider="anthropic", account_id="bb")
    assert compact_quota(entry, active_id=("anthropic", "bb"))[0].startswith("C1*")
    assert not compact_quota(entry, active_id=("openai-codex", "bb"))[0].startswith("C1*")
    providerless = dataclasses.replace(entry, provider=None)
    assert not compact_quota(providerless, active_id=("anthropic", "bb"))[0].startswith("C1*")


def test_expired_state_wins_over_cached_snapshot():
    entry = QuotaEntry("C2", _snap(10, 20), "expired", provider="anthropic", account_id="c2")
    assert compact_quota(entry) == ("C2 exp", None)
    hud = QuotaHud((("C2", "anthropic", "c2"),), fetch=lambda *args: _snap(10, 20))
    hud._entries[("anthropic", "c2")] = entry
    usage = "\n".join(hud.usage_lines())
    assert "C2 exp" not in usage
    assert "5h" not in usage and "Weekly" not in usage
    assert "Unknown: expired" in usage
