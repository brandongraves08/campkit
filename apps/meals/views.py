from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.households.permissions import current_household
from apps.inventory.services import list_pantry
from apps.meals.models import FoodReservation, Meal
from apps.meals.services import confirm_meal_plan, create_meal, reconcile_trip_food
from apps.trips.services import get_trip_for_actor


def _trip_or_404(trip_id, actor):
    """Keep foreign trip ids indistinguishable from missing ones in the UI."""
    try:
        return get_trip_for_actor(trip_id, actor)
    except (ObjectDoesNotExist, PermissionDenied):
        raise Http404("Trip not found.")


@login_required
def meal_list(request, trip_id):
    household = current_household(request)
    trip = _trip_or_404(trip_id, request.user)
    pantry = list_pantry(actor=request.user, household=household).order_by("name")
    error = None
    if request.method == "POST":
        try:
            name = request.POST.get("name", "").strip()
            if not name:
                raise ValidationError("Meal name is required.")
            data = {"name": name, "servings": int(request.POST.get("servings", 1)),
                    "notes": request.POST.get("notes", "")}
            ingredients = []
            item_id = request.POST.get("pantry_item_id", "").strip()
            amount = request.POST.get("quantity", "").strip()
            if item_id or amount:
                ingredients.append({"pantry_item_id": item_id, "quantity": Decimal(amount)})
            create_meal(trip_id=trip.pk, actor=request.user, data=data, ingredients=ingredients)
            return redirect("meals:meal_list", trip_id=trip.pk)
        except (ValueError, InvalidOperation, ValidationError) as exc:
            error = str(exc)
    return render(request, "meals/meal_list.html", {
        "nav": "trips",
        "trip": trip, "meals": trip.meals.prefetch_related("ingredients__pantry_item", "reservations"),
        "pantry": pantry, "error": error,
    })


@login_required
@require_POST
def meal_plan_confirm(request, trip_id):
    household = current_household(request)
    trip = _trip_or_404(trip_id, request.user)
    try:
        confirm_meal_plan(trip_id=trip.pk, actor=request.user)
    except ValidationError as exc:
        return render(request, "meals/meal_list.html", {
            "nav": "trips",
            "trip": trip, "meals": trip.meals.prefetch_related("ingredients__pantry_item", "reservations"),
            "pantry": list_pantry(actor=request.user, household=household), "error": str(exc),
        }, status=400)
    return redirect("meals:meal_list", trip_id=trip.pk)


@login_required
@require_POST
def trip_reconcile(request, trip_id):
    household = current_household(request)
    trip = _trip_or_404(trip_id, request.user)
    reservations = FoodReservation.objects.filter(meal__trip=trip, status=FoodReservation.Status.RESERVED)
    consumed = {str(res.pk): request.POST.get(f"consumed_{res.pk}", "0") for res in reservations}
    try:
        reconcile_trip_food(trip_id=trip.pk, consumed_quantities=consumed, actor=request.user)
    except ValidationError as exc:
        return render(request, "meals/meal_list.html", {
            "nav": "trips",
            "trip": trip, "meals": trip.meals.prefetch_related("ingredients__pantry_item", "reservations"),
            "pantry": list_pantry(actor=request.user, household=household), "error": str(exc),
        }, status=400)
    return redirect("meals:meal_list", trip_id=trip.pk)
