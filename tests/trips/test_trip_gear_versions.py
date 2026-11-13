import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.households.models import Membership
from apps.inventory.models import GearItem
from apps.trips.models import Trip, TripGear
from apps.trips.services import (
    TripGearVersionConflict,
    set_trip_gear_packed,
    set_trip_gear_packed_versioned,
)

pytestmark = pytest.mark.django_db


def _entry(household, member, *, status=Trip.Status.PLANNED):
    trip = Trip.objects.create(household=household, created_by=member, name="Weekend", status=status)
    gear = GearItem.objects.create(household=household, name="Tent", category="shelter")
    return trip, TripGear.objects.create(trip=trip, gear_item=gear)


def test_trip_gear_starts_at_version_one_and_mutations_increment_only_changed_row(member, household):
    trip, first = _entry(household, member)
    second_gear = GearItem.objects.create(household=household, name="Stove", category="cooking")
    second = TripGear.objects.create(trip=trip, gear_item=second_gear)
    assert first.version == second.version == 1

    updated, applied = set_trip_gear_packed_versioned(
        trip_id=trip.pk, entry_id=first.pk, packed=True, base_version=1, actor=member
    )
    assert applied is True
    assert (updated.packed, updated.version) == (True, 2)
    second.refresh_from_db()
    assert (second.packed, second.version) == (False, 1)


def test_repeating_desired_state_at_current_version_is_a_noop(member, household):
    trip, entry = _entry(household, member)
    updated, applied = set_trip_gear_packed_versioned(
        trip_id=trip.pk, entry_id=entry.pk, packed=False, base_version=1, actor=member
    )
    assert applied is False
    assert (updated.packed, updated.version) == (False, 1)


def test_stale_row_version_raises_conflict_without_mutation(member, household):
    trip, entry = _entry(household, member)
    set_trip_gear_packed_versioned(
        trip_id=trip.pk, entry_id=entry.pk, packed=True, base_version=1, actor=member
    )

    with pytest.raises(TripGearVersionConflict) as error:
        set_trip_gear_packed_versioned(
            trip_id=trip.pk, entry_id=entry.pk, packed=False, base_version=1, actor=member
        )
    assert (error.value.packed, error.value.version) == (True, 2)
    entry.refresh_from_db()
    assert (entry.packed, entry.version) == (True, 2)


@pytest.mark.parametrize("status", [Trip.Status.COMPLETED, Trip.Status.CANCELLED])
def test_read_only_trip_rejects_versioned_and_legacy_writes(member, household, status):
    trip, entry = _entry(household, member, status=status)
    with pytest.raises(ValidationError):
        set_trip_gear_packed_versioned(
            trip_id=trip.pk, entry_id=entry.pk, packed=True, base_version=1, actor=member
        )
    with pytest.raises(ValidationError):
        set_trip_gear_packed(trip_id=trip.pk, entry_id=entry.pk, packed=True, actor=member)
    entry.refresh_from_db()
    assert (entry.packed, entry.version) == (False, 1)


def test_missing_or_cross_trip_entry_cannot_be_written(member, household):
    first_trip, first_entry = _entry(household, member)
    second_trip = Trip.objects.create(household=household, created_by=member, name="Other")
    with pytest.raises(TripGear.DoesNotExist):
        set_trip_gear_packed_versioned(
            trip_id=second_trip.pk, entry_id=first_entry.pk, packed=True, base_version=1, actor=member
        )
    with pytest.raises(TripGear.DoesNotExist):
        set_trip_gear_packed_versioned(
            trip_id=first_trip.pk, entry_id=999999, packed=True, base_version=1, actor=member
        )
    first_entry.refresh_from_db()
    assert (first_entry.packed, first_entry.version) == (False, 1)


def test_nonmember_cannot_use_versioned_write(member, household, other_owner):
    trip, entry = _entry(household, member)
    Membership.objects.filter(household=household, user=other_owner).delete()
    with pytest.raises((PermissionDenied, Trip.DoesNotExist)):
        set_trip_gear_packed_versioned(
            trip_id=trip.pk, entry_id=entry.pk, packed=True, base_version=1, actor=other_owner
        )
    entry.refresh_from_db()
    assert (entry.packed, entry.version) == (False, 1)


def test_legacy_web_write_remains_compatible_and_advances_row_version(member, household):
    trip, entry = _entry(household, member)
    updated = set_trip_gear_packed(trip_id=trip.pk, entry_id=entry.pk, packed=True, actor=member)
    assert (updated.packed, updated.version) == (True, 2)
