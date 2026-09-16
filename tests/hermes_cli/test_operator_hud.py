"""Operator HUD row in the classic CLI: foreground state, running counts, copy-last, narrow layout."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import tools.process_registry as registry_mod
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.quota_hud import QuotaEntry
from cli import HermesCLI
from tools.delegate_tool_registry import _register_subagent, _unregister_subagent


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "anthropic/claude-opus-4.6"
    cli_obj.agent = None
    cli_obj.session_start = datetime.now()
    cli_obj.conversation_history = []
    cli_obj._background_tasks = {}
    return cli_obj


def _text(frags):
    return "".join(t for _, t in frags)


def _width(cli_obj, frags):
    return sum(cli_obj._status_bar_display_width(t) for _, t in frags)


def test_state_maps_idle_running_waiting():
    cli_obj = _make_cli()
    assert cli_obj._operator_state_fragment()[1] == "○ IDLE"

    cli_obj._agent_running = True
    cli_obj._prompt_start_time = time.time() - 161
    assert cli_obj._operator_state_fragment()[1] == "● RUNNING 02:41"

    cli_obj._approval_state = {"command": "rm"}
    assert cli_obj._operator_state_fragment()[1] == "◐ WAITING"

    cli_obj._approval_state = None
    cli_obj._agent_running = False
    cli_obj._clarify_state = {"question": "?"}
    assert cli_obj._operator_state_fragment()[1] == "◐ WAITING"


def test_idle_controller_with_running_process_shows_running(monkeypatch):
    """controller idle + PROC 1 => RUNNING (aggregate status, not just controller generation)."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    monkeypatch.setattr(registry_mod, "save_completed_result", lambda session: None)
    registry = registry_mod.process_registry
    session = registry_mod.ProcessSession(id="proc_runtest", command="sleep 60")
    registry._running[session.id] = session
    try:
        assert cli_obj._operator_state_fragment()[1].startswith("● RUNNING")
    finally:
        registry._running.pop(session.id, None)
        registry._finished.pop(session.id, None)


def test_idle_controller_with_running_subagent_shows_running():
    """controller idle + AGENTS 1 => RUNNING."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    _register_subagent({"subagent_id": "sa-runtest"})
    try:
        assert cli_obj._operator_state_fragment()[1].startswith("● RUNNING")
    finally:
        _unregister_subagent("sa-runtest")


def test_idle_controller_with_bg_task_shows_running():
    """controller idle + BG 1 => RUNNING."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    cli_obj._background_tasks = {"bg_1": object()}
    assert cli_obj._operator_state_fragment()[1].startswith("● RUNNING")


def test_idle_controller_no_workers_shows_idle():
    """controller idle + PROC 0 + AGENTS 0 + BG 0 => IDLE."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    cli_obj._background_tasks = {}
    assert cli_obj._operator_state_fragment()[1] == "○ IDLE"


def test_active_controller_shows_running():
    """active controller (no background workers) => RUNNING."""
    cli_obj = _make_cli()
    cli_obj._agent_running = True
    cli_obj._prompt_start_time = time.time() - 5
    assert cli_obj._operator_state_fragment()[1].startswith("● RUNNING")


def test_background_worker_takes_precedence_over_idle_even_with_stale_flag():
    """Active worker takes precedence over IDLE regardless of controller state."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    _register_subagent({"subagent_id": "sa-precedence"})
    try:
        state = cli_obj._operator_state_fragment()[1]
        assert state != "○ IDLE"
        assert state.startswith("● RUNNING")
    finally:
        _unregister_subagent("sa-precedence")


def test_running_elapsed_timer_sensible_while_background_work_continues():
    """Elapsed RUNNING timer stays sane while a background worker keeps the state RUNNING,
    including when there is no foreground _prompt_start_time (background-only RUNNING)."""
    cli_obj = _make_cli()
    cli_obj._agent_running = False
    cli_obj._background_tasks = {"bg_1": object()}
    label = cli_obj._operator_state_fragment()[1]
    assert label == "● RUNNING"  # no _prompt_start_time set -> bare RUNNING, no bogus clock

    cli_obj._agent_running = True
    cli_obj._prompt_start_time = time.time() - 7
    label2 = cli_obj._operator_state_fragment()[1]
    assert label2.startswith("● RUNNING 00:0")


