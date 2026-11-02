"""Protocol-level MCP server for CampKit: JSON-RPC 2.0 over stdio.

Run locally with::

    CAMPKIT_MCP_TOKEN=ck_... python -m apps.mcp.server

It speaks the MCP stdio transport (one JSON-RPC object per line, stdout is
protocol-only, diagnostics go to stderr) and exposes ``initialize``,
``tools/list``, and ``tools/call``. Registering it with a Hermes install is an
operator decision made outside this repository; nothing here reads or writes
Hermes configuration, and no network call is made.
"""

import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
SERVER_INFO = {"name": "campkit", "version": "1.0.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload, is_error):
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True, default=str)}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


class McpServer:
    """A single MCP session. Authentication is re-checked on every tool call."""

    def __init__(self, token=None, stderr=None):
        self._token = token
        self._stderr = stderr if stderr is not None else sys.stderr

    def _protocol_version(self, requested):
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        return PROTOCOL_VERSION

    def _identity(self):
        from apps.mcp import auth

        try:
            return auth.authenticate_session(self._token), None
        except auth.McpAuthError as exc:
            return None, str(exc)

    def handle_message(self, message):
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            request_id = message.get("id") if isinstance(message, dict) else None
            return _error(request_id, INVALID_REQUEST, "Invalid Request")

        method = message["method"]
        params = message.get("params") or {}
        request_id = message.get("id")
        is_notification = "id" not in message

        if method == "initialize":
            if not isinstance(params, dict):
                return _error(request_id, INVALID_PARAMS, "Invalid params")
            return _result(
                request_id,
                {
                    "protocolVersion": self._protocol_version(params.get("protocolVersion")),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )

        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None

        if method == "ping":
            return _result(request_id, {})

        if method == "tools/list":
            from apps.mcp import tools

            return _result(request_id, {"tools": tools.list_tools()})

        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return _error(request_id, INVALID_PARAMS, "Invalid params")
            arguments = params.get("arguments") or {}
            identity, auth_error = self._identity()
            if identity is None:
                print(f"campkit-mcp: {auth_error}", file=self._stderr)
                payload = {
                    "status": "error",
                    "error": {
                        "code": "unauthorized",
                        "message": "A valid CampKit API token is required.",
                    },
                }
                return _result(request_id, _tool_result(payload, True))
            from apps.mcp import tools

            payload, is_error = tools.call_tool(params["name"], arguments, identity)
            return _result(request_id, _tool_result(payload, is_error))

        if is_notification:
            return None
        return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def handle_line(self, line):
        """Handle one JSON-RPC line; returns the serialized response or None."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
        try:
            response = self.handle_message(message)
        except Exception as exc:  # keep the session alive; never leak a traceback on stdout
            print(f"campkit-mcp: internal error: {type(exc).__name__}: {exc}", file=self._stderr)
            request_id = message.get("id") if isinstance(message, dict) else None
            return json.dumps(_error(request_id, INTERNAL_ERROR, "Internal error"))
        return None if response is None else json.dumps(response, default=str)


def serve(stdin=None, stdout=None, stderr=None):
    server = McpServer(stderr=stderr)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        response = server.handle_line(line)
        if response is not None:
            stdout.write(response + "\n")
            stdout.flush()


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
