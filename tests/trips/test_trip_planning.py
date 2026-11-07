import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from django.utils import timezone
from datetime import date, timedelta

from apps.inventory.models import GearItem, InventoryVerification
from apps.trips.models import GearKit, GearKitItem, Trip, TripGear
from apps.trips.services import (
    cancel_trip,
    create_gear_kit_from_trip,
    create_trip,
    delete_gear_kit,
    list_gear_kits,
    packed_base_weight,
    update_gear_kit,
)

pytestmark = pytest.mark.django_db


def _trip_with_gear(*, actor, household, trip_name="Pine weekend"):
    trip = create_trip(actor=actor, household=household, data={"name": trip_name})
    tent = GearItem.objects.create(household=household, name="Tent", weight_grams=1800, quantity=3)
    stove = GearItem.objects.create(household=household, name="Stove", weight_grams=500)
    TripGear.objects.create(trip=trip, gear_item=tent, quantity=2, packed=True)
    TripGear.objects.create(trip=trip, gear_item=stove, quantity=1)
    return trip, tent, stove


def _kit_items(kit):
    return {item.gear_item_id: item.quantity for item in kit.items.all()}


def _exception_signature(call):
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the signature is the assertion
        return type(exc), str(exc)
    raise AssertionError("expected the call to raise")


def test_trip_lifecycle_is_household_scoped(owner, other_owner, household, other_household):
    trip = create_trip(actor=owner, household=household, data={"name": "Pine weekend"})
    assert trip.status == Trip.Status.PLANNED
    with pytest.raises(PermissionDenied):
        create_trip(actor=other_owner, household=household, data={"name": "No access"})
    with pytest.raises(Trip.DoesNotExist):
        Trip.objects.for_household(other_household).get(pk=trip.pk)
    cancel_trip(trip_id=trip.pk, actor=owner)
    assert Trip.objects.get(pk=trip.pk).status == Trip.Status.CANCELLED


def test_packed_weight_includes_estimated_annotations(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Pine weekend"})
    confirmed = GearItem.objects.create(household=household, name="Stove", weight_grams=500,
        verification_status=InventoryVerification.CONFIRMED, verified_by=owner, verified_at=timezone.now())
    estimated = GearItem.objects.create(household=household, name="Tent", weight_grams=1800, quantity=2)
    TripGear.objects.create(trip=trip, gear_item=confirmed, quantity=1)
    TripGear.objects.create(trip=trip, gear_item=estimated, quantity=1)
    assert packed_base_weight(trip_id=trip.pk, actor=owner) == {
        "confirmed_grams": 500, "estimated_grams": 3600, "total_grams": 4100,
    }


def test_trip_detail_is_household_scoped_and_packing_list_is_usable(client, owner, household, other_owner, other_household):
    trip = create_trip(actor=owner, household=household, data={"name": "Pine weekend"})
    gear = GearItem.objects.create(household=household, name="Stove", weight_grams=500)
    client.force_login(owner)
    response = client.get(reverse("trips:trip_detail", args=[trip.pk]))
    assert response.status_code == 200
    assert b"Packing list" in response.content
    response = client.post(reverse("trips:trip_gear_add", args=[trip.pk]), {"gear_item_id": str(gear.pk), "quantity": "1"})
    assert response.status_code == 302
    entry = TripGear.objects.get(trip=trip, gear_item=gear)
    response = client.post(reverse("trips:trip_gear_packed", args=[trip.pk, entry.pk]), {"packed": "true"})
    assert response.status_code == 302
    entry.refresh_from_db()
    assert entry.packed is True
    outsider_trip = create_trip(actor=other_owner, household=other_household, data={"name": "Other"})
    response = client.get(reverse("trips:trip_detail", args=[outsider_trip.pk]))
    assert response.status_code == 404


def test_trip_dates_cannot_end_before_start(owner, household):
    with pytest.raises(ValidationError):
        create_trip(actor=owner, household=household, data={
            "name": "Invalid", "start_date": date.today(), "end_date": date.today() - timedelta(days=1)
        })


def test_packing_an_unknown_or_foreign_gear_id_is_a_message_not_a_crash(
    client, owner, other_owner, household, other_household
):
    """An id the household cannot pack must not become a 500 (Task 3 finding)."""

    trip = create_trip(actor=owner, household=household, data={"name": "Pine weekend"})
    foreign_gear = GearItem.objects.create(household=other_household, name="Not mine", weight_grams=100)
    client.force_login(owner)

    scoped_ids = (str(uuid.uuid4()), str(foreign_gear.pk))
    for gear_id in scoped_ids:
        response = client.post(
            reverse("trips:trip_gear_add", args=[trip.pk]),
            {"gear_item_id": gear_id, "quantity": "1"},
            follow=True,
        )
        assert response.status_code == 200, gear_id
        assert b"not available in this household" in response.content, gear_id
    malformed = client.post(
        reverse("trips:trip_gear_add", args=[trip.pk]),
        {"gear_item_id": "not-a-uuid", "quantity": "1"},
        follow=True,
    )
    assert malformed.status_code == 200, "a malformed id is a message, not a 500"
    assert not TripGear.objects.filter(trip=trip).exists()


def test_gear_kit_snapshot_preserves_source_trip_and_shared_gear(owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    trip_before = list(
        TripGear.objects.filter(trip=trip).order_by("pk").values("gear_item_id", "quantity", "packed")
    )
    gear_before = list(GearItem.objects.filter(household=household).order_by("pk").values())

    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="  Base camp  ")

    assert kit.name == "Base camp"
    assert kit.household_id == household.pk
    assert kit.created_by_id == owner.pk
    assert _kit_items(kit) == {tent.pk: 2, stove.pk: 1}
    assert list(
        TripGear.objects.filter(trip=trip).order_by("pk").values("gear_item_id", "quantity", "packed")
    ) == trip_before
    assert list(GearItem.objects.filter(household=household).order_by("pk").values()) == gear_before


def test_gear_kit_snapshot_rejects_an_empty_trip_without_writes(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "No gear yet"})

    with pytest.raises(ValidationError):
        create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Empty kit")

    assert not GearKit.objects.exists()
    assert not GearKitItem.objects.exists()


