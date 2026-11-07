import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from django.utils import timezone
from datetime import date, timedelta

from apps.households.models import Membership, MembershipRole
from apps.inventory.models import GearItem, InventoryVerification
from apps.trips import services as trip_services
from apps.trips.models import GearKit, GearKitItem, Trip, TripGear
from apps.trips.services import (
    add_trip_gear,
    cancel_trip,
    create_gear_kit_from_trip,
    create_trip,
    delete_gear_kit,
    list_gear_kits,
    packed_base_weight,
    set_trip_gear_packed,
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


def test_kit_management_lists_only_household_kits_and_has_empty_state(client, owner, household, other_owner, other_household):
    from apps.trips.services import create_gear_kit_from_trip

    own_trip, _, _ = _trip_with_gear(actor=owner, household=household, trip_name="Own source")
    foreign_trip, _, _ = _trip_with_gear(actor=other_owner, household=other_household, trip_name="Foreign source")
    own = create_gear_kit_from_trip(actor=owner, trip_id=own_trip.pk, name="Own kit")
    create_gear_kit_from_trip(actor=other_owner, trip_id=foreign_trip.pk, name="Foreign kit")
    client.force_login(owner)
    response = client.get(reverse("trips:gear_kit_list"))
    assert response.status_code == 200
    assert b"Own kit" in response.content
    assert b"Foreign kit" not in response.content
    assert str(own.pk).encode() in response.content


def test_save_trip_as_kit_success_and_empty_trip_validation(client, owner, household):
    trip, tent, _ = _trip_with_gear(actor=owner, household=household)
    empty_trip = create_trip(actor=owner, household=household, data={"name": "Empty"})
    client.force_login(owner)
    response = client.post(reverse("trips:gear_kit_create_from_trip", args=[trip.pk]), {"name": "Weekend kit"})
    assert response.status_code == 302
    kit = GearKit.objects.get(household=household, name="Weekend kit")
    assert kit.items.get(gear_item=tent).quantity == 2
    response = client.post(reverse("trips:gear_kit_create_from_trip", args=[empty_trip.pk]), {"name": "Empty kit"})
    assert response.status_code == 200
    assert b"at least one gear item" in response.content
    assert b"Add gear to this trip first" in response.content
    assert reverse("trips:trip_detail", args=[empty_trip.pk]).encode() in response.content
    assert not GearKit.objects.filter(name="Empty kit").exists()


def test_kit_management_edits_name_items_and_preserves_invalid_quantity(client, owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Old name")
    client.force_login(owner)
    response = client.post(reverse("trips:gear_kit_edit", args=[kit.pk]), {
        "name": "New name", f"include_{tent.pk}": "on", f"quantity_{tent.pk}": "3",
        f"include_{stove.pk}": "on", f"quantity_{stove.pk}": "zero",
    })
    assert response.status_code == 200
    assert b"New name" in response.content
    assert b"zero" in response.content
    kit.refresh_from_db()
    assert kit.name == "Old name"
    assert kit.items.get(gear_item=tent).quantity == 2
    response = client.post(reverse("trips:gear_kit_edit", args=[kit.pk]), {
        "name": "New name", f"include_{tent.pk}": "on", f"quantity_{tent.pk}": "3",
        f"quantity_{stove.pk}": "1",
    })
    assert response.status_code == 302
    kit.refresh_from_db()
    assert kit.name == "New name"
    assert list(kit.items.values_list("gear_item_id", "quantity")) == [(tent.pk, 3)]


def test_unselected_kit_row_does_not_require_a_quantity(client, owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Quantity optional")
    client.force_login(owner)
    response = client.post(reverse("trips:gear_kit_edit", args=[kit.pk]), {
        "name": "Quantity optional", f"include_{tent.pk}": "on", f"quantity_{tent.pk}": "1",
        f"quantity_{stove.pk}": "",
    })
    assert response.status_code == 302
    assert list(kit.items.values_list("gear_item_id", "quantity")) == [(tent.pk, 1)]


def test_edit_rejects_quantity_change_for_unchecked_gear(client, owner, household):
    trip, tent, stove = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Quantity selection")
    client.force_login(owner)

    response = client.post(reverse("trips:gear_kit_edit", args=[kit.pk]), {
        "name": "Quantity selection",
        f"include_{tent.pk}": "on", f"quantity_{tent.pk}": "2",
        f"quantity_{stove.pk}": "4",
    })

    assert response.status_code == 200
    assert b"Select this gear to save its quantity" in response.content
    kit.refresh_from_db()
    assert list(kit.items.values_list("gear_item_id", "quantity")) == [(tent.pk, 2), (stove.pk, 1)]


def test_kit_duplicate_name_and_foreign_id_do_not_disclose_or_mutate(client, owner, other_owner, household, other_household):
    trip, _, _ = _trip_with_gear(actor=owner, household=household)
    foreign_trip, _, _ = _trip_with_gear(actor=other_owner, household=other_household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="First")
    create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Second")
    foreign = create_gear_kit_from_trip(actor=other_owner, trip_id=foreign_trip.pk, name="Secret kit")
    client.force_login(owner)
    response = client.post(reverse("trips:gear_kit_edit", args=[kit.pk]), {
        "name": "Second", **{f"include_{row.gear_item_id}": "on" for row in kit.items.all()},
        **{f"quantity_{row.gear_item_id}": str(row.quantity) for row in kit.items.all()},
    })
    assert response.status_code == 200
    assert b"already exists" in response.content
    response = client.get(reverse("trips:gear_kit_edit", args=[foreign.pk]))
    assert response.status_code == 404
    assert b"Secret kit" not in response.content


def test_kit_delete_is_post_only_csrf_protected_and_removes_kit_only(client, owner, household):
    trip, tent, _ = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="Delete me")
    client.force_login(owner)
    assert client.get(reverse("trips:gear_kit_delete", args=[kit.pk])).status_code == 405
    response = client.post(reverse("trips:gear_kit_delete", args=[kit.pk]))
    assert response.status_code == 302
    assert not GearKit.objects.filter(pk=kit.pk).exists()
    assert GearItem.objects.filter(pk=tent.pk).exists()
    assert TripGear.objects.filter(trip=trip, gear_item=tent).exists()


def test_kit_delete_rejects_post_without_csrf_token(owner, household):
    from django.test import Client

    trip, _, _ = _trip_with_gear(actor=owner, household=household)
    kit = create_gear_kit_from_trip(actor=owner, trip_id=trip.pk, name="CSRF kit")
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    response = csrf_client.post(reverse("trips:gear_kit_delete", args=[kit.pk]))
    assert response.status_code == 403
    assert GearKit.objects.filter(pk=kit.pk).exists()


def test_gear_delete_protection_is_friendly_and_rolls_back_audit(client, owner, household):
    from apps.audit.models import InventoryChange
    from apps.inventory.services import create_gear_item

    trip = create_trip(actor=owner, household=household, data={"name": "Protected trip"})
    gear = create_gear_item(actor=owner, household=household, data={"name": "Protected tent"})
    add_trip_gear(trip_id=trip.pk, gear_item_id=gear.pk, quantity=1, actor=owner)
    before_audit = InventoryChange.objects.filter(object_id=str(gear.pk)).count()
    client.force_login(owner)
    response = client.post(reverse("inventory:gear_delete", args=[gear.pk]))
    assert response.status_code == 302
    page = client.get(response["Location"])
    assert b"cannot be deleted while it is referenced by a trip or saved kit" in page.content.lower(), page.content.decode()
    assert reverse("trips:trip_list").encode() in page.content
    assert reverse("trips:gear_kit_list").encode() in page.content
    assert GearItem.objects.filter(pk=gear.pk).exists()
    assert TripGear.objects.filter(trip=trip, gear_item=gear).exists()
    assert InventoryChange.objects.filter(object_id=str(gear.pk)).count() == before_audit


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


# --------------------------------------------------------------------------- #
# Trip seeding and immutable packing history (Task 2)
# --------------------------------------------------------------------------- #


def _gear_item(household, name, *, weight=500, quantity=1):
    return GearItem.objects.create(household=household, name=name, weight_grams=weight, quantity=quantity)


def _kit_with_rows(*, actor, household, entries):
    """Create a kit whose ``{gear_id: GearKitItem}`` rows are independent sources."""
    kit = GearKit.objects.create(household=household, created_by=actor, name=f"Kit {uuid.uuid4().hex[:8]}")
    rows = {
        gear.pk: GearKitItem.objects.create(kit=kit, gear_item=gear, quantity=quantity)
        for gear, quantity in entries
    }
    return kit, rows


def _set_trip_status(trip, status):
    Trip.objects.filter(pk=trip.pk).update(status=status)
    trip.refresh_from_db()


def _target_rows(trip):
    return {
        row.gear_item_id: (row.quantity, row.packed)
        for row in TripGear.objects.filter(trip=trip).order_by("pk")
    }


def _gear_snapshot(household):
    return list(GearItem.objects.filter(household=household).order_by("pk").values())


def _kit_snapshot(kit):
    return list(
        GearKitItem.objects.filter(kit=kit).order_by("pk").values("gear_item_id", "quantity")
    )


def _trip_snapshot(trip):
    return Trip.objects.filter(pk=trip.pk).values().get()


def _completed_source_trip(*, actor, household, entries):
    trip = create_trip(actor=actor, household=household, data={"name": "Last autumn"})
    rows = {gear.pk: TripGear.objects.create(trip=trip, gear_item=gear, quantity=qty, packed=True)
            for gear, qty in entries}
    _set_trip_status(trip, Trip.Status.COMPLETED)
    return trip, rows


def test_seed_trip_gear_copies_selected_kit_rows_with_quantity_overrides(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent", weight=1800)
    stove = _gear_item(household, "Stove")
    lantern = _gear_item(household, "Lantern", weight=300)
    kit, rows = _kit_with_rows(
        actor=owner, household=household, entries=[(tent, 2), (stove, 2), (lantern, 3)]
    )
    gear_before = _gear_snapshot(household)
    kit_before = _kit_snapshot(kit)
    trip_before = _trip_snapshot(target)

    result = trip_services.seed_trip_gear(
        actor=owner,
        target_trip_id=target.pk,
        source_kind="kit",
        source_id=kit.pk,
        selections=[(tent.pk, 3), (rows[stove.pk].pk, 1)],
    )

    assert (result.added, result.skipped) == (2, 0)
    assert _target_rows(target) == {tent.pk: (3, False), stove.pk: (1, False)}
    assert lantern.pk not in _target_rows(target)
    assert _trip_snapshot(target) == trip_before
    assert _gear_snapshot(household) == gear_before
    assert _kit_snapshot(kit) == kit_before


def test_seed_trip_gear_copies_selected_completed_trip_rows_without_history_mutation(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent", weight=1800)
    stove = _gear_item(household, "Stove")
    source, source_rows = _completed_source_trip(
        actor=owner, household=household, entries=[(tent, 2), (stove, 1)]
    )
    source_before = list(TripGear.objects.filter(trip=source).order_by("pk").values())
    gear_before = _gear_snapshot(household)

    result = trip_services.seed_trip_gear(
        actor=owner,
        target_trip_id=target.pk,
        source_kind="completed_trip",
        source_id=source.pk,
        selections=[(tent.pk, 1), (stove.pk, 3)],
    )

    assert (result.added, result.skipped) == (2, 0)
    assert _target_rows(target) == {tent.pk: (1, False), stove.pk: (3, False)}
    assert list(TripGear.objects.filter(trip=source).order_by("pk").values()) == source_before
    assert _gear_snapshot(household) == gear_before
    assert _trip_snapshot(source)["status"] == Trip.Status.COMPLETED


def test_seed_trip_gear_skips_existing_target_rows_and_repeated_submission_is_idempotent(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent", weight=1800)
    stove = _gear_item(household, "Stove")
    tent_entry = add_trip_gear(trip_id=target.pk, gear_item_id=tent.pk, quantity=5, actor=owner)
    set_trip_gear_packed(trip_id=target.pk, entry_id=tent_entry.pk, packed=True, actor=owner)
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(tent, 2), (stove, 1)])
    selections = [(tent.pk, 2), (stove.pk, 1)]

    first = trip_services.seed_trip_gear(
        actor=owner, target_trip_id=target.pk, source_kind="kit", source_id=kit.pk, selections=selections
    )
    assert (first.added, first.skipped) == (1, 1)
    assert _target_rows(target) == {tent.pk: (5, True), stove.pk: (1, False)}

    second = trip_services.seed_trip_gear(
        actor=owner,
        target_trip_id=target.pk,
        source_kind="kit",
        source_id=kit.pk,
        selections=[(tent.pk, 9), (stove.pk, 4)],
    )
    assert (second.added, second.skipped) == (0, 2)
    assert _target_rows(target) == {tent.pk: (5, True), stove.pk: (1, False)}
    assert TripGear.objects.filter(trip=target).count() == 2


def test_seed_trip_gear_rejects_invalid_selections_without_writes(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent")
    stove = _gear_item(household, "Stove")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(tent, 2)])
    other_kit, other_rows = _kit_with_rows(
        actor=owner, household=household, entries=[(stove, 1)]
    )
    valid = tent.pk
    invalid_selections = [
        [],
        None,
        [(valid, 0)],
        [(valid, -1)],
        [(valid, "2")],
        [(valid, 1.5)],
        [(valid, True)],
        [(valid, 32768)],
        [(valid, 1), (valid, 2)],
        [("not-a-uuid", 1)],
        [(uuid.uuid4(), 1)],
        [(stove.pk, 1)],
        [(None, 1)],
    ]
    for selections in invalid_selections:
        with pytest.raises(ValidationError):
            trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target.pk,
                source_kind="kit",
                source_id=kit.pk,
                selections=selections,
            )
        assert not TripGear.objects.filter(trip=target).exists()

    assert other_kit.pk != kit.pk
    boundary = trip_services.seed_trip_gear(
        actor=owner,
        target_trip_id=target.pk,
        source_kind="kit",
        source_id=kit.pk,
        selections=[(valid, 32767)],
    )
    assert (boundary.added, boundary.skipped) == (1, 0)
    assert _target_rows(target) == {tent.pk: (32767, False)}


def test_seed_trip_gear_rejects_non_completed_and_unknown_source_kinds(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent")
    source = create_trip(actor=owner, household=household, data={"name": "Still planning"})
    entry = TripGear.objects.create(trip=source, gear_item=tent, quantity=2)
    for status in (Trip.Status.PLANNED, Trip.Status.CONFIRMED, Trip.Status.CANCELLED):
        _set_trip_status(source, status)
        with pytest.raises(ValidationError):
            trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target.pk,
                source_kind="completed_trip",
                source_id=source.pk,
                selections=[(entry.pk, 1)],
            )
        assert not TripGear.objects.filter(trip=target).exists()

    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="saved_trip",
            source_id=source.pk,
            selections=[(entry.pk, 1)],
        )
    assert not TripGear.objects.filter(trip=target).exists()


