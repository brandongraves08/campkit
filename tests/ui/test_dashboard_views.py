import ast
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import InventoryChange
from apps.enrichment.services import record_price
from apps.inventory.models import InventoryVerification
from apps.inventory.services import (
    create_gear_item,
    create_nutrition_fact,
    create_pantry_item,
    update_gear_item,
    update_pantry_item,
)
from apps.trips.services import add_trip_gear, create_trip, set_trip_gear_packed

REPO_ROOT = Path(__file__).resolve().parents[2]


def _confirmed_gear(owner, household, offer_data):
    item = create_gear_item(actor=owner, household=household, data=offer_data)
    return update_gear_item(
        actor=owner,
        household=household,
        item_id=item.pk,
        data={"verification_status": InventoryVerification.CONFIRMED},
    )


# --------------------------------------------------------------------------- #
# Access and empty states
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_every_screen_requires_an_authenticated_household_member(client):
    urls = [
        reverse("dashboard"),
        reverse("recommendations"),
        reverse("inventory:verification_queue"),
        reverse("inventory:gear_list"),
        reverse("inventory:pantry_list"),
        reverse("trips:trip_list"),
        reverse("intake:list"),
    ]
    login_url = reverse(settings.LOGIN_URL)
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302, url
        assert response["Location"].startswith(login_url), url


@pytest.mark.django_db
def test_empty_overview_is_explicit_instead_of_inventing_inventory(client, owner, household):
    client.force_login(owner)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    body = response.content
    assert b"Household overview" in body
    assert household.name.encode() in body
    assert b"No trips planned yet" in body
    assert b"No gear yet" in body
    assert b"No pantry items yet" in body
    assert b"No facts need verification" in body
    assert b"No recommendations right now" in body
    # Nothing is invented just to fill the dashboard.
    assert b"Basecamp tent" not in body
    assert b"Trail mix" not in body


# --------------------------------------------------------------------------- #
# Populated overview: readiness, badges, scope, provenance
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_overview_shows_upcoming_trip_readiness_weights_and_scope(
    client, owner, household, gear_data, trip_data
):
    tent = _confirmed_gear(owner, household, gear_data)
    pad_data = {**gear_data, "name": "Sleeping pad", "category": "Sleep", "weight_grams": 700}
    pad = create_gear_item(actor=owner, household=household, data=pad_data)
    trip = create_trip(actor=owner, household=household, data=trip_data)
    tent_entry = add_trip_gear(trip_id=trip.pk, gear_item_id=tent.pk, quantity=1, actor=owner)
    add_trip_gear(trip_id=trip.pk, gear_item_id=pad.pk, quantity=1, actor=owner)
    set_trip_gear_packed(trip_id=trip.pk, entry_id=tent_entry.pk, packed=True, actor=owner)

    client.force_login(owner)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    body = response.content
    assert b"Ridge weekend" in body
    assert b"Blue Ridge" in body
    assert b"1 of 2 packed" in body
    assert b"2100 g confirmed" in body
    assert b"700 g estimated" in body
    assert b"Estimated" in body
    assert b"Confirmed" in body
    assert household.name.encode() in body
    assert b"Open trip" in body


@pytest.mark.django_db
def test_overview_surfaces_expiring_pantry_and_verification_backlog_without_leaks(
    client, owner, other_owner, household, other_household, gear_data, pantry_data, pending_review
):
    create_gear_item(actor=owner, household=household, data=gear_data)
    expiring = {**pantry_data, "name": "Fresh eggs", "quantity": Decimal("6"), "unit": "each",
                "expiry_date": date.today() + timedelta(days=2)}
    create_pantry_item(actor=owner, household=household, data=expiring)
    create_gear_item(actor=owner, household=household, data=gear_data)  # still estimated
    create_pantry_item(actor=other_owner, household=other_household,
                       data={**pantry_data, "household": other_household, "name": "Other household oats"})

    client.force_login(owner)
    body = client.get(reverse("dashboard")).content
    assert b"Fresh eggs" in body
    assert b"awaiting household confirmation" in body
    assert b"Other household oats" not in body
    assert b"1 open intake question" in body


