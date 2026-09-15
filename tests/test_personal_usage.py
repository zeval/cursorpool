from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from cursorpool import analytics
from cursorpool.cli import main
from cursorpool.pool import load_pool, resolve_session_file
from cursorpool.session_io import (
    apply_env, build_share_envelope, load_session, validate_session, write_session,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()


def summary(used=1234, on_demand=50):
    return {
        "billingCycleStart": "2026-09-05T00:00:00Z",
        "billingCycleEnd": "2026-10-05T00:00:00Z",
        "individualUsage": {
            "plan": {"enabled": True, "used": used, "limit": 2000, "remaining": 766, "totalPercentUsed": 61.7},
            "onDemand": {"enabled": True, "used": on_demand, "limit": 1000},
        },
    }


def attach(pool, home, email="alice@acme.com", token="fake-dashboard-alice"):
    path = resolve_session_file(pool.get(email), home)
    session = json.loads(path.read_text())
    # Write the future credential shape directly so API tests exercise fetching,
    # independently of the validator under test.
    path.write_text(json.dumps({**session, "usageSessionToken": token}))


def http_fake(calls, *, email="alice@acme.com", payload=None):
    def get(url, headers):
        calls.append((url, headers))
        if url.endswith("/api/auth/me"):
            return {"email": email}
        assert url == "https://cursor.com/api/usage-summary"
        return summary() if payload is None else payload
    return get


def test_optional_usage_session_is_saved_but_never_exported_or_injected():
    session = {"apiKey": "fake-cli-key", "usageSessionToken": "fake-dashboard-token"}
    assert validate_session(session) == session
    assert apply_env(session, {}) == {"CURSOR_API_KEY": "fake-cli-key"}
    envelope = build_share_envelope(email="alice@example.com", session=session)
    assert envelope["session"] == {"apiKey": "fake-cli-key"}


@pytest.mark.parametrize("token", ["", "   ", 42, True, "fake; other=cookie", "fake\r\nheader", "fake token"])
def test_invalid_usage_session_is_rejected_without_echoing(token):
    with pytest.raises(ValueError) as raised:
        validate_session({"apiKey": "fake-cli-key", "usageSessionToken": token})
    assert "fake" not in str(raised.value)


def test_attach_usage_session_to_existing_account(two_account_pool, home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("fake-dashboard-token\n"))
    assert main(["--home", str(home), "import", "--usage-session", "-", "--email", "alice@acme.com", "--json"]) == 0
    account = load_pool(home).get("alice@acme.com")
    path = resolve_session_file(account, home)
    session = load_session(path)
    assert session["apiKey"] == "tok-alice"
    assert session["usageSessionToken"] == "fake-dashboard-token"
    assert path.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr()
    assert json.loads(out.out)["email"] == "alice@acme.com"
    assert "fake-dashboard" not in out.out + out.err


def test_fetch_personal_usage_checks_identity_and_converts_cents(two_account_pool, home):
    attach(two_account_pool, home)
    two_account_pool.accounts[1].enabled = False
    calls = []
    cache = analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake(calls), now=NOW)
    assert cache["by_id"] == {"alice@acme.com": 12.84}
    assert cache["provider"] == "cursor-dashboard"
    assert cache["unit"] == "USD"
    details = cache["accounts"]["alice@acme.com"]
    assert details["plan_used_usd"] == 12.34
    assert details["on_demand_used_usd"] == 0.5
    assert details["billing_cycle_end"] == "2026-10-05T00:00:00Z"
    assert len(calls) == 2
    assert all(headers["Cookie"] == "WorkosCursorSessionToken=fake-dashboard-alice" for _, headers in calls)
    assert "tok-alice" not in json.dumps(calls)
    assert "fake-dashboard-alice" not in json.dumps(cache)


def test_wrong_session_identity_is_not_used(two_account_pool, home):
    attach(two_account_pool, home)
    calls = []
    cache = analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake(calls, email="other@example.com"), now=NOW)
    assert cache["by_id"] == {}
    assert len(calls) == 1
    assert "identity" in " ".join(cache["errors"])


def test_missing_usage_fields_are_not_reported_as_zero(two_account_pool, home):
    attach(two_account_pool, home)
    cache = analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([], payload={}), now=NOW)
    assert cache["by_id"] == {}
    assert cache["fetches_ok"] == 0
    assert "unsupported" in " ".join(cache["errors"])