def test_seed_trip_gear_does_not_disclose_foreign_source_or_target_ids(
    owner, other_owner, household, other_household
):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    own_gear = _gear_item(household, "Tent")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(own_gear, 1)])
    selection = [(own_gear.pk, 1)]

    foreign_gear = _gear_item(other_household, "Foreign tent")
    foreign_kit, foreign_rows = _kit_with_rows(
        actor=other_owner, household=other_household, entries=[(foreign_gear, 1)]
    )
    foreign_trip, foreign_trip_rows = _completed_source_trip(
        actor=other_owner, household=other_household, entries=[(foreign_gear, 1)]
    )
    unknown = uuid.uuid4()

    expected_kit = (GearKit.DoesNotExist, "Gear kit not found.")
    for source_id in (foreign_kit.pk, unknown):
        assert _exception_signature(
            lambda source_id=source_id: trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target.pk,
                source_kind="kit",
                source_id=source_id,
                selections=selection,
            )
        ) == expected_kit

    expected_trip = (Trip.DoesNotExist, "Trip not found.")
    for source_id in (foreign_trip.pk, unknown):
        assert _exception_signature(
            lambda source_id=source_id: trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target.pk,
                source_kind="completed_trip",
                source_id=source_id,
                selections=selection,
            )
        ) == expected_trip

    for target_id in (foreign_trip.pk, unknown, "not-a-uuid"):
        assert _exception_signature(
            lambda target_id=target_id: trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target_id,
                source_kind="kit",
                source_id=kit.pk,
                selections=selection,
            )
        ) == expected_trip

    assert _exception_signature(
        lambda: trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id="not-a-uuid",
            selections=selection,
        )
    ) == expected_kit

    expected_selected_row = _exception_signature(
        lambda: trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(uuid.uuid4(), 1)],
        )
    )
    for stale_row_id in (foreign_gear.pk, uuid.uuid4()):
        assert _exception_signature(
            lambda stale_row_id=stale_row_id: trip_services.seed_trip_gear(
                actor=owner,
                target_trip_id=target.pk,
                source_kind="kit",
                source_id=kit.pk,
                selections=[(stale_row_id, 1)],
            )
        ) == expected_selected_row

    assert not TripGear.objects.filter(trip=target).exists()
    assert TripGear.objects.filter(trip=foreign_trip).count() == len(foreign_trip_rows)


