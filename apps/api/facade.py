"""Adapter shared by the REST views and the MCP tools.

Every operation delegates to an existing domain service (inventory, trips,
meals, intake, enrichment) — the same functions the server-rendered UI calls.
This module only translates external JSON into service arguments and results
back into a stable payload, and it owns the two cross-surface rules that must
not drift:

* the token's household pins every lookup, and
* a confirmation-gated mutation refuses to run until ``confirm`` is true.

It deliberately contains no inventory, reservation, intake, or pricing rules.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.api import serializers
from apps.enrichment.services import nutrition_facts as enrichment_nutrition_facts
from apps.enrichment.services import price_history as enrichment_price_history
from apps.enrichment.services import record_price as enrichment_record_price
from apps.households.permissions import require_household_member
from apps.intake import services as intake
from apps.inventory import services as inventory
from apps.meals import services as meals
from apps.meals.models import Meal
from apps.trips import services as trips
from apps.trips.models import Trip


class InvalidRequest(Exception):
    """The caller's payload cannot be mapped onto a service call."""


class NotFound(Exception):
    """No record the token's household may see.

    A foreign-household id and a nonexistent id raise this same error; neither
    the REST layer nor the MCP layer may reveal which one it was.
    """


class ConfirmationRequired(Exception):
    """A sensitive mutation needs an explicit, human-carried confirmation."""

    def __init__(self, *, action, prompt, details=None):
        super().__init__(prompt)
        self.action = action
        self.prompt = prompt
        self.details = details or {}

    def as_dict(self) -> dict:
        return {
            "required": True,
            "action": self.action,
            "prompt": self.prompt,
            "details": self.details,
        }


GEAR_FIELDS = {
    "name",
    "category",
    "owner",
    "condition",
    "weight_grams",
    "quantity",
    "storage_location",
    "availability",
}
PANTRY_FIELDS = {"name", "quantity", "unit", "expiry_date", "storage_location"}
TRIP_FIELDS = {"name", "destination", "start_date", "end_date", "party_size", "conditions"}
MEAL_FIELDS = {"name", "servings", "scheduled_for", "notes"}
INGREDIENT_FIELDS = {"pantry_item_id", "quantity", "unit"}
PRICE_FIELDS = {"amount", "currency", "merchant", "source", "source_url", "observed_at", "confidence"}
REVIEW_ACTIONS = {"approve", "correct", "confirm", "reject"}


def _uuid(value, field_name):
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidRequest(f"{field_name} must be a UUID.") from exc


def _clean(payload, allowed, *, required=()):
    if not isinstance(payload, dict):
        raise InvalidRequest("A JSON object is required.")
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise InvalidRequest(f"Unsupported fields: {', '.join(unknown)}.")
    missing = [name for name in required if payload.get(name) in (None, "")]
    if missing:
        raise InvalidRequest(f"Missing required fields: {', '.join(missing)}.")
    return {key: value for key, value in payload.items() if key in allowed}


def _reject_unknown(payload, allowed):
    if not isinstance(payload, dict):
        raise InvalidRequest("A JSON object is required.")
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise InvalidRequest(f"Unsupported fields: {', '.join(unknown)}.")


def _confirmation(payload, *, action, prompt, details=None):
    if not isinstance(payload, dict) or payload.get("confirm") is not True:
        raise ConfirmationRequired(action=action, prompt=prompt, details=details)


def format_validation_error(exc) -> tuple[str, dict]:
    """Return ``(message, fields)`` for a Django ``ValidationError``."""
    if hasattr(exc, "message_dict"):
        fields = {name: [str(message) for message in values] for name, values in exc.message_dict.items()}
    else:
        fields = {"non_field_errors": [str(message) for message in exc.messages]}
    message = "; ".join(
        f"{name}: {', '.join(values)}" for name, values in fields.items()
    )
    return message, fields


def _scoped_trip(identity, trip_id):
    """Fetch a trip the token's household owns.

    ``get_trip_for_actor`` only proves the actor is a member of the trip's
    household; the service identity may belong to more than one household, so
    the token's household is pinned here before any trip or meal write.
    """
    trip = trips.get_trip_for_actor(trip_id, identity.actor)
    if trip.household_id != identity.household.pk:
        raise NotFound
    return trip


