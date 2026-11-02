"""Every MCP mutation must be attributable to the dedicated ``hermes`` identity."""

from decimal import Decimal

import pytest

from apps.api.auth import authenticate_token, issue_token
from apps.audit.models import InventoryChange
from apps.intake.models import (
    IntakeCandidate,
    IntakeChannel,
    IntakeKind,
    IntakeStatus,
    IntakeSubmission,
)
from apps.inventory.models import PantryItem, PriceObservation
from apps.mcp import tools

pytestmark = pytest.mark.django_db


@pytest.fixture
def identity(household):
    _token, raw = issue_token(household=household, label="mcp-audit")
    return authenticate_token(raw)


def _call(identity, name, arguments=None):
    payload, is_error = tools.call_tool(name, arguments or {}, identity)
    assert is_error is False, payload
    return payload


def test_gear_mutations_are_audited_as_hermes(identity, household, owner):
    created = _call(identity, "campkit_add_gear", {"name": "Stove", "weight_grams": 900})
    gear_id = created["gear"]["id"]

    create_change = InventoryChange.objects.get(object_id=gear_id, action="created")
    assert create_change.actor.username == "hermes"
    assert create_change.household_id == household.pk
    assert create_change.object_type == "inventory.gearitem"

    _call(identity, "campkit_update_gear", {"gear_id": gear_id, "condition": "worn"})
    update_change = InventoryChange.objects.filter(object_id=gear_id, action="updated").latest("created_at")
    assert update_change.actor.username == "hermes"
    assert update_change.after["condition"] == "worn"

    confirmed = _call(
        identity, "campkit_confirm_inventory_fact", {"kind": "gear", "item_id": gear_id, "confirm": True}
    )
    assert confirmed["gear"]["verified_by"] == "hermes"
    confirm_change = InventoryChange.objects.filter(object_id=gear_id, action="updated").latest("created_at")
    assert confirm_change.actor.username == "hermes"
    assert confirm_change.after["verification_status"] == "confirmed"
    assert InventoryChange.objects.filter(actor=owner).count() == 0


def test_intake_review_through_mcp_is_attributed_to_hermes(identity, household, owner):
    submission = IntakeSubmission.objects.create(
        household=household,
        submitted_by=owner,
        kind=IntakeKind.PANTRY,
        channel=IntakeChannel.BARCODE,
        barcode="0123456789012",
        dedupe_key="mcp-audit-dedupe",
        status=IntakeStatus.NEEDS_REVIEW,
    )
    IntakeCandidate.objects.create(
        submission=submission,
        name="Trail mix",
        quantity=Decimal("2.000"),
        unit="each",
        provider="fake-vision",
        overall_confidence=Decimal("0.700"),
    )

    blocked, is_error = tools.call_tool(
        "campkit_review_intake", {"submission_id": str(submission.pk), "action": "approve"}, identity
    )
    assert is_error is False
    assert blocked["status"] == "confirmation_required"
    assert not PantryItem.objects.filter(household=household).exists()

    approved = _call(
        identity,
        "campkit_review_intake",
        {"submission_id": str(submission.pk), "action": "approve", "confirm": True},
    )
    assert approved["intake"]["status"] == "added"
    change = InventoryChange.objects.filter(action="intake_approved").latest("created_at")
    assert change.actor.username == "hermes"
    item = PantryItem.objects.get(pk=approved["intake"]["created_item"]["id"])
    assert item.verified_by.username == "hermes"
    assert InventoryChange.objects.filter(actor=owner).count() == 0


def test_recorded_price_is_attributed_to_hermes(identity, household):
    pantry = _call(identity, "campkit_add_pantry_item", {"name": "Trail mix", "quantity": "1", "unit": "each"})
    item_id = pantry["pantry_item"]["id"]
    result = _call(
        identity,
        "campkit_record_price",
        {"pantry_item_id": item_id, "amount": "3.50", "source": "manual", "merchant": "CampCo"},
    )
    assert result["price"]["amount"] == "3.50"
    observation = PriceObservation.objects.get(pk=result["price"]["id"])
    assert observation.actor.username == "hermes"
    assert observation.item.household_id == household.pk
    change = InventoryChange.objects.filter(action="price_observed").latest("created_at")
    assert change.actor.username == "hermes"


def test_meal_reservation_audit_records_hermes(identity, household):
    pantry = _call(identity, "campkit_add_pantry_item", {"name": "Oats", "quantity": "2", "unit": "kg"})
    item_id = pantry["pantry_item"]["id"]
    _call(identity, "campkit_confirm_inventory_fact", {"kind": "pantry", "item_id": item_id, "confirm": True})
    trip = _call(identity, "campkit_create_trip", {"name": "Pine weekend"})
    trip_id = trip["trip"]["id"]
    _call(
        identity,
        "campkit_plan_meals",
        {"trip_id": trip_id, "meals": [{"name": "Porridge",
                                        "ingredients": [{"pantry_item_id": item_id, "quantity": "1"}]}]},
    )
    confirmed = _call(identity, "campkit_confirm_meal_plan", {"trip_id": trip_id, "confirm": True})
    assert confirmed["trip"]["status"] == "confirmed"
    actions = set(
        InventoryChange.objects.filter(household=household).values_list("action", flat=True)
    )
    assert {"food_reserved", "food_reservation_created"} <= actions
    assert set(InventoryChange.objects.filter(household=household).values_list("actor__username", flat=True)) == {"hermes"}
