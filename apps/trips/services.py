import uuid

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.households.permissions import require_household_member
from apps.inventory.services import get_gear_item, packed_gear_weight_totals
from apps.trips.models import GearKit, GearKitItem, Trip, TripGear


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


def _normalized_kit_name(name):
    normalized = " ".join(str(name or "").split())
    if not normalized:
        raise ValidationError({"name": "Kit name is required."})
    if len(normalized) > 160:
        raise ValidationError({"name": "Kit name must be 160 characters or fewer."})
    return normalized


def _kit_for_actor(*, kit_id, actor, lock=False):
    """Resolve one kit and require membership; never leak another household's kit."""
    kits = GearKit.objects.all()
    if lock:
        kits = kits.select_for_update()
    try:
        kit = kits.filter(pk=kit_id).first()
    except (TypeError, ValueError, ValidationError):
        kit = None
    if kit is None:
        raise GearKit.DoesNotExist("Gear kit not found.")
    require_household_member(actor, kit.household_id)
    return kit


def list_gear_kits(*, actor, household):
    """Household-scoped kits, deterministic order, for the UI (never a model access)."""
    require_household_member(actor, household.pk)
    return GearKit.objects.for_household(household).prefetch_related("items").order_by("name", "pk")


def _validated_kit_items(*, actor, kit, items):
    """Validate the whole replacement payload; returns [(GearItem, quantity), ...]."""
    try:
        entries = list(items)
    except TypeError:
        raise ValidationError("Kit items must be a list of gear and quantity pairs.")
    if not entries:
        raise ValidationError("A kit needs at least one gear item.")

    quantities = {}
    for entry in entries:
        try:
            gear_id, quantity = entry
        except (TypeError, ValueError):
            raise ValidationError("Each kit item needs a gear item and a quantity.")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValidationError({"quantity": "Kit quantity must be a whole number of at least one."})
        if quantity < 1:
            raise ValidationError({"quantity": "Kit quantity must be at least one."})
        if quantity > 32767:
            raise ValidationError({"quantity": "Kit quantity is too large."})
        try:
            gear_id = uuid.UUID(str(gear_id))
        except (AttributeError, TypeError, ValueError):
            raise ValidationError("Kit gear ids must be valid identifiers.")
        if gear_id in quantities:
            raise ValidationError("Each gear item may appear only once in a kit.")
        quantities[gear_id] = quantity

    resolved = []
    for gear_id, quantity in quantities.items():
        try:
            gear = get_gear_item(actor=actor, household=kit.household, item_id=gear_id)
        except ObjectDoesNotExist:
            raise PermissionDenied("Kit gear must belong to the kit household.")
        resolved.append((gear, quantity))
    return resolved


@transaction.atomic
def create_gear_kit_from_trip(*, actor, trip_id, name):
    """Snapshot a non-empty trip's gear rows into a named household kit."""
    trip = get_trip_for_actor(trip_id, actor, lock=True)
    name = _normalized_kit_name(name)
    rows = list(TripGear.objects.filter(trip=trip).select_related("gear_item").order_by("pk"))
    if not rows:
        raise ValidationError("A kit needs at least one gear item.")
    if GearKit.objects.for_household(trip.household).filter(name__iexact=name).exists():
        raise ValidationError({"name": "A kit with this name already exists in this household."})

    try:
        with transaction.atomic():
            kit = GearKit(household=trip.household, created_by=actor, name=name)
            kit.full_clean()
            kit.save()
            for row in rows:
                item = GearKitItem(kit=kit, gear_item=row.gear_item, quantity=row.quantity)
                item.full_clean()
                item.save()
    except IntegrityError as exc:
        raise ValidationError({"name": "A kit with this name already exists in this household."}) from exc
    return kit


@transaction.atomic
def update_gear_kit(*, actor, kit_id, name, items):
    """Atomically rename a kit and replace its membership/quantities."""
    kit = _kit_for_actor(kit_id=kit_id, actor=actor, lock=True)
    name = _normalized_kit_name(name)
    replacement = _validated_kit_items(actor=actor, kit=kit, items=items)
    if (
        GearKit.objects.for_household(kit.household)
        .filter(name__iexact=name)
        .exclude(pk=kit.pk)
        .exists()
    ):
        raise ValidationError({"name": "A kit with this name already exists in this household."})

    try:
        with transaction.atomic():
            kit.name = name
            kit.full_clean()
            kit.save(update_fields=["name", "updated_at"])
            GearKitItem.objects.filter(kit=kit).delete()
            GearKitItem.objects.bulk_create(
                [GearKitItem(kit=kit, gear_item=gear, quantity=quantity) for gear, quantity in replacement]
            )
    except IntegrityError as exc:
        raise ValidationError({"name": "A kit with this name already exists in this household."}) from exc
    return kit


@transaction.atomic
def delete_gear_kit(*, actor, kit_id):
    """Delete a kit and its membership rows only; trips and gear are untouched."""
    kit = _kit_for_actor(kit_id=kit_id, actor=actor, lock=True)
    kit.delete()


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


def list_trips(*, actor, household):
    """Household-scoped trips, newest first, for the UI (never a model access)."""
    require_household_member(actor, household.pk)
    return Trip.objects.for_household(household).order_by("-created_at")


def upcoming_trips(*, actor, household, limit=None):
    """Planned/confirmed trips that have not finished yet, soonest first."""
    require_household_member(actor, household.pk)
    today = timezone.localdate()
    trips = (
        Trip.objects.for_household(household)
        .filter(status__in=[Trip.Status.PLANNED, Trip.Status.CONFIRMED])
        .filter(
            Q(end_date__gte=today)
            | Q(end_date__isnull=True, start_date__gte=today)
            | Q(end_date__isnull=True, start_date__isnull=True)
        )
        .order_by("start_date", "created_at")
    )
    return trips[:limit] if limit else trips


def trip_readiness(*, trip_id, actor):
    """Packing progress and both weight bands for one trip."""
    trip = get_trip_for_actor(trip_id, actor)
    entries = list(TripGear.objects.filter(trip=trip).values_list("packed", flat=True))
    days_until = (trip.start_date - timezone.localdate()).days if trip.start_date else None
    return {
        "trip": trip,
        "total_items": len(entries),
        "packed_items": sum(1 for packed in entries if packed),
        "weights": packed_base_weight(trip_id=trip.pk, actor=actor),
        "days_until": days_until,
    }


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
