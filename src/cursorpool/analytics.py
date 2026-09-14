"""Explicit local-only usage fallback with the inherited reporting interface."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cursorpool import paths
from cursorpool.pool import Pool
from cursorpool.session_io import write_json_atomic

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
    "Cursor credit usage is unavailable in this version; account selection uses "
    "local session counts. See https://cursor.com/dashboard for actual usage."
)


def refresh_usage(
    pool: Pool,
    *,
    root: Path | None = None,
    http_get: HttpGetter | None = None,
    now: float | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Record unavailable remote usage without sending credentials to any API."""
    start, end = default_date_window(today)
    cache = {
        "fetched_at": time.time() if now is None else now,
        "start_date": start,
        "end_date": end,
        "by_id": {},
        "errors": [USAGE_UNAVAILABLE],
        "fetches_ok": 0,
        "tenants_queried": 0,
    }
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
    """Report local-only selection; never trust a copied Augment credit cache."""
    cache = refresh_usage(pool, root=root, now=now) if force else None
    return UsageResult(None, cache, force, False, [USAGE_UNAVAILABLE])


def get_fresh_usage(
    pool: Pool,
    *,
    root: Path | None = None,
    force: bool = False,
    http_get: HttpGetter | None = None,
    now: float | None = None,
    refresh_if_stale: bool = True,
) -> dict[str, float] | None:
    """Compatibility wrapper returning only the usage map for ranking."""
    return get_usage_result(
        pool,
        root=root,
        force=force,
        http_get=http_get,
        now=now,
        refresh_if_stale=refresh_if_stale,
    ).by_id