# --------------------------------------------------------------------------- #
# Inventory screens: estimated/confirmed badges, provenance, search
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_gear_and_pantry_lists_show_badges_provenance_and_cta_semantics(
    client, owner, household, gear_data, pantry_data
):
    _confirmed_gear(owner, household, gear_data)
    create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)

    gear = client.get(reverse("inventory:gear_list"))
    assert gear.status_code == 200
    assert b"Basecamp tent" in gear.content
    assert b"Confirmed" in gear.content
    assert b"Manufacturer label" in gear.content
    assert b"https://example.test/tent" in gear.content

    pantry = client.get(reverse("inventory:pantry_list"))
    assert b"Trail mix" in pantry.content
    assert b"estimated (not guaranteed stock)" in pantry.content
    assert b"Estimated" in pantry.content
    assert b"Confirm quantity" in pantry.content
    assert b'type="submit"' in pantry.content


@pytest.mark.django_db
def test_pantry_confirm_from_ui_writes_confirmation_and_audit(client, owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)
    response = client.post(reverse("inventory:pantry_confirm", args=[item.pk]))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.verification_status == InventoryVerification.CONFIRMED
    assert item.verified_by == owner and item.verified_at is not None
    change = InventoryChange.objects.filter(household=household, object_id=str(item.pk), action="updated").first()
    assert change is not None and change.actor == owner
    body = client.get(reverse("inventory:pantry_list")).content
    assert b"500 g confirmed and guaranteed" in body


@pytest.mark.django_db
def test_search_filters_inventory_within_the_household(client, owner, household, gear_data, pantry_data):
    create_gear_item(actor=owner, household=household, data=gear_data)
    create_gear_item(actor=owner, household=household,
                     data={**gear_data, "name": "Camp stove", "category": "Kitchen"})
    create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)

    gear = client.get(reverse("inventory:gear_list"), {"q": "stove"})
    assert b"Camp stove" in gear.content
    assert b"Basecamp tent" not in gear.content

    pantry = client.get(reverse("inventory:pantry_list"), {"q": "trail"})
    assert b"Trail mix" in pantry.content


@pytest.mark.django_db
def test_pantry_detail_shows_sourced_nutrition_and_dated_price_history(client, owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    create_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
        "nutrient": "Protein", "amount": Decimal("12"), "unit": "g",
        "source": "USDA FoodData Central", "source_url": "https://fdc.nal.usda.gov/food/1",
        "confidence": Decimal("0.900"),
    })
    record_price(item_id=item.pk, amount=Decimal("4.25"), merchant="Camp Store",
                 source="receipt", source_url="https://example.test/receipt",
                 observed_at=timezone.now() - timedelta(days=60), actor=owner,
                 confidence=Decimal("1.000"))
    client.force_login(owner)
    response = client.get(reverse("inventory:pantry_detail", args=[item.pk]))
    assert response.status_code == 200
    body = response.content
    assert b"Nutrition facts" in body
    assert b"Protein" in body
    assert b"USDA FoodData Central" in body
    assert b"https://fdc.nal.usda.gov/food/1" in body
    assert b"Price history" in body
    assert b"Camp Store" in body
    assert b"4.25" in body and b"USD" in body
    assert b"stale" in body
    assert b"Package label" in body


# --------------------------------------------------------------------------- #
# Verification queue and manual correction
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_verification_queue_lists_household_facts_and_open_intake_questions(
    client, owner, other_owner, household, other_household, gear_data, pantry_data, pending_review
):
    create_pantry_item(actor=owner, household=household, data=pantry_data)
    create_gear_item(actor=owner, household=household, data=gear_data)
    create_pantry_item(actor=other_owner, household=other_household,
                       data={**pantry_data, "household": other_household, "name": "Other household sugar"})
    client.force_login(owner)
    response = client.get(reverse("inventory:verification_queue"))
    assert response.status_code == 200
    body = response.content
    assert b"Verification queue" in body
    assert b"Trail mix" in body
    assert b"Basecamp tent" in body
    assert b"Quantity confidence below the household threshold" in body
    assert b"Review and correct" in body
    assert b"Other household sugar" not in body


