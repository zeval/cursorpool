from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

import pytest

from cursorpool.analytics import default_date_window, save_usage_cache
from cursorpool.cli import main
from cursorpool.session_io import decode_share_blob, write_json_atomic


def test_add_list_restore_legacy_backup(home: Path, capsys):
    from cursorpool.pool import load_pool
    from cursorpool.session_io import backup_and_use, load_session

    session = home / "in.json"
    write_json_atomic(
        session,
        {"apiKey": "t1", "tenantURL": "https://e5.api.augmentcode.com/", "scopes": []},
    )
    assert (
        main(
            [
                "--home", str(home), "add",
                "--email", "me@x.com",
                "--session", str(session),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--home", str(home), "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["email"] == "me@x.com"
    assert data[0]["locked"] is False

    s2 = home / "in2.json"
    write_json_atomic(
        s2,
        {"apiKey": "t2", "tenantURL": "https://e5.api.augmentcode.com/", "scopes": []},
    )
    assert main([
        "--home", str(home), "add", "--email", "o@x.com", "--session", str(s2)
    ]) == 0

    target_parent = home / "fake-augment"
    pool_path = home / "pool.json"
    pool = json.loads(pool_path.read_text())
    pool["cursor_session_path"] = str(target_parent / "session.json")
    pool_path.write_text(json.dumps(pool), encoding="utf-8")

    assert main(["--home", str(home), "mode", "me@x.com"]) == 0
    capsys.readouterr()

    target_parent.mkdir(parents=True)
    (target_parent / "session.json").write_text(
        json.dumps({"apiKey": "old", "tenantURL": "https://t/", "scopes": []}),
        encoding="utf-8",
    )

    backup_and_use(load_session(session), target_parent / "session.json", root=home)
    used = json.loads((target_parent / "session.json").read_text())
    assert used["apiKey"] == "t1"

    assert main(["--home", str(home), "restore"]) == 0
    restored = json.loads((target_parent / "session.json").read_text())
    assert restored["apiKey"] == "old"
    assert load_pool(home).locked_email == "me@x.com"


def test_stats_json_emits_versioned_safe_snapshot(
    home: Path, two_account_pool, capsys
):
    start_date, end_date = default_date_window()
    save_usage_cache(
        {
            "fetched_at": time.time(),
            "start_date": start_date,
            "end_date": end_date,
            "by_id": {"alice@acme.com": 5, "bob@acme.com": 10},
            "errors": [],
            "fetches_ok": 1,
            "tenants_queried": 1,
        },
        home,
    )

    assert main(["--home", str(home), "stats", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["schema_version"] == 2
    assert snapshot["mode"] == "auto"
    assert snapshot["locked_email"] is None
    assert [row["email"] for row in snapshot["accounts"]] == [
        "alice@acme.com",
        "bob@acme.com",
    ]
    assert "session_path" not in json.dumps(snapshot)


def test_update_json_changes_account_and_reports_unlocked(
    home: Path, two_account_pool, capsys
):
    from cursorpool.pool import load_pool

    assert main([
        "--home",
        str(home),
        "update",
        "alice@acme.com",
        "--disable",
        "--weight",
        "2.5",
        "--json",
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "ok": True,
        "action": "update",
        "email": "alice@acme.com",
        "enabled": False,
        "weight": 2.5,
        "locked": False,
    }
    pool = load_pool(home)
    assert pool.locked_email is None
    assert pool.get("alice@acme.com").enabled is False
    assert pool.get("alice@acme.com").weight == 2.5


def test_remove_json_emits_mutation_metadata(home: Path, two_account_pool, capsys):
    assert main([
        "--home",
        str(home),
        "remove",
        "bob@acme.com",
        "--json",
    ]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "action": "remove",
        "email": "bob@acme.com",
    }


def test_import_blob_from_stdin_has_safe_json_response(
    home: Path, capsys, monkeypatch
):
    from cursorpool.session_io import build_share_envelope, encode_share_blob

    blob = encode_share_blob(
        build_share_envelope(
            email="alice@example.com",
            label="Alice",
            session={
                "apiKey": "tok-never-print",
                "tenantURL": "https://e5.api.augmentcode.com/",
                "scopes": ["r"],
            },
        )
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(blob + "\n"))

    assert main(["--home", str(home), "import", "-", "--json"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "ok": True,
        "action": "import",
        "email": "alice@example.com",
    }
    assert blob not in output
    assert "tok-never-print" not in output


def test_export_import_blob_roundtrip(home: Path, capsys, tmp_path: Path):
    session = home / "in.json"
    write_json_atomic(
        session,
        {
            "apiKey": "tok-secret",
            "tenantURL": "https://e5.api.augmentcode.com/",
            "scopes": ["r"],
        },
    )
    main([
        "--home", str(home), "add",
        "--email", "alice@example.com",
        "--session", str(session),
    ])
    capsys.readouterr()

    assert main(["--home", str(home), "export", "alice@example.com"]) == 0
    blob = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", blob)
    env = decode_share_blob(blob)
    assert env["email"] == "alice@example.com"
    assert "id" not in env
    assert env["session"]["apiKey"] == "tok-secret"

    other = tmp_path / "other-home"
    other.mkdir()
    assert main(["--home", str(other), "import", blob]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    # creds file named from email
    creds = list((other / "creds").glob("*.json"))
    assert len(creds) == 1
    assert json.loads(creds[0].read_text())["apiKey"] == "tok-secret"


def test_export_env_flag(home: Path, capsys):
    session = home / "in.json"
    write_json_atomic(
        session,
        {"apiKey": "tok", "tenantURL": "https://e5.api.augmentcode.com/", "scopes": ["r"]},
    )
    main(["--home", str(home), "add", "--email", "me@x.com", "--session", str(session)])
    capsys.readouterr()
    assert main(["--home", str(home), "export", "--env", "me@x.com"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("unset CURSOR_AUTH_TOKEN; export CURSOR_API_KEY=")
    assert "tok" in out


def test_import_self_and_export_self(home: Path, capsys):
    aug = home / "fake-augment"
    aug.mkdir()
    sess = {
        "apiKey": "fake-key",
        "tenantURL": "https://e5.api.augmentcode.com/",
        "scopes": ["read"],
    }
    (aug / "session.json").write_text(json.dumps(sess), encoding="utf-8")
    pool_path = home / "pool.json"
    assert main(["--home", str(home), "list"]) == 0
    capsys.readouterr()
    pool = json.loads(pool_path.read_text())
    pool["cursor_session_path"] = str(aug / "session.json")
    pool_path.write_text(json.dumps(pool), encoding="utf-8")

    assert main([
        "--home", str(home), "import", "--self",
        "--email", "alice@example.com",
    ]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out

    # refresh token
    sess2 = {**sess, "apiKey": "fake-key-2"}
    (aug / "session.json").write_text(json.dumps(sess2), encoding="utf-8")
    assert main([
        "--home", str(home), "import", "--self",
        "--email", "alice@example.com",
    ]) == 0
    capsys.readouterr()

    # lock current mode then export --self
    assert main(["--home", str(home), "mode", "alice@example.com"]) == 0
    capsys.readouterr()
    assert main(["--home", str(home), "export", "--self"]) == 0
    blob = capsys.readouterr().out.strip()
    env = decode_share_blob(blob)
    assert env["email"] == "alice@example.com"
    assert env["session"]["apiKey"] == "fake-key-2"
    assert "id" not in env


def test_normalize_argv_defaults_to_agent():
    from cursorpool.cli import _normalize_argv

    assert _normalize_argv([]) == ["run", "--", "agent"]
    assert _normalize_argv(["-p", "-q", "hi"]) == [
        "run", "--", "agent", "-p", "-q", "hi"
    ]
    assert _normalize_argv(["--email", "a@b.com", "--acp"]) == [
        "run", "--email", "a@b.com", "--", "agent", "--acp"
    ]
    assert _normalize_argv(["--home", "/tmp/x", "list"]) == [
        "--home", "/tmp/x", "list"
    ]
    assert _normalize_argv(["run", "--", "agent", "-p", "x"]) == [
        "run", "--", "agent", "-p", "x"
    ]
    # already starts with agent — do not double
    assert _normalize_argv(["agent", "-p", "x"]) == [
        "run", "--", "agent", "-p", "x"
    ]
    assert _normalize_argv(["--help"]) == ["--help"]
    assert _normalize_argv(["--version"]) == ["--version"]


def test_machine_api_commands_do_not_fall_through_to_agent():
    from cursorpool.cli import _normalize_argv

    assert _normalize_argv(["stats", "--json"]) == ["stats", "--json"]
    assert _normalize_argv([
        "update", "alice@example.com", "--weight", "2",
    ]) == ["update", "alice@example.com", "--weight", "2"]


def test_mode_command_does_not_fall_through_to_agent():
    from cursorpool.cli import _normalize_argv

    assert _normalize_argv(["mode"]) == ["mode"]
    assert _normalize_argv(["mode", "alice@example.com"]) == [
        "mode",
        "alice@example.com",
    ]


def test_mode_command_queries_and_persists_selection(
    home: Path, two_account_pool, capsys
):
    from cursorpool.pool import load_pool

    assert main(["--home", str(home), "mode"]) == 0
    assert capsys.readouterr().out == "auto\n"

    assert main(["--home", str(home), "mode", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "auto",
        "email": None,
    }

    assert main([
        "--home",
        str(home),
        "mode",
        "bob",
        "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "action": "mode",
        "mode": "locked",
        "email": "bob@acme.com",
    }
    assert load_pool(home).locked_email == "bob@acme.com"

    assert main(["--home", str(home), "mode", "auto"]) == 0
    assert capsys.readouterr().out == "auto\n"
    assert load_pool(home).locked_email is None


@pytest.mark.parametrize("command", ["next", "use"])
def test_removed_selection_commands_point_to_mode(
    command: str,
    home: Path,
    two_account_pool,
    capsys,
    monkeypatch,
):
    from cursorpool import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_usage_map", lambda *args, **kwargs: {})

    assert main(["--home", str(home), command]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"'{command}' was removed" in captured.err
    assert "cursorpool mode" in captured.err


def test_locked_run_skips_analytics_and_disables_failover(
    home: Path,
    two_account_pool,
    monkeypatch,
):
    from cursorpool import cli as cli_mod
    from cursorpool.pool import load_pool, set_mode
    from cursorpool.runner import RunResult

    set_mode(two_account_pool, "bob@acme.com", root=home)

    def unexpected_analytics(*args, **kwargs):
        raise AssertionError("locked run must not load Analytics")

    seen = {}

    def fake_run_pooled(pool, state, cmd, **kwargs):
        seen.update(kwargs)
        return RunResult(
            exit_code=0,
            account_email=kwargs["account_email"],
            attempts=1,
            failovers=0,
        )

    monkeypatch.setattr(cli_mod, "_usage_map", unexpected_analytics)
    monkeypatch.setattr(cli_mod, "run_pooled", fake_run_pooled)

    assert main(["--home", str(home), "run", "--", "fake-command"]) == 0
    assert seen["account_email"] == "bob@acme.com"
    assert seen["usage"] is None
    assert seen["allow_failover"] is False
    assert load_pool(home).locked_email == "bob@acme.com"


def test_email_override_wins_over_locked_mode(
    home: Path,
    two_account_pool,
    monkeypatch,
):
    from cursorpool import cli as cli_mod
    from cursorpool.pool import set_mode
    from cursorpool.runner import RunResult

    set_mode(two_account_pool, "bob@acme.com", root=home)
    seen = {}

    def fake_run_pooled(pool, state, cmd, **kwargs):
        seen.update(kwargs)
        return RunResult(
            exit_code=0,
            account_email=kwargs["account_email"],
            attempts=1,
            failovers=0,
        )

    def unexpected_analytics(*args, **kwargs):
        raise AssertionError("fixed override must not load Analytics")

    monkeypatch.setattr(cli_mod, "_usage_map", unexpected_analytics)
    monkeypatch.setattr(cli_mod, "run_pooled", fake_run_pooled)

    assert main([
        "--home",
        str(home),
        "run",
        "--email",
        "alice",
        "--",
        "fake-command",
    ]) == 0
    assert seen["account_email"] == "alice@acme.com"
    assert seen["allow_failover"] is False


def test_auto_run_loads_usage_and_allows_failover(
    home: Path,
    two_account_pool,
    monkeypatch,
):
    from cursorpool import cli as cli_mod
    from cursorpool.runner import RunResult

    usage = {"alice@acme.com": 10.0, "bob@acme.com": 20.0}
    seen = {}

    def fake_run_pooled(pool, state, cmd, **kwargs):
        seen.update(kwargs)
        return RunResult(
            exit_code=0,
            account_email="alice@acme.com",
            attempts=1,
            failovers=0,
        )

    monkeypatch.setattr(cli_mod, "_usage_map", lambda *args, **kwargs: usage)
    monkeypatch.setattr(cli_mod, "run_pooled", fake_run_pooled)

    assert main(["--home", str(home), "run", "--", "fake-command"]) == 0
    assert seen["account_email"] is None
    assert seen["usage"] is usage
    assert seen["allow_failover"] is True


def test_status_reports_locked_mode_and_help_omits_removed_commands(
    home: Path,
    two_account_pool,
    capsys,
    monkeypatch,
):
    from cursorpool import cli as cli_mod
    from cursorpool.cli import build_parser
    from cursorpool.pool import set_mode

    set_mode(two_account_pool, "bob@acme.com", root=home)
    monkeypatch.setattr(cli_mod, "_usage_map", lambda *args, **kwargs: {})

    assert main(["--home", str(home), "status"]) == 0
    output = capsys.readouterr().out
    assert "mode:     locked: bob@acme.com" in output
    assert "LOCKED" in output

    help_text = build_parser().format_help()
    assert "next" not in help_text
    assert "\n    use " not in help_text


def test_export_self_requires_lock_with_multiple_auto_accounts(
    home: Path,
    two_account_pool,
    capsys,
):
    assert main(["--home", str(home), "export", "--self"]) == 1
    error = capsys.readouterr().err
    assert "needs a locked account" in error
    assert "cursorpool mode" in error


def test_export_skips_analytics_when_email_given(home: Path, capsys, monkeypatch):
    from cursorpool import cli as cli_mod
    from cursorpool.session_io import write_json_atomic

    session = home / "in.json"
    write_json_atomic(
        session,
        {
            "apiKey": "tok",
            "tenantURL": "https://e5.api.augmentcode.com/",
            "scopes": [],
        },
    )
    assert main([
        "--home", str(home), "add",
        "--email", "me@x.com",
        "--session", str(session),
    ]) == 0
    capsys.readouterr()

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("analytics should not run for export with email")

    monkeypatch.setattr(cli_mod, "_usage_map", boom)
    assert main(["--home", str(home), "export", "me@x.com"]) == 0
    out = capsys.readouterr().out.strip()
    assert out  # blob printed
    assert calls["n"] == 0


def test_import_session_file(home: Path, capsys):
    from cursorpool.session_io import write_json_atomic

    sess = home / "s.json"
    write_json_atomic(
        sess,
        {
            "apiKey": "file-tok",
            "tenantURL": "https://e5.api.augmentcode.com/",
            "scopes": [],
        },
    )
    # missing email
    assert main([
        "--home", str(home), "import", "--session", str(sess),
    ]) == 1
    err = capsys.readouterr().err
    assert "--email" in err

    assert main([
        "--home", str(home), "import",
        "--session", str(sess),
        "--email", "bob@x.com",
    ]) == 0
    out = capsys.readouterr().out
    assert "bob@x.com" in out
    assert (home / "creds").exists()

    # conflict with blob
    assert main([
        "--home", str(home), "import", "eyJ",
        "--session", str(sess),
        "--email", "bob@x.com",
    ]) == 1
