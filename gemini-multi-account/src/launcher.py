"""
Launcher — set GEMINI_CONFIG_DIR + proxy env vars then exec Gemini CLI.

Mirrors the intent of Antigravity's proxy_pool.rs account-binding logic:
  account_id → proxy_id  →  effective HTTP client

Here the binding is simpler:
  alias → meta.proxy  →  environment variables for the child process
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .accounts import get_account, load_creds, profile_dir_for, save_creds
from .oauth import refresh_access_token


def launch(
    alias: str,
    gemini_args: list[str],
    gemini_bin: str = "gemini",
) -> int:
    """
    Launch Gemini CLI for the given account alias.

    Steps:
      1. Load account meta + credentials
      2. Refresh token if expired
      3. Set GEMINI_CONFIG_DIR to the profile directory
      4. Set proxy env vars if account has a proxy configured
      5. exec Gemini CLI — this process replaces (or spawns) it

    Returns the exit code of the Gemini CLI process.
    """
    meta = get_account(alias)
    if meta is None:
        print(f"[ERROR] Account '{alias}' not found.", file=sys.stderr)
        return 1

    if meta.disabled:
        print(f"[ERROR] Account '{alias}' is disabled.", file=sys.stderr)
        return 1

    creds = load_creds(alias)
    if creds is None:
        print(f"[ERROR] No credentials found for '{alias}'. Run: gemini-mgr add", file=sys.stderr)
        return 1

    # Refresh if expired
    if creds.is_expired():
        print(f"[INFO] Token expired for '{alias}', refreshing...")
        try:
            creds = refresh_access_token(creds.refresh_token, proxy=meta.proxy or None)
            save_creds(alias, creds)
            print(f"[INFO] Token refreshed for '{alias}'.")
        except Exception as e:
            print(f"[WARN] Token refresh failed: {e}. Continuing with expired token.", file=sys.stderr)

    profile_dir = profile_dir_for(alias)
    env = _build_env(meta.proxy, profile_dir)

    cmd = [gemini_bin, *gemini_args]
    print(f"[gemini-mgr] Launching: {alias} ({meta.email})")
    if meta.proxy:
        print(f"[gemini-mgr] Proxy: {meta.proxy}")
    print(f"[gemini-mgr] Config dir: {profile_dir}")
    print()

    try:
        result = subprocess.run(cmd, env=env)
        return result.returncode
    except FileNotFoundError:
        print(
            f"[ERROR] Gemini CLI binary '{gemini_bin}' not found. "
            "Install it or pass --bin <path>.",
            file=sys.stderr,
        )
        return 127


def _build_env(proxy: str, profile_dir: Path) -> dict[str, str]:
    """
    Build a child-process environment that:
      - Points GEMINI_CONFIG_DIR to the account's profile directory
      - Sets HTTP_PROXY / HTTPS_PROXY / ALL_PROXY if the account has a proxy
    """
    env = os.environ.copy()

    # Core: tell Gemini CLI which config directory to use
    env["GEMINI_CONFIG_DIR"] = str(profile_dir)

    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        # Some tools read lowercase variants
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["all_proxy"] = proxy
    else:
        # Clear any inherited proxy settings so the account runs without proxy
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(var, None)

    return env