def test_counts_only_running_processes_subagents_and_bg_tasks(monkeypatch):
    cli_obj = _make_cli()
    assert cli_obj._operator_activity_labels() == []

    monkeypatch.setattr(registry_mod, "save_completed_result", lambda session: None)
    registry = registry_mod.process_registry
    session = registry_mod.ProcessSession(id="proc_hudtest", command="sleep 60")
    registry._running[session.id] = session
    _register_subagent({"subagent_id": "sa-hudtest"})
    cli_obj._background_tasks = {"bg_1": object()}
    try:
        assert cli_obj._operator_activity_labels() == ["⚙ PROC 1", "◈ AGENTS 1", "↻ BG 1"]
        registry._move_to_finished(session)
        _unregister_subagent("sa-hudtest")
        cli_obj._background_tasks = {}
        assert cli_obj._operator_activity_labels() == []
    finally:
        registry._running.pop(session.id, None)
        registry._finished.pop(session.id, None)
        _unregister_subagent("sa-hudtest")


def test_interactive_bang_process_is_rendered_as_proc():
    cli_obj = _make_cli()
    cli_obj._active_bang_processes = 1
    assert cli_obj._operator_activity_labels() == ["⚙ PROC 1"]
    assert "● RUNNING" in _text(cli_obj._get_operator_hud_fragments(width=120))


def test_copy_last_uses_existing_copy_command_and_clipboard_path():
    cli_obj = _make_cli()
    assert not cli_obj._copy_last_available()
    assert "Copy last" in _text(cli_obj._get_operator_hud_fragments(width=120))
    with patch.object(HermesCLI, "_handle_copy_command") as copy_cmd:
        cli_obj._tui_handle_copy_last(None)
    copy_cmd.assert_not_called()

    cli_obj.conversation_history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "final answer"},
                                    {"role": "assistant", "content": ""}]
    assert "Copy last" in _text(cli_obj._get_operator_hud_fragments(width=120))
    with patch("hermes_cli.clipboard.is_remote_shell_session", return_value=False), \
            patch("hermes_cli.clipboard.write_clipboard_text", return_value=True) as write, \
            patch.object(HermesCLI, "_handle_copy_command", wraps=cli_obj._handle_copy_command) as copy_cmd:
        cli_obj._tui_handle_copy_last(None)
    copy_cmd.assert_called_once_with("/copy")
    write.assert_called_once_with("final answer")


def test_narrow_terminal_keeps_running_and_never_overflows():
    cli_obj = _make_cli()
    cli_obj._agent_running = True
    cli_obj._prompt_start_time = time.time() - 5
    cli_obj.conversation_history = [{"role": "assistant", "content": "x"}]
    cli_obj._background_tasks = {"a": object(), "b": object()}
    snap = AccountUsageSnapshot(provider="anthropic", source="t", fetched_at=datetime.now(timezone.utc),
                                windows=(AccountUsageWindow("Current session", 5), AccountUsageWindow("Current week", 90)))
    cli_obj._quota_hud = SimpleNamespace(entries=lambda: [QuotaEntry("C1", snap), QuotaEntry("CX", None, "unavailable")])

    wide = _text(cli_obj._get_operator_hud_fragments(width=200))
    assert wide.startswith(" ● RUNNING 00:0") and "↻ BG 2" in wide and "C1 5h 95% W 10%" in wide and "CX ?" in wide

    for width in (60, 30, 17, 8):
        frags = cli_obj._get_operator_hud_fragments(width=width)
        assert _width(cli_obj, frags) <= width
        assert "\n" not in _text(frags)
    assert "RUNNING" in _text(cli_obj._get_operator_hud_fragments(width=17))


def test_quota_style_thresholds():
    style = HermesCLI._quota_style
    assert style(51) == "class:status-bar-good"
    assert style(50) == style(20) == "class:status-bar-warn"
    assert style(19) == "class:status-bar-critical"
    assert style(None) == "class:status-bar-dim"
