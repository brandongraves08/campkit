import pytest
from django.utils import timezone

from django.core.exceptions import PermissionDenied

from apps.audit.models import InventoryChange
from apps.audit.services import record_change
from apps.inventory.models import GearItem, InventoryVerification
from apps.inventory.services import create_gear_item, update_gear_item


@pytest.mark.django_db
def test_record_change_attributes_actor_household_and_snapshots(owner, household):
    before = {"name": "Old name", "quantity": 1}
    after = {"name": "New name", "quantity": 2}
    change = record_change(actor=owner, household=household, object=None, action="created", before=before, after=after)
    assert isinstance(change, InventoryChange)
    assert change.actor == owner and change.household == household
    assert change.before == before and change.after == after
    assert change.created_at <= timezone.now()


@pytest.mark.django_db
def test_inventory_mutations_create_audit_entries_and_confirmation_transition(owner, member, household):
    item = create_gear_item(actor=owner, household=household, data={"name": "Stove", "weight_grams": 900})
    created = InventoryChange.objects.get(object_id=str(item.pk), action="created")
    assert created.actor == owner
    assert created.after["verification_status"] == InventoryVerification.ESTIMATED
    update_gear_item(actor=member, household=household, item_id=item.pk,
      data={"verification_status": InventoryVerification.CONFIRMED})
    confirmed = InventoryChange.objects.get(object_id=str(item.pk), action="updated")
    assert confirmed.actor == member
    assert confirmed.before["verification_status"] == InventoryVerification.ESTIMATED
    assert confirmed.after["verified_by"] == member.pk
    assert confirmed.after["verified_at"]


@pytest.mark.django_db
def test_audit_service_prohibits_cross_household_object(owner, household, other_owner, other_household):
    item = GearItem.objects.create(household=other_household, name="Other tent")
    with pytest.raises(PermissionDenied):
        record_change(actor=owner, household=household, object=item, action="updated", before={}, after={})
