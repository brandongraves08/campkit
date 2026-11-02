from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.households.permissions import current_household
from apps.inventory.services import list_gear
from apps.trips.models import Trip, TripGear
from apps.trips.services import (
    add_trip_gear,
    cancel_trip,
    create_trip,
    list_trips,
    set_trip_gear_packed,
    set_trip_status,
    trip_packing_list,
    trip_readiness,
)


class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ("name", "destination", "start_date", "end_date", "party_size", "conditions")
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}


@login_required
def trip_list(request):
    household = current_household(request)
    form = TripForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        trip = create_trip(actor=request.user, household=household, data=form.cleaned_data)
        return redirect("trips:trip_detail", trip_id=trip.pk)
    return render(request, "trips/trip_list.html", {
        "nav": "trips",
        "trips": list_trips(actor=request.user, household=household), "form": form,
    })


@login_required
def trip_detail(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    readiness = trip_readiness(trip_id=trip.pk, actor=request.user)
    return render(request, "trips/trip_detail.html", {
        "nav": "trips",
        "trip": trip, "readiness": readiness, "weights": readiness["weights"],
        "gear": trip_packing_list(trip_id=trip.pk, actor=request.user),
        "available_gear": list_gear(actor=request.user, household=household),
    })


@login_required
@require_POST
def trip_gear_add(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    try:
        quantity = int(request.POST.get("quantity", "1"))
        add_trip_gear(trip_id=trip.pk, gear_item_id=request.POST.get("gear_item_id"), quantity=quantity,
                      actor=request.user)
    except (ValueError, TypeError):
        messages.error(request, "Packed quantity must be a whole number of at least one.")
    except (ObjectDoesNotExist, PermissionDenied):
        messages.error(request, "That gear item is not available in this household.")
    except ValidationError as exc:
        messages.error(request, " ".join(str(item) for item in exc.messages))
    return redirect("trips:trip_detail", trip_id=trip.pk)


@login_required
@require_POST
def trip_gear_packed(request, trip_id, entry_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    entry = get_object_or_404(TripGear.objects.filter(trip=trip), pk=entry_id)
    set_trip_gear_packed(trip_id=trip.pk, entry_id=entry.pk,
                         packed=request.POST.get("packed") == "true", actor=request.user)
    return redirect("trips:trip_detail", trip_id=trip.pk)


@login_required
@require_POST
def trip_cancel(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    cancel_trip(trip_id=trip.pk, actor=request.user)
    return redirect("trips:trip_detail", trip_id=trip.pk)


@login_required
@require_POST
def trip_status(request, trip_id):
    """Advance a trip through the existing confirmed/completed workflow."""
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    try:
        set_trip_status(trip_id=trip.pk, status=request.POST.get("status", ""), actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(str(item) for item in exc.messages))
    return redirect("trips:trip_detail", trip_id=trip.pk)