def single_account(pool, home):
    pool.accounts[1].enabled = False
    attach(pool, home)


def test_fresh_cache_skips_network(two_account_pool, home):
    single_account(two_account_pool, home)
    calls = []
    first = analytics.get_usage_result(two_account_pool, root=home, http_get=http_fake(calls), now=NOW)
    assert first.by_id == {"alice@acme.com": 12.84}
    assert first.refresh_succeeded
    second = analytics.get_usage_result(two_account_pool, root=home, http_get=http_fake(calls), now=NOW + 10)
    assert second.by_id == first.by_id
    assert not second.refresh_attempted
    assert len(calls) == 2


def test_expired_cache_refreshes(two_account_pool, home):
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    calls = []
    result = analytics.get_usage_result(two_account_pool, root=home, http_get=http_fake(calls, payload=summary(used=1500)), now=NOW + 301)
    assert result.by_id == {"alice@acme.com": 15.5}
    assert len(calls) == 2


def test_transient_failure_preserves_cache_timestamp(two_account_pool, home):
    from cursorpool.cursor_usage import UsageError
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    def unavailable(*args):
        raise UsageError("Cursor usage HTTP 503", allow_stale=True)
    result = analytics.get_usage_result(two_account_pool, root=home, http_get=unavailable, force=True, now=NOW + 301)
    assert result.by_id == {"alice@acme.com": 12.84}
    assert not result.refresh_succeeded
    assert result.cache["fetched_at"] == NOW
    assert result.errors


def test_auth_failure_drops_cached_usage(two_account_pool, home):
    from cursorpool.cursor_usage import UsageError
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    def rejected(*args):
        raise UsageError("dashboard session expired or rejected")
    result = analytics.get_usage_result(two_account_pool, root=home, http_get=rejected, force=True, now=NOW + 301)
    assert result.by_id is None


def test_changed_credential_cannot_reuse_old_cache(two_account_pool, home):
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    attach(two_account_pool, home, token="fake-different-session")
    calls = []
    result = analytics.get_usage_result(two_account_pool, root=home, http_get=http_fake(calls, email="different@example.com"), now=NOW + 10)
    assert result.by_id is None
    assert len(calls) == 1


def test_previous_billing_cycle_cannot_drive_ranking(two_account_pool, home):
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    next_cycle = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
    result = analytics.get_usage_result(two_account_pool, root=home, http_get=http_fake([]), now=next_cycle)
    assert result.by_id is None


def test_old_cache_without_network_refresh_is_bounded(two_account_pool, home):
    single_account(two_account_pool, home)
    analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    result = analytics.get_usage_result(two_account_pool, root=home, refresh_if_stale=False, now=NOW + 86401)
    assert result.by_id is None


def test_partial_usage_is_not_compared_to_local_counts(two_account_pool, home):
    from cursorpool.select import rank_accounts
    from cursorpool.state import empty_state
    state = empty_state()
    state.for_account("alice@acme.com").local_uses = 1
    state.for_account("bob@acme.com").local_uses = 3
    ranked = rank_accounts(two_account_pool, state, {"alice@acme.com": 100.0})
    assert [r.account.email for r in ranked] == ["alice@acme.com", "bob@acme.com"]
    assert all(r.source == "local" for r in ranked)


def test_dashboard_labels_dollars_and_preserves_billing_details(two_account_pool, home):
    from cursorpool.usage_view import render_usage_dashboard, usage_report_payload
    from cursorpool.state import empty_state
    single_account(two_account_pool, home)
    cache = analytics.refresh_usage(two_account_pool, root=home, http_get=http_fake([]), now=NOW)
    payload = usage_report_payload(two_account_pool, empty_state(), cache["by_id"], cache, now=NOW)
    assert payload["usage_unit"] == "USD"
    assert payload["account_usage"]["alice@acme.com"]["plan_used_usd"] == 12.34
    output = render_usage_dashboard(two_account_pool, empty_state(), cache["by_id"], cache, now=NOW, color=False, width=88)
    assert "$12.84" in output
    assert "12.84 credits" not in output
    assert "USD" in output


def test_list_labels_retrieved_amounts_in_usd(two_account_pool, home, monkeypatch, capsys):
    monkeypatch.setattr("cursorpool.analytics.time.time", lambda: NOW)
    single_account(two_account_pool, home)
    from cursorpool.pool import save_pool
    save_pool(two_account_pool, home)
    monkeypatch.setattr("cursorpool.cursor_usage.http_get_json", http_fake([]))
    assert main(["--home", str(home), "list"]) == 0
    output = capsys.readouterr().out
    assert "USED USD" in output
    assert "12.84" in output


