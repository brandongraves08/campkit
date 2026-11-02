"""Protocol-level MCP smoke tests.

The first test drives the server's JSON-RPC framing in-process. The second
launches the real stdio server as a subprocess against a throw-away SQLite
database, a temporary household, and a temporary token, then revokes the token
and proves the same session can no longer read anything. Nothing here touches a
live Hermes install or the network.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.mcp.server import McpServer

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOL_COUNT = 15

BOOTSTRAP = """
import json

import django

django.setup()
from django.core.management import call_command

call_command("migrate", verbosity=0, interactive=False)

from django.contrib.auth import get_user_model

from apps.api.auth import issue_token
from apps.households.models import Household, Membership, MembershipRole
from apps.inventory.services import create_gear_item

household = Household.objects.create(name="MCP smoke household")
owner = get_user_model().objects.create_user(username="smoke-owner", password="smoke-pass-123")
Membership.objects.create(user=owner, household=household, role=MembershipRole.OWNER)
gear = create_gear_item(
    actor=owner, household=household, data={"name": "Smoke tent", "weight_grams": 1500}
)
_token, raw = issue_token(household=household, label="mcp-smoke")
print(json.dumps({
    "token": raw,
    "prefix": raw[:12],
    "gear_id": str(gear.pk),
    "household_id": str(household.pk),
}))
"""

REVOKE = """
import json
import os

import django

django.setup()
from apps.api.auth import revoke_token
from apps.api.models import ApiToken

token = ApiToken.objects.get(key_prefix=os.environ["SMOKE_PREFIX"])
revoke_token(token)
print(json.dumps({"revoked": token.revoked_at is not None}))
"""

CHECK_GEAR = """
import json
import os

import django

django.setup()
from apps.inventory.models import GearItem

gear = GearItem.objects.get(pk=os.environ["SMOKE_GEAR_ID"])
print(json.dumps({"verification_status": gear.verification_status}))
"""


def _settings_env(tmp_path):
    return {
        "DJANGO_SETTINGS_MODULE": "config.settings_test",
        "CAMPKIT_TEST_DB": str(tmp_path / "smoke.sqlite3"),
        "PYTHONPATH": str(REPO_ROOT),
    }


def _run_script(script, env, **extra_env):
    merged = {**os.environ, **env, **extra_env}
    merged.pop("CAMPKIT_MCP_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-"],
        input=script,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=merged,
        timeout=180,
    )


def _run_server(requests, env, token):
    payload = "\n".join(json.dumps(request) for request in requests) + "\n"
    merged = {**os.environ, **env, "CAMPKIT_MCP_TOKEN": token}
    return subprocess.run(
        [sys.executable, "-m", "apps.mcp.server"],
        input=payload,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=merged,
        timeout=180,
    )


def test_jsonrpc_framing_in_process():
    server = McpServer(stderr=open(os.devnull, "w"))
    initialized = json.loads(
        server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke", "version": "1"},
                    },
                }
            )
        )
    )
    assert initialized["result"]["protocolVersion"] == "2024-11-05"
    assert initialized["result"]["serverInfo"] == {"name": "campkit", "version": "1.0.0"}
    assert initialized["result"]["capabilities"] == {"tools": {"listChanged": False}}

    listed = json.loads(
        server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    )
    assert len(listed["result"]["tools"]) == EXPECTED_TOOL_COUNT

    # Notifications never produce a response.
    assert server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None

    parse_error = json.loads(server.handle_line("{"))
    assert parse_error["error"]["code"] == -32700

    unknown = json.loads(
        server.handle_line(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}))
    )
    assert unknown["error"]["code"] == -32601


def test_stdio_smoke_with_temporary_household_and_token(tmp_path):
    env = _settings_env(tmp_path)
    bootstrap = _run_script(BOOTSTRAP, env)
    assert bootstrap.returncode == 0, bootstrap.stderr
    state = json.loads(bootstrap.stdout.strip().splitlines()[-1])
    token, gear_id = state["token"], state["gear_id"]

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "campkit_list_gear", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "campkit_confirm_inventory_fact",
                    "arguments": {"kind": "gear", "item_id": gear_id}}},
    ]
    server = _run_server(requests, env, token)
    assert server.returncode == 0, server.stderr
    responses = [json.loads(line) for line in server.stdout.splitlines() if line.strip()]
    assert [response["id"] for response in responses] == [1, 2, 3, 4]
    assert len(responses[1]["result"]["tools"]) == EXPECTED_TOOL_COUNT

    listing = responses[2]["result"]
    assert listing["isError"] is False
    assert [row["id"] for row in listing["structuredContent"]["gear"]] == [gear_id]
    assert listing["structuredContent"]["gear"][0]["verification_status"] == "estimated"

    blocked = responses[3]["result"]
    assert blocked["isError"] is False
    assert blocked["structuredContent"]["status"] == "confirmation_required"
    assert blocked["structuredContent"]["confirmation"]["action"] == "confirm_gear_fact"

    revoke = _run_script(REVOKE, env, SMOKE_PREFIX=state["prefix"])
    assert revoke.returncode == 0, revoke.stderr
    assert json.loads(revoke.stdout.strip().splitlines()[-1]) == {"revoked": True}

    denied = _run_server(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "campkit_list_gear", "arguments": {}}}],
        env,
        token,
    )
    assert denied.returncode == 0, denied.stderr
    denied_payload = json.loads(denied.stdout.strip().splitlines()[-1])
    assert denied_payload["result"]["isError"] is True
    assert denied_payload["result"]["structuredContent"]["error"]["code"] == "unauthorized"

    checked = _run_script(CHECK_GEAR, env, SMOKE_GEAR_ID=gear_id)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout.strip().splitlines()[-1]) == {"verification_status": "estimated"}
