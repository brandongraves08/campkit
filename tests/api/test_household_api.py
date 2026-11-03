"""Household-scoped REST contract tests for ``/api/v1``.

These cover scoping (a foreign id and a nonexistent id must be
indistinguishable), estimated-versus-confirmed serialization, confirmation of
sensitive mutations, and the stable schema the client depends on.
"""

import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.api.auth import revoke_token
from apps.api.models import ApiToken
from apps.audit.models import InventoryChange
from apps.households.models import Membership
from apps.intake.models import (
    IntakeCandidate,
    IntakeChannel,
    IntakeKind,
    IntakeStatus,
    IntakeSubmission,
    VerificationRequest,
    VerificationRequestStatus,
)
from apps.inventory.models import GearItem, PantryItem
from apps.inventory.services import upsert_nutrition_fact
from apps.trips.models import Trip

from helpers import auth, get, patch, post

pytestmark = pytest.mark.django_db

UNAUTHORIZED = {"error": {"code": "unauthorized", "message": "A valid API token is required."}}
NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}

GEAR_KEYS = {
    "id", "name", "category", "owner", "condition", "weight_grams", "quantity",
    "total_weight_grams", "storage_location", "availability", "verification_status",
    "verified_by", "verified_at", "provenance", "created_at", "updated_at",
}
PANTRY_KEYS = {
    "id", "name", "quantity", "unit", "estimated_quantity", "confirmed_quantity",
    "is_guaranteed", "expiry_date", "storage_location", "verification_status",
    "verified_by", "verified_at", "provenance", "created_at", "updated_at",
}
TRIP_KEYS = {
    "id", "name", "destination", "start_date", "end_date", "party_size", "conditions",
    "status", "created_by", "created_at", "updated_at",
}


def _pantry(client, token, **overrides):
    payload = {"name": "Trail mix", "quantity": "2", "unit": "each"}
    payload.update(overrides)
    response = post(client, reverse("api:pantry-collection"), payload, token)
    assert response.status_code == 200, response.content
    return response.json()["pantry_item"]


def _confirmed_pantry(client, token, **overrides):
    item = _pantry(client, token, **overrides)
    response = post(
        client, reverse("api:pantry-confirm", args=[item["id"]]), {"confirm": True}, token
    )
    assert response.status_code == 200, response.content
    return response.json()["pantry_item"]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def test_unusable_tokens_fail_identically_and_write_nothing(client, tokens):
    before = GearItem.objects.count()
    missing = client.get(reverse("api:gear-collection"))
    malformed = client.get(reverse("api:gear-collection"), **auth("Bearer nope"))
    wrong_scheme = client.get(reverse("api:gear-collection"), HTTP_AUTHORIZATION="Basic abc")
    token = ApiToken.objects.get(key_prefix=tokens["primary"][:12])
    revoke_token(token)
    revoked = get(client, reverse("api:gear-collection"), tokens["primary"])
    for response in (missing, malformed, wrong_scheme, revoked):
        assert response.status_code == 401
        assert response.json() == UNAUTHORIZED
        assert response.headers["Cache-Control"] == "no-store"
    # A revoked token cannot mutate anything either.
    response = post(client, reverse("api:gear-collection"), {"name": "Ghost tent"}, tokens["primary"])
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert GearItem.objects.count() == before
    assert not GearItem.objects.filter(name="Ghost tent").exists()
    token.refresh_from_db()
    assert token.last_used_at is None


def test_session_login_does_not_authorize_the_api(client, owner):
    client.force_login(owner)
    response = client.get(reverse("api:gear-collection"))
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


def test_removing_the_service_membership_disables_only_that_household_token(
    client, tokens, household
):
    Membership.objects.filter(
        user__username="hermes", household=household
    ).delete()
    disabled = get(client, reverse("api:gear-collection"), tokens["primary"])
    assert disabled.status_code == 401
    assert disabled.json() == UNAUTHORIZED
    still_valid = get(client, reverse("api:gear-collection"), tokens["other"])
    assert still_valid.status_code == 200


# --------------------------------------------------------------------------- #
# Household scoping
# --------------------------------------------------------------------------- #


