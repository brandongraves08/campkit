"""Tool allowlist, household pinning, and confirmation tests for the MCP surface."""

import io
import json

import pytest
from django.urls import reverse

from apps.api.auth import authenticate_token, issue_token, revoke_token
from apps.api.models import ApiToken
from apps.inventory.models import GearItem
from apps.mcp import auth as mcp_auth
from apps.mcp import tools
from apps.mcp.server import McpServer
from apps.trips.services import create_trip

EXPECTED_TOOLS = [
    "campkit_list_trips",
    "campkit_get_trip",
    "campkit_create_trip",
    "campkit_list_gear",
    "campkit_add_gear",
    "campkit_update_gear",
    "campkit_list_pantry",
    "campkit_add_pantry_item",
    "campkit_update_pantry_item",
    "campkit_list_verification_requests",
    "campkit_confirm_inventory_fact",
    "campkit_plan_meals",
    "campkit_confirm_meal_plan",
    "campkit_review_intake",
    "campkit_record_price",
]

pytestmark = pytest.mark.django_db


@pytest.fixture
def identity(household):
    _token, raw = issue_token(household=household, label="mcp")
    return authenticate_token(raw)


@pytest.fixture
def other_identity(other_household):
    _token, raw = issue_token(household=other_household, label="mcp-other")
    return authenticate_token(raw)


def _call(identity, name, arguments=None):
    return tools.call_tool(name, arguments or {}, identity)


def test_tool_registry_is_exactly_the_plan_allowlist():
    descriptors = tools.list_tools()
    assert [tool["name"] for tool in descriptors] == EXPECTED_TOOLS
    for tool in descriptors:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
    names = " ".join(EXPECTED_TOOLS)
    for forbidden in ("delete", "remove", "purge", "bulk", "purchase"):
        assert forbidden not in names


def test_calls_without_a_token_are_rejected_and_mutate_nothing(monkeypatch, household):
    monkeypatch.delenv("CAMPKIT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("CAMPKIT_API_TOKEN", raising=False)
    with pytest.raises(mcp_auth.McpAuthError):
        mcp_auth.authenticate_session()
    server = McpServer(stderr=io.StringIO())
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "campkit_add_gear", "arguments": {"name": "Ghost tent"}},
        }
    )
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "unauthorized"
    assert not GearItem.objects.filter(name="Ghost tent").exists()


def test_revoked_token_is_rejected_by_the_mcp_session(monkeypatch, household):
    token, raw = issue_token(household=household, label="mcp-revoke")
    monkeypatch.setenv("CAMPKIT_MCP_TOKEN", raw)
    assert mcp_auth.authenticate_session().household == household
    revoke_token(token)
    with pytest.raises(mcp_auth.McpAuthError):
        mcp_auth.authenticate_session()
    server = McpServer(stderr=io.StringIO())
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "campkit_list_gear", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == "unauthorized"


def test_foreign_household_records_are_not_found_and_unchanged(identity, other_identity):
    created, is_error = _call(other_identity, "campkit_add_gear", {"name": "Other tent"})
    assert is_error is False
    gear_id = created["gear"]["id"]

    payload, is_error = _call(identity, "campkit_update_gear", {"gear_id": gear_id, "name": "Stolen"})
    assert is_error is True
    assert payload["error"]["code"] == "not_found"
    assert GearItem.objects.get(pk=gear_id).name == "Other tent"

    trip = create_trip(actor=other_identity.actor, household=other_identity.household, data={"name": "Theirs"})
    payload, is_error = _call(identity, "campkit_get_trip", {"trip_id": str(trip.pk)})
    assert is_error is True
    assert payload["error"]["code"] == "not_found"

    listing, _ = _call(identity, "campkit_list_gear")
    assert listing["gear"] == []


def test_confirmation_gated_tools_do_not_mutate_until_confirmed(identity):
    created, _ = _call(identity, "campkit_add_gear", {"name": "Stove", "weight_grams": 900})
    gear_id = created["gear"]["id"]

    payload, is_error = _call(
        identity, "campkit_confirm_inventory_fact", {"kind": "gear", "item_id": gear_id}
    )
    assert is_error is False
    assert payload["status"] == "confirmation_required"
    assert payload["confirmation"]["action"] == "confirm_gear_fact"
    assert payload["confirmation"]["required"] is True
    assert GearItem.objects.get(pk=gear_id).verification_status == "estimated"

    confirmed, is_error = _call(
        identity,
        "campkit_confirm_inventory_fact",
        {"kind": "gear", "item_id": gear_id, "confirm": True},
    )
    assert is_error is False
    assert confirmed["gear"]["verification_status"] == "confirmed"
    assert confirmed["gear"]["verified_by"] == "hermes"


def test_unknown_tools_and_arguments_are_rejected(identity):
    payload, is_error = _call(identity, "campkit_delete_gear", {"gear_id": "x"})
    assert is_error is True
    assert payload["error"]["code"] == "unknown_tool"

    payload, is_error = _call(identity, "campkit_add_gear", {"name": "Tent", "verification_status": "confirmed"})
    assert is_error is True
    assert payload["error"]["code"] == "invalid_arguments"
    assert not GearItem.objects.filter(name="Tent").exists()

    payload, is_error = _call(identity, "campkit_add_gear", {})
    assert is_error is True
    assert "name" in payload["error"]["message"]


def test_mcp_and_rest_share_the_same_domain_services(identity, client):
    _token, raw = issue_token(household=identity.household, label="rest-reader")
    created, is_error = _call(identity, "campkit_add_gear", {"name": "Shared stove", "weight_grams": 700})
    assert is_error is False
    gear_id = created["gear"]["id"]

    response = client.get(
        reverse("api:gear-detail", args=[gear_id]),
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert response.status_code == 200
    assert response.json()["gear"]["id"] == gear_id

    rest_created = client.post(
        reverse("api:gear-collection"),
        data=json.dumps({"name": "Shared lantern"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert rest_created.status_code == 200
    rest_id = rest_created.json()["gear"]["id"]
    listing, _ = _call(identity, "campkit_list_gear")
    assert rest_id in [row["id"] for row in listing["gear"]]
