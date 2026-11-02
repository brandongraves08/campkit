from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.households.permissions import current_household
from apps.inventory.forms import GearItemForm, PantryItemForm
from apps.inventory.models import GearItem, PantryItem
from apps.inventory.services import (
    create_gear_item, create_pantry_item, delete_gear_item, delete_pantry_item,
    list_gear, list_pantry, update_gear_item, update_pantry_item, weight_totals,
)


@login_required
def gear_list(request):
    household = current_household(request)
    return render(request, "inventory/gear_list.html", {
        "items": list_gear(actor=request.user, household=household),
        "weights": weight_totals(household=household),
    })


@login_required
@require_http_methods(["GET", "POST"])
def gear_create(request):
    household = current_household(request)
    form = GearItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_gear_item(actor=request.user, household=household, data=form.cleaned_data)
        return redirect("inventory:gear_list")
    return render(request, "inventory/gear_form.html", {"form": form, "title": "Add gear"})


@login_required
@require_http_methods(["GET", "POST"])
def gear_edit(request, item_id):
    household = current_household(request)
    item = get_object_or_404(GearItem.objects.for_household(household), pk=item_id)
    form = GearItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        update_gear_item(actor=request.user, household=household, item_id=item.pk, data=form.cleaned_data)
        return redirect("inventory:gear_list")
    return render(request, "inventory/gear_form.html", {"form": form, "title": "Edit gear"})


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
    items = list_pantry(actor=request.user, household=household)
    return render(request, "inventory/pantry_list.html", {"items": items})


@login_required
@require_http_methods(["GET", "POST"])
def pantry_create(request):
    household = current_household(request)
    form = PantryItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_pantry_item(actor=request.user, household=household, data=form.cleaned_data)
        return redirect("inventory:pantry_list")
    return render(request, "inventory/pantry_form.html", {"form": form, "title": "Add pantry item"})


@login_required
@require_http_methods(["GET", "POST"])
def pantry_edit(request, item_id):
    household = current_household(request)
    item = get_object_or_404(PantryItem.objects.for_household(household), pk=item_id)
    form = PantryItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        update_pantry_item(actor=request.user, household=household, item_id=item.pk, data=form.cleaned_data)
        return redirect("inventory:pantry_list")
    return render(request, "inventory/pantry_form.html", {"form": form, "title": "Edit pantry item"})


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