def test_seed_trip_gear_validates_all_selected_gear_households_before_inserts(
    owner, household, other_household, monkeypatch
):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    own_gear = _gear_item(household, "Tent")
    foreign_gear = _gear_item(other_household, "Foreign stove")
    kit, _ = _kit_with_rows(actor=owner, household=household, entries=[(own_gear, 1)])
    # Simulate corrupted/legacy ORM data that bypassed model validation.
    foreign_row = GearKitItem.objects.create(kit=kit, gear_item=foreign_gear, quantity=1)
    attempted_saves = []
    original_save = TripGear.save

    def recording_save(entry, *args, **kwargs):
        attempted_saves.append(entry.gear_item_id)
        return original_save(entry, *args, **kwargs)

    monkeypatch.setattr(TripGear, "save", recording_save)
    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(own_gear.pk, 1), (foreign_row.pk, 1)],
        )

    assert attempted_saves == []
    assert not TripGear.objects.filter(trip=target).exists()


def test_seed_trip_gear_keeps_multi_household_sources_inside_the_target_household(
    owner, other_owner, household, other_household
):
    Membership.objects.create(user=owner, household=other_household, role=MembershipRole.MEMBER)
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    foreign_gear = _gear_item(other_household, "Foreign tent")
    foreign_kit, foreign_rows = _kit_with_rows(
        actor=other_owner, household=other_household, entries=[(foreign_gear, 1)]
    )
    foreign_trip, foreign_trip_rows = _completed_source_trip(
        actor=other_owner, household=other_household, entries=[(foreign_gear, 1)]
    )

    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=foreign_kit.pk,
            selections=[(foreign_gear.pk, 1)],
        )
    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="completed_trip",
            source_id=foreign_trip.pk,
            selections=[(foreign_gear.pk, 1)],
        )
    assert not TripGear.objects.filter(trip=target).exists()