# --------------------------------------------------------------------------- #
# Trips
# --------------------------------------------------------------------------- #


def list_trips(identity) -> dict:
    require_household_member(identity.actor, identity.household.pk)
    rows = Trip.objects.for_household(identity.household).order_by("-created_at")
    return {"trips": [serializers.trip_dict(trip) for trip in rows], "count": len(rows)}


def get_trip(identity, trip_id) -> dict:
    trip = _scoped_trip(identity, _uuid(trip_id, "trip_id"))
    meals_rows = (
        Meal.objects.filter(trip=trip)
        .prefetch_related("ingredients")
        .order_by("scheduled_for", "created_at")
    )
    payload = serializers.trip_dict(trip)
    payload["packed_weight"] = trips.packed_base_weight(trip_id=trip.pk, actor=identity.actor)
    payload["meals"] = [serializers.meal_dict(meal) for meal in meals_rows]
    return {"trip": payload}


def create_trip(identity, payload) -> dict:
    data = _clean(payload, TRIP_FIELDS, required=("name",))
    trip = trips.create_trip(actor=identity.actor, household=identity.household, data=data)
    return {"trip": serializers.trip_dict(trip)}


def plan_meals(identity, payload) -> dict:
    _reject_unknown(payload, {"trip_id", "meals"})
    trip = _scoped_trip(identity, _uuid(payload.get("trip_id"), "trip_id"))
    requested = payload.get("meals")
    if not isinstance(requested, list) or not requested:
        raise InvalidRequest("meals must be a non-empty list of meal objects.")
    created = []
    with transaction.atomic():
        for entry in requested:
            if not isinstance(entry, dict):
                raise InvalidRequest("Each meal must be a JSON object.")
            values = dict(entry)
            ingredients = values.pop("ingredients", [])
            data = _clean(values, MEAL_FIELDS, required=("name",))
            created.append(
                meals.create_meal(
                    trip_id=trip.pk,
                    actor=identity.actor,
                    data=data,
                    ingredients=_ingredients(ingredients or []),
                )
            )
    return {
        "trip_id": str(trip.pk),
        "meals": [serializers.meal_dict(meal) for meal in created],
    }


def _ingredients(entries) -> list[dict]:
    if not isinstance(entries, list):
        raise InvalidRequest("ingredients must be a list.")
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise InvalidRequest("Each ingredient must be a JSON object.")
        unknown = sorted(set(entry) - INGREDIENT_FIELDS)
        if unknown:
            raise InvalidRequest(f"Unsupported ingredient fields: {', '.join(unknown)}.")
        if entry.get("pantry_item_id") in (None, "") or entry.get("quantity") in (None, ""):
            raise InvalidRequest("Each ingredient needs pantry_item_id and quantity.")
        row = {
            "pantry_item_id": _uuid(entry["pantry_item_id"], "pantry_item_id"),
            "quantity": entry["quantity"],
        }
        if entry.get("unit") not in (None, ""):
            row["unit"] = entry["unit"]
        cleaned.append(row)
    return cleaned


def confirm_meal_plan(identity, payload) -> dict:
    _reject_unknown(payload, {"trip_id", "confirm"})
    trip = _scoped_trip(identity, _uuid(payload.get("trip_id"), "trip_id"))
    _confirmation(
        payload,
        action="confirm_meal_plan",
        prompt=(
            f"Confirm the meal plan for trip '{trip.name}': confirmed pantry stock is "
            "reserved and the trip becomes confirmed."
        ),
        details={"trip_id": str(trip.pk), "trip_status": trip.status},
    )
    reservations = meals.confirm_meal_plan(trip_id=trip.pk, actor=identity.actor)
    trip.refresh_from_db()
    return {
        "trip": serializers.trip_dict(trip),
        "reservations": [serializers.reservation_dict(row) for row in reservations],
    }


# --------------------------------------------------------------------------- #
# Gear
# --------------------------------------------------------------------------- #


def list_gear(identity, payload=None) -> dict:
    _reject_unknown(payload or {}, {"category"})
    rows = inventory.list_gear(actor=identity.actor, household=identity.household)
    category = (payload or {}).get("category")
    if category:
        rows = rows.filter(category=str(category))
    rows = rows.order_by("name")
    return {"gear": [serializers.gear_dict(item) for item in rows], "count": len(rows)}


