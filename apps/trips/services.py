import uuid
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.households.permissions import require_household_member
from apps.inventory.services import get_gear_item, packed_gear_weight_totals
from apps.trips.models import GearKit, GearKitItem, Trip, TripGear


SOURCE_KIND_KIT = "kit"
SOURCE_KIND_COMPLETED_TRIP = "completed_trip"
_SOURCE_KINDS = frozenset({SOURCE_KIND_KIT, SOURCE_KIND_COMPLETED_TRIP})
_READ_ONLY_TRIP_STATUSES = frozenset({Trip.Status.COMPLETED, Trip.Status.CANCELLED})
_READ_ONLY_PACKING_MESSAGE = "Completed and cancelled trips keep a read-only packing list."
_SEED_QUANTITY_MIN = 1
_SEED_QUANTITY_MAX = 32767


@dataclass(frozen=True)
class SeedResult:
    """Exact outcome of one seeding request; skipped rows are left untouched."""

    added: int
    skipped: int


def _require_packing_mutable(trip):
    if trip.status in _READ_ONLY_TRIP_STATUSES:
        raise ValidationError(_READ_ONLY_PACKING_MESSAGE)


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
    trip = _trip_for_member_actor(trip_id=trip_id, actor=actor, lock=True)
    _require_packing_mutable(trip)
    gear = get_gear_item(actor=actor, household=trip.household, item_id=gear_item_id)
    entry = TripGear(trip=trip, gear_item=gear, quantity=quantity)
    entry.full_clean()
    entry.save()
    return entry


def _normalized_kit_name(name):
    normalized = str(name or "").strip()
    if not normalized:
        raise ValidationError({"name": "Kit name is required."})
    if len(normalized) > 160:
        raise ValidationError({"name": "Kit name must be 160 characters or fewer."})
    return normalized


_DUPLICATE_KIT_NAME_MESSAGE = "A kit with this name already exists in this household."
_DUPLICATE_KIT_NAME_CONSTRAINT = "unique_gear_kit_name_per_household"


def _save_kit(kit, *, update_fields=None):
    """Save only the kit row, translating only its own name-collision error.

    Item writes run outside this helper: an integrity failure there is not a
    duplicate kit name and must not be reported to the user as one.
    """
    try:
        with transaction.atomic():
            if update_fields is None:
                kit.save()
            else:
                kit.save(update_fields=update_fields)
    except IntegrityError as exc:
        if _DUPLICATE_KIT_NAME_CONSTRAINT not in str(exc):
            raise
        raise ValidationError({"name": _DUPLICATE_KIT_NAME_MESSAGE}) from exc
    return kit


def _kit_for_actor(*, kit_id, actor, lock=False):
    """Resolve one kit through the actor's memberships.

    A foreign kit and an unknown kit fail identically, so an identifier never
    discloses another household's kit.
    """
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise GearKit.DoesNotExist("Gear kit not found.")
    kits = GearKit.objects.filter(household__memberships__user=actor)
    if lock:
        kits = kits.select_for_update()
    try:
        kit = kits.filter(pk=kit_id).first()
    except (TypeError, ValueError, ValidationError):
        kit = None
    if kit is None:
        raise GearKit.DoesNotExist("Gear kit not found.")
    return kit


def _trip_for_member_actor(*, trip_id, actor, lock=False):
    """Membership-scoped trip lookup (foreign == unknown, malformed == unknown).

    New packing paths use this instead of ``get_trip_for_actor`` so an id that
    belongs to another household cannot be distinguished from one that does not
    exist. The older helper keeps its existing permission-denied contract for
    meals/API callers.
    """
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise Trip.DoesNotExist("Trip not found.")
    trips = Trip.objects.filter(household__memberships__user=actor)
    if lock:
        trips = trips.select_for_update()
    try:
        trip = trips.filter(pk=trip_id).first()
    except (TypeError, ValueError, ValidationError):
        trip = None
    if trip is None:
        raise Trip.DoesNotExist("Trip not found.")
    return trip


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
    trip = _trip_for_member_actor(trip_id=trip_id, actor=actor, lock=True)
    name = _normalized_kit_name(name)
    rows = list(TripGear.objects.filter(trip=trip).select_related("gear_item").order_by("pk"))
    if not rows:
        raise ValidationError("A kit needs at least one gear item.")
    if GearKit.objects.for_household(trip.household).filter(name__iexact=name).exists():
        raise ValidationError({"name": _DUPLICATE_KIT_NAME_MESSAGE})

    with transaction.atomic():
        kit = GearKit(household=trip.household, created_by=actor, name=name)
        kit.full_clean()
        _save_kit(kit)
        for row in rows:
            item = GearKitItem(kit=kit, gear_item=row.gear_item, quantity=row.quantity)
            item.full_clean()
            item.save()
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
        raise ValidationError({"name": _DUPLICATE_KIT_NAME_MESSAGE})

    with transaction.atomic():
        kit.name = name
        kit.full_clean()
        _save_kit(kit, update_fields=["name", "updated_at"])
        GearKitItem.objects.filter(kit=kit).delete()
        GearKitItem.objects.bulk_create(
            [GearKitItem(kit=kit, gear_item=gear, quantity=quantity) for gear, quantity in replacement]
        )
    return kit


@transaction.atomic
def delete_gear_kit(*, actor, kit_id):
    """Delete a kit and its membership rows only; trips and gear are untouched."""
    kit = _kit_for_actor(kit_id=kit_id, actor=actor, lock=True)
    kit.delete()


