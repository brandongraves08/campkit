from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.enrichment.services import nutrition_facts, price_history
from apps.households.permissions import current_household
from apps.intake.services import list_verification_requests
from apps.inventory.forms import GearItemForm, PantryItemForm
from apps.inventory.models import GearItem, PantryItem
from apps.inventory.services import (
    create_gear_item, create_pantry_item, delete_gear_item, delete_pantry_item,
    get_pantry_item, list_gear, list_pantry, update_gear_item, update_pantry_item,
    verification_backlog, weight_totals,
)


@login_required
def gear_list(request):
    household = current_household(request)
    query = request.GET.get("q", "")
    return render(request, "inventory/gear_list.html", {
        "nav": "gear",
        "items": list_gear(actor=request.user, household=household, query=query),
        "weights": weight_totals(actor=request.user, household=household),
        "query": query,
    })


@login_required
@require_http_methods(["GET", "POST"])
def gear_create(request):
    household = current_household(request)
    form = GearItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_gear_item(actor=request.user, household=household, data=form.cleaned_data)
        return redirect("inventory:gear_list")
    return render(request, "inventory/gear_form.html", {"nav": "gear", "form": form, "title": "Add gear"})


@login_required
@require_http_methods(["GET", "POST"])
def gear_edit(request, item_id):
    household = current_household(request)
    item = get_object_or_404(GearItem.objects.for_household(household), pk=item_id)
    form = GearItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        update_gear_item(actor=request.user, household=household, item_id=item.pk, data=form.cleaned_data)
        return redirect("inventory:gear_list")
    return render(request, "inventory/gear_form.html", {"nav": "gear", "form": form, "title": "Edit gear"})


@login_required
@require_http_methods(["POST"])
def gear_delete(request, item_id):
    household = current_household(request)
    delete_gear_item(actor=request.user, household=household, item_id=item_id)
    return redirect("inventory:gear_list")


@login_required
@require_POST
def gear_confirm(request, item_id):
    household = current_household(request)
    update_gear_item(actor=request.user, household=household, item_id=item_id,
                     data={"verification_status": "confirmed"})
    return redirect("inventory:gear_list")


@login_required
def pantry_list(request):
    household = current_household(request)
    query = request.GET.get("q", "")
    return render(request, "inventory/pantry_list.html", {
        "nav": "pantry",
        "items": list_pantry(actor=request.user, household=household, query=query),
        "query": query,
    })


@login_required
def pantry_detail(request, item_id):
    household = current_household(request)
    try:
        item = get_pantry_item(actor=request.user, item_id=item_id, household=household)
    except (PantryItem.DoesNotExist, PermissionDenied):
        raise Http404("Pantry item not found in this household.")
    return render(request, "inventory/pantry_detail.html", {
        "nav": "pantry",
        "item": item,
        "nutrition": nutrition_facts(actor=request.user, household=household, item_id=item.pk),
        "prices": price_history(actor=request.user, household=household, item_id=item.pk),
    })


@login_required
def verification_queue(request):
    """Estimated inventory facts plus open intake questions for one household."""
    household = current_household(request)
    return render(request, "inventory/verification_queue.html", {
        "nav": "verification",
        "backlog": verification_backlog(actor=request.user, household=household),
        "verification_requests": list_verification_requests(actor=request.user, household=household),
    })


@login_required
@require_http_methods(["GET", "POST"])
def pantry_create(request):
    household = current_household(request)
    form = PantryItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_pantry_item(actor=request.user, household=household, data=form.cleaned_data)
        return redirect("inventory:pantry_list")
    return render(request, "inventory/pantry_form.html", {"nav": "pantry", "form": form, "title": "Add pantry item"})


@login_required
@require_http_methods(["GET", "POST"])
def pantry_edit(request, item_id):
    household = current_household(request)
    item = get_object_or_404(PantryItem.objects.for_household(household), pk=item_id)
    form = PantryItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        update_pantry_item(actor=request.user, household=household, item_id=item.pk, data=form.cleaned_data)
        return redirect("inventory:pantry_list")
    return render(request, "inventory/pantry_form.html", {"nav": "pantry", "form": form, "title": "Edit pantry item"})


@login_required
@require_http_methods(["POST"])
def pantry_delete(request, item_id):
    household = current_household(request)
    delete_pantry_item(actor=request.user, household=household, item_id=item_id)
    return redirect("inventory:pantry_list")


@login_required
@require_POST
def pantry_confirm(request, item_id):
    household = current_household(request)
    update_pantry_item(actor=request.user, household=household, item_id=item_id,
                       data={"verification_status": "confirmed"})
    return redirect("inventory:pantry_list")
