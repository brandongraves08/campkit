from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import InventoryChange
from apps.inventory.models import InventoryVerification, PantryItem
from apps.meals.models import FoodReservation, Meal, MealIngredient, MealVerificationRequest
from apps.meals.services import confirm_meal_plan, reconcile_trip_food
from apps.trips.services import cancel_trip, create_trip

pytestmark = pytest.mark.django_db


def pantry(household, owner, quantity, status=InventoryVerification.CONFIRMED):
    return PantryItem.objects.create(
        household=household, name="Beans", quantity=Decimal(quantity), unit="each",
        verification_status=status,
        verified_by=owner if status == InventoryVerification.CONFIRMED else None,
        verified_at=timezone.now() if status == InventoryVerification.CONFIRMED else None,
    )


def meal_for(owner, household, trip, item, amount):
    meal = Meal.objects.create(trip=trip, name="Dinner", servings=2)
    MealIngredient.objects.create(meal=meal, pantry_item=item, quantity=Decimal(amount), unit=item.unit)
    return meal


def test_confirmation_reserves_confirmed_stock_atomically_and_audits(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal_for(owner, household, trip, item, "3")
    confirm_meal_plan(trip_id=trip.pk, actor=owner)
    item.refresh_from_db()
    reservation = FoodReservation.objects.get(meal__trip=trip)
    assert item.quantity == Decimal("2")
    assert reservation.quantity == Decimal("3")
    assert reservation.status == FoodReservation.Status.RESERVED
    assert InventoryChange.objects.filter(action="food_reserved", actor=owner).count() == 1


def test_conflict_rolls_back_all_reservations(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    enough = pantry(household, owner, "4")
    short = pantry(household, owner, "1")
    MealIngredient.objects.create(meal=Meal.objects.create(trip=trip, name="Meal 1"), pantry_item=enough,
                                  quantity=Decimal("2"), unit="each")
    MealIngredient.objects.create(meal=Meal.objects.create(trip=trip, name="Meal 2"), pantry_item=short,
                                  quantity=Decimal("2"), unit="each")
    with pytest.raises(ValidationError):
        confirm_meal_plan(trip_id=trip.pk, actor=owner)
    enough.refresh_from_db()
    short.refresh_from_db()
    assert enough.quantity == Decimal("4") and short.quantity == Decimal("1")
    assert not FoodReservation.objects.filter(meal__trip=trip).exists()


def test_estimated_stock_creates_verification_request_and_is_never_reserved(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    estimated = pantry(household, owner, "5", InventoryVerification.ESTIMATED)
    meal_for(owner, household, trip, estimated, "2")
    with pytest.raises(ValidationError):
        confirm_meal_plan(trip_id=trip.pk, actor=owner)
    estimated.refresh_from_db()
    assert estimated.quantity == Decimal("5")
    assert not FoodReservation.objects.filter(meal__trip=trip).exists()
    assert MealVerificationRequest.objects.filter(trip=trip, pantry_item=estimated, resolved_at__isnull=True).exists()


def test_confirmation_is_idempotent_and_audits_reservation_state(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal_for(owner, household, trip, item, "3")
    reservations = confirm_meal_plan(trip_id=trip.pk, actor=owner)
    assert len(reservations) == 1
    confirm_meal_plan(trip_id=trip.pk, actor=owner)
    item.refresh_from_db()
    assert item.quantity == Decimal("2")
    assert InventoryChange.objects.filter(action="food_reservation_created", actor=owner).count() == 1


def test_reconciliation_requires_every_reserved_item(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal_for(owner, household, trip, item, "3")
    confirm_meal_plan(trip_id=trip.pk, actor=owner)
    with pytest.raises(ValidationError):
        reconcile_trip_food(trip_id=trip.pk, consumed_quantities={}, actor=owner)
    item.refresh_from_db()
    assert item.quantity == Decimal("2")


def test_trip_cancellation_releases_reservation(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal_for(owner, household, trip, item, "3")
    confirm_meal_plan(trip_id=trip.pk, actor=owner)
    cancel_trip(trip_id=trip.pk, actor=owner)
    item.refresh_from_db()
    assert item.quantity == Decimal("5")
    reservation = FoodReservation.objects.get(meal__trip=trip)
    assert reservation.status == FoodReservation.Status.RELEASED


def test_reconciliation_returns_unused_stock_and_records_consumed(owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal = meal_for(owner, household, trip, item, "4")
    confirm_meal_plan(trip_id=trip.pk, actor=owner)
    reservation = FoodReservation.objects.get(meal=meal)
    reconcile_trip_food(trip_id=trip.pk, consumed_quantities={str(reservation.pk): "2.5"}, actor=owner)
    item.refresh_from_db()
    reservation.refresh_from_db()
    assert item.quantity == Decimal("2.5")
    assert reservation.consumed_quantity == Decimal("2.5")
    assert reservation.status == FoodReservation.Status.RECONCILED


def test_meal_screen_creates_and_confirms_plan_through_services(client, owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "UI weekend"})
    item = pantry(household, owner, "5")
    client.force_login(owner)
    page = client.get(f"/meals/trips/{trip.pk}/")
    assert page.status_code == 200
    assert b"Meal plan" in page.content
    response = client.post(f"/meals/trips/{trip.pk}/", {
        "name": "Supper", "servings": "2", "pantry_item_id": str(item.pk), "quantity": "2",
    })
    assert response.status_code == 302
    response = client.post(f"/meals/trips/{trip.pk}/confirm/", {})
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.quantity == Decimal("3")


def test_meal_plan_cannot_be_confirmed_by_nonmember(owner, other_owner, household):
    trip = create_trip(actor=owner, household=household, data={"name": "Camp"})
    item = pantry(household, owner, "5")
    meal_for(owner, household, trip, item, "2")
    with pytest.raises(PermissionDenied):
        confirm_meal_plan(trip_id=trip.pk, actor=other_owner)
    item.refresh_from_db()
    assert item.quantity == Decimal("5")
