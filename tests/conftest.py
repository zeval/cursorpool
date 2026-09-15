from __future__ import annotations

import json
from pathlib import Path

import pytest

from cursorpool.pool import Account, Pool, save_pool
from cursorpool.session_io import write_json_atomic
from cursorpool.state import empty_state, save_state


def _session(token: str = "tok-a", tenant: str = "https://e5.api.augmentcode.com/") -> dict:
    return {
        "apiKey": token,
        "tenantURL": tenant,
        "scopes": ["read", "write"],
    }


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cursorpool-home"
    root.mkdir()
    monkeypatch.setenv("CURSORPOOL_HOME", str(root))
    return root


@pytest.fixture
def two_account_pool(home: Path) -> Pool:
    pool = Pool()
    for email, tok in (
        ("alice@acme.com", "tok-alice"),
        ("bob@acme.com", "tok-bob"),
    ):
        from cursorpool.pool import creds_filename
        rel = f"creds/{creds_filename(email)}"
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(dest, _session(tok), mode=0o600)
        pool.accounts.append(
            Account(
                email=email,
                session_path=rel,
                tenant_url="https://e5.api.augmentcode.com/",
            )
        )
    pool.cursor_session_path = str(home / "fake-augment" / "session.json")
    save_pool(pool, home)
    save_state(empty_state(), home)
    return pool


@pytest.fixture
def credit_records() -> list[dict]:
    return [
        {"user_email": "alice@acme.com", "credits_consumed": 4200},
        {"user_email": "bob@acme.com", "credits_consumed": 1850},
        {"service_account_name": "ci-bot", "credits_consumed": 900},
    ]


@pytest.fixture(autouse=True)
def isolate_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("CURSOR_AUTH_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def forbid_real_usage_network(monkeypatch: pytest.MonkeyPatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must inject Cursor HTTP responses; real network is forbidden")
    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
