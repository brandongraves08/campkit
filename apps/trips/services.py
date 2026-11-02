from django.core.exceptions import ValidationError
from django.db import transaction
from apps.households.permissions import require_household_member
from apps.inventory.services import get_gear_item, packed_gear_weight_totals
from apps.trips.models import Trip, TripGear


def get_trip_for_actor(trip_id, actor, *, lock=False):
    trip = Trip.objects.filter(pk=trip_id)
    if lock:
        trip = trip.select_for_update()
    trip = trip.first()
    if trip is None:
        raise Trip.DoesNotExist
    require_household_member(actor, trip.household_id)
    return trip


@transaction.atomic
def create_trip(*, actor, household, data):
    actual = require_household_member(actor, household.pk)
    values = dict(data)
    values.pop("household", None)
    trip = Trip(household=actual, created_by=actor, **values)
    trip.full_clean()
    trip.save()
    return trip


@transaction.atomic
def add_trip_gear(*, trip_id, gear_item_id, quantity, actor):
    trip = get_trip_for_actor(trip_id, actor, lock=True)
    gear = get_gear_item(actor=actor, household=trip.household, item_id=gear_item_id)
    entry = TripGear(trip=trip, gear_item=gear, quantity=quantity)
    entry.full_clean()
    entry.save()
    return entry


def packed_base_weight(*, trip_id, actor):
    trip = get_trip_for_actor(trip_id, actor)
    entries = list(TripGear.objects.filter(trip=trip).values_list("gear_item_id", "quantity"))
    return packed_gear_weight_totals(actor=actor, household=trip.household, entries=entries)


@transaction.atomic
def set_trip_gear_packed(*, trip_id, entry_id, packed, actor):
    trip = get_trip_for_actor(trip_id, actor, lock=True)
    entry = TripGear.objects.select_for_update().filter(trip=trip, pk=entry_id).first()
    if entry is None:
        raise TripGear.DoesNotExist
    entry.packed = bool(packed)
    entry.full_clean()
    entry.save(update_fields=["packed"])
    return entry


def trip_packing_list(*, trip_id, actor):
    trip = get_trip_for_actor(trip_id, actor)
    entries = list(TripGear.objects.filter(trip=trip).order_by("pk"))
    return [(entry, get_gear_item(actor=actor, household=trip.household, item_id=entry.gear_item_id))
            for entry in entries]


def set_trip_status(*, trip_id, status, actor):
    trip = get_trip_for_actor(trip_id, actor, lock=True)
    allowed = {
        (Trip.Status.PLANNED, Trip.Status.CONFIRMED),
        (Trip.Status.CONFIRMED, Trip.Status.COMPLETED),
    }
    if status != trip.status and (trip.status, status) not in allowed:
        raise ValidationError("Invalid trip status transition.")
    trip.status = status
    trip.save(update_fields=["status", "updated_at"])
    return trip


@transaction.atomic
def cancel_trip(*, trip_id, actor):
    trip = get_trip_for_actor(trip_id, actor, lock=True)
    if trip.status == Trip.Status.CANCELLED:
        return trip
    from apps.meals.services import release_trip_food
    release_trip_food(trip_id=trip.pk, actor=actor)
    trip.status = Trip.Status.CANCELLED
    trip.save(update_fields=["status", "updated_at"])
    return trip
