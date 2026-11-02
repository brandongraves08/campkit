from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.households.permissions import current_household
from apps.inventory.services import list_gear
from apps.trips.models import Trip, TripGear
from apps.trips.services import add_trip_gear, cancel_trip, create_trip, packed_base_weight, set_trip_gear_packed, trip_packing_list


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
        "trips": Trip.objects.for_household(household).order_by("-created_at"), "form": form,
    })


@login_required
def trip_detail(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    return render(request, "trips/trip_detail.html", {
        "trip": trip, "weights": packed_base_weight(trip_id=trip.pk, actor=request.user),
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
        return render(request, "trips/trip_detail.html", {"trip": trip}, status=400)
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
