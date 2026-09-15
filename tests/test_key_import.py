import io
import json
import pytest
from cursorpool.cli import main
from cursorpool.pool import load_pool, resolve_session_file
from cursorpool.session_io import load_session


@pytest.mark.parametrize('command', ['import', 'add'])
@pytest.mark.parametrize('mode', ['direct', 'prompt', 'stdin'])
def test_key_import_without_json(home, monkeypatch, capsys, command, mode):
    args = ['--home', str(home), command, '--email', 'new@example.com', '--json']
    if mode == 'direct':
        args += ['--api-key', 'fake-direct-key']
    elif mode == 'stdin':
        args += ['--api-key', '-']
        monkeypatch.setattr('sys.stdin', io.StringIO('fake-direct-key\n'))
    else:
        monkeypatch.setattr('getpass.getpass', lambda prompt: 'fake-direct-key')
    assert main(args) == 0
    path = resolve_session_file(load_pool(home).get('new@example.com'), home)
    assert load_session(path)['apiKey'] == 'fake-direct-key'
    assert path.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert json.loads(out.out)['email'] == 'new@example.com'
    assert 'fake-direct-key' not in out.out + out.err


def test_key_import_requires_force_for_duplicate(two_account_pool, home, capsys):
    args = ['--home', str(home), 'import', '--email', 'alice@acme.com', '--api-key', 'fake-replacement']
    assert main(args) == 1
    assert 'fake-replacement' not in capsys.readouterr().err
    assert main(args + ['--force']) == 0


def test_key_source_conflict_does_not_echo_key(home, capsys):
    assert main(['--home', str(home), 'import', '--email', 'new@example.com', '--api-key', 'fake-private-key', '--self']) == 1
    assert 'fake-private-key' not in capsys.readouterr().err


def test_import_help_uses_cursor_terms(capsys):
    with pytest.raises(SystemExit):
        main(['import', '--help'])
    text = capsys.readouterr().out
    assert '--api-key' in text
    assert 'session.json' not in text


def test_key_prompt_refuses_echo_fallback(home, monkeypatch, capsys):
    import getpass
    import warnings
    def prompt(_):
        warnings.warn('cannot hide input', getpass.GetPassWarning)
        pytest.fail('must not read echoed input')
    monkeypatch.setattr('getpass.getpass', prompt)
    assert main(['--home', str(home), 'import', '--email', 'new@example.com']) == 1
    assert 'stdin' in capsys.readouterr().err


def test_missing_environment_key_suggests_direct_import(home, monkeypatch, capsys):
    monkeypatch.delenv('CURSOR_API_KEY', raising=False)
    assert main(['--home', str(home), 'import', '--self', '--email', 'new@example.com']) == 1
    error = capsys.readouterr().err
    assert '--api-key' in error
    assert 'JSON' not in error


def test_add_key_preserves_requested_settings(home):
    assert main(['--home', str(home), 'add', '--email', 'new@example.com', '--api-key', 'fake-key', '--weight', '2.5', '--notes', 'test note', '--label', 'Work']) == 0
    account = load_pool(home).get('new@example.com')
    assert (account.weight, account.notes, account.label) == (2.5, 'test note', 'Work')