def test_seed_trip_gear_rejects_stale_source_rows_without_partial_rows(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent")
    stove = _gear_item(household, "Stove")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(tent, 2), (stove, 1)])
    stale_kit_gear = stove.pk
    GearKitItem.objects.filter(kit=kit, gear_item_id=stale_kit_gear).delete()

    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(tent.pk, 1), (stale_kit_gear, 1)],
        )
    assert not TripGear.objects.filter(trip=target).exists()

    source, source_rows = _completed_source_trip(
        actor=owner, household=household, entries=[(tent, 2), (stove, 1)]
    )
    stale_trip_gear = stove.pk
    TripGear.objects.filter(trip=source, gear_item_id=stale_trip_gear).delete()
    with pytest.raises(ValidationError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="completed_trip",
            source_id=source.pk,
            selections=[(tent.pk, 1), (stale_trip_gear, 1)],
        )
    assert not TripGear.objects.filter(trip=target).exists()


def test_seed_trip_gear_rolls_back_every_row_when_a_write_fails(owner, household, monkeypatch):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent")
    stove = _gear_item(household, "Stove")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(tent, 2), (stove, 1)])
    original_save = TripGear.save
    calls = {"count": 0}

    def flaky_save(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise IntegrityError("simulated insert failure")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(TripGear, "save", flaky_save)
    with pytest.raises(IntegrityError):
        trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(tent.pk, 2), (stove.pk, 1)],
        )

    assert calls["count"] == 2
    assert not TripGear.objects.filter(trip=target).exists()


