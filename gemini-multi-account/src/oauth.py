"""
OAuth flow for Gemini CLI — ported from Antigravity Manager's oauth.rs + oauth_server.rs

Key differences from Antigravity:
  - Uses Gemini CLI OAuth scopes (cloud-platform, openid, email, profile)
  - Supports user-provided client_id/client_secret (Google Cloud Console)
  - Local TCP callback server (dual IPv4 + IPv6, same as oauth_server.rs)
  - Produces oauth_creds.json in Gemini CLI's expected format

Usage:
    creds = run_oauth_flow(client_id, client_secret, proxy=None)
    # → opens browser, waits for callback, returns OAuthCreds
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
import webbrowser
from typing import Optional

import requests

from .accounts import OAuthCreds


# ──────────────────────────────────────────────
# Constants — same scopes that Gemini CLI uses
# ──────────────────────────────────────────────
GEMINI_SCOPES = " ".join([
    "https://www.googleapis.com/auth/cloud-platform",
    "openid",
    "email",
    "profile",
])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


# ──────────────────────────────────────────────
# Build Authorization URL
# ──────────────────────────────────────────────
def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Mirror of oauth.rs::get_auth_url()."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GEMINI_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


# ──────────────────────────────────────────────
# Token exchange + refresh
# ──────────────────────────────────────────────
def exchange_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    proxy: Optional[str] = None,
) -> OAuthCreds:
    """Mirror of oauth.rs::exchange_code()."""
    proxies = {"https": proxy, "http": proxy} if proxy else None
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        proxies=proxies,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return _token_response_to_creds(data)


def refresh_access_token(
    refresh_token: str,
    proxy: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> OAuthCreds:
    """
    Mirror of oauth.rs::refresh_access_token().
    client_id / client_secret are loaded from config if not provided.
    """
    if client_id is None or client_secret is None:
        cfg = _load_client_config()
        client_id = cfg["client_id"]
        client_secret = cfg["client_secret"]

    proxies = {"https": proxy, "http": proxy} if proxy else None
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        proxies=proxies,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # Refresh response doesn't include a new refresh_token — keep the old one
    data.setdefault("refresh_token", refresh_token)
    return _token_response_to_creds(data)


def get_user_info(access_token: str, proxy: Optional[str] = None) -> dict:
    proxies = {"https": proxy, "http": proxy} if proxy else None
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        proxies=proxies,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ──────────────────────────────────────────────
# Local callback server — ported from oauth_server.rs
# ──────────────────────────────────────────────
SUCCESS_HTML = b"""HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n
<html><body style='font-family:sans-serif;text-align:center;padding:50px'>
<h1 style='color:green'>Authorization Successful!</h1>
<p>You can close this window and return to the terminal.</p>
<script>setTimeout(()=>window.close(),2000)</script>
</body></html>"""

FAIL_HTML = b"""HTTP/1.1 400 Bad Request\r\nContent-Type: text/html; charset=utf-8\r\n\r\n
<html><body style='font-family:sans-serif;text-align:center;padding:50px'>
<h1 style='color:red'>Authorization Failed</h1>
<p>Could not obtain Authorization Code. Please try again.</p>
</body></html>"""


class _CallbackServer:
    """
    Minimal TCP server that handles exactly one OAuth callback request.
    Mirrors the dual-stack (IPv4 + IPv6) approach in oauth_server.rs.
    """

    def __init__(self):
        self._result: Optional[dict] = None
        self._event = threading.Event()
        self._sockets: list[socket.socket] = []
        self.port: int = 0
        self.redirect_uri: str = ""

    def start(self) -> str:
        """Bind sockets and return the redirect_uri."""
        # Try to bind IPv6 first (like oauth_server.rs), then same port for IPv4
        ipv6_sock: Optional[socket.socket] = None
        ipv4_sock: Optional[socket.socket] = None

        try:
            s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s6.bind(("::1", 0))
            s6.listen(1)
            self.port = s6.getsockname()[1]
            ipv6_sock = s6
        except OSError:
            pass

        try:
            s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port = self.port or 0
            s4.bind(("127.0.0.1", port))
            s4.listen(1)
            self.port = s4.getsockname()[1]
            ipv4_sock = s4
        except OSError:
            pass

        if ipv6_sock is None and ipv4_sock is None:
            raise RuntimeError("Cannot bind local callback port")

        if ipv6_sock:
            self._sockets.append(ipv6_sock)
        if ipv4_sock:
            self._sockets.append(ipv4_sock)

        # Choose redirect_uri based on available stacks
        if ipv4_sock and ipv6_sock:
            self.redirect_uri = f"http://localhost:{self.port}/oauth-callback"
        elif ipv4_sock:
            self.redirect_uri = f"http://127.0.0.1:{self.port}/oauth-callback"
        else:
            self.redirect_uri = f"http://[::1]:{self.port}/oauth-callback"

        for sock in self._sockets:
            t = threading.Thread(target=self._accept_loop, args=(sock,), daemon=True)
            t.start()

        return self.redirect_uri

    def wait(self, timeout: float = 300) -> dict:
        """Block until callback is received. Returns {"code": ..., "state": ...}."""
        if not self._event.wait(timeout):
            raise TimeoutError("OAuth callback timed out")
        self._close()
        if self._result is None:
            raise RuntimeError("OAuth callback failed")
        return self._result

    def _accept_loop(self, sock: socket.socket):
        try:
            conn, _ = sock.accept()
            with conn:
                data = conn.recv(4096).decode("utf-8", errors="replace")
                parsed = self._parse_callback(data)
                if parsed:
                    conn.sendall(SUCCESS_HTML)
                    if not self._event.is_set():
                        self._result = parsed
                        self._event.set()
                else:
                    conn.sendall(FAIL_HTML)
        except OSError:
            pass

    @staticmethod
    def _parse_callback(raw: str) -> Optional[dict]:
        """Extract code + state from raw HTTP GET request."""
        try:
            first_line = raw.split("\r\n")[0]          # "GET /oauth-callback?code=...&state=... HTTP/1.1"
            path = first_line.split(" ")[1]
            qs = urllib.parse.urlparse(path).query
            params = dict(urllib.parse.parse_qsl(qs))
            if "code" in params:
                return params
        except Exception:
            pass
        return None

    def _close(self):
        for s in self._sockets:
            try:
                s.close()
            except OSError:
                pass


# ──────────────────────────────────────────────
# Main public function
# ──────────────────────────────────────────────
def run_oauth_flow(
    client_id: str,
    client_secret: str,
    proxy: Optional[str] = None,
    timeout: float = 300,
) -> tuple[OAuthCreds, dict]:
    """
    Full OAuth flow — opens the browser, waits for callback, exchanges code.
    Returns (OAuthCreds, user_info_dict).

    Mirrors: oauth_server.rs::start_oauth_flow()
    """
    server = _CallbackServer()
    redirect_uri = server.start()

    state = str(uuid.uuid4())
    auth_url = build_auth_url(client_id, redirect_uri, state)

    print(f"\nOpening browser for Google OAuth...\nIf it doesn't open automatically:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    params = server.wait(timeout)

    if params.get("state") != state:
        raise ValueError("CSRF state mismatch — possible attack")

    code = params["code"]
    creds = exchange_code(code, redirect_uri, client_id, client_secret, proxy)
    user_info = get_user_info(creds.access_token, proxy)
    return creds, user_info


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _token_response_to_creds(data: dict) -> OAuthCreds:
    expires_in = data.get("expires_in", 3600)
    expiry_ms = int((time.time() + expires_in) * 1000)
    return OAuthCreds(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        scope=data.get("scope", GEMINI_SCOPES),
        token_type=data.get("token_type", "Bearer"),
        expiry_date=expiry_ms,
        id_token=data.get("id_token", ""),
    )


def _load_client_config() -> dict:
    """Load client_id/secret from config file (set during `gemini-mgr config`)."""
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(
            "No config.json found. Run: gemini-mgr config --client-id <id> --client-secret <secret>"
        )
    data = json.loads(cfg_path.read_text())
    return {"client_id": data["client_id"], "client_secret": data["client_secret"]}
