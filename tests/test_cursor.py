"""Cursor-specific contracts, exercised without live accounts or network."""
from __future__ import annotations

import pytest

from cursorpool.cli import build_parser, _normalize_argv
from cursorpool.session_io import apply_env, validate_session
from cursorpool.runner import should_capture_output


def test_cursor_cli_identity():
    assert build_parser().prog == "cursorpool"
    assert _normalize_argv([]) == ["run", "--", "agent"]


def test_cursor_session_accepts_api_key():
    assert validate_session({"apiKey": " fake-cursor-key "}) == {"apiKey": "fake-cursor-key"}


def test_cursor_env_replaces_ambient_auth():
    env = apply_env({"apiKey": "fake-cursor-key"}, {"CURSOR_AUTH_TOKEN": "fake-other-token", "PATH": "/bin"})
    assert env == {"CURSOR_API_KEY": "fake-cursor-key", "PATH": "/bin"}


def test_cursor_interactive_inherits_stdio():
    assert should_capture_output(["agent"]) is False
    assert should_capture_output(["cursor-agent"]) is False
    assert should_capture_output(["agent", "-p", "hello"]) is True


def test_import_self_uses_cursor_api_key(home, monkeypatch, capsys):
    from cursorpool.cli import main
    from cursorpool.pool import load_pool, resolve_session_file
    from cursorpool.session_io import load_session
    monkeypatch.setenv("CURSOR_API_KEY", "fake-import-key")
    assert main(["--home", str(home), "import", "--self", "--email", "me@example.com", "--json"]) == 0
    account = load_pool(home).get("me@example.com")
    path = resolve_session_file(account, home)
    assert load_session(path) == {"apiKey": "fake-import-key"}
    assert path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "fake-import-key" not in output.out + output.err


def test_import_self_missing_key_is_actionable(home, monkeypatch, capsys):
    from cursorpool.cli import main
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert main(["--home", str(home), "import", "--self", "--email", "me@example.com"]) == 1
    assert "CURSOR_API_KEY" in capsys.readouterr().err


def test_cursor_acp_preserves_protocol_stdio():
    from cursorpool.runner import is_protocol_mode, prepare_cmd
    assert is_protocol_mode(["agent", "acp"])
    assert not should_capture_output(["agent", "acp"])
    assert prepare_cmd(["agent", "--acp"], add_continue=True) == ["agent", "acp"]


@pytest.mark.parametrize("flag", ["--api-key", "--api-key=fake-override", "-a", "--auth-token", "--auth-token=fake-override"])
def test_cursor_rejects_child_auth_override(flag, two_account_pool, home):
    from cursorpool.runner import _build_env_and_cmd
    with pytest.raises(ValueError, match="pooled credentials"):
        _build_env_and_cmd(["agent", flag], two_account_pool.accounts[0], root=home, env={}, add_continue=False)


def test_cursor_usage_does_not_send_keys_to_augment(two_account_pool, home):
    from cursorpool.analytics import refresh_usage, get_usage_result
    calls = []
    def fake_get(url, headers):
        calls.append(url)
        return {"records": []}
    cache = refresh_usage(two_account_pool, root=home, http_get=fake_get)
    assert calls == []
    assert cache["fetches_ok"] == 0
    assert cache["by_id"] == {}
    result = get_usage_result(two_account_pool, root=home, force=True, http_get=fake_get)
    assert result.by_id is None
    assert not result.refresh_succeeded
    assert "unavailable" in " ".join(result.errors).lower()


def test_private_project_does_not_publish_to_pypi():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    workflows = (root / ".github" / "workflows").glob("*.yml")
    assert all("pypa/gh-action-pypi-publish" not in path.read_text() for path in workflows)


def test_cursor_shorthand_does_not_double_binary():
    assert _normalize_argv(["cursor-agent", "-p", "hello"]) == ["run", "--", "cursor-agent", "-p", "hello"]


@pytest.mark.parametrize("key", [None, "", "   ", True, 123, "fake-key\n", "fake\x00key"])
def test_cursor_rejects_invalid_api_key(key):
    with pytest.raises(ValueError):
        validate_session({"apiKey": key})


def test_cursor_rejects_augment_session():
    with pytest.raises(ValueError, match="apiKey"):
        validate_session({"accessToken": "fake-augment-token", "tenantURL": "https://example.invalid"})