def test_remove_trip_gear_removes_only_the_target_association(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent", weight=1800)
    stove = _gear_item(household, "Stove")
    tent_entry = add_trip_gear(trip_id=target.pk, gear_item_id=tent.pk, quantity=2, actor=owner)
    add_trip_gear(trip_id=target.pk, gear_item_id=stove.pk, quantity=1, actor=owner)
    source, _source_rows = _completed_source_trip(
        actor=owner, household=household, entries=[(tent, 3), (stove, 1)]
    )
    kit, _kit_rows = _kit_with_rows(actor=owner, household=household, entries=[(tent, 3), (stove, 1)])
    source_before = list(TripGear.objects.filter(trip=source).order_by("pk").values())
    kit_before = _kit_snapshot(kit)
    gear_before = _gear_snapshot(household)

    assert trip_services.remove_trip_gear(
        actor=owner, trip_id=target.pk, entry_id=tent_entry.pk
    ) is None

    assert _target_rows(target) == {stove.pk: (1, False)}
    assert list(TripGear.objects.filter(trip=source).order_by("pk").values()) == source_before
    assert _kit_snapshot(kit) == kit_before
    assert _gear_snapshot(household) == gear_before


def test_remove_trip_gear_rejects_unknown_or_other_trip_entries(owner, household):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    tent = _gear_item(household, "Tent")
    TripGear.objects.create(trip=target, gear_item=tent, quantity=1)
    source, source_rows = _completed_source_trip(
        actor=owner, household=household, entries=[(tent, 1)]
    )
    expected = (TripGear.DoesNotExist, "Trip gear not found.")

    for entry_id in (uuid.uuid4(), source_rows[tent.pk].pk, "not-a-uuid"):
        assert _exception_signature(
            lambda entry_id=entry_id: trip_services.remove_trip_gear(
                actor=owner, trip_id=target.pk, entry_id=entry_id
            )
        ) == expected

    assert _target_rows(target) == {tent.pk: (1, False)}
    assert TripGear.objects.filter(trip=source).count() == 1


@pytest.mark.parametrize("status", [Trip.Status.COMPLETED, Trip.Status.CANCELLED])
def test_read_only_trip_statuses_reject_every_packing_mutation(owner, household, status):
    target = create_trip(actor=owner, household=household, data={"name": "Finished trip"})
    tent = _gear_item(household, "Tent")
    stove = _gear_item(household, "Stove")
    lantern = _gear_item(household, "Lantern")
    tent_entry = add_trip_gear(trip_id=target.pk, gear_item_id=tent.pk, quantity=2, actor=owner)
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(stove, 1)])
    _set_trip_status(target, status)
    rows_before = _target_rows(target)
    trip_before = _trip_snapshot(target)
    gear_before = _gear_snapshot(household)

    operations = (
        lambda: add_trip_gear(
            trip_id=target.pk, gear_item_id=lantern.pk, quantity=1, actor=owner
        ),
        lambda: set_trip_gear_packed(
            trip_id=target.pk, entry_id=tent_entry.pk, packed=True, actor=owner
        ),
        lambda: trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=target.pk,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(stove.pk, 1)],
        ),
        lambda: trip_services.remove_trip_gear(
            actor=owner, trip_id=target.pk, entry_id=tent_entry.pk
        ),
    )
    for operation in operations:
        with pytest.raises(ValidationError):
            operation()

    assert _target_rows(target) == rows_before
    assert _trip_snapshot(target) == trip_before
    assert _gear_snapshot(household) == gear_before


