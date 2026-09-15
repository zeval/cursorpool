from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from cursorpool import pool as pool_mod
from cursorpool.pool import load_pool, pool_from_dict, update_account


def test_v2_active_email_migrates_to_v3_auto_mode():
    pool = pool_from_dict(
        {
            "version": 2,
            "active_email": "alice@acme.com",
            "accounts": [
                {
                    "email": "alice@acme.com",
                    "session_path": "creds/alice.json",
                }
            ],
        }
    )

    assert pool.version == 3
    assert pool.locked_email is None


def test_set_mode_persists_lock_and_auto(home: Path, two_account_pool):
    pool_mod.set_mode(two_account_pool, "bob", root=home)

    assert two_account_pool.locked_email == "bob@acme.com"
    assert load_pool(home).locked_email == "bob@acme.com"

    pool_mod.set_mode(two_account_pool, "auto", root=home)

    assert two_account_pool.locked_email is None
    assert load_pool(home).locked_email is None


def test_v3_pool_serializes_locked_email_without_legacy_active_email(
    home: Path, two_account_pool
):
    pool_mod.set_mode(two_account_pool, "alice@acme.com", root=home)

    raw = json.loads((home / "pool.json").read_text(encoding="utf-8"))
    assert raw["locked_email"] == "alice@acme.com"
    assert "active_email" not in raw


def test_resolve_locked_account_rejects_manually_disabled_lock(
    home: Path, two_account_pool
):
    pool_mod.set_mode(two_account_pool, "bob@acme.com", root=home)
    two_account_pool.get("bob@acme.com").enabled = False

    with pytest.raises(ValueError, match="locked account .* disabled"):
        pool_mod.resolve_locked_account(two_account_pool)

    pool_mod.set_mode(two_account_pool, "auto", root=home)
    assert pool_mod.resolve_locked_account(two_account_pool) is None


def test_auto_mode_can_repair_manually_stale_lock(home: Path, two_account_pool):
    two_account_pool.locked_email = "missing@acme.com"
    pool_mod.save_pool(two_account_pool, home)

    with pytest.raises(ValueError, match="locked account .* missing"):
        pool_mod.resolve_locked_account(load_pool(home))

    pool_mod.set_mode(two_account_pool, "auto", root=home)
    assert load_pool(home).locked_email is None


def test_set_mode_rejects_disabled_account_without_changing_mode(
    home: Path, two_account_pool
):
    two_account_pool.get("bob@acme.com").enabled = False

    with pytest.raises(ValueError, match="cannot lock disabled account"):
        pool_mod.set_mode(two_account_pool, "bob@acme.com", root=home)

    assert two_account_pool.locked_email is None
    assert load_pool(home).locked_email is None


def test_update_account_refuses_to_disable_locked_account(
    home: Path, two_account_pool
):
    pool_mod.set_mode(two_account_pool, "alice@acme.com", root=home)

    with pytest.raises(ValueError, match="cursorpool mode auto"):
        update_account(
            two_account_pool,
            "alice@acme.com",
            enabled=False,
            root=home,
        )

    reloaded = load_pool(home)
    assert reloaded.locked_email == "alice@acme.com"
    assert reloaded.get("alice@acme.com").enabled is True


def test_remove_account_refuses_locked_account_without_deleting_credentials(
    home: Path, two_account_pool
):
    pool_mod.set_mode(two_account_pool, "bob@acme.com", root=home)
    credential = pool_mod.resolve_session_file(
        two_account_pool.get("bob@acme.com"), home
    )

    with pytest.raises(ValueError, match="cursorpool mode auto"):
        pool_mod.remove_account(two_account_pool, "bob@acme.com", root=home)

    assert credential.is_file()
    reloaded = load_pool(home)
    assert reloaded.locked_email == "bob@acme.com"
    assert reloaded.get("bob@acme.com").email == "bob@acme.com"


def test_update_account_persists_fields(home: Path, two_account_pool):
    account = update_account(
        two_account_pool,
        "alice@acme.com",
        enabled=False,
        weight=2.5,
        root=home,
    )

    assert account.enabled is False
    assert account.weight == 2.5

    reloaded = load_pool(home)
    assert reloaded.get("alice@acme.com").enabled is False
    assert reloaded.get("alice@acme.com").weight == 2.5

    raw = json.loads((home / "pool.json").read_text(encoding="utf-8"))
    assert "active_email" not in raw


@pytest.mark.parametrize("weight", [0, -1, math.inf, -math.inf, math.nan])
def test_update_account_rejects_non_positive_or_non_finite_weight(
    home: Path, two_account_pool, weight: float
):
    with pytest.raises(ValueError, match="finite number > 0"):
        update_account(two_account_pool, "alice@acme.com", weight=weight, root=home)


def test_update_account_requires_a_change(home: Path, two_account_pool):
    with pytest.raises(ValueError, match="enabled or weight"):
        update_account(two_account_pool, "alice@acme.com", root=home)


def test_update_account_rejects_non_boolean_enabled(home: Path, two_account_pool):
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        update_account(two_account_pool, "alice@acme.com", enabled=1, root=home)