def test_gear_kit_names_are_trimmed_and_case_insensitively_unique_per_household(
    owner, other_owner, household, other_household
):
    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    other_trip, _other_tent, _other_stove = _trip_with_gear(
        actor=other_owner, household=other_household, trip_name="Other trip"
    )

    create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base Camp")

    with pytest.raises(ValidationError):
        create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="  base camp ")
    for bad_name in ("   ", "x" * 161):
        with pytest.raises(ValidationError):
            create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name=bad_name)

    other_kit = create_gear_kit_from_trip(actor=other_owner, trip_id=other_trip.pk, name="base camp")
    assert other_kit.name == "base camp"
    assert GearKit.objects.filter(household=household).count() == 1


def test_gear_kit_name_uniqueness_is_enforced_by_the_database(owner, household):
    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base Camp")

    with pytest.raises(IntegrityError), transaction.atomic():
        GearKit.objects.create(household=household, created_by=owner, name="BASE CAMP")


def test_gear_kit_name_uniqueness_trims_in_the_database(
    owner, other_owner, household, other_household
):
    """Direct ORM writes cannot smuggle around the trimmed-name contract."""

    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base Camp")

    for padded in (" Base Camp ", "  base camp  "):
        with pytest.raises(IntegrityError), transaction.atomic():
            GearKit.objects.create(household=household, created_by=owner, name=padded)

    other = GearKit.objects.create(household=other_household, created_by=other_owner, name=" Base Camp ")
    assert other.household_id == other_household.pk
    assert GearKit.objects.filter(household=household).count() == 1


def test_list_gear_kits_is_household_scoped(owner, member, other_owner, household, other_household):
    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    other_trip, _other_tent, _other_stove = _trip_with_gear(
        actor=other_owner, household=other_household, trip_name="Other trip"
    )
    own_kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    create_gear_kit_from_trip(actor=other_owner, trip_id=other_trip.pk, name="Base camp")

    assert list(list_gear_kits(actor=owner, household=household)) == [own_kit]
    assert list(list_gear_kits(actor=member, household=household)) == [own_kit]
    with pytest.raises(PermissionDenied):
        list_gear_kits(actor=owner, household=other_household)
    with pytest.raises(PermissionDenied):
        list_gear_kits(actor=other_owner, household=household)


