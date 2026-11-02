from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.inventory.services import (
    get_pantry_stock,
    move_confirmed_pantry_stock,
    record_pantry_reservation_change,
)
from apps.meals.models import FoodReservation, Meal, MealIngredient, MealVerificationRequest
from apps.trips.services import get_trip_for_actor, set_trip_status


def _audit_reservation_change(*, actor, household, reservation, before, after, action):
    record_pantry_reservation_change(actor=actor, household=household,
                                    item_id=reservation.pantry_item_id, action=action,
                                    before=before, after=after)


def _trip(trip_id, actor, lock=False):
    return get_trip_for_actor(trip_id, actor, lock=lock)


@transaction.atomic
def create_meal(*, trip_id, actor, data, ingredients=()):
    trip = _trip(trip_id, actor, lock=True)
    if trip.status != "planned":
        raise ValidationError("Meals can only be changed while a trip is planned.")
    meal = Meal(trip=trip, **data)
    meal.full_clean()
    meal.save()
    ingredient_rows = list(ingredients)
    stock = get_pantry_stock(actor=actor, household=trip.household,
                             item_ids=[entry["pantry_item_id"] for entry in ingredient_rows])
    for entry in ingredient_rows:
        item = stock.get(str(entry["pantry_item_id"]))
        if item is None:
            raise PermissionDenied("Meal pantry stock must belong to the trip household.")
        ingredient = MealIngredient(meal=meal, pantry_item_id=item["id"], quantity=entry["quantity"],
                                    unit=entry.get("unit", item["unit"]))
        ingredient.full_clean()
        ingredient.save()
    return meal


@transaction.atomic
def _confirm_meal_plan_atomic(*, trip_id, actor):
    trip = _trip(trip_id, actor, lock=True)
    if trip.status in {"cancelled", "completed"}:
        raise ValidationError("A cancelled or completed trip cannot reserve food.")
    existing = list(FoodReservation.objects.filter(meal__trip_id=trip.pk).order_by("pk"))
    if trip.status == "confirmed":
        return existing
    ingredients = list(
        MealIngredient.objects.filter(meal__trip_id=trip.pk).select_related("meal")
        .order_by("pantry_item_id", "pk")
    )
    if not ingredients:
        set_trip_status(trip_id=trip.pk, status="confirmed", actor=actor)
        return []
    item_ids = {ingredient.pantry_item_id for ingredient in ingredients}
    stock = get_pantry_stock(actor=actor, household=trip.household, item_ids=item_ids)
    if len(stock) != len(item_ids):
        raise PermissionDenied("Meal pantry stock is outside the trip household.")
    estimated = [item for item in stock.values()
                 if item["verification_status"] != "confirmed"]
    if estimated:
        raise ValidationError("Estimated pantry stock requires household verification before reservation.")

    locked_stock = get_pantry_stock(actor=actor, household=trip.household,
                                    item_ids=item_ids, for_update=True)
    totals = {}
    for ingredient in ingredients:
        item = locked_stock.get(str(ingredient.pantry_item_id))
        if item is None:
            raise PermissionDenied("Meal pantry stock is outside the trip household.")
        if item["verification_status"] != "confirmed":
            raise ValidationError("Estimated pantry stock requires household verification before reservation.")
        if ingredient.unit != item["unit"]:
            raise ValidationError("Meal ingredient and pantry units must match.")
        item_id = str(ingredient.pantry_item_id)
        totals[item_id] = totals.get(item_id, Decimal("0")) + ingredient.quantity
    for item_id, quantity in totals.items():
        item = locked_stock[item_id]
        if item["quantity"] < quantity:
            raise ValidationError(f"Insufficient confirmed stock for {item['name']} ({item['unit']}).")

    reservations = []
    for item_id, quantity in totals.items():
        move_confirmed_pantry_stock(actor=actor, household=trip.household, item_id=item_id,
                                    delta=-quantity, action="food_reserved")
    for ingredient in ingredients:
        reservation = FoodReservation(meal=ingredient.meal, pantry_item_id=ingredient.pantry_item_id,
                                      quantity=ingredient.quantity, created_by=actor)
        reservation.full_clean()
        reservation.save()
        _audit_reservation_change(actor=actor, household=trip.household, reservation=reservation, before=None,
                                  after={"reservation": str(reservation.pk), "meal": str(ingredient.meal_id),
                                         "quantity": str(reservation.quantity), "status": reservation.status},
                                  action="food_reservation_created")
        reservations.append(reservation)
    set_trip_status(trip_id=trip.pk, status="confirmed", actor=actor)
    return reservations


