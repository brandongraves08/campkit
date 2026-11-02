"""Stable JSON shapes for the versioned ``/api/v1`` contract.

Serializers are hand-written so the project keeps a single, explicit contract
without a third-party framework: decimals are strings, datetimes are ISO-8601,
and every payload separates an estimated fact from a confirmed one so a client
cannot read a guess as guaranteed stock.
"""

from decimal import Decimal

from apps.intake.models import VerificationRequestStatus

#: Inventory quantities and confidences are stored with three decimal places;
#: the API always renders that full scale so a client parsing "2" and "2.000"
#: never sees two different shapes for one value.
QUANTITY_QUANTUM = Decimal("0.001")


def _decimal(value):
    return None if value is None else str(value)


def _scaled(value):
    if value is None:
        return None
    return str(Decimal(value).quantize(QUANTITY_QUANTUM))


def _iso(value):
    return None if value is None else value.isoformat()


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _username(user):
    return user.get_username() if user is not None else None


def verification_fields(obj) -> dict:
    """Flat, explicit confirmation state shared by gear and pantry payloads."""
    return {
        "verification_status": obj.verification_status,
        "verified_by": _username(obj.verified_by),
        "verified_at": _iso(obj.verified_at),
    }


def provenance_fields(obj) -> dict:
    return {
        "provenance": {
            "source": obj.source,
            "source_url": obj.source_url,
            "observed_at": _iso(obj.observed_at),
            "confidence": _decimal(obj.confidence),
        }
    }


def gear_dict(item) -> dict:
    return {
        "id": str(item.pk),
        "name": item.name,
        "category": item.category,
        "owner": item.owner,
        "condition": item.condition,
        "weight_grams": item.weight_grams,
        "quantity": item.quantity,
        "total_weight_grams": item.total_weight_grams,
        "storage_location": item.storage_location,
        "availability": item.availability,
        **verification_fields(item),
        **provenance_fields(item),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def pantry_dict(item) -> dict:
    return {
        "id": str(item.pk),
        "name": item.name,
        "quantity": _scaled(item.quantity),
        "unit": item.unit,
        "estimated_quantity": _scaled(item.estimated_quantity),
        "confirmed_quantity": _scaled(item.confirmed_quantity),
        "is_guaranteed": item.verification_status == "confirmed",
        "expiry_date": _iso(item.expiry_date),
        "storage_location": item.storage_location,
        **verification_fields(item),
        **provenance_fields(item),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def trip_dict(trip) -> dict:
    return {
        "id": str(trip.pk),
        "name": trip.name,
        "destination": trip.destination,
        "start_date": _iso(trip.start_date),
        "end_date": _iso(trip.end_date),
        "party_size": trip.party_size,
        "conditions": trip.conditions,
        "status": trip.status,
        "created_by": _username(trip.created_by),
        "created_at": _iso(trip.created_at),
        "updated_at": _iso(trip.updated_at),
    }


def ingredient_dict(ingredient) -> dict:
    return {
        "id": str(ingredient.pk),
        "pantry_item_id": str(ingredient.pantry_item_id),
        "quantity": _scaled(ingredient.quantity),
        "unit": ingredient.unit,
    }


def meal_dict(meal, *, ingredients=None) -> dict:
    rows = meal.ingredients.all() if ingredients is None else ingredients
    return {
        "id": str(meal.pk),
        "name": meal.name,
        "scheduled_for": _iso(meal.scheduled_for),
        "servings": meal.servings,
        "notes": meal.notes,
        "ingredients": [ingredient_dict(row) for row in rows],
    }


def reservation_dict(reservation) -> dict:
    return {
        "id": str(reservation.pk),
        "meal_id": str(reservation.meal_id),
        "pantry_item_id": str(reservation.pantry_item_id),
        "quantity": _scaled(reservation.quantity),
        "consumed_quantity": _scaled(reservation.consumed_quantity),
        "status": reservation.status,
        "created_at": _iso(reservation.created_at),
    }


def candidate_dict(candidate) -> dict | None:
    if candidate is None:
        return None
    return {
        "name": candidate.name,
        "category": candidate.category,
        "brand": candidate.brand,
        "model_number": candidate.model_number,
        "barcode": candidate.barcode,
        "quantity": _scaled(candidate.quantity),
        "unit": candidate.unit,
        "weight_grams": candidate.weight_grams,
        "nutrition": _json_value(candidate.nutrition),
        "allergens": _json_value(candidate.allergens),
        "provider": candidate.provider,
        "model_version": candidate.model_version,
        "confidence": {
            "overall": _scaled(candidate.overall_confidence),
            "identity": _scaled(candidate.identity_confidence),
            "quantity": _scaled(candidate.quantity_confidence),
            "nutrition": _scaled(candidate.nutrition_confidence),
            "allergen": _scaled(candidate.allergen_confidence),
        },
        "evidence": [
            {
                "field": row.field_name,
                "value": _json_value(row.value),
                "source": row.source,
                "source_url": row.source_url,
                "confidence": _scaled(row.confidence),
                "observed_at": _iso(row.observed_at),
            }
            for row in candidate.field_evidence.all()
        ],
    }


def verification_request_dict(request) -> dict:
    return {
        "id": str(request.pk),
        "submission_id": str(request.submission_id) if request.submission_id else None,
        "reason": request.reason,
        "field_names": list(request.field_names or []),
        "status": request.status,
        "channel": request.channel,
        "resolution": request.resolution,
        "resolved_at": _iso(request.resolved_at),
        "created_at": _iso(request.created_at),
        "hermes_status": request.hermes_status,
    }


def intake_dict(submission) -> dict:
    """One intake submission with its candidate and open verification request.

    The stored image key is never serialized: photos stay behind the
    household-scoped image endpoint, so the API cannot become a raw-image bypass.
    The relation is iterated (not filtered) so a prefetched list is reused.
    """
    candidate = getattr(submission, "candidate", None)
    open_request = next(
        (
            row
            for row in submission.verification_requests.all()
            if row.status == VerificationRequestStatus.OPEN
        ),
        None,
    )
    return {
        "id": str(submission.pk),
        "kind": submission.kind,
        "channel": submission.channel,
        "status": submission.status,
        "barcode": submission.barcode,
        "failure_reason": submission.failure_reason,
        "attempts": submission.attempts,
        "has_image": bool(submission.image_key),
        "created_item": (
            {"kind": submission.created_item_kind, "id": submission.created_item_id}
            if submission.created_item_id
            else None
        ),
        "candidate": candidate_dict(candidate),
        "verification_request": (
            verification_request_dict(open_request) if open_request is not None else None
        ),
        "created_at": _iso(submission.created_at),
        "updated_at": _iso(submission.updated_at),
        "processed_at": _iso(submission.processed_at),
    }
