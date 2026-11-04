from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.inventory.models import InventoryVerification, PantryItem, PriceObservation
from apps.inventory.services import (
    create_nutrition_fact,
    create_pantry_item,
    delete_pantry_item,
    record_price,
    update_pantry_item,
)


@pytest.fixture
def pantry_data(household):
    return {
        "household": household,
        "name": "Trail mix",
        "quantity": Decimal("500"),
        "unit": "g",
        "expiry_date": date.today() + timedelta(days=30),
        "storage_location": "Pantry bin",
        "source": "Package label",
        "source_url": "https://example.test/label",
    }


@pytest.mark.django_db
def test_pantry_crud_quantity_unit_expiry_and_provenance(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    assert PantryItem.objects.for_household(household).get(pk=item.pk) == item
    assert item.expiry_date == pantry_data["expiry_date"]
    assert item.source == "Package label"
    assert update_pantry_item(actor=owner, household=household, item_id=item.pk,
                              data={"quantity": Decimal("250")}).quantity == Decimal("250")


@pytest.mark.django_db
def test_pantry_rejects_negative_quantity_and_unknown_unit(owner, household, pantry_data):
    pantry_data["quantity"] = Decimal("-1")
    with pytest.raises(ValidationError):
        create_pantry_item(actor=owner, household=household, data=pantry_data)
    pantry_data["quantity"] = Decimal("100")
    pantry_data["unit"] = "unknown-unit"
    with pytest.raises(ValidationError):
        create_pantry_item(actor=owner, household=household, data=pantry_data)


@pytest.mark.django_db
def test_pantry_service_rejects_cross_household_write(owner, other_household, pantry_data):
    with pytest.raises(PermissionDenied):
        create_pantry_item(actor=owner, household=other_household, data=pantry_data)


@pytest.mark.django_db
def test_pantry_delete_records_actor_and_snapshot(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    delete_pantry_item(actor=owner, household=household, item_id=item.pk)
    assert not PantryItem.objects.filter(pk=item.pk).exists()
    from apps.audit.models import InventoryChange
    change = InventoryChange.objects.get(action="deleted")
    assert change.actor == owner
    assert change.before["name"] == "Trail mix"
    assert change.after is None


@pytest.mark.django_db
def test_estimated_pantry_stock_is_separate_from_confirmed_stock(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    assert item.confirmed_quantity == 0
    assert item.estimated_quantity == Decimal("500")
    confirmed = update_pantry_item(actor=owner, household=household, item_id=item.pk,
                                   data={"verification_status": InventoryVerification.CONFIRMED})
    assert confirmed.confirmed_quantity == Decimal("500")
    assert confirmed.estimated_quantity == 0
    assert confirmed.verified_by == owner and confirmed.verified_at is not None


@pytest.mark.django_db
def test_editing_confirmed_quantity_invalidates_quantity_confirmation(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    item = update_pantry_item(actor=owner, household=household, item_id=item.pk,
                              data={"verification_status": InventoryVerification.CONFIRMED})
    item = update_pantry_item(actor=owner, household=household, item_id=item.pk,
                              data={"quantity": Decimal("250")})
    assert item.verification_status == InventoryVerification.ESTIMATED
    assert item.verified_by is None and item.verified_at is None
    assert item.estimated_quantity == Decimal("250")
    assert item.confirmed_quantity == 0


@pytest.mark.django_db
def test_pantry_list_visibly_separates_estimates_and_confirms_with_actor(client, owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    client.force_login(owner)
    response = client.get(reverse("inventory:pantry_list"))
    assert response.status_code == 200
    assert b"estimated (not guaranteed stock)" in response.content
    response = client.post(reverse("inventory:pantry_confirm", args=[item.pk]))
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.verification_status == InventoryVerification.CONFIRMED
    assert item.verified_by == owner and item.verified_at is not None


@pytest.mark.django_db
def test_price_observations_are_timestamped_snapshots(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    observation = record_price(actor=owner, household=household, item_id=item.pk, amount=Decimal("5.99"), currency="USD",
                               merchant="Camp Store", source="Receipt", source_url="https://example.test/price", confidence=Decimal("1.0"))
    assert isinstance(observation, PriceObservation)
    assert observation.amount == Decimal("5.99")
    assert observation.observed_at is not None
    assert observation.source_url == "https://example.test/price"
    assert observation.source == "Receipt"
    assert observation.confidence == Decimal("1.0")
    assert observation.verification_status == InventoryVerification.ESTIMATED


@pytest.mark.django_db
def test_nutrition_fact_retains_source_and_confidence(owner, household, pantry_data):
    item = create_pantry_item(actor=owner, household=household, data=pantry_data)
    fact = create_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
        "nutrient": "protein_g", "amount": Decimal("8.2"), "unit": "g", "source": "Label",
        "source_url": "https://example.test/nutrition", "confidence": Decimal("0.92")
    })
    fact.full_clean()
    assert fact.observed_at is not None
    assert fact.verification_status == InventoryVerification.ESTIMATED
    assert fact.source == "Label"
    assert fact.source_url == "https://example.test/nutrition"
    assert fact.confidence == Decimal("0.92")
    assert fact.item == item
    from apps.audit.models import InventoryChange
    audit = InventoryChange.objects.get(action="nutrition_recorded")
    assert audit.actor == owner and audit.household == household
    assert audit.after["source_url"] == "https://example.test/nutrition"