def test_gear_is_household_scoped_and_foreign_ids_are_indistinguishable(client, tokens):
    created = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Tent", "category": "shelter", "weight_grams": 1800},
        tokens["primary"],
    )
    assert created.status_code == 200
    item_id = created.json()["gear"]["id"]

    assert get(client, reverse("api:gear-collection"), tokens["other"]).json()["gear"] == []
    foreign = get(client, reverse("api:gear-detail", args=[item_id]), tokens["other"])
    missing = get(client, reverse("api:gear-detail", args=[uuid.uuid4()]), tokens["other"])
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND

    hijack = patch(
        client, reverse("api:gear-detail", args=[item_id]), {"name": "Hijacked"}, tokens["other"]
    )
    assert hijack.status_code == 404
    assert GearItem.objects.get(pk=item_id).name == "Tent"

    # The owner's household still sees exactly its own row.
    listing = get(client, reverse("api:gear-collection"), tokens["primary"]).json()
    assert listing["count"] == 1
    assert listing["gear"][0]["id"] == item_id


def test_api_refuses_food_categories_on_gear(client, tokens):
    response = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Mountain House bucket", "category": "Food", "quantity": 1},
        tokens["primary"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert get(client, reverse("api:gear-collection"), tokens["primary"]).json()["gear"] == []

    # The same shape is accepted once it is filed as pantry stock.
    pantry = post(
        client,
        reverse("api:pantry-collection"),
        {"name": "Mountain House bucket", "quantity": "30", "unit": "each"},
        tokens["primary"],
    )
    assert pantry.status_code == 200


def test_api_refuses_gear_categories_outside_the_vocabulary(client, tokens):
    response = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Mystery item", "category": "Camping", "quantity": 1},
        tokens["primary"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert get(client, reverse("api:gear-collection"), tokens["primary"]).json()["gear"] == []

    allowed = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Tent", "category": "Shelter", "quantity": 1},
        tokens["primary"],
    )
    assert allowed.status_code == 200
    assert allowed.json()["gear"]["category"] == "Shelter"



def test_trip_scope_is_pinned_even_when_the_service_identity_joins_both_households(
    client, tokens
):
    created = post(
        client, reverse("api:trip-collection"), {"name": "Other trip"}, tokens["other"]
    )
    trip_id = created.json()["trip"]["id"]
    foreign = get(client, reverse("api:trip-detail", args=[trip_id]), tokens["primary"])
    missing = get(client, reverse("api:trip-detail", args=[uuid.uuid4()]), tokens["primary"])
    assert foreign.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND
    assert get(client, reverse("api:trip-collection"), tokens["primary"]).json()["trips"] == []


# --------------------------------------------------------------------------- #
# Estimation versus confirmation
# --------------------------------------------------------------------------- #


def test_pantry_serializes_estimates_and_confirmed_stock_distinctly(client, tokens):
    item = _pantry(client, tokens["primary"], quantity="2")
    assert set(item) == PANTRY_KEYS
    assert item["verification_status"] == "estimated"
    assert item["estimated_quantity"] == "2.000"
    assert item["confirmed_quantity"] == "0.000"
    assert item["is_guaranteed"] is False
    assert item["verified_by"] is None
    assert item["verified_at"] is None
    assert item["provenance"]["confidence"] is None

    blocked = post(client, reverse("api:pantry-confirm", args=[item["id"]]), {}, tokens["primary"])
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["error"]["code"] == "confirmation_required"
    assert body["error"]["confirmation"]["action"] == "confirm_pantry_fact"
    assert body["error"]["confirmation"]["details"]["name"] == "Trail mix"
    assert PantryItem.objects.get(pk=item["id"]).verification_status == "estimated"

    confirmed = post(
        client, reverse("api:pantry-confirm", args=[item["id"]]), {"confirm": True}, tokens["primary"]
    )
    assert confirmed.status_code == 200
    payload = confirmed.json()["pantry_item"]
    assert payload["verification_status"] == "confirmed"
    assert payload["verified_by"] == "hermes"
    assert payload["verified_at"] is not None
    assert payload["confirmed_quantity"] == "2.000"
    assert payload["estimated_quantity"] == "0.000"
    assert payload["is_guaranteed"] is True

    change = InventoryChange.objects.filter(object_id=item["id"]).latest("created_at")
    assert change.actor.username == "hermes"
    assert change.action == "updated"


def test_meal_confirmation_reserves_confirmed_stock_only(client, tokens):
    pantry = _confirmed_pantry(client, tokens["primary"], name="Oats", quantity="2", unit="kg")
    trip = post(
        client, reverse("api:trip-collection"), {"name": "Pine weekend", "party_size": 2},
        tokens["primary"],
    ).json()["trip"]

    plan = post(
        client,
        reverse("api:trip-meal-plan", args=[trip["id"]]),
        {"meals": [{"name": "Porridge", "servings": 2,
                    "ingredients": [{"pantry_item_id": pantry["id"], "quantity": "1"}]}]},
        tokens["primary"],
    )
    assert plan.status_code == 200, plan.content
    assert plan.json()["meals"][0]["ingredients"][0]["unit"] == "kg"

    blocked = post(
        client, reverse("api:trip-meal-confirm", args=[trip["id"]]), {}, tokens["primary"]
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["confirmation"]["action"] == "confirm_meal_plan"
    assert Trip.objects.get(pk=trip["id"]).status == "planned"
    assert PantryItem.objects.get(pk=pantry["id"]).quantity == Decimal("2.000")

    confirmed = post(
        client,
        reverse("api:trip-meal-confirm", args=[trip["id"]]),
        {"confirm": True},
        tokens["primary"],
    )
    assert confirmed.status_code == 200, confirmed.content
    assert confirmed.json()["trip"]["status"] == "confirmed"
    assert len(confirmed.json()["reservations"]) == 1
    assert PantryItem.objects.get(pk=pantry["id"]).quantity == Decimal("1.000")


def test_planned_meal_cannot_reserve_estimated_stock(client, tokens):
    pantry = _pantry(client, tokens["primary"], name="Oats", quantity="2", unit="kg")
    trip = post(
        client, reverse("api:trip-collection"), {"name": "Unverified trip"}, tokens["primary"]
    ).json()["trip"]
    plan = post(
        client,
        reverse("api:trip-meal-plan", args=[trip["id"]]),
        {"meals": [{"name": "Porridge",
                    "ingredients": [{"pantry_item_id": pantry["id"], "quantity": "1"}]}]},
        tokens["primary"],
    )
    assert plan.status_code == 200
    response = post(
        client, reverse("api:trip-meal-confirm", args=[trip["id"]]), {"confirm": True}, tokens["primary"]
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert Trip.objects.get(pk=trip["id"]).status == "planned"
    assert PantryItem.objects.get(pk=pantry["id"]).quantity == Decimal("2.000")


# --------------------------------------------------------------------------- #
# Trips and gear schema
# --------------------------------------------------------------------------- #


def test_trip_crud_and_packed_weight_keep_estimates_and_confirmed_apart(client, tokens):
    created = post(
        client,
        reverse("api:trip-collection"),
        {"name": "Pine weekend", "destination": "North shore", "party_size": 3},
        tokens["primary"],
    )
    assert created.status_code == 200
    trip = created.json()["trip"]
    assert set(trip) == TRIP_KEYS
    assert trip["status"] == "planned"
    assert trip["created_by"] == "hermes"

    detail = get(client, reverse("api:trip-detail", args=[trip["id"]]), tokens["primary"]).json()
    assert detail["trip"]["id"] == trip["id"]
    assert detail["trip"]["packed_weight"] == {
        "estimated_grams": 0, "confirmed_grams": 0, "total_grams": 0,
    }
    assert detail["trip"]["meals"] == []
    assert [row["id"] for row in
            get(client, reverse("api:trip-collection"), tokens["primary"]).json()["trips"]] == [trip["id"]]

    invalid = post(
        client,
        reverse("api:trip-collection"),
        {"name": "Backwards", "start_date": "2026-05-02", "end_date": "2026-05-01"},
        tokens["primary"],
    )
    assert invalid.status_code == 400
    assert "end_date" in invalid.json()["error"]["fields"]
    assert Trip.objects.count() == 1


def test_gear_schema_matches_the_declared_contract(client, tokens):
    created = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Stove", "weight_grams": 500, "quantity": 2},
        tokens["primary"],
    )
    gear = created.json()["gear"]
    assert set(gear) == GEAR_KEYS
    assert gear["total_weight_grams"] == 1000
    assert gear["verification_status"] == "estimated"
    assert gear["verified_by"] is None
    assert gear["provenance"]["source_url"] == ""

    updated = patch(
        client, reverse("api:gear-detail", args=[gear["id"]]), {"condition": "worn"}, tokens["primary"]
    )
    assert updated.status_code == 200
    assert updated.json()["gear"]["condition"] == "worn"


# --------------------------------------------------------------------------- #
# Intake review and verification requests
# --------------------------------------------------------------------------- #


@pytest.fixture
def pending_intake(household, owner):
    submission = IntakeSubmission.objects.create(
        household=household,
        submitted_by=owner,
        kind=IntakeKind.PANTRY,
        channel=IntakeChannel.BARCODE,
        barcode="0123456789012",
        dedupe_key="smoke-dedupe",
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
    VerificationRequest.objects.create(
        household=household,
        submission=submission,
        created_by=owner,
        reason="Intake needs review: identity confidence is low.",
        field_names=["identity"],
    )
    return submission


def test_intake_status_review_and_verification_requests(client, tokens, pending_intake):
    listing = get(client, reverse("api:intake-collection"), tokens["primary"]).json()
    assert listing["count"] == 1
    payload = listing["intake"][0]
    assert payload["status"] == "needs_review"
    assert payload["candidate"]["name"] == "Trail mix"
    assert payload["candidate"]["confidence"]["overall"] == "0.700"
    assert payload["verification_request"]["status"] == "open"
    assert "image_key" not in payload

    requests = get(client, reverse("api:verification-requests"), tokens["primary"]).json()
    assert requests["count"] == 1
    request_payload = requests["verification_requests"][0]
    assert request_payload["field_names"] == ["identity"]
    assert request_payload["submission_id"] == str(pending_intake.pk)

    foreign = get(client, reverse("api:intake-detail", args=[pending_intake.pk]), tokens["other"])
    missing = get(client, reverse("api:intake-detail", args=[uuid.uuid4()]), tokens["other"])
    assert foreign.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND

    blocked = post(
        client,
        reverse("api:intake-review", args=[pending_intake.pk]),
        {"action": "approve"},
        tokens["primary"],
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "confirmation_required"
    assert not PantryItem.objects.filter(household=pending_intake.household).exists()

    approved = post(
        client,
        reverse("api:intake-review", args=[pending_intake.pk]),
        {"action": "approve", "confirm": True, "note": "Household confirmed the label."},
        tokens["primary"],
    )
    assert approved.status_code == 200, approved.content
    body = approved.json()["intake"]
    assert body["status"] == "added"
    assert body["created_item"]["kind"] == "pantry"
    created = PantryItem.objects.get(pk=body["created_item"]["id"])
    assert created.verification_status == "confirmed"
    assert created.verified_by.username == "hermes"
    assert body["verification_request"] is None
    resolved = VerificationRequest.objects.get(pk=request_payload["id"])
    assert resolved.status == VerificationRequestStatus.RESOLVED
    assert resolved.resolved_by.username == "hermes"
    change = InventoryChange.objects.filter(action="intake_approved").latest("created_at")
    assert change.actor.username == "hermes"


def test_intake_reject_requires_confirmation_and_leaves_no_inventory(client, tokens, pending_intake):
    blocked = post(
        client,
        reverse("api:intake-review", args=[pending_intake.pk]),
        {"action": "reject"},
        tokens["primary"],
    )
    assert blocked.status_code == 409
    pending_intake.refresh_from_db()
    assert pending_intake.status == IntakeStatus.NEEDS_REVIEW

    rejected = post(
        client,
        reverse("api:intake-review", args=[pending_intake.pk]),
        {"action": "reject", "confirm": True, "note": "Not our product."},
        tokens["primary"],
    )
    assert rejected.status_code == 200
    pending_intake.refresh_from_db()
    assert pending_intake.status == IntakeStatus.REJECTED
    assert not PantryItem.objects.filter(household=pending_intake.household).exists()
    assert InventoryChange.objects.filter(action="intake_rejected").count() == 1


# --------------------------------------------------------------------------- #
# Sourced prices and nutrition
# --------------------------------------------------------------------------- #


def test_price_observations_and_nutrition_sources_are_household_scoped(client, tokens, household, owner):
    item = _pantry(client, tokens["primary"], name="Trail mix")
    recorded = post(
        client,
        reverse("api:pantry-prices", args=[item["id"]]),
        {
            "amount": "4.25",
            "currency": "usd",
            "merchant": "CampCo",
            "source": "receipt",
            "source_url": "https://example.test/receipt/42",
            "observed_at": timezone.now().isoformat(),
            "confidence": "0.900",
        },
        tokens["primary"],
    )
    assert recorded.status_code == 200, recorded.content
    price = recorded.json()["price"]
    assert price["amount"] == "4.25"
    assert price["currency"] == "USD"
    assert price["is_stale"] is False
    assert price["verification_status"] == "estimated"
    assert price["source_url"] == "https://example.test/receipt/42"
    observation = PantryItem.objects.get(pk=item["id"]).price_observations.get()
    assert observation.actor.username == "hermes"

    history = get(client, reverse("api:pantry-prices", args=[item["id"]]), tokens["primary"]).json()
    assert history["count"] == 1
    assert history["latest"]["id"] == price["id"]

    upsert_nutrition_fact(
        actor=owner,
        household=household,
        item_id=item["id"],
        data={
            "nutrient": "protein_g",
            "amount": Decimal("3.500"),
            "unit": "g",
            "source": "fake-nutrition:2039158",
            "source_url": "https://fdc.nal.usda.gov/fdc-app.html#/food-details/2039158/nutrients",
            "observed_at": timezone.now(),
            "confidence": Decimal("0.900"),
            "verification_status": "estimated",
        },
    )
    nutrition = get(
        client, reverse("api:pantry-nutrition", args=[item["id"]]), tokens["primary"]
    ).json()
    assert nutrition["count"] == 1
    fact = nutrition["facts"][0]
    assert fact["nutrient"] == "protein_g"
    assert fact["verification_status"] == "estimated"
    assert fact["source_url"].startswith("https://")

    # Neither read nor write crosses households.
    foreign = get(client, reverse("api:pantry-prices", args=[item["id"]]), tokens["other"])
    assert foreign.status_code == 404
    assert foreign.json() == NOT_FOUND
    write = post(
        client,
        reverse("api:pantry-prices", args=[item["id"]]),
        {"amount": "9.99"},
        tokens["other"],
    )
    assert write.status_code == 404
    assert PantryItem.objects.get(pk=item["id"]).price_observations.count() == 1


def test_invalid_price_is_rejected_without_writing(client, tokens):
    item = _pantry(client, tokens["primary"])
    response = post(
        client, reverse("api:pantry-prices", args=[item["id"]]), {"amount": "0"}, tokens["primary"]
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert PantryItem.objects.get(pk=item["id"]).price_observations.count() == 0


# --------------------------------------------------------------------------- #
# Allowlist and payload discipline
# --------------------------------------------------------------------------- #


def test_destructive_and_unlisted_operations_are_not_exposed(client, tokens):
    gear = post(
        client, reverse("api:gear-collection"), {"name": "Tent"}, tokens["primary"]
    ).json()["gear"]
    deleted = client.delete(reverse("api:gear-detail", args=[gear["id"]]), **auth(tokens["primary"]))
    assert deleted.status_code == 405
    assert GearItem.objects.filter(pk=gear["id"]).exists()
    replaced = client.put(
        reverse("api:gear-detail", args=[gear["id"]]),
        data="{}",
        content_type="application/json",
        **auth(tokens["primary"]),
    )
    assert replaced.status_code == 405


def test_unknown_or_privileged_fields_are_rejected_without_writing(client, tokens):
    response = post(
        client,
        reverse("api:gear-collection"),
        {"name": "Pre-confirmed tent", "verification_status": "confirmed"},
        tokens["primary"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "verification_status" in response.json()["error"]["message"]
    assert not GearItem.objects.filter(name="Pre-confirmed tent").exists()

    response = post(
        client,
        reverse("api:pantry-collection"),
        {"name": "Bucket of oats", "quantity": "1", "unit": "bucket"},
        tokens["primary"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert "unit" in response.json()["error"]["fields"]
    assert not PantryItem.objects.filter(name="Bucket of oats").exists()


def test_non_json_bodies_are_rejected(client, tokens):
    response = client.post(
        reverse("api:gear-collection"),
        data="name=Tent",
        content_type="application/x-www-form-urlencoded",
        **auth(tokens["primary"]),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