def get_gear(identity, gear_id) -> dict:
    item = inventory.get_gear_item(
        actor=identity.actor, household=identity.household, item_id=_uuid(gear_id, "gear_id")
    )
    return {"gear": serializers.gear_dict(item)}


def add_gear(identity, payload) -> dict:
    data = _clean(payload, GEAR_FIELDS, required=("name",))
    item = inventory.create_gear_item(actor=identity.actor, household=identity.household, data=data)
    return {"gear": serializers.gear_dict(item)}


def update_gear(identity, payload) -> dict:
    values = dict(payload)
    gear_id = _uuid(values.pop("gear_id", None), "gear_id")
    data = _clean(values, GEAR_FIELDS)
    if not data:
        raise InvalidRequest("At least one gear field must be supplied.")
    item = inventory.update_gear_item(
        actor=identity.actor, household=identity.household, item_id=gear_id, data=data
    )
    return {"gear": serializers.gear_dict(item)}


# --------------------------------------------------------------------------- #
# Pantry
# --------------------------------------------------------------------------- #


def list_pantry(identity) -> dict:
    rows = inventory.list_pantry(actor=identity.actor, household=identity.household).order_by("name")
    return {"pantry": [serializers.pantry_dict(item) for item in rows], "count": len(rows)}


def get_pantry(identity, item_id) -> dict:
    item = inventory.get_pantry_item(
        actor=identity.actor, item_id=_uuid(item_id, "pantry_item_id"), household=identity.household
    )
    return {"pantry_item": serializers.pantry_dict(item)}


def add_pantry_item(identity, payload) -> dict:
    data = _clean(payload, PANTRY_FIELDS, required=("name", "quantity", "unit"))
    item = inventory.create_pantry_item(actor=identity.actor, household=identity.household, data=data)
    return {"pantry_item": serializers.pantry_dict(item)}


def update_pantry_item(identity, payload) -> dict:
    values = dict(payload)
    item_id = _uuid(values.pop("pantry_item_id", None), "pantry_item_id")
    data = _clean(values, PANTRY_FIELDS)
    if not data:
        raise InvalidRequest("At least one pantry field must be supplied.")
    item = inventory.update_pantry_item(
        actor=identity.actor, household=identity.household, item_id=item_id, data=data
    )
    return {"pantry_item": serializers.pantry_dict(item)}


def confirm_inventory_fact(identity, payload) -> dict:
    """Confirm a gear or pantry fact as household-verified stock."""
    _reject_unknown(payload, {"kind", "item_id", "confirm"})
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in {"gear", "pantry"}:
        raise InvalidRequest("kind must be 'gear' or 'pantry'.")
    item_id = _uuid(payload.get("item_id"), "item_id")
    if kind == "gear":
        item = inventory.get_gear_item(
            actor=identity.actor, household=identity.household, item_id=item_id
        )
        current = item.verification_status
        name = item.name
    else:
        item = inventory.get_pantry_item(
            actor=identity.actor, item_id=item_id, household=identity.household
        )
        current = item.verification_status
        name = item.name
    _confirmation(
        payload,
        action=f"confirm_{kind}_fact",
        prompt=(
            f"Confirm the {kind} record '{name}' as verified household data. "
            "Confirmed pantry quantities become guaranteed trip stock."
        ),
        details={"kind": kind, "item_id": str(item_id), "name": name, "current_status": current},
    )
    if current == "confirmed":
        if kind == "gear":
            return {"gear": serializers.gear_dict(item), "changed": False}
        return {"pantry_item": serializers.pantry_dict(item), "changed": False}
    if kind == "gear":
        updated = inventory.update_gear_item(
            actor=identity.actor,
            household=identity.household,
            item_id=item_id,
            data={"verification_status": "confirmed"},
        )
        return {"gear": serializers.gear_dict(updated), "changed": True}
    updated = inventory.update_pantry_item(
        actor=identity.actor,
        household=identity.household,
        item_id=item_id,
        data={"verification_status": "confirmed"},
    )
    return {"pantry_item": serializers.pantry_dict(updated), "changed": True}


# --------------------------------------------------------------------------- #
# Intake and verification
# --------------------------------------------------------------------------- #


