"""Bearer token verifier for JEV_MCP_TOKEN.

Reads from env (JEV_MCP_TOKEN) or file (~/.jev/token, 0600). Token is used by
FastMCP via `token_verifier=` to enforce Authorization: Bearer <token> on HTTP.
"""
from __future__ import annotations

import hmac
import os
from typing import TYPE_CHECKING

from mcp.server.auth.provider import AccessToken

if TYPE_CHECKING:
    pass


_TOKEN_FILE = os.path.expanduser("~/.jev/token")


def load_bearer_token() -> str | None:
    """Return the configured bearer token, or None if not set.

    Priority: env JEV_MCP_TOKEN > file ~/.jev/token > None.
    """
    env = os.environ.get("JEV_MCP_TOKEN")
    if env:
        return env.strip()
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (FileNotFoundError, OSError):
        return None


class BearerTokenVerifier:
    """Constant-token verifier. Suitable for single-user home network (Tailscale).

    Uses hmac.compare_digest for timing-safe equality. On match, returns an
    AccessToken with empty scopes (no fine-grained authz in v1).
    """

    def __init__(self, expected_token: str):
        if not expected_token:
            raise ValueError("expected_token must be non-empty")
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if not hmac.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="jev-mcp-client",
            scopes=[],
        )