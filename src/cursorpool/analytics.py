"""Per-account Cursor dashboard usage, cache provenance, and local fallback."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from cursorpool import paths
from cursorpool.pool import Pool, resolve_session_file
from cursorpool.cursor_usage import fetch_personal_usage, UsageError
from cursorpool.session_io import load_session, write_json_atomic

HttpGetter = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class UsageResult:
    """Usage map plus cache provenance for machine-readable reporting."""

    by_id: dict[str, float] | None
    cache: dict[str, Any] | None
    refresh_attempted: bool
    refresh_succeeded: bool
    errors: list[str]


def default_date_window(today: date | None = None) -> tuple[str, str]:
    """Current UTC calendar month, inclusive of today."""
    today = today or datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


def load_usage_cache(root: Path | None = None) -> dict[str, Any] | None:
    p = paths.usage_cache_path(root)
    if not p.is_file():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_usage_cache(data: dict[str, Any], root: Path | None = None) -> None:
    root = paths.ensure_layout(root)
    write_json_atomic(paths.usage_cache_path(root), data, mode=0o600)


def cache_is_fresh(cache: dict[str, Any] | None, ttl_seconds: int, now: float | None = None) -> bool:
    if not cache:
        return False
    fetched = cache.get("fetched_at")
    if fetched is None:
        return False
    now = time.time() if now is None else now
    return (now - float(fetched)) < ttl_seconds


USAGE_UNAVAILABLE = (
    "Cursor usage is unavailable without a dashboard session; use "
    "cursorpool import --usage-session --email EMAIL. Selection uses local counts."
)
MAX_STALE_SECONDS = 86_400


def _usage_tokens(pool: Pool, root: Path | None) -> tuple[dict[str, str], list[str]]:
    tokens: dict[str, str] = {}
    missing: list[str] = []
    for account in pool.enabled_accounts():
        try:
            token = load_session(resolve_session_file(account, root)).get("usageSessionToken")
        except (OSError, ValueError):
            token = None
        if token:
            tokens[account.email] = token
        else:
            missing.append(f"{account.email}: usage session unavailable; import --usage-session")
    return tokens, missing


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_rows(root: Path | None, tokens: dict[str, str], now: float) -> dict[str, Any]:
    try:
        cache = load_usage_cache(root)
    except (OSError, ValueError):
        return {}
    if not isinstance(cache, dict) or cache.get("provider") != "cursor-dashboard" or cache.get("unit") != "USD":
        return {}
    accounts = cache.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    rows: dict[str, Any] = {}
    for email, token in tokens.items():
        row = accounts.get(email)
        if not isinstance(row, dict) or row.get("credential_fingerprint") != _fingerprint(token):
            continue
        try:
            if not all(isinstance(row.get(key), str) for key in ("billing_cycle_start", "billing_cycle_end")):
                continue
            fetched = float(row["fetched_at"])
            start = float(row["billing_cycle_start_ts"])
            end = float(row["billing_cycle_end_ts"])
            used = float(row["total_used_usd"])
            if (
                all(math.isfinite(value) for value in (fetched, start, end, used))
                and 0 <= now - fetched <= MAX_STALE_SECONDS
                and start <= now < end
                and used >= 0
            ):
                rows[email] = {**row, "fetched_at": fetched, "total_used_usd": used}
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return rows


def _usage_cache(
    rows: dict[str, Any], errors: list[str], *, now: float,
    fetches_ok: int, queried: int = 0, today: date | None = None,
) -> dict[str, Any]:
    start, end = default_date_window(today)
    if rows:
        starts = {row["billing_cycle_start"][:10] for row in rows.values()}
        ends = {row["billing_cycle_end"][:10] for row in rows.values()}
        start = next(iter(starts)) if len(starts) == 1 else None
        end = next(iter(ends)) if len(ends) == 1 else None
    return {
        "provider": "cursor-dashboard",
        "unit": "USD",
        "fetched_at": min((row["fetched_at"] for row in rows.values()), default=now),
        "start_date": start,
        "end_date": end,
        "accounts": rows,
        "by_id": {email: row["total_used_usd"] for email, row in rows.items()},
        "errors": errors,
        "fetches_ok": fetches_ok,
        "tenants_queried": 0,
        "accounts_queried": queried,
    }


def refresh_usage(
    pool: Pool,
    *,
    root: Path | None = None,
    http_get: HttpGetter | None = None,
    now: float | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch each configured account; retain recent data only on transient errors."""
    now = time.time() if now is None else now
    tokens, missing = _usage_tokens(pool, root)
    previous = _cached_rows(root, tokens, now)
    rows: dict[str, Any] = {}
    errors = list(missing) if tokens else [USAGE_UNAVAILABLE]
    successes = 0
    for email, token in tokens.items():
        try:
            data = fetch_personal_usage(email, token, http_get=http_get)
            if not data["billing_cycle_start_ts"] <= now < data["billing_cycle_end_ts"]:
                raise UsageError("Cursor returned an expired or future billing cycle")
            rows[email] = {**data, "fetched_at": now, "credential_fingerprint": _fingerprint(token)}
            successes += 1
        except UsageError as exc:
            errors.append(f"{email}: {exc}")
            if exc.allow_stale and email in previous:
                rows[email] = previous[email]
                errors.append(f"{email}: using cached usage after a temporary failure")
        except Exception:
            # Injected clients and unexpected errors can include secrets in messages.
            errors.append(f"{email}: usage request failed")
    cache = _usage_cache(rows, errors, now=now, fetches_ok=successes, queried=len(tokens), today=today)
    save_usage_cache(cache, root)
    return cache


