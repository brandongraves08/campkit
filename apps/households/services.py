"""Home/advisor composition for the task-first household screens.

This module composes read-only domain services into explained suggestions. It
owns no inventory rules: every recommendation is derived from records a service
returned, names that source, and is never written back as inventory.
"""

from apps.households.permissions import require_household_member
from apps.households.models import Membership
from apps.inventory.services import (
    expiring_pantry,
    list_gear,
    verification_backlog,
)
from apps.intake.services import list_verification_requests
from apps.trips.services import trip_readiness, upcoming_trips


def household_role(*, actor, household):
    """The actor's role in one household, or ``None`` if they do not belong."""
    if actor is None or not actor.is_authenticated:
        return None
    return (
        Membership.objects.filter(user=actor, household=household)
        .values_list("role", flat=True)
        .first()
    )


def _day_label(value):
    return value.strftime("%d %b %Y") if hasattr(value, "strftime") else str(value)


def household_recommendations(*, actor, household):
    """Read-only, explained suggestions for one household.

    Each item carries ``source`` (where the observation came from) and an
    ``action_key`` the view maps to a URL. Nothing here creates or mutates an
    inventory record, and no value is invented when a record is missing.
    """
    require_household_member(actor, household.pk)
    recommendations = []

    backlog = verification_backlog(actor=actor, household=household)
    if backlog["total"]:
        plural = "s" if backlog["total"] != 1 else ""
        recommendations.append({
            "key": "verification_backlog",
            "severity": "attention",
            "title": f"Confirm {backlog['total']} estimated fact{plural}",
            "detail": (
                "Estimated facts are visible but are not guaranteed stock. Only confirmed "
                "pantry quantities can be reserved for a trip."
            ),
            "source": "Inventory records (estimated vs confirmed)",
            "action_key": "verification_queue",
            "action_label": "Review verification queue",
        })

    expiring = list(expiring_pantry(actor=actor, household=household))
    if expiring:
        earliest = expiring[0]
        names = ", ".join(f"{item.name} ({_day_label(item.expiry_date)})" for item in expiring[:4])
        recommendations.append({
            "key": "expiring_pantry",
            "severity": "attention",
            "title": "Plan meals around pantry items expiring soon",
            "detail": f"Recorded expiry dates: {names}.",
            "source": "Pantry expiry dates",
            "action_key": "pantry_list",
            "action_label": f"Open pantry ({len(expiring)} expiring)",
            "detail_secondary": earliest.name,
        })

    trips = list(upcoming_trips(actor=actor, household=household, limit=1))
    if trips:
        readiness = trip_readiness(trip_id=trips[0].pk, actor=actor)
        if readiness["days_until"] is not None:
            timing = (
                f"starts in {readiness['days_until']} day{'s' if readiness['days_until'] != 1 else ''}"
                if readiness["days_until"] >= 0 else "start date has passed"
            )
        else:
            timing = "has no start date yet"
        recommendations.append({
            "key": "trip_packing",
            "severity": "info",
            "title": f"Finish packing for {readiness['trip'].name}",
            "detail": (
                f"{readiness['packed_items']} of {readiness['total_items']} packed items are "
                f"checked off and the trip {timing}."
            ),
            "source": "Trip packing list",
            "action_key": "trip_detail",
            "action_label": "Open trip",
            "trip_id": readiness["trip"].pk,
        })

    gear = list(list_gear(actor=actor, household=household))
    missing_weight = [item for item in gear if item.weight_grams is None]
    if missing_weight:
        plural = "s" if len(missing_weight) != 1 else ""
        names = ", ".join(item.name for item in missing_weight[:4])
        recommendations.append({
            "key": "gear_without_weight",
            "severity": "info",
            "title": f"Add weights to {len(missing_weight)} gear item{plural}",
            "detail": f"Without a weight these items ({names}) are left out of base-weight totals.",
            "source": "Gear weights",
            "action_key": "gear_list",
            "action_label": "Open gear",
        })

    open_requests = list_verification_requests(actor=actor, household=household)
    if open_requests:
        plural = "s" if len(open_requests) != 1 else ""
        recommendations.append({
            "key": "open_intake_questions",
            "severity": "attention",
            "title": f"Answer {len(open_requests)} open intake question{plural}",
            "detail": "A photo or barcode could not be confirmed automatically; review or correct it before it counts.",
            "source": "Smart intake verification requests",
            "action_key": "intake_list",
            "action_label": "Open smart intake",
        })

    return recommendations