@pytest.mark.django_db
def test_manual_correction_from_intake_review_creates_confirmed_inventory(
    client, owner, household, pending_review
):
    submission, _request = pending_review
    client.force_login(owner)
    response = client.post(reverse("intake:resolve", args=[submission.pk]), {
        "action": "correct",
        "name": "Instant noodles",
        "quantity": "2",
        "unit": "each",
        "note": "Checked the package label",
    })
    assert response.status_code == 302
    from apps.inventory.models import PantryItem
    item = PantryItem.objects.get(household=household, name="Instant noodles")
    assert item.verification_status == InventoryVerification.CONFIRMED
    assert item.verified_by == owner
    assert "smart intake" in item.source


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_recommendations_are_explained_suggestions_not_inventory(
    client, owner, household, gear_data, pantry_data, trip_data, pending_review
):
    create_gear_item(actor=owner, household=household, data=gear_data)
    create_pantry_item(actor=owner, household=household, data={
        **pantry_data, "name": "Oatmeal", "expiry_date": date.today() + timedelta(days=2)})
    trip = create_trip(actor=owner, household=household, data=trip_data)
    add_trip_gear(trip_id=trip.pk, gear_item_id=create_gear_item(
        actor=owner, household=household, data={**gear_data, "name": "Lantern"}).pk,
        quantity=1, actor=owner)

    before = household.gear_items.count() + household.pantry_items.count()
    client.force_login(owner)
    response = client.get(reverse("recommendations"))
    assert response.status_code == 200
    body = response.content
    assert b"Recommendations" in body
    assert b"Suggestions only" in body
    assert b"inventory until a household member adds it" in body
    assert b"Oatmeal" in body
    assert b"Ridge weekend" in body
    assert b"Confirm 3 estimated facts" in body
    assert b"Source:" in body
    assert household.gear_items.count() + household.pantry_items.count() == before


# --------------------------------------------------------------------------- #
# Trips workflow
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_trip_confirm_and_complete_workflow_uses_existing_status_service(client, owner, household, trip_data):
    trip = create_trip(actor=owner, household=household, data=trip_data)
    client.force_login(owner)
    response = client.post(reverse("trips:trip_status", args=[trip.pk]), {"status": "confirmed"})
    assert response.status_code == 302
    trip.refresh_from_db()
    assert trip.status == "confirmed"
    client.post(reverse("trips:trip_status", args=[trip.pk]), {"status": "completed"})
    trip.refresh_from_db()
    assert trip.status == "completed"

    # An invalid jump is rejected by the domain service, not written anyway.
    planned = create_trip(actor=owner, household=household, data={**trip_data, "name": "Second trip"})
    client.post(reverse("trips:trip_status", args=[planned.pk]), {"status": "completed"})
    planned.refresh_from_db()
    assert planned.status == "planned"


@pytest.mark.django_db
def test_unknown_gear_id_is_a_form_error_not_a_server_error(client, owner, household, trip_data):
    """Regression for the deferred Task 3 finding: unknown gear id must not 500."""
    trip = create_trip(actor=owner, household=household, data=trip_data)
    client.force_login(owner)
    response = client.post(
        reverse("trips:trip_gear_add", args=[trip.pk]),
        {"gear_item_id": "00000000-0000-0000-0000-000000000000", "quantity": "1"},
    )
    assert response.status_code == 302
    page = client.get(reverse("trips:trip_detail", args=[trip.pk]))
    assert b"not available in this household" in page.content
    assert trip.packed_gear.count() == 0


@pytest.mark.django_db
def test_foreign_pantry_detail_is_not_found(client, owner, other_owner, household, other_household, pantry_data):
    other_item = create_pantry_item(
        actor=other_owner, household=other_household,
        data={**pantry_data, "household": other_household, "name": "Other household rice"},
    )
    client.force_login(owner)
    assert client.get(reverse("inventory:pantry_detail", args=[other_item.pk])).status_code == 404