def test_transport_does_not_follow_redirects_or_echo_http_bodies(monkeypatch):
    import urllib.error
    from cursorpool.cursor_usage import http_get_json, UsageError
    observed = []
    class Opener:
        def open(self, request, timeout):
            assert timeout == 10
            raise urllib.error.HTTPError(request.full_url, 302, "fake-sensitive-value", {}, io.BytesIO(b"fake-secret-body"))
    def build(*handlers):
        observed.extend(handlers)
        return Opener()
    monkeypatch.setattr("urllib.request.build_opener", build)
    with pytest.raises(UsageError) as raised:
        http_get_json("https://cursor.com/api/auth/me", {"Cookie": "fake-sensitive-cookie"})
    assert "302" in str(raised.value)
    assert "fake" not in str(raised.value)
    assert observed[0].redirect_request(None, None, 302, None, None, "https://example.invalid") is None


def test_transport_rejects_an_untrusted_endpoint():
    from cursorpool.cursor_usage import http_get_json, UsageError
    with pytest.raises(UsageError):
        http_get_json("https://example.invalid/api/auth/me", {"Cookie": "fake-sensitive-cookie"})


@pytest.mark.parametrize("code,stale", [(401, False), (403, False), (429, True), (500, True)])
def test_transport_classifies_safe_errors(monkeypatch, code, stale):
    import urllib.error
    from cursorpool.cursor_usage import http_get_json, UsageError
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError("https://cursor.com/api/auth/me", code, "fake-secret", {}, io.BytesIO(b"fake-secret"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(UsageError) as raised:
        http_get_json("https://cursor.com/api/auth/me", {})
    assert raised.value.allow_stale is stale
    assert "fake-secret" not in str(raised.value)


def test_stats_exposes_usd_provenance_without_session_material(two_account_pool, home, monkeypatch):
    from cursorpool.reporting import collect_stats
    from cursorpool.pool import save_pool
    single_account(two_account_pool, home)
    save_pool(two_account_pool, home)
    monkeypatch.setattr("cursorpool.cursor_usage.http_get_json", http_fake([]))
    snapshot = collect_stats(home, now=NOW)
    assert snapshot["usage"]["unit"] == "USD"
    assert snapshot["usage"]["account_usage"]["alice@acme.com"]["total_used_usd"] == 12.84
    encoded = json.dumps(snapshot)
    assert "fake-dashboard" not in encoded
    assert "credential_fingerprint" not in encoded


@pytest.mark.parametrize("field,value", [("billing_cycle_start", None), ("fetched_at", "invalid")])
def test_malformed_cached_row_is_ignored(two_account_pool, home, field, value):
    pool = two_account_pool
    pool.accounts[1].enabled = False
    attach(pool, home)
    cache = analytics.refresh_usage(pool, root=home, http_get=http_fake([]), now=NOW)
    cache["accounts"]["alice@acme.com"][field] = value
    analytics.save_usage_cache(cache, home)
    result = analytics.get_usage_result(pool, root=home, now=NOW + 10, refresh_if_stale=False)
    assert result.by_id is None


def test_hidden_prompt_preserves_registry(two_account_pool, home, monkeypatch, capsys):
    before = load_pool(home).to_dict()
    monkeypatch.setattr("getpass.getpass", lambda prompt: "fake-hidden-session")
    assert main(["--home", str(home), "import", "--usage-session", "--email", "alice@acme.com"]) == 0
    assert load_pool(home).to_dict() == before
    account = load_pool(home).get("alice@acme.com")
    assert load_session(resolve_session_file(account, home))["usageSessionToken"] == "fake-hidden-session"
    out = capsys.readouterr()
    assert "fake-hidden-session" not in out.out + out.err


def test_hidden_prompt_refuses_echo_fallback(two_account_pool, home, monkeypatch, capsys):
    import getpass
    import warnings

    def unsafe_prompt(prompt):
        warnings.warn("cannot hide input", getpass.GetPassWarning)
        pytest.fail("must stop before echoing input")

    monkeypatch.setattr("getpass.getpass", unsafe_prompt)
    assert main(["--home", str(home), "import", "--usage-session", "--email", "alice@acme.com"]) != 0
    assert "no secure terminal" in capsys.readouterr().err
