from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.households.permissions import current_household
from apps.inventory.services import list_gear
from apps.trips.forms import GearKitForm, SaveGearKitForm, SeedTripGearForm
from apps.trips.models import GearKitItem, Trip, TripGear
from apps.trips.services import (
    add_trip_gear,
    cancel_trip,
    create_gear_kit_from_trip,
    create_trip,
    delete_gear_kit,
    list_gear_kits,
    list_trips,
    remove_trip_gear,
    seed_trip_gear,
    set_trip_gear_packed,
    set_trip_status,
    trip_packing_list,
    trip_readiness,
    update_gear_kit,
)


class TripForm(forms.ModelForm):
    class Meta:
        model = Trip
        fields = ("name", "destination", "start_date", "end_date", "party_size", "conditions")
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}


def _add_validation_error(form, error):
    if getattr(error, "error_dict", None):
        for field, errors in error.error_dict.items():
            for message in errors:
                form.add_error(field if field in form.fields else None, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


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
        "packing_mutable": trip.status in (Trip.Status.PLANNED, Trip.Status.CONFIRMED),
    })


@login_required
@require_http_methods(["GET", "POST"])
def trip_gear_seed(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    if trip.status in (Trip.Status.COMPLETED, Trip.Status.CANCELLED):
        messages.error(request, "Completed and cancelled trips keep a read-only packing list.")
        return redirect("trips:trip_detail", trip_id=trip.pk)
    kits = list(list_gear_kits(actor=request.user, household=household))
    completed_trips = list(Trip.objects.for_household(household).filter(status=Trip.Status.COMPLETED).order_by("name", "pk"))
    sources = ([ ("kit", kit.pk, f"Saved kit · {kit.name}") for kit in kits ] +
               [("completed_trip", source.pk, f"Completed trip · {source.name}") for source in completed_trips])
    source_rows = []
    source_kind = (request.POST.get("source_kind") if request.method == "POST" else request.GET.get("source_kind", ""))
    source_id = (request.POST.get("source_id") if request.method == "POST" else request.GET.get("source_id", ""))
    selected = next(((kind, pk) for kind, pk, _label in sources
                     if kind == source_kind and str(pk) == str(source_id)), None)
    if selected:
        kind, pk = selected
        if kind == "kit":
            source_rows = list(GearKitItem.objects.filter(kit_id=pk).select_related("gear_item").order_by("pk"))
        else:
            source_rows = list(TripGear.objects.filter(trip_id=pk).select_related("gear_item").order_by("pk"))
    form = SeedTripGearForm(
        request.POST or None, sources=sources, source_rows=source_rows,
        initial={"source_kind": source_kind, "source_id": source_id},
    )
    if request.method == "POST" and form.is_valid():
        chosen = (form.cleaned_data["source_kind"], form.cleaned_data["source_id"])
        if not any(kind == chosen[0] and str(pk) == chosen[1] for kind, pk, _label in sources):
            form.add_error("source_id", "Choose a source from this household.")
        elif request.POST.get("choose_source"):
            form = SeedTripGearForm(
                sources=sources, source_rows=source_rows,
                initial={"source_kind": chosen[0], "source_id": chosen[1]},
            )
        else:
            try:
                result = seed_trip_gear(
                    actor=request.user, target_trip_id=trip.pk, source_kind=chosen[0], source_id=chosen[1],
                    selections=form.selected_items(),
                )
            except ValidationError as exc:
                _add_validation_error(form, exc)
            else:
                note = f"{result.added} item{'s' if result.added != 1 else ''} added"
                if result.skipped:
                    note += f"; {result.skipped} already on this trip and left unchanged"
                messages.success(request, note + ".")
                return redirect("trips:trip_detail", trip_id=trip.pk)
    return render(request, "trips/trip_gear_seed.html", {
        "nav": "trips", "trip": trip, "form": form,
        "seed_rows": [{"gear": row.gear_item, "include": form[f"include_{row.gear_item.pk}"],
                       "quantity": form[f"quantity_{row.gear_item.pk}"]} for row in source_rows],
        "source_rows": source_rows, "has_sources": bool(sources), "source_selected": bool(selected),
    })


@login_required
@require_POST
def trip_gear_remove(request, trip_id, entry_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    entry = get_object_or_404(TripGear.objects.filter(trip=trip), pk=entry_id)
    try:
        remove_trip_gear(actor=request.user, trip_id=trip.pk, entry_id=entry.pk)
    except ValidationError as exc:
        messages.error(request, " ".join(str(item) for item in exc.messages))
    except ObjectDoesNotExist:
        messages.error(request, "That packing entry is no longer available.")
    else:
        messages.success(request, f"Removed {entry.gear_item.name} from this trip.")
    return redirect("trips:trip_detail", trip_id=trip.pk)


@login_required
def gear_kit_list(request):
    household = current_household(request)
    return render(request, "trips/gear_kit_list.html", {
        "nav": "trips", "kits": list_gear_kits(actor=request.user, household=household),
    })


@login_required
@require_http_methods(["GET", "POST"])
def gear_kit_create_from_trip(request, trip_id):
    household = current_household(request)
    trip = get_object_or_404(Trip.objects.for_household(household), pk=trip_id)
    trip_has_gear = bool(trip_packing_list(trip_id=trip.pk, actor=request.user))
    form = SaveGearKitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            kit = create_gear_kit_from_trip(actor=request.user, trip_id=trip.pk, name=form.cleaned_data["name"])
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, f"Saved {kit.name} as a reusable kit.")
            return redirect("trips:gear_kit_list")
    return render(request, "trips/gear_kit_form.html", {
        "nav": "trips", "form": form, "title": "Save packing list as a kit", "trip": trip,
        "trip_has_gear": trip_has_gear,
    })


@login_required
@require_http_methods(["GET", "POST"])
def gear_kit_edit(request, kit_id):
    household = current_household(request)
    kit = next((candidate for candidate in list_gear_kits(actor=request.user, household=household)
                if candidate.pk == kit_id), None)
    if kit is None:
        raise Http404("Saved kit not found.")
    gear_items = list(list_gear(actor=request.user, household=household).order_by("name", "pk"))
    quantities = {str(item.gear_item_id): item.quantity for item in kit.items.all()}
    form = GearKitForm(
        request.POST or None, gear_items=gear_items, quantities=quantities, initial={"name": kit.name},
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_gear_kit(actor=request.user, kit_id=kit.pk, name=form.cleaned_data["name"],
                            items=form.selected_items())
        except ValidationError as exc:
            _add_validation_error(form, exc)
        else:
            messages.success(request, f"Updated {form.cleaned_data['name']}.")
            return redirect("trips:gear_kit_list")
    return render(request, "trips/gear_kit_form.html", {
        "nav": "trips", "form": form, "title": "Edit saved kit", "kit": kit,
        "kit_rows": [{"gear": gear, "include": form[f"include_{gear.pk}"],
                      "quantity": form[f"quantity_{gear.pk}"]} for gear in gear_items],
    })


@login_required
@require_POST
def gear_kit_delete(request, kit_id):
    household = current_household(request)
    kits = list_gear_kits(actor=request.user, household=household)
    kit = next((candidate for candidate in kits if candidate.pk == kit_id), None)
    if kit is None:
        raise Http404("Saved kit not found.")
    name = kit.name
    delete_gear_kit(actor=request.user, kit_id=kit.pk)
    messages.success(request, f"Deleted {name}. Shared gear and trip lists were not changed.")
    return redirect("trips:gear_kit_list")


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
    try:
        set_trip_gear_packed(trip_id=trip.pk, entry_id=entry.pk,
                             packed=request.POST.get("packed") == "true", actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(str(item) for item in exc.messages))
    except ObjectDoesNotExist:
        messages.error(request, "That packing entry is no longer available.")
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