def _seed_source_rows(*, actor, target, source_kind, source_id):
    """Resolve every source row inside the target household, or fail before writes."""
    kind = str(source_kind or "").strip()
    if kind not in _SOURCE_KINDS:
        raise ValidationError("Choose a saved kit or a completed trip as the source.")
    if kind == SOURCE_KIND_KIT:
        kit = _kit_for_actor(kit_id=source_id, actor=actor, lock=True)
        if kit.household_id != target.household_id:
            raise ValidationError("The source must belong to the target trip's household.")
        rows = GearKitItem.objects.select_for_update().filter(kit=kit)
    else:
        source = _trip_for_member_actor(trip_id=source_id, actor=actor, lock=True)
        if source.household_id != target.household_id:
            raise ValidationError("The source must belong to the target trip's household.")
        if source.status != Trip.Status.COMPLETED:
            raise ValidationError("Only a completed trip can be used as a source.")
        rows = TripGear.objects.select_for_update().filter(trip=source)
    # A source has at most one row per gear item. Accepting either the source
    # row id or its gear UUID keeps form submissions simple without exposing
    # another household's records: only rows in this source are in the map.
    rows_by_id = {}
    for row in rows:
        rows_by_id[row.pk] = row
        rows_by_id[row.gear_item_id] = row
    return rows_by_id


def _selection_key(row_id):
    if isinstance(row_id, bool) or row_id is None:
        raise ValidationError("Selected gear must be valid identifiers.")
    if isinstance(row_id, int):
        return row_id
    text = str(row_id).strip()
    if text.isdigit():
        return int(text)
    try:
        return uuid.UUID(text)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("Selected gear must be valid identifiers.")


def _validated_seed_rows(selections, source_rows):
    """Validate the whole selection and map gear UUIDs to ``(GearItem, quantity)``."""
    try:
        entries = list(selections)
    except TypeError:
        raise ValidationError("Select at least one gear item to add.")
    if not entries:
        raise ValidationError("Select at least one gear item to add.")

    validated = {}
    for entry in entries:
        try:
            row_id, quantity = entry
        except (TypeError, ValueError):
            raise ValidationError("Each selected item needs a source gear row and a quantity.")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValidationError({"quantity": "Packed quantity must be a whole number of at least one."})
        if quantity < _SEED_QUANTITY_MIN:
            raise ValidationError({"quantity": "Packed quantity must be at least one."})
        if quantity > _SEED_QUANTITY_MAX:
            raise ValidationError({"quantity": "Packed quantity is too large."})
        row = source_rows.get(_selection_key(row_id))
        if row is None:
            raise ValidationError("Selected gear is no longer available in this source. Refresh and try again.")
        if row.gear_item_id in validated:
            raise ValidationError("Each source gear item may be selected only once.")
        validated[row.gear_item_id] = (row.gear_item, quantity)
    return validated


@transaction.atomic
def seed_trip_gear(*, actor, target_trip_id, source_kind, source_id, selections):
    """Copy selected kit or completed-trip gear rows onto a mutable trip.

    All target/source/gear rows are resolved and every quantity validated
    before the first insert. Existing target gear is skipped untouched, so a
    repeated submission adds no duplicates and preserves quantity/packed state.
    """
    target = _trip_for_member_actor(trip_id=target_trip_id, actor=actor, lock=True)
    _require_packing_mutable(target)
    source_rows = _seed_source_rows(
        actor=actor, target=target, source_kind=source_kind, source_id=source_id
    )
    wanted = _validated_seed_rows(selections, source_rows)
    existing_gear_ids = set(
        TripGear.objects.select_for_update().filter(trip=target).values_list("gear_item_id", flat=True)
    )

    added = 0
    skipped = 0
    for gear_id, (gear, quantity) in wanted.items():
        if gear_id in existing_gear_ids:
            skipped += 1
            continue
        if gear.household_id != target.household_id:
            raise ValidationError("Seeded gear must belong to the trip household.")
        entry = TripGear(trip=target, gear_item=gear, quantity=quantity, packed=False)
        entry.full_clean()
        entry.save()
        added += 1
    return SeedResult(added=added, skipped=skipped)


@transaction.atomic
def remove_trip_gear(*, actor, trip_id, entry_id):
    """Remove one packing association from a mutable trip; never delete gear."""
    trip = _trip_for_member_actor(trip_id=trip_id, actor=actor, lock=True)
    _require_packing_mutable(trip)
    try:
        entry = TripGear.objects.select_for_update().filter(trip=trip, pk=entry_id).first()
    except (TypeError, ValueError, ValidationError):
        entry = None
    if entry is None:
        raise TripGear.DoesNotExist("Trip gear not found.")
    entry.delete()


def packed_base_weight(*, trip_id, actor):
    trip = get_trip_for_actor(trip_id, actor)
    entries = list(TripGear.objects.filter(trip=trip).values_list("gear_item_id", "quantity"))
    return packed_gear_weight_totals(actor=actor, household=trip.household, entries=entries)


@transaction.atomic
def set_trip_gear_packed(*, trip_id, entry_id, packed, actor):
    trip = _trip_for_member_actor(trip_id=trip_id, actor=actor, lock=True)
    _require_packing_mutable(trip)
    try:
        entry = TripGear.objects.select_for_update().filter(trip=trip, pk=entry_id).first()
    except (TypeError, ValueError, ValidationError):
        entry = None
    if entry is None:
        raise TripGear.DoesNotExist("Trip gear not found.")
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