def test_stats_explains_unavailable_credits(two_account_pool, home):
    from cursorpool.reporting import collect_stats
    snapshot = collect_stats(home)
    assert "unavailable" in " ".join(snapshot["usage"]["errors"])
    assert snapshot["usage"]["fetches_ok"] == 0


def test_dashboard_does_not_suggest_nonexistent_analytics(two_account_pool, home, capsys):
    from cursorpool.cli import main
    assert main(["--home", str(home), "usage", "--no-color"]) == 0
    output = capsys.readouterr().out
    assert "cursor.com/dashboard" in output
    assert "run cursorpool refresh for Analytics" not in output


def test_list_does_not_label_local_counts_as_credits(two_account_pool, home, capsys):
    from cursorpool.cli import main
    from cursorpool.state import load_state, save_state
    state = load_state(home)
    state.for_account("alice@acme.com").local_uses = 3
    save_state(state, home)
    assert main(["--home", str(home), "list"]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("alice@"))
    assert row.split()[2] == "-"


def test_cursor_print_failover_injects_each_selected_key(two_account_pool, home, tmp_path, monkeypatch, capsys):
    import os
    import sys
    from cursorpool.cli import main
    from cursorpool.state import load_state

    binary = tmp_path / "agent"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert 'CURSOR_AUTH_TOKEN' not in os.environ\n"
        "assert '--api-key' not in sys.argv\n"
        "assert '--augment-session-json' not in sys.argv\n"
        "if os.environ['CURSOR_API_KEY'] == 'tok-alice':\n"
        "    print('rate limit exceeded', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "assert os.environ['CURSOR_API_KEY'] == 'tok-bob'\n"
        "assert '--continue' in sys.argv\n"
        "print('cursor-success')\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CURSOR_API_KEY", "fake-ambient-key")
    monkeypatch.setenv("CURSOR_AUTH_TOKEN", "fake-ambient-token")
    assert main(["--home", str(home), "-p", "hello"]) == 0
    output = capsys.readouterr()
    assert "cursor-success" in output.out
    assert "1 failovers" in output.err
    for key in ("tok-alice", "tok-bob", "fake-ambient-key", "fake-ambient-token"):
        assert key not in output.out + output.err
    state = load_state(home)
    assert state.for_account("alice@acme.com").local_uses == 0
    assert state.for_account("alice@acme.com").cooldown_until is not None
    assert state.for_account("bob@acme.com").local_uses == 1


def test_cursor_acp_exec_passes_selected_key(two_account_pool, home, monkeypatch):
    from cursorpool.cli import main
    from cursorpool.state import load_state

    class Executed(Exception):
        pass

    def fake_exec(binary, argv, env):
        assert binary == "agent"
        assert argv == ["agent", "acp"]
        assert env["CURSOR_API_KEY"] == "tok-alice"
        assert "CURSOR_AUTH_TOKEN" not in env
        raise Executed

    monkeypatch.setattr("os.execvpe", fake_exec)
    with pytest.raises(Executed):
        main(["--home", str(home), "--acp"])
    assert load_state(home).for_account("alice@acme.com").local_uses == 1


def test_cursor_retains_augpool_subcommands():
    import argparse
    parser = build_parser()
    commands = next(action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert set(commands) == {
        "add", "import", "remove", "update", "mode", "list", "stats",
        "refresh", "usage", "export", "run", "status", "restore",
    }


def test_cursor_print_prompt_named_acp_is_not_protocol():
    from cursorpool.runner import is_protocol_mode
    assert not is_protocol_mode(["agent", "-p", "acp"])
    assert should_capture_output(["agent", "-p", "acp"])


def test_cursor_acp_after_model_option_is_protocol():
    from cursorpool.runner import is_protocol_mode
    assert is_protocol_mode(["agent", "--model", "fake-model", "acp"])
    assert not is_protocol_mode(["agent", "--model", "acp", "hello"])


def test_invalid_blob_error_does_not_echo_credential_text():
    from cursorpool.session_io import decode_share_blob
    blob = "fake-sensitive-credential-with-invalid+character"
    with pytest.raises(ValueError) as raised:
        decode_share_blob(blob)
    assert "fake-sensitive" not in str(raised.value)


def test_cursor_rejects_attached_short_auth_flag(two_account_pool, home):
    from cursorpool.runner import _build_env_and_cmd
    with pytest.raises(ValueError, match="pooled credentials"):
        _build_env_and_cmd(["cursor-agent", "-afake-override"], two_account_pool.accounts[0], root=home, env={}, add_continue=False)
