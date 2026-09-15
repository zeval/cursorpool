"""Cursor's session-authenticated dashboard API (not a public API-key contract).

Endpoint/field behavior verified against CodexBar's Cursor provider; this module
is an independent standard-library implementation. No browser stores are read.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

HttpGetter = Callable[[str, dict[str, str]], dict[str, Any]]
BASE_URL = "https://cursor.com"


class UsageError(RuntimeError):
    """A safe public message; never include response bodies or credentials."""

    def __init__(self, message: str, *, allow_stale: bool = False):
        super().__init__(message)
        self.allow_stale = allow_stale


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    if url not in {BASE_URL + "/api/auth/me", BASE_URL + "/api/usage-summary"}:
        raise UsageError("unsupported Cursor usage endpoint")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            raise UsageError("dashboard session expired or rejected; re-import --usage-session") from None
        raise UsageError(f"Cursor usage HTTP {code}", allow_stale=code == 429 or code >= 500) from None
    except (urllib.error.URLError, OSError):
        raise UsageError("Cursor usage network error", allow_stale=True) from None
    except (ValueError, UnicodeError):
        raise UsageError("unsupported Cursor usage response") from None
    if not isinstance(payload, dict):
        raise UsageError("unsupported Cursor usage response")
    return payload


def _amount(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError("unsupported Cursor usage response: missing numeric amount")
    if not math.isfinite(value) or value < 0:
        raise UsageError("unsupported Cursor usage response: invalid amount")
    return float(value) / 100.0


def _cycle_date(value: Any) -> tuple[str, float]:
    if not isinstance(value, str):
        raise UsageError("unsupported Cursor usage response: missing billing cycle")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return value, parsed.timestamp()
    except ValueError:
        raise UsageError("unsupported Cursor usage response: invalid billing cycle") from None


def fetch_personal_usage(
    email: str, session_token: str, *, http_get: HttpGetter | None = None,
) -> dict[str, Any]:
    get = http_get or http_get_json
    headers = {"Accept": "application/json", "Cookie": f"WorkosCursorSessionToken={session_token}"}
    identity = get(BASE_URL + "/api/auth/me", headers)
    found_email = identity.get("email")
    if not isinstance(found_email, str) or found_email.strip().lower() != email.lower():
        raise UsageError("dashboard session identity does not match the pool email")
    payload = get(BASE_URL + "/api/usage-summary", headers)
    individual = payload.get("individualUsage")
    if not isinstance(individual, dict) or not isinstance(individual.get("plan"), dict):
        raise UsageError("unsupported Cursor usage response: personal plan data missing")
    plan = individual["plan"]
    plan_used = _amount(plan.get("used"))
    on_demand = individual.get("onDemand")
    if on_demand is None:
        extra_used = 0.0
    elif isinstance(on_demand, dict):
        extra_used = _amount(on_demand.get("used", 0) if on_demand.get("enabled") is False else on_demand.get("used"))
    else:
        raise UsageError("unsupported Cursor usage response: on-demand data invalid")
    start, start_ts = _cycle_date(payload.get("billingCycleStart"))
    end, end_ts = _cycle_date(payload.get("billingCycleEnd"))
    if end_ts <= start_ts:
        raise UsageError("unsupported Cursor usage response: invalid billing cycle")
    return {
        "plan_used_usd": plan_used,
        "on_demand_used_usd": extra_used,
        "total_used_usd": round(plan_used + extra_used, 8),
        "plan_limit_usd": _amount(plan["limit"]) if plan.get("limit") is not None else None,
        "plan_remaining_usd": _amount(plan["remaining"]) if plan.get("remaining") is not None else None,
        "billing_cycle_start": start,
        "billing_cycle_end": end,
        "billing_cycle_start_ts": start_ts,
        "billing_cycle_end_ts": end_ts,
    }