@pytest.mark.parametrize("status", [Trip.Status.PLANNED, Trip.Status.CONFIRMED])
def test_mutable_trip_statuses_allow_seed_add_pack_and_remove(owner, household, status):
    target = create_trip(actor=owner, household=household, data={"name": "Active trip"})
    stove = _gear_item(household, "Stove")
    lantern = _gear_item(household, "Lantern")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(stove, 1)])
    _set_trip_status(target, status)

    seeded = trip_services.seed_trip_gear(
        actor=owner,
        target_trip_id=target.pk,
        source_kind="kit",
        source_id=kit.pk,
        selections=[(stove.pk, 2)],
    )
    assert (seeded.added, seeded.skipped) == (1, 0)
    added = add_trip_gear(trip_id=target.pk, gear_item_id=lantern.pk, quantity=1, actor=owner)
    set_trip_gear_packed(trip_id=target.pk, entry_id=added.pk, packed=True, actor=owner)
    stove_entry = TripGear.objects.get(trip=target, gear_item=stove)
    trip_services.remove_trip_gear(actor=owner, trip_id=target.pk, entry_id=stove_entry.pk)

    assert _target_rows(target) == {lantern.pk: (1, True)}


def test_packing_mutations_do_not_disclose_foreign_trip_ids(
    owner, other_owner, household, other_household
):
    target = create_trip(actor=owner, household=household, data={"name": "New trip"})
    own_gear = _gear_item(household, "Tent")
    kit, rows = _kit_with_rows(actor=owner, household=household, entries=[(own_gear, 1)])
    foreign_trip = create_trip(actor=other_owner, household=other_household, data={"name": "Foreign"})
    foreign_gear = _gear_item(other_household, "Foreign tent")
    foreign_entry = TripGear.objects.create(trip=foreign_trip, gear_item=foreign_gear, quantity=1)
    unknown = uuid.uuid4()
    expected = (Trip.DoesNotExist, "Trip not found.")

    actions = (
        lambda trip_id: add_trip_gear(
            trip_id=trip_id, gear_item_id=own_gear.pk, quantity=1, actor=owner
        ),
        lambda trip_id: set_trip_gear_packed(
            trip_id=trip_id, entry_id=foreign_entry.pk, packed=True, actor=owner
        ),
        lambda trip_id: trip_services.remove_trip_gear(
            actor=owner, trip_id=trip_id, entry_id=foreign_entry.pk
        ),
        lambda trip_id: trip_services.seed_trip_gear(
            actor=owner,
            target_trip_id=trip_id,
            source_kind="kit",
            source_id=kit.pk,
            selections=[(own_gear.pk, 1)],
        ),
    )
    for action in actions:
        assert _exception_signature(lambda: action(foreign_trip.pk)) == expected
        assert _exception_signature(lambda: action(unknown)) == expected
        assert _exception_signature(lambda: action("not-a-uuid")) == expected

    expected_entry = (TripGear.DoesNotExist, "Trip gear not found.")
    for entry_id in (foreign_entry.pk, uuid.uuid4(), "not-a-uuid"):
        assert _exception_signature(
            lambda entry_id=entry_id: set_trip_gear_packed(
                trip_id=target.pk, entry_id=entry_id, packed=True, actor=owner
            )
        ) == expected_entry

    assert not TripGear.objects.filter(trip=target).exists()
    assert TripGear.objects.filter(trip=foreign_trip).count() == 1
    foreign_entry.refresh_from_db()
    assert foreign_entry.packed is False