def confirm_meal_plan(*, trip_id, actor):
    try:
        return _confirm_meal_plan_atomic(trip_id=trip_id, actor=actor)
    except ValidationError as exc:
        if "Estimated pantry stock" not in str(exc):
            raise
        trip = _trip(trip_id, actor)
        ingredients = MealIngredient.objects.filter(meal__trip_id=trip.pk).values_list("pantry_item_id", flat=True)
        stock = get_pantry_stock(actor=actor, household=trip.household, item_ids=set(ingredients))
        for item in stock.values():
            if item["verification_status"] == "estimated":
                MealVerificationRequest.objects.get_or_create(
                    trip=trip, pantry_item_id=item["id"], resolved_at__isnull=True,
                    defaults={"created_by": actor},
                )
        raise


@transaction.atomic
def release_trip_food(*, trip_id, actor):
    trip = _trip(trip_id, actor, lock=True)
    reservations = list(FoodReservation.objects.select_for_update().filter(
        meal__trip_id=trip.pk, status=FoodReservation.Status.RESERVED,
    ).order_by("pk"))
    stock = get_pantry_stock(actor=actor, household=trip.household,
                             item_ids={reservation.pantry_item_id for reservation in reservations}, for_update=True)
    for reservation in reservations:
        item = stock.get(str(reservation.pantry_item_id))
        if item is None:
            raise PermissionDenied("Reserved pantry stock is outside the trip household.")
        move_confirmed_pantry_stock(actor=actor, household=trip.household, item_id=item["id"],
                                    delta=reservation.quantity, action="food_reservation_released")
        before = {"reservation": str(reservation.pk), "status": reservation.status,
                  "quantity": str(reservation.quantity), "consumed_quantity": str(reservation.consumed_quantity)}
        reservation.status = FoodReservation.Status.RELEASED
        reservation.save(update_fields=["status", "updated_at"])
        _audit_reservation_change(actor=actor, household=trip.household, reservation=reservation,
                                  before=before,
                                  after={"reservation": str(reservation.pk), "status": reservation.status,
                                         "reason": "trip_cancelled"},
                                  action="food_reservation_released")
    return reservations


@transaction.atomic
def reconcile_trip_food(*, trip_id, consumed_quantities, actor):
    trip = _trip(trip_id, actor, lock=True)
    if trip.status == "cancelled":
        raise ValidationError("A cancelled trip cannot be reconciled.")
    reservations = list(FoodReservation.objects.select_for_update().filter(
        meal__trip_id=trip.pk, status=FoodReservation.Status.RESERVED,
    ).order_by("pk"))
    valid_keys = {str(reservation.pk) for reservation in reservations}
    supplied_keys = {str(key) for key in consumed_quantities}
    if supplied_keys != valid_keys:
        raise ValidationError("Consumption must be provided for every reserved food item, with no outside reservations.")
    stock = get_pantry_stock(actor=actor, household=trip.household,
                             item_ids={reservation.pantry_item_id for reservation in reservations}, for_update=True)
    updates = []
    for reservation in reservations:
        raw = consumed_quantities.get(str(reservation.pk), consumed_quantities.get(reservation.pk))
        try:
            consumed = Decimal(str(raw))
        except (InvalidOperation, TypeError):
            raise ValidationError("Consumed quantities must be decimal values.")
        if consumed < 0 or consumed > reservation.quantity:
            raise ValidationError("Consumed quantity must be between zero and the reserved quantity.")
        item = stock.get(str(reservation.pantry_item_id))
        if item is None:
            raise PermissionDenied("Reserved pantry stock is outside the trip household.")
        updates.append((reservation, consumed, reservation.quantity - consumed, item))
    for reservation, consumed, returned, item in updates:
        if returned:
            move_confirmed_pantry_stock(actor=actor, household=trip.household, item_id=item["id"],
                                        delta=returned, action="food_reconciled")
        before = {"reservation": str(reservation.pk), "status": reservation.status,
                  "quantity": str(reservation.quantity), "consumed_quantity": str(reservation.consumed_quantity)}
        reservation.consumed_quantity = consumed
        reservation.status = FoodReservation.Status.RECONCILED
        reservation.save(update_fields=["consumed_quantity", "status", "updated_at"])
        _audit_reservation_change(actor=actor, household=trip.household, reservation=reservation,
                                  before=before,
                                  after={"reservation": str(reservation.pk), "status": reservation.status,
                                         "consumed_quantity": str(consumed), "returned_quantity": str(returned)},
                                  action="food_reservation_reconciled")
    set_trip_status(trip_id=trip.pk, status="completed", actor=actor)
    return reservations
