"""Session identity for the MCP server.

The server never mints or stores credentials itself: it accepts a CampKit API
token from its own process environment and resolves it through
:func:`apps.api.auth.authenticate_token`, the same function the REST layer uses.
That keeps one definition of a usable, household-scoped credential.
"""

import os

from apps.api.auth import TokenRejected, authenticate_token

TOKEN_ENV_VARS = ("CAMPKIT_MCP_TOKEN", "CAMPKIT_API_TOKEN")


class McpAuthError(Exception):
    """No usable token is configured for this MCP session."""


def configured_token(explicit=None) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "")
        if value.strip():
            return value.strip()
    raise McpAuthError(
        "No CampKit API token is configured; set CAMPKIT_MCP_TOKEN for the server process."
    )


def authenticate_session(explicit=None):
    """Return the household-scoped service identity for this session."""
    raw = configured_token(explicit)
    try:
        return authenticate_token(raw)
    except TokenRejected as exc:
        raise McpAuthError(
            "The CampKit API token is invalid, revoked, or expired."
        ) from exc