def get_usage_result(
    pool: Pool,
    *,
    root: Path | None = None,
    force: bool = False,
    http_get: HttpGetter | None = None,
    now: float | None = None,
    refresh_if_stale: bool = True,
) -> UsageResult:
    """Use a credential-bound, current-cycle cache before querying the dashboard."""
    now = time.time() if now is None else now
    tokens, missing = _usage_tokens(pool, root)
    if not tokens:
        cache = refresh_usage(pool, root=root, http_get=http_get, now=now) if force else None
        return UsageResult(None, cache, force, False, [USAGE_UNAVAILABLE])
    rows = _cached_rows(root, tokens, now)
    fresh = len(rows) == len(tokens) and all(
        now - row["fetched_at"] < pool.usage_cache_ttl_seconds for row in rows.values()
    )
    if not force and (fresh or not refresh_if_stale):
        cache = _usage_cache(rows, missing, now=now, fetches_ok=len(rows))
        if rows and not fresh:
            cache["errors"].append("using stale cached usage; refresh was not requested")
        return UsageResult(cache["by_id"] or None, cache, False, False, cache["errors"])
    cache = refresh_usage(pool, root=root, http_get=http_get, now=now)
    return UsageResult(cache["by_id"] or None, cache, True, cache["fetches_ok"] > 0, cache["errors"])


def get_fresh_usage(
    pool: Pool,
    *,
    root: Path | None = None,
    force: bool = False,
    http_get: HttpGetter | None = None,
    now: float | None = None,
    refresh_if_stale: bool = True,
) -> dict[str, float] | None:
    """Compatibility wrapper returning per-account USD, or None when unavailable."""
    return get_usage_result(
        pool, root=root, force=force, http_get=http_get, now=now,
        refresh_if_stale=refresh_if_stale,
    ).by_id


def public_usage_details(cache: dict[str, Any] | None, emails: Iterable[str]) -> dict[str, Any]:
    """Expose only billing fields, excluding credential fingerprints and tokens."""
    if not cache or cache.get("provider") != "cursor-dashboard":
        return {}
    fields = (
        "plan_used_usd", "on_demand_used_usd", "total_used_usd",
        "plan_limit_usd", "plan_remaining_usd", "billing_cycle_start",
        "billing_cycle_end", "fetched_at",
    )
    accounts = cache.get("accounts") or {}
    return {
        email: {key: accounts[email].get(key) for key in fields}
        for email in emails if email in accounts
    }