def test_update_gear_kit_renames_and_replaces_membership(owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    extra = GearItem.objects.create(household=household, name="Lantern", weight_grams=300)
    gear_before = list(GearItem.objects.filter(household=household).order_by("pk").values())
    trip_before = list(TripGear.objects.filter(trip=trip).order_by("pk").values())

    updated = update_gear_kit(
        actor=owner, kit_id=kit.pk, name="  Winter base  ", items=[(stove.pk, 3), (extra.pk, 1)]
    )

    assert updated.pk == kit.pk
    assert updated.name == "Winter base"
    assert _kit_items(updated) == {stove.pk: 3, extra.pk: 1}
    assert tent.pk not in _kit_items(updated)
    assert list(GearItem.objects.filter(household=household).order_by("pk").values()) == gear_before
    assert list(TripGear.objects.filter(trip=trip).order_by("pk").values()) == trip_before


def test_update_gear_kit_rejects_invalid_payloads_without_partial_writes(
    owner, household, other_household
):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    foreign = GearItem.objects.create(household=other_household, name="Not mine")

    invalid_items = [
        [(tent.pk, 0)],
        [(tent.pk, -1)],
        [(tent.pk, "2")],
        [(tent.pk, 1), (tent.pk, 2)],
        [],
    ]
    for items in invalid_items:
        with pytest.raises(ValidationError):
            update_gear_kit(actor=owner, kit_id=kit.pk, name="Renamed", items=items)

    with pytest.raises(PermissionDenied):
        update_gear_kit(actor=owner, kit_id=kit.pk, name="Renamed", items=[(foreign.pk, 1)])
    with pytest.raises(PermissionDenied):
        update_gear_kit(actor=owner, kit_id=kit.pk, name="Renamed", items=[(uuid.uuid4(), 1)])
    with pytest.raises(ValidationError):
        update_gear_kit(actor=owner, kit_id=kit.pk, name="Renamed", items=[("not-a-uuid", 1)])
    with pytest.raises(ValidationError):
        update_gear_kit(actor=owner, kit_id=kit.pk, name="   ", items=[(tent.pk, 1)])

    kit.refresh_from_db()
    assert kit.name == "Base camp"
    assert _kit_items(kit) == {tent.pk: 2, stove.pk: 1}


def test_update_gear_kit_rejects_another_kits_name(owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    first = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    second = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Summit kit")

    with pytest.raises(ValidationError):
        update_gear_kit(actor=owner, kit_id=second.pk, name=" base CAMP ", items=[(tent.pk, 1)])

    second.refresh_from_db()
    assert second.name == "Summit kit"
    assert _kit_items(second) == {tent.pk: 2, stove.pk: 1}
    assert first.name == "Base camp"


def test_delete_gear_kit_removes_membership_only(owner, household):
    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    kept = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Spare kit")
    gear_before = list(GearItem.objects.filter(household=household).order_by("pk").values())
    trip_before = list(TripGear.objects.filter(trip=trip).order_by("pk").values())

    delete_gear_kit(actor=owner, kit_id=kit.pk)

    assert not GearKit.objects.filter(pk=kit.pk).exists()
    assert not GearKitItem.objects.filter(kit_id=kit.pk).exists()
    assert GearKit.objects.filter(pk=kept.pk).exists()
    assert list(GearItem.objects.filter(household=household).order_by("pk").values()) == gear_before
    assert list(TripGear.objects.filter(trip=trip).order_by("pk").values()) == trip_before


def test_kit_lifecycle_rejects_nonmembers_as_not_found(owner, other_owner, household, other_household):
    """A nonmember sees the same not-found outcome as a random id (no existence leak)."""

    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")

    with pytest.raises(Trip.DoesNotExist):
        create_gear_kit_from_trip(actor=other_owner, trip_id=trip.pk, name="Stolen")
    with pytest.raises(GearKit.DoesNotExist):
        update_gear_kit(actor=other_owner, kit_id=kit.pk, name="Stolen", items=[(tent.pk, 1)])
    with pytest.raises(GearKit.DoesNotExist):
        delete_gear_kit(actor=other_owner, kit_id=kit.pk)

    kit.refresh_from_db()
    assert kit.name == "Base camp"
    assert _kit_items(kit) == {tent.pk: 2, stove.pk: 1}
    with pytest.raises(GearKit.DoesNotExist):
        update_gear_kit(actor=owner, kit_id=uuid.uuid4(), name="Missing", items=[(tent.pk, 1)])


def test_kit_identifiers_do_not_disclose_foreign_households(owner, other_owner, household, other_household):
    """Update and delete of a foreign kit look exactly like an unknown id, with no writes."""

    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    own_kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Own kit")
    other_trip, other_tent, other_stove = _trip_with_gear(
        actor=other_owner, household=other_household, trip_name="Other trip"
    )
    foreign_kit = create_gear_kit_from_trip(actor=other_owner, trip_id=other_trip.pk, name="Foreign kit")
    unknown_id = uuid.uuid4()

    actions = (
        lambda kit_id: update_gear_kit(actor=owner, kit_id=kit_id, name="Renamed", items=[(tent.pk, 1)]),
        lambda kit_id: delete_gear_kit(actor=owner, kit_id=kit_id),
    )
    expected = (GearKit.DoesNotExist, "Gear kit not found.")
    for action in actions:
        assert _exception_signature(lambda: action(foreign_kit.pk)) == expected
        assert _exception_signature(lambda: action(unknown_id)) == expected

    foreign_kit.refresh_from_db()
    assert foreign_kit.name == "Foreign kit"
    assert _kit_items(foreign_kit) == {other_tent.pk: 2, other_stove.pk: 1}
    own_kit.refresh_from_db()
    assert own_kit.name == "Own kit"
    assert _kit_items(own_kit) == {tent.pk: 2, stove.pk: 1}
    assert GearKit.objects.filter(household=other_household).count() == 1


def test_create_kit_from_trip_does_not_disclose_foreign_trips(
    owner, other_owner, household, other_household
):
    """A foreign trip id and an unknown id fail identically and write nothing."""

    foreign_trip, _tent, _stove = _trip_with_gear(
        actor=other_owner, household=other_household, trip_name="Other trip"
    )
    kits_before = GearKit.objects.count()
    items_before = GearKitItem.objects.count()
    expected = (Trip.DoesNotExist, "Trip not found.")

    assert (
        _exception_signature(
            lambda: create_gear_kit_from_trip(actor=owner, trip_id=foreign_trip.pk, name="Sneaky kit")
        )
        == expected
    )
    assert (
        _exception_signature(
            lambda: create_gear_kit_from_trip(actor=owner, trip_id=uuid.uuid4(), name="Sneaky kit")
        )
        == expected
    )
    assert GearKit.objects.count() == kits_before
    assert GearKitItem.objects.count() == items_before


def test_update_does_not_mislabel_item_integrity_failures_as_name_collisions(
    owner, household, monkeypatch
):
    """Only the kit row's own name collision is a form error; item DB errors surface."""

    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")

    # Bypass payload validation to force a real UNIQUE(kit, gear_item) failure.
    monkeypatch.setattr(
        "apps.trips.services._validated_kit_items",
        lambda *, actor, kit, items: [(tent, 1), (tent, 1)],
    )

    with pytest.raises(IntegrityError):
        update_gear_kit(actor=owner, kit_id=kit.pk, name="Renamed", items=[])

    kit.refresh_from_db()
    assert kit.name == "Base camp"
    assert _kit_items(kit) == {tent.pk: 2, stove.pk: 1}


def test_kit_row_save_translates_only_its_own_name_collision(owner, household):
    """The kit-row save helper classifies real DB errors, not every IntegrityError."""

    from apps.trips.services import _save_kit

    trip, _tent, _stove = _trip_with_gear(actor=owner, household=household)
    create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base Camp")

    colliding = GearKit(household=household, created_by=owner, name=" base camp ")
    with pytest.raises(ValidationError) as excinfo:
        _save_kit(colliding)
    assert "already exists" in str(excinfo.value)

    broken = GearKit(household=household, created_by=owner, name=None)
    with pytest.raises(IntegrityError):
        _save_kit(broken)
    assert not GearKit.objects.filter(name=None).exists()


def test_gear_kit_item_constraints_protect_gear_and_quantities(owner, household, other_household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Base camp")
    foreign = GearItem.objects.create(household=other_household, name="Not mine")

    cross_household = GearKitItem(kit=kit, gear_item=foreign, quantity=1)
    with pytest.raises(ValidationError):
        cross_household.full_clean()

    zero = GearKitItem(kit=kit, gear_item=tent, quantity=0)
    with pytest.raises(ValidationError):
        zero.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        GearKitItem.objects.create(kit=kit, gear_item=tent, quantity=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        GearKitItem.objects.create(kit=kit, gear_item=tent, quantity=1)

    # This gear is referenced only by the kit, so protection cannot come from a trip row.
    kit_only = GearItem.objects.create(household=household, name="Lantern", weight_grams=300)
    update_gear_kit(
        actor=owner,
        kit_id=kit.pk,
        name="Base camp",
        items=[(tent.pk, 2), (stove.pk, 1), (kit_only.pk, 1)],
    )
    GearItem.objects.filter(pk=kit_only.pk).update(name="Lantern v2")
    with pytest.raises(ProtectedError):
        GearItem.objects.get(pk=kit_only.pk).delete()
    assert GearKitItem.objects.filter(kit=kit, gear_item_id=kit_only.pk).exists()
    assert GearItem.objects.get(pk=kit_only.pk).name == "Lantern v2"
