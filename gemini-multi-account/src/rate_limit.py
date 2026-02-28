"""
RateLimitTracker — ported from Antigravity Manager's rate_limit.rs

Tracks per-account (and per-model) rate-limit cooldowns.
Supports:
  - Account-level lockout
  - Model-level lockout  (key = "account_id:model_name")
  - Exponential back-off for QUOTA_EXHAUSTED
  - Fixed short back-off for RATE_LIMIT_EXCEEDED / SERVER_ERROR
  - ISO-8601 / duration-string reset-time parsing
  - Auto-cleanup of expired records
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
FAILURE_COUNT_EXPIRY_SECONDS = 3600   # Reset failure count after 1 h of no failures
DEFAULT_BACKOFF_STEPS = [60, 300, 1800, 7200]  # seconds: 1m, 5m, 30m, 2h


# ──────────────────────────────────────────────
# Enums / data classes
# ──────────────────────────────────────────────
class RateLimitReason(Enum):
    QUOTA_EXHAUSTED = auto()
    RATE_LIMIT_EXCEEDED = auto()
    MODEL_CAPACITY_EXHAUSTED = auto()
    SERVER_ERROR = auto()
    UNKNOWN = auto()


@dataclass
class RateLimitInfo:
    reset_at: float          # epoch seconds when the lockout expires
    retry_after_sec: int
    detected_at: float
    reason: RateLimitReason
    model: Optional[str] = None


# ──────────────────────────────────────────────
# Tracker
# ──────────────────────────────────────────────
class RateLimitTracker:
    """Thread-safe rate-limit tracker."""

    def __init__(self):
        self._lock = threading.Lock()
        # key → RateLimitInfo
        self._limits: dict[str, RateLimitInfo] = {}
        # account_id → (failure_count, last_failure_epoch)
        self._failure_counts: dict[str, tuple[int, float]] = {}

    # ── key helpers ───────────────────────────
    def _limit_key(self, account_id: str, model: Optional[str]) -> str:
        if model:
            return f"{account_id}:{model}"
        return account_id

    # ── public query API ──────────────────────
    def is_rate_limited(self, account_id: str, model: Optional[str] = None) -> bool:
        return self.get_remaining_wait(account_id, model) > 0

    def get_remaining_wait(self, account_id: str, model: Optional[str] = None) -> float:
        now = time.time()
        with self._lock:
            # 1. Account-level lock
            info = self._limits.get(account_id)
            if info and info.reset_at > now:
                return info.reset_at - now
            # 2. Model-level lock
            if model:
                key = self._limit_key(account_id, model)
                info = self._limits.get(key)
                if info and info.reset_at > now:
                    return info.reset_at - now
        return 0.0

    def get_reset_seconds(self, account_id: str) -> Optional[float]:
        with self._lock:
            info = self._limits.get(account_id)
            if info:
                remaining = info.reset_at - time.time()
                return max(0.0, remaining)
        return None

    # ── mark success ──────────────────────────
    def mark_success(self, account_id: str):
        """Call after a successful request to reset exponential back-off."""
        with self._lock:
            self._failure_counts.pop(account_id, None)
            self._limits.pop(account_id, None)

    # ── precise lockout (from quota reset time) ───
    def set_lockout_until(
        self,
        account_id: str,
        reset_epoch: float,
        reason: RateLimitReason,
        model: Optional[str] = None,
    ):
        now = time.time()
        retry_sec = max(0, int(reset_epoch - now))
        info = RateLimitInfo(
            reset_at=reset_epoch,
            retry_after_sec=retry_sec,
            detected_at=now,
            reason=reason,
            model=model,
        )
        key = self._limit_key(account_id, model)
        with self._lock:
            self._limits[key] = info

    def set_lockout_until_iso(
        self,
        account_id: str,
        iso_str: str,
        reason: RateLimitReason,
        model: Optional[str] = None,
    ) -> bool:
        """Parse an ISO-8601 string and set a precise lockout. Returns True on success."""
        from dateutil import parser as dtparser
        try:
            dt = dtparser.parse(iso_str)
            epoch = dt.timestamp()
            self.set_lockout_until(account_id, epoch, reason, model)
            return True
        except Exception:
            return False

    # ── parse from HTTP error response ────────
    def parse_from_error(
        self,
        account_id: str,
        status: int,
        retry_after_header: Optional[str] = None,
        body: str = "",
        model: Optional[str] = None,
        backoff_steps: list[int] = DEFAULT_BACKOFF_STEPS,
    ) -> Optional[RateLimitInfo]:
        """
        Inspect an HTTP error and record the appropriate lockout.
        Returns the RateLimitInfo that was stored, or None if status is not retriable.
        """
        if status not in (429, 500, 503, 529, 404):
            return None

        # 1. Determine reason
        if status == 429:
            reason = self._parse_reason(body)
        elif status == 404:
            reason = RateLimitReason.SERVER_ERROR
        else:
            reason = RateLimitReason.SERVER_ERROR

        # 2. Determine retry seconds
        retry_sec: Optional[int] = None

        if retry_after_header:
            try:
                retry_sec = int(retry_after_header)
            except ValueError:
                pass

        if retry_sec is None:
            retry_sec = self._parse_retry_from_body(body)

        if retry_sec is None:
            retry_sec = self._default_retry(account_id, reason, status, backoff_steps)
        else:
            retry_sec = max(2, retry_sec)  # safety buffer

        now = time.time()
        info = RateLimitInfo(
            reset_at=now + retry_sec,
            retry_after_sec=retry_sec,
            detected_at=now,
            reason=reason,
            model=model,
        )

        # Model-level isolation only for QUOTA_EXHAUSTED
        use_model_key = reason == RateLimitReason.QUOTA_EXHAUSTED and model is not None
        key = self._limit_key(account_id, model if use_model_key else None)
        with self._lock:
            self._limits[key] = info

        return info

    # ── clear ─────────────────────────────────
    def clear(self, account_id: str):
        with self._lock:
            self._limits.pop(account_id, None)

    def clear_all(self):
        with self._lock:
            self._limits.clear()

    def cleanup_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._limits.items() if v.reset_at <= now]
            for k in expired:
                del self._limits[k]
        return len(expired)

    # ── internal helpers ──────────────────────
    def _default_retry(
        self,
        account_id: str,
        reason: RateLimitReason,
        status: int,
        backoff_steps: list[int],
    ) -> int:
        if reason == RateLimitReason.QUOTA_EXHAUSTED:
            count = self._increment_failure(account_id)
            idx = min(count - 1, len(backoff_steps) - 1)
            lockout = backoff_steps[idx] if backoff_steps else 7200
            return lockout
        elif reason == RateLimitReason.RATE_LIMIT_EXCEEDED:
            return 5
        elif reason == RateLimitReason.MODEL_CAPACITY_EXHAUSTED:
            count = self._increment_failure(account_id)
            return [5, 10, 15][min(count - 1, 2)]
        elif reason == RateLimitReason.SERVER_ERROR:
            return 5 if status == 404 else 8
        else:
            return 60

    def _increment_failure(self, account_id: str) -> int:
        now = time.time()
        with self._lock:
            count, last_time = self._failure_counts.get(account_id, (0, now))
            if now - last_time > FAILURE_COUNT_EXPIRY_SECONDS:
                count = 0
            count += 1
            self._failure_counts[account_id] = (count, now)
        return count

    def _parse_reason(self, body: str) -> RateLimitReason:
        """Parse the 429 reason from response body JSON or plain text."""
        import json
        body = body.strip()
        if body.startswith("{") or body.startswith("["):
            try:
                j = json.loads(body)
                reason_str = (
                    j.get("error", {})
                    .get("details", [{}])[0]
                    .get("reason", "")
                )
                mapping = {
                    "QUOTA_EXHAUSTED": RateLimitReason.QUOTA_EXHAUSTED,
                    "RATE_LIMIT_EXCEEDED": RateLimitReason.RATE_LIMIT_EXCEEDED,
                    "MODEL_CAPACITY_EXHAUSTED": RateLimitReason.MODEL_CAPACITY_EXHAUSTED,
                }
                if reason_str in mapping:
                    return mapping[reason_str]
                # Fallback: check message field
                msg = j.get("error", {}).get("message", "").lower()
                if "per minute" in msg or "rate limit" in msg:
                    return RateLimitReason.RATE_LIMIT_EXCEEDED
            except Exception:
                pass

        b = body.lower()
        if "per minute" in b or "rate limit" in b or "too many requests" in b:
            return RateLimitReason.RATE_LIMIT_EXCEEDED
        if "exhausted" in b or "quota" in b:
            return RateLimitReason.QUOTA_EXHAUSTED
        return RateLimitReason.UNKNOWN

    def _parse_retry_from_body(self, body: str) -> Optional[int]:
        """Extract retry delay (seconds) from response body."""
        import json
        body_s = body.strip()

        # A. JSON precise parsing
        if body_s.startswith("{") or body_s.startswith("["):
            try:
                j = json.loads(body_s)
                delay_str = (
                    j.get("error", {})
                    .get("details", [{}])[0]
                    .get("metadata", {})
                    .get("quotaResetDelay")
                )
                if delay_str:
                    parsed = self._parse_duration_string(delay_str)
                    if parsed:
                        return parsed
                retry = (
                    j.get("error", {}).get("retry_after")
                )
                if isinstance(retry, (int, float)):
                    return int(retry)
            except Exception:
                pass

        # B. Regex fallback patterns
        patterns = [
            (r"(?i)try again in (\d+)m\s*(\d+)s", lambda m: int(m[1]) * 60 + int(m[2])),
            (r"(?i)(?:try again in|backoff for|wait)\s*(\d+)s", lambda m: int(m[1])),
            (r"(?i)quota will reset in (\d+) second", lambda m: int(m[1])),
            (r"(?i)retry after (\d+) second", lambda m: int(m[1])),
            (r"\(wait (\d+)s\)", lambda m: int(m[1])),
        ]
        for pattern, extractor in patterns:
            m = re.search(pattern, body)
            if m:
                return extractor(m.groups())
        return None

    @staticmethod
    def _parse_duration_string(s: str) -> Optional[int]:
        """
        Parse Google-style duration strings like "2h1m30s", "500ms", "42s".
        Returns total seconds (rounded up).
        """
        m = re.fullmatch(
            r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?",
            s.strip(),
        )
        if not m:
            return None
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        sec = float(m.group(3) or 0)
        ms = float(m.group(4) or 0)
        total = h * 3600 + mi * 60 + int(sec) + (1 if sec != int(sec) else 0) + (1 if ms > 0 else 0)
        return total if total > 0 else None
