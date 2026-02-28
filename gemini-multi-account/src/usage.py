"""
Usage checker — query Gemini API quota for each account.

Gemini CLI uses Google AI Studio (generativelanguage.googleapis.com).
There is no official public quota endpoint, but we can:
  1. Make a lightweight API call to detect 429 (rate-limited)
  2. Parse the error body for quotaResetDelay
  3. Cache results in meta.json (same pattern as Antigravity's quota.rs)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

from .accounts import AccountMeta, load_creds, save_meta
from .oauth import refresh_access_token, get_user_info
from .rate_limit import RateLimitTracker, RateLimitReason


# Lightweight model to probe — Flash is cheapest
PROBE_MODEL = "gemini-1.5-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com"


@dataclass
class UsageResult:
    alias: str
    email: str
    status: str          # "ok" | "rate_limited" | "error" | "expired"
    remaining_pct: Optional[int] = None
    reset_in_sec: Optional[float] = None
    tier: Optional[str] = None
    error: Optional[str] = None


def check_account(meta: AccountMeta) -> UsageResult:
    """
    Probe a single account's quota status.
    Updates meta.json with the result.
    """
    creds = load_creds(meta.alias)
    if creds is None:
        return UsageResult(meta.alias, meta.email, "error", error="No credentials found")

    proxy = meta.proxy or None
    proxies = {"https": proxy, "http": proxy} if proxy else None

    # Refresh if expired
    if creds.is_expired():
        try:
            creds = refresh_access_token(creds.refresh_token, proxy=proxy)
        except Exception as e:
            return UsageResult(meta.alias, meta.email, "expired", error=str(e))

    # Make a minimal request to detect quota
    url = f"{GEMINI_API_BASE}/v1beta/models/{PROBE_MODEL}:generateContent"
    headers = {"Authorization": f"Bearer {creds.access_token}"}
    # Smallest possible request
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=20)
    except requests.exceptions.RequestException as e:
        return UsageResult(meta.alias, meta.email, "error", error=str(e))

    result = _parse_response(meta, resp)
    _update_meta(meta, result)
    return result


def check_all(accounts: list[AccountMeta]) -> list[UsageResult]:
    return [check_account(acc) for acc in accounts if not acc.disabled]


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────
def _parse_response(meta: AccountMeta, resp: requests.Response) -> UsageResult:
    tracker = RateLimitTracker()

    if resp.status_code == 200:
        return UsageResult(
            alias=meta.alias,
            email=meta.email,
            status="ok",
            remaining_pct=meta.remaining_quota,  # keep cached value
            tier=meta.subscription_tier,
        )

    if resp.status_code == 429:
        body = resp.text
        info = tracker.parse_from_error(
            meta.alias,
            429,
            resp.headers.get("Retry-After"),
            body,
        )
        reset_in = info.retry_after_sec if info else None
        return UsageResult(
            alias=meta.alias,
            email=meta.email,
            status="rate_limited",
            remaining_pct=0,
            reset_in_sec=float(reset_in) if reset_in else None,
        )

    if resp.status_code in (401, 403):
        return UsageResult(meta.alias, meta.email, "error", error=f"HTTP {resp.status_code}: auth failure")

    return UsageResult(
        meta.alias, meta.email, "error",
        error=f"HTTP {resp.status_code}: {resp.text[:200]}"
    )


def _update_meta(meta: AccountMeta, result: UsageResult):
    meta.last_synced = time.time()
    if result.remaining_pct is not None:
        meta.remaining_quota = result.remaining_pct
    if result.reset_in_sec is not None:
        meta.reset_time = time.time() + result.reset_in_sec
    if result.tier:
        meta.subscription_tier = result.tier
    save_meta(meta)
