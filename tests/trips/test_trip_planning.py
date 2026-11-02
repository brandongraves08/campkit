import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from datetime import date, timedelta

from apps.inventory.models import GearItem, InventoryVerification
from apps.trips.models import Trip, TripGear
from apps.trips.services import cancel_trip, create_trip, packed_base_weight

pytestmark = pytest.mark.django_db


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
