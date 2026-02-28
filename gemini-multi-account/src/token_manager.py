"""
TokenManager — ported from Antigravity Manager's token_manager.rs

Core responsibilities:
  1. Load all accounts from disk into memory
  2. Auto-refresh expired access tokens via refresh_token
  3. Select the best account for a given session using:
       - Sticky-session affinity (same session → same account)
       - P2C (Power of 2 Choices) from sorted candidate pool
       - Three scheduling modes: CacheFirst | Balance | PerformanceFirst
  4. Track rate-limit cooldowns via RateLimitTracker
  5. Auto-rotate on 429 / 401 / 403

Scheduling modes (ported from sticky_config.rs):
  CACHE_FIRST      — lock same account, wait on 429 (maximises prompt-cache hits)
  BALANCE          — lock same account, rotate on 429 (default)
  PERFORMANCE_FIRST — pure round-robin, ignores session affinity
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .accounts import AccountMeta, OAuthCreds, list_accounts, load_creds, save_creds
from .oauth import refresh_access_token
from .rate_limit import DEFAULT_BACKOFF_STEPS, RateLimitReason, RateLimitTracker


# ──────────────────────────────────────────────
# Scheduling modes
# ──────────────────────────────────────────────
class SchedulingMode(Enum):
    CACHE_FIRST = auto()       # sticky + wait on 429
    BALANCE = auto()           # sticky + rotate on 429  (default)
    PERFORMANCE_FIRST = auto() # round-robin, no stickiness


# ──────────────────────────────────────────────
# In-memory token entry
# ──────────────────────────────────────────────
@dataclass
class ProxyToken:
    alias: str
    account_id: str
    email: str
    access_token: str
    refresh_token: str
    expiry_ms: int              # milliseconds epoch
    proxy: str
    # quota / health info (from AccountMeta)
    remaining_quota: Optional[int] = None   # 0-100
    reset_time: Optional[float] = None
    subscription_tier: Optional[str] = None
    health_score: float = 1.0
    protected_models: list[str] = field(default_factory=list)

    def is_expired(self) -> bool:
        if self.expiry_ms == 0:
            return True
        return time.time() * 1000 >= self.expiry_ms - 30_000

    # Priority score for sorting (higher = preferred)
    def priority_score(self) -> float:
        quota = self.remaining_quota if self.remaining_quota is not None else 50
        health = self.health_score
        # Tier bonus: ULTRA > PRO > FREE
        tier_bonus = {"ULTRA": 20, "PRO": 10, "FREE": 0}.get(
            self.subscription_tier or "FREE", 0
        )
        # Reset-time bonus: accounts that reset sooner get a small boost
        reset_bonus = 0.0
        if self.reset_time:
            secs_until_reset = max(0, self.reset_time - time.time())
            # Closer reset = higher bonus (max 10 points for reset within 1 h)
            reset_bonus = max(0, 10 - secs_until_reset / 360)
        return quota * health + tier_bonus + reset_bonus


# ──────────────────────────────────────────────
# P2C constant — same as token_manager.rs
# ──────────────────────────────────────────────
P2C_POOL_SIZE = 5


class TokenManager:
    """
    Thread-safe manager that holds all Gemini account tokens in memory
    and implements the same rotation / scheduling logic as Antigravity Manager.
    """

    def __init__(
        self,
        mode: SchedulingMode = SchedulingMode.BALANCE,
        max_wait_seconds: int = 60,
        backoff_steps: list[int] = DEFAULT_BACKOFF_STEPS,
    ):
        self._lock = threading.RLock()
        self._tokens: dict[str, ProxyToken] = {}       # alias → ProxyToken
        self._session_map: dict[str, str] = {}         # session_id → alias
        self._rr_index = 0                             # round-robin cursor
        self.mode = mode
        self.max_wait_seconds = max_wait_seconds
        self.backoff_steps = backoff_steps
        self.rate_limiter = RateLimitTracker()

    # ── load ─────────────────────────────────
    def load_all(self) -> int:
        """Reload all enabled accounts from disk."""
        with self._lock:
            self._tokens.clear()
            self._session_map.clear()

        accounts = list_accounts()
        count = 0
        for meta in accounts:
            if meta.disabled:
                continue
            creds = load_creds(meta.alias)
            if creds is None:
                continue
            token = _build_token(meta, creds)
            with self._lock:
                self._tokens[meta.alias] = token
            count += 1
        return count

    def reload_account(self, alias: str):
        """Hot-reload a single account (e.g. after quota update)."""
        from .accounts import get_account
        meta = get_account(alias)
        if meta is None or meta.disabled:
            with self._lock:
                self._tokens.pop(alias, None)
            return
        creds = load_creds(alias)
        if creds is None:
            return
        token = _build_token(meta, creds)
        with self._lock:
            self._tokens[alias] = token
        self.rate_limiter.clear(alias)

    def remove_account(self, alias: str):
        with self._lock:
            self._tokens.pop(alias, None)
            self._session_map = {s: a for s, a in self._session_map.items() if a != alias}
        self.rate_limiter.clear(alias)

    # ── get_token — core rotation ─────────────
    def get_token(
        self,
        session_id: Optional[str] = None,
        target_model: Optional[str] = None,
        attempted: Optional[set[str]] = None,
        force_rotate: bool = False,
    ) -> Optional[ProxyToken]:
        """
        Select the best ProxyToken for a request.

        Port of token_manager.rs::get_token():
          1. Build sorted candidate list (by priority_score, excluding rate-limited)
          2. Check sticky session
          3. Apply scheduling mode
          4. Use P2C selection from top-N candidates
        """
        attempted = attempted or set()

        with self._lock:
            candidates = self._sorted_candidates(target_model, attempted)

        if not candidates:
            return None

        # ── Sticky session logic ──────────────
        if session_id and not force_rotate:
            with self._lock:
                pinned_alias = self._session_map.get(session_id)

            if pinned_alias and pinned_alias not in attempted:
                token = self._get_by_alias(pinned_alias)
                if token and not self.rate_limiter.is_rate_limited(pinned_alias, target_model):
                    token = self._ensure_valid_token(token)
                    return token
                # Pinned account rate-limited
                if self.mode == SchedulingMode.CACHE_FIRST:
                    wait = self.rate_limiter.get_remaining_wait(pinned_alias, target_model)
                    if wait <= self.max_wait_seconds:
                        time.sleep(wait + 0.1)
                        token = self._get_by_alias(pinned_alias)
                        if token:
                            return self._ensure_valid_token(token)

        # ── P2C selection from sorted pool ────
        selected = self._p2c_select(candidates)
        if selected is None:
            return None

        # Record session affinity
        if session_id and self.mode != SchedulingMode.PERFORMANCE_FIRST:
            with self._lock:
                self._session_map[session_id] = selected.alias

        return self._ensure_valid_token(selected)

    # ── report outcome ────────────────────────
    def report_success(self, alias: str):
        self.rate_limiter.mark_success(alias)

    def report_error(
        self,
        alias: str,
        status: int,
        body: str = "",
        retry_after: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.rate_limiter.parse_from_error(
            alias, status, retry_after, body, model, self.backoff_steps
        )
        # Degrade health score
        token = self._get_by_alias(alias)
        if token:
            with self._lock:
                token.health_score = max(0.0, token.health_score - 0.2)

    # ── list / info ───────────────────────────
    def all_tokens(self) -> list[ProxyToken]:
        with self._lock:
            return list(self._tokens.values())

    def get_by_alias(self, alias: str) -> Optional[ProxyToken]:
        return self._get_by_alias(alias)

    # ── internals ─────────────────────────────
    def _get_by_alias(self, alias: str) -> Optional[ProxyToken]:
        with self._lock:
            return self._tokens.get(alias)

    def _sorted_candidates(
        self,
        target_model: Optional[str],
        attempted: set[str],
    ) -> list[ProxyToken]:
        """
        Build a sorted list of tokens eligible for selection.

        Exclusion criteria (mirrors token_manager.rs):
          - disabled / not loaded
          - rate-limited at account or model level
          - in `attempted` set
          - protected_models contains target_model
        """
        now = time.time()
        result = []
        for token in self._tokens.values():
            if token.alias in attempted:
                continue
            if self.rate_limiter.is_rate_limited(token.alias, target_model):
                continue
            if target_model and target_model in token.protected_models:
                continue
            result.append(token)

        result.sort(key=lambda t: t.priority_score(), reverse=True)
        return result

    def _p2c_select(self, candidates: list[ProxyToken]) -> Optional[ProxyToken]:
        """
        Power of 2 Choices from the top-P2C_POOL_SIZE candidates.
        Picks the one with higher remaining_quota.
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        pool = candidates[:P2C_POOL_SIZE]
        if len(pool) == 1:
            return pool[0]

        i1 = random.randrange(len(pool))
        i2 = random.randrange(len(pool))
        if i2 == i1:
            i2 = (i1 + 1) % len(pool)

        c1, c2 = pool[i1], pool[i2]
        q1 = c1.remaining_quota if c1.remaining_quota is not None else 0
        q2 = c2.remaining_quota if c2.remaining_quota is not None else 0
        return c1 if q1 >= q2 else c2

    def _ensure_valid_token(self, token: ProxyToken) -> ProxyToken:
        """Refresh access token if expired (mirrors oauth.rs refresh logic)."""
        if not token.is_expired():
            return token
        try:
            new_creds = refresh_access_token(token.refresh_token, token.proxy)
            with self._lock:
                token.access_token = new_creds.access_token
                token.expiry_ms = new_creds.expiry_date
            save_creds(token.alias, new_creds)
        except Exception as e:
            # Don't crash; caller may retry with a different account
            pass
        return token

    # ── round-robin helpers ───────────────────
    def _next_rr(self, candidates: list[ProxyToken]) -> Optional[ProxyToken]:
        if not candidates:
            return None
        with self._lock:
            idx = self._rr_index % len(candidates)
            self._rr_index += 1
        return candidates[idx]


# ──────────────────────────────────────────────
# Builder helper
# ──────────────────────────────────────────────
def _build_token(meta: AccountMeta, creds: OAuthCreds) -> ProxyToken:
    return ProxyToken(
        alias=meta.alias,
        account_id=meta.id,
        email=meta.email,
        access_token=creds.access_token,
        refresh_token=creds.refresh_token,
        expiry_ms=creds.expiry_date,
        proxy=meta.proxy,
        remaining_quota=meta.remaining_quota,
        reset_time=meta.reset_time,
        subscription_tier=meta.subscription_tier,
        health_score=meta.health_score,
        protected_models=list(meta.protected_models),
    )
