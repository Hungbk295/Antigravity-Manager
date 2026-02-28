"""
Account storage — mirrors Antigravity Manager's account-per-JSON-file pattern.

Each account lives in:
  profiles/<alias>/
      oauth_creds.json   ← Gemini CLI token format
      meta.json          ← alias, email, proxy, disabled, usage cache
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


PROFILES_DIR = Path(__file__).parent.parent / "profiles"


# ──────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────
@dataclass
class OAuthCreds:
    """
    Format that Gemini CLI reads from oauth_creds.json.
    Matches the structure written by the official Gemini CLI OAuth flow.
    """
    access_token: str
    refresh_token: str
    scope: str
    token_type: str = "Bearer"
    expiry_date: int = 0        # epoch milliseconds
    id_token: str = ""

    def is_expired(self) -> bool:
        if self.expiry_date == 0:
            return True
        return time.time() * 1000 >= self.expiry_date - 30_000  # 30s buffer


@dataclass
class AccountMeta:
    """Tool-specific metadata stored alongside the Gemini credential."""
    id: str
    alias: str
    email: str
    proxy: str = ""             # e.g. "socks5://127.0.0.1:1080" or ""
    disabled: bool = False
    # Usage cache — populated by usage.py
    remaining_quota: Optional[int] = None   # 0-100 %
    reset_time: Optional[float] = None      # epoch seconds
    subscription_tier: Optional[str] = None # "FREE" | "PRO" | "ULTRA"
    last_synced: Optional[float] = None
    health_score: float = 1.0
    protected_models: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Storage helpers
# ──────────────────────────────────────────────
def _profile_dir(alias: str) -> Path:
    return PROFILES_DIR / alias


def _oauth_path(alias: str) -> Path:
    return _profile_dir(alias) / "oauth_creds.json"


def _meta_path(alias: str) -> Path:
    return _profile_dir(alias) / "meta.json"


# ──────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────
def list_accounts() -> list[AccountMeta]:
    accounts: list[AccountMeta] = []
    if not PROFILES_DIR.exists():
        return accounts
    for entry in sorted(PROFILES_DIR.iterdir()):
        if not entry.is_dir():
            continue
        meta_file = entry / "meta.json"
        if not meta_file.exists():
            continue
        try:
            data = json.loads(meta_file.read_text())
            accounts.append(_dict_to_meta(data))
        except Exception:
            pass
    return accounts


def get_account(alias: str) -> Optional[AccountMeta]:
    mp = _meta_path(alias)
    if not mp.exists():
        return None
    try:
        return _dict_to_meta(json.loads(mp.read_text()))
    except Exception:
        return None


def save_account(meta: AccountMeta, creds: OAuthCreds):
    """Persist both meta.json and oauth_creds.json for an account."""
    p = _profile_dir(meta.alias)
    p.mkdir(parents=True, exist_ok=True)
    _meta_path(meta.alias).write_text(json.dumps(asdict(meta), indent=2))
    _oauth_path(meta.alias).write_text(json.dumps(asdict(creds), indent=2))


def save_meta(meta: AccountMeta):
    """Update only the meta.json (e.g. after a quota check)."""
    _meta_path(meta.alias).write_text(json.dumps(asdict(meta), indent=2))


def load_creds(alias: str) -> Optional[OAuthCreds]:
    op = _oauth_path(alias)
    if not op.exists():
        return None
    try:
        d = json.loads(op.read_text())
        return OAuthCreds(**{k: d[k] for k in OAuthCreds.__dataclass_fields__ if k in d})
    except Exception:
        return None


def save_creds(alias: str, creds: OAuthCreds):
    _oauth_path(alias).write_text(json.dumps(asdict(creds), indent=2))


def delete_account(alias: str) -> bool:
    p = _profile_dir(alias)
    if not p.exists():
        return False
    import shutil
    shutil.rmtree(p)
    return True


def create_new_meta(alias: str, email: str, proxy: str = "") -> AccountMeta:
    return AccountMeta(
        id=str(uuid.uuid4()),
        alias=alias,
        email=email,
        proxy=proxy,
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _dict_to_meta(d: dict) -> AccountMeta:
    valid_keys = AccountMeta.__dataclass_fields__.keys()
    filtered = {k: v for k, v in d.items() if k in valid_keys}
    return AccountMeta(**filtered)


def profile_dir_for(alias: str) -> Path:
    """Return the profile directory path (used by the launcher to set GEMINI_CONFIG_DIR)."""
    return _profile_dir(alias)