# --------------------------------------------------------------------------- #
# Mobile navigation labels and CTA semantics (markup contract)
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_navigation_labels_are_visible_and_marked_for_current_page(client, owner, household):
    client.force_login(owner)
    body = client.get(reverse("dashboard")).content
    for label in (b"Overview", b"Trips", b"Gear", b"Pantry", b"Smart intake",
                  b"Verification", b"Recommendations"):
        assert label in body, label
    assert b'aria-label="Primary"' in body
    assert b'aria-current="page"' in body
    assert b'class="skip-link"' in body
    assert b'id="main"' in body
    assert b"Private to" in body


@pytest.mark.django_db
def test_primary_ctas_are_real_submit_controls_with_wrapping_css(client, owner, household, pantry_data):
    create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)
    body = client.get(reverse("inventory:pantry_list")).content
    assert b"button button-primary" in body
    assert b'type="submit"' in body
    css = (settings.BASE_DIR / "static" / "css" / "campkit.css").read_text()
    assert "white-space: normal" in css or "white-space:normal" in css
    assert "flex-wrap: wrap" in css or "flex-wrap:wrap" in css
    assert "@media (max-width: 640px)" in css or "@media (max-width:640px)" in css
    assert "overflow-x: auto" in css or "overflow-x:auto" in css


# --------------------------------------------------------------------------- #
# Id-bearing actions collapse a foreign id and a missing id to the same 404
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_id_bearing_ui_actions_collapse_foreign_and_missing_ids_to_404(
    client, owner, other_owner, other_household, pantry_data, trip_data
):
    """Whole-branch regression: unguarded lookups raised DoesNotExist (HTTP 500).

    Four inventory actions and the three meal screens answered a *nonexistent*
    id with a 500 while a *foreign* id returned 404, so the two cases were
    distinguishable and a caller could probe which UUIDs exist. Both must be the
    same 404, and the rejected call must not touch the other household.
    """
    other_gear = create_gear_item(
        actor=other_owner, household=other_household,
        data={"name": "Other household lantern", "weight_grams": 500},
    )
    other_pantry = create_pantry_item(
        actor=other_owner, household=other_household,
        data={**pantry_data, "household": other_household, "name": "Other household rice"},
    )
    other_trip = create_trip(
        actor=other_owner, household=other_household,
        data={**trip_data, "name": "Other household trip"},
    )
    client.force_login(owner)

    absent = uuid.uuid4()
    checks = [
        ("post", "inventory:gear_delete", other_gear.pk),
        ("post", "inventory:gear_confirm", other_gear.pk),
        ("post", "inventory:pantry_delete", other_pantry.pk),
        ("post", "inventory:pantry_confirm", other_pantry.pk),
        ("get", "meals:meal_list", other_trip.pk),
        ("post", "meals:confirm_meal_plan", other_trip.pk),
        ("post", "meals:reconcile_trip_food", other_trip.pk),
    ]
    for method, name, record_id in checks:
        foreign = getattr(client, method)(reverse(name, args=[record_id]))
        missing = getattr(client, method)(reverse(name, args=[absent]))
        assert foreign.status_code == 404, (name, foreign.status_code)
        assert missing.status_code == 404, (name, missing.status_code)
        assert b"Other household" not in foreign.content, name

    assert type(other_gear).objects.filter(pk=other_gear.pk).exists()
    assert type(other_pantry).objects.filter(pk=other_pantry.pk).exists()


# --------------------------------------------------------------------------- #
# Architecture guard: UI views compose domain services, not foreign models
# --------------------------------------------------------------------------- #

def test_ui_view_modules_never_import_another_apps_models():
    offenders = []
    for views_path in (REPO_ROOT / "apps").glob("*/views.py"):
        app_name = views_path.parent.name
        tree = ast.parse(views_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                if len(parts) >= 3 and parts[0] == "apps" and parts[2] == "models" and parts[1] != app_name:
                    offenders.append((str(views_path), node.module, node.lineno))
    assert offenders == []
