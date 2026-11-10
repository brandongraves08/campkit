import hashlib
import json
import uuid

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from apps.mobile_api.models import PackingOperation
from apps.mobile_api.permissions import authenticate_mobile, not_found
from apps.mobile_api.serializers import household_dict, packing_snapshot, trip_dict
from apps.mobile_api.services import (
    active_household_for_member,
    member_households,
    trip_for_member_household,
    trips_for_member_household,
)
from apps.trips.services import TripGearVersionConflict, set_trip_gear_packed_versioned


def _sync_error(code, message, status, **extra):
    return JsonResponse({"error": {"code": code, "message": message, **extra}}, status=status)


def _parse_sync_payload(request):
    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"packed", "client_operation_id", "base_version"}:
        return None
    if type(payload["packed"]) is not bool:
        return None
    if not isinstance(payload["client_operation_id"], str):
        return None
    try:
        operation_id = uuid.UUID(payload["client_operation_id"])
    except (ValueError, AttributeError):
        return None
    base_version = payload["base_version"]
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        return None
    return {"packed": payload["packed"], "client_operation_id": operation_id, "base_version": base_version}


def _request_hash(*, household_id, trip_id, entry_id, payload):
    canonical = {
        "household_id": str(household_id),
        "trip_id": str(trip_id),
        "entry_id": int(entry_id),
        "packed": payload["packed"],
        "base_version": payload["base_version"],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _conflict_response():
    return _sync_error("idempotency_key_reused", "Operation ID was already used for a different request.", 409)


@require_GET
@authenticate_mobile
def households(request):
    rows = member_households(member=request.user)
    return JsonResponse({"households": [household_dict(row) for row in rows]})


@require_GET
@authenticate_mobile
def household_trips(request, household_id):
    household = active_household_for_member(household_id=household_id, member=request.user)
    if household is None:
        return not_found()
    # Keep the established membership-checked trip service as the query boundary.
    trips = trips_for_member_household(member=request.user, household=household)
    return JsonResponse({"trips": [trip_dict(trip) for trip in trips]})


@require_GET
@authenticate_mobile
def trip_packing(request, household_id, trip_id):
    household = active_household_for_member(household_id=household_id, member=request.user)
    if household is None:
        return not_found()
    # Do not query trips until the requested household membership is proven.
    trip = trip_for_member_household(trip_id=trip_id, household=household)
    if trip is None:
        return not_found()
    return JsonResponse(packing_snapshot(trip=trip, household=household))


@require_http_methods(["PUT"])
@authenticate_mobile
@transaction.atomic
def packing_sync(request, household_id, trip_id, entry_id):
    payload = _parse_sync_payload(request)
    if payload is None:
        return _sync_error("invalid_request", "A packed boolean, operation UUID, and positive base version are required.", 400)

    household = active_household_for_member(household_id=household_id, member=request.user)
    if household is None:
        return not_found()

    request_hash = _request_hash(household_id=household.pk, trip_id=trip_id, entry_id=entry_id, payload=payload)
    existing = PackingOperation.objects.filter(actor=request.user, operation_id=payload["client_operation_id"]).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            return _conflict_response()
        if existing.result is not None:
            return JsonResponse(existing.result)
        return _sync_error("operation_in_progress", "Operation is still being processed.", 409)

    trip = trip_for_member_household(trip_id=trip_id, household=household)
    if trip is None:
        return not_found()

    try:
        with transaction.atomic():
            operation = PackingOperation.objects.create(
                actor=request.user,
                operation_id=payload["client_operation_id"],
                household=household,
                trip_id=trip.pk,
                entry_id=entry_id,
                request_hash=request_hash,
            )
    except IntegrityError:
        existing = PackingOperation.objects.filter(actor=request.user, operation_id=payload["client_operation_id"]).first()
        if existing and existing.request_hash == request_hash and existing.result is not None:
            return JsonResponse(existing.result)
        if existing:
            return _conflict_response()
        raise

    try:
        entry, _changed = set_trip_gear_packed_versioned(
            trip_id=trip.pk,
            entry_id=entry_id,
            packed=payload["packed"],
            base_version=payload["base_version"],
            actor=request.user,
        )
    except ObjectDoesNotExist:
        operation.delete()
        return not_found()
    except TripGearVersionConflict as conflict:
        operation.delete()
        return _sync_error(
            "version_conflict", "Packing entry changed.", 409,
            packed=conflict.packed, version=conflict.version,
        )
    except ValidationError:
        operation.delete()
        return _sync_error("trip_read_only", "Trip packing is read-only.", 409)

    result = {
        "entry_id": str(entry.pk),
        "packed": entry.packed,
        "version": entry.version,
        "client_operation_id": str(payload["client_operation_id"]),
    }
    operation.result = result
    operation.save(update_fields=["result"])
    return JsonResponse(result)