def list_intake(identity) -> dict:
    rows = intake.list_intake_submissions(
        actor=identity.actor, household=identity.household
    ).prefetch_related("candidate__field_evidence", "verification_requests")
    return {"intake": [serializers.intake_dict(row) for row in rows], "count": len(rows)}


def get_intake(identity, submission_id) -> dict:
    submission = intake.get_submission(
        actor=identity.actor,
        household=identity.household,
        submission_id=_uuid(submission_id, "submission_id"),
    )
    return {"intake": serializers.intake_dict(submission)}


def list_verification_requests(identity, payload=None) -> dict:
    open_only = True
    if payload is not None and "open_only" in payload:
        open_only = payload["open_only"]
        if not isinstance(open_only, bool):
            raise InvalidRequest("open_only must be true or false.")
    rows = intake.list_verification_requests(
        actor=identity.actor, household=identity.household, open_only=open_only
    )
    payload_rows = [serializers.verification_request_dict(row) for row in rows]
    return {"verification_requests": payload_rows, "count": len(payload_rows)}


def review_intake(identity, payload) -> dict:
    _reject_unknown(payload, {"submission_id", "action", "corrections", "note", "confirm"})
    submission = intake.get_submission(
        actor=identity.actor,
        household=identity.household,
        submission_id=_uuid(payload.get("submission_id"), "submission_id"),
    )
    action = str(payload.get("action") or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise InvalidRequest("action must be approve, correct, confirm, or reject.")
    corrections = payload.get("corrections") or {}
    if not isinstance(corrections, dict):
        raise InvalidRequest("corrections must be a JSON object.")
    note = payload.get("note") or ""
    if not isinstance(note, str):
        raise InvalidRequest("note must be text.")
    candidate = getattr(submission, "candidate", None)
    _confirmation(
        payload,
        action=f"review_intake_{action}",
        prompt=(
            f"Review intake {submission.pk} ({submission.status}) with action '{action}'. "
            "Approving or correcting creates verified inventory; rejecting discards the review."
        ),
        details={
            "submission_id": str(submission.pk),
            "status": submission.status,
            "candidate": candidate.name if candidate is not None else None,
        },
    )
    submission = intake.resolve_intake(
        submission.pk, actor=identity.actor,
        decision={"action": action, "corrections": corrections, "note": note},
    )
    submission.refresh_from_db()
    open_requests = submission.verification_requests.filter(status="open")
    return {
        "intake": serializers.intake_dict(submission),
        "verification_requests": [
            serializers.verification_request_dict(row) for row in open_requests
        ],
    }


# --------------------------------------------------------------------------- #
# Sourced prices and nutrition
# --------------------------------------------------------------------------- #


def record_price(identity, payload) -> dict:
    values = dict(payload)
    _reject_unknown(values, PRICE_FIELDS | {"pantry_item_id"})
    item = inventory.get_pantry_item(
        actor=identity.actor,
        item_id=_uuid(values.pop("pantry_item_id", None), "pantry_item_id"),
        household=identity.household,
    )
    data = _clean(values, PRICE_FIELDS, required=("amount",))
    observation = enrichment_record_price(
        item_id=item.pk,
        amount=data.pop("amount"),
        actor=identity.actor,
        **data,
    )
    history = enrichment_price_history(
        actor=identity.actor, household=identity.household, item_id=item.pk
    )
    recorded = next(
        (entry for entry in history if entry["id"] == str(observation.pk)), None
    )
    return {"item_id": str(item.pk), "price": recorded, "prices": history}


def price_history(identity, item_id) -> dict:
    item = inventory.get_pantry_item(
        actor=identity.actor,
        item_id=_uuid(item_id, "pantry_item_id"),
        household=identity.household,
    )
    history = enrichment_price_history(
        actor=identity.actor, household=identity.household, item_id=item.pk
    )
    return {
        "item_id": str(item.pk),
        "prices": history,
        "latest": history[0] if history else None,
        "count": len(history),
    }


def nutrition_facts(identity, item_id) -> dict:
    item = inventory.get_pantry_item(
        actor=identity.actor,
        item_id=_uuid(item_id, "pantry_item_id"),
        household=identity.household,
    )
    facts = enrichment_nutrition_facts(
        actor=identity.actor, household=identity.household, item_id=item.pk
    )
    return {"item_id": str(item.pk), "facts": facts, "count": len(facts)}
