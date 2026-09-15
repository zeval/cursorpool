from datetime import date

from cursorpool.analytics import (
    USAGE_UNAVAILABLE, cache_is_fresh, default_date_window, get_fresh_usage,
    get_usage_result, load_usage_cache, refresh_usage, save_usage_cache,
)


def test_default_window_uses_current_calendar_month():
    assert default_date_window(date(2026, 7, 29)) == ("2026-07-01", "2026-07-29")
    assert default_date_window(date(2026, 8, 1)) == ("2026-08-01", "2026-08-01")


def test_cache_freshness():
    assert not cache_is_fresh(None, 300, now=1000)
    assert not cache_is_fresh({}, 300, now=1000)
    assert cache_is_fresh({"fetched_at": 900}, 300, now=1000)
    assert not cache_is_fresh({"fetched_at": 600}, 300, now=1000)


def test_refresh_reports_unavailable_without_network(two_account_pool, home):
    def forbidden_get(*args):
        raise AssertionError("No usage API should receive a personal Cursor key")
    cache = refresh_usage(two_account_pool, root=home, http_get=forbidden_get, now=1000, today=date(2026, 9, 14))
    assert cache["by_id"] == {}
    assert cache["fetches_ok"] == 0
    assert cache["tenants_queried"] == 0
    assert cache["errors"] == [USAGE_UNAVAILABLE]
    assert cache["start_date"] == "2026-09-01"
    assert load_usage_cache(home) == cache
    assert (home / "cache/usage.json").stat().st_mode & 0o777 == 0o600


def test_copied_augment_cache_cannot_drive_cursor_selection(two_account_pool, home):
    save_usage_cache({"by_id": {"alice@acme.com": 1000}, "fetches_ok": 1, "fetched_at": 999}, home)
    assert get_fresh_usage(two_account_pool, root=home, now=1000) is None
    result = get_usage_result(two_account_pool, root=home, now=1000)
    assert not result.refresh_attempted
    assert not result.refresh_succeeded
    assert result.errors == [USAGE_UNAVAILABLE]


def test_forced_refresh_preserves_failure_metadata(two_account_pool, home):
    result = get_usage_result(two_account_pool, root=home, force=True, now=1000)
    assert result.by_id is None
    assert result.refresh_attempted
    assert not result.refresh_succeeded
    assert result.cache["fetched_at"] == 1000
    assert result.errors == [USAGE_UNAVAILABLE]


def test_normal_selection_does_not_write_usage_cache(two_account_pool, home):
    assert get_fresh_usage(two_account_pool, root=home) is None
    assert load_usage_cache(home) is None
