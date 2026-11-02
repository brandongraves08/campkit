import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.inventory.models import GearItem, InventoryVerification
from apps.inventory.services import create_gear_item, delete_gear_item, update_gear_item, weight_totals


@pytest.fixture
def gear_data(household):
    return {
        "household": household,
        "name": "Tent",
        "category": "shelter",
        "condition": "good",
        "weight_grams": 1800,
        "quantity": 1,
        "storage_location": "Garage shelf",
        "availability": "available",
    }


@pytest.mark.django_db
def test_gear_create_and_household_scoped_crud(owner, household, gear_data):
    item = create_gear_item(actor=owner, household=household, data=gear_data)
    assert GearItem.objects.for_household(household).get(pk=item.pk) == item
    updated = update_gear_item(actor=owner, household=household, item_id=item.pk, data={"condition": "worn"})
    assert updated.condition == "worn"


@pytest.mark.django_db
def test_gear_service_rejects_cross_household_write(owner, other_household, gear_data):
    with pytest.raises(PermissionDenied):
        create_gear_item(actor=owner, household=other_household, data=gear_data)


@pytest.mark.django_db
def test_gear_rejects_negative_quantity_and_weight(owner, household, gear_data):
    gear_data["quantity"] = -1
    with pytest.raises(ValidationError):
        create_gear_item(actor=owner, household=household, data=gear_data)
    gear_data["quantity"] = 1
    gear_data["weight_grams"] = -1
    with pytest.raises(ValidationError):
        create_gear_item(actor=owner, household=household, data=gear_data)


@pytest.mark.django_db
def test_gear_delete_is_household_scoped_and_audited(owner, household, gear_data):
    item = create_gear_item(actor=owner, household=household, data=gear_data)
    delete_gear_item(actor=owner, household=household, item_id=item.pk)
    assert not GearItem.objects.filter(pk=item.pk).exists()
    from apps.audit.models import InventoryChange
    change = InventoryChange.objects.get(action="deleted")
    assert change.actor == owner
    assert change.before["name"] == "Tent"
    assert change.after is None


@pytest.mark.django_db
def test_weight_totals_include_estimated_and_confirmed_with_distinction(owner, household, gear_data):
    estimated = create_gear_item(actor=owner, household=household, data=gear_data)
    confirmed = create_gear_item(actor=owner, household=household, data={**gear_data, "name": "Stove", "weight_grams": 900,
      "verification_status": InventoryVerification.CONFIRMED})
    assert weight_totals(household=household) == {"estimated_grams": 1800, "confirmed_grams": 900, "total_grams": 2700}
    assert estimated.verification_status == InventoryVerification.ESTIMATED
    assert confirmed.verification_status == InventoryVerification.CONFIRMED


@pytest.mark.django_db
def test_estimate_confirmation_records_actor_and_time(owner, member, household, gear_data):
    item = create_gear_item(actor=owner, household=household, data=gear_data)
    result = update_gear_item(actor=member, household=household, item_id=item.pk,
      data={"verification_status": InventoryVerification.CONFIRMED})
    assert result.verified_by == member
    assert result.verified_at is not None


@pytest.mark.django_db
def test_confirmation_provenance_cannot_be_forged_by_submitted_data(owner, member, household, gear_data):
    gear_data.update({"verification_status": InventoryVerification.CONFIRMED,
                      "verified_by": member, "verified_at": "2000-01-01T00:00:00Z"})
    item = create_gear_item(actor=owner, household=household, data=gear_data)
    assert item.verified_by == owner
    assert item.verified_at.year > 2000


@pytest.mark.django_db
def test_gear_form_and_list_are_authenticated_and_household_scoped(client, owner, other_owner, household, other_household, gear_data):
    own = create_gear_item(actor=owner, household=household, data=gear_data)
    create_gear_item(actor=other_owner, household=other_household, data={**gear_data, "household": other_household, "name": "Other tent"})
    assert client.get(reverse("inventory:gear_list")).status_code == 302
    client.force_login(owner)
    response = client.get(reverse("inventory:gear_list"))
    assert response.status_code == 200
    assert b"Tent" in response.content
    assert b"Other tent" not in response.content
    assert own.name == "Tent"
