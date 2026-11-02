from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.households.permissions import require_household_member
from apps.inventory.models import GearItem, InventoryVerification, NutritionFact, PantryItem


def _validate_scope(actor, household, data):
    actual = require_household_member(actor, household.pk)
    supplied_household = data.get("household")
    supplied_household_id = getattr(supplied_household, "pk", supplied_household)
    if actual.pk != household.pk or (supplied_household_id and supplied_household_id != household.pk):
        raise PermissionDenied("Inventory writes must stay within the actor's household.")


def _safe_values(data):
    values = dict(data)
    for field in ("household", "id", "pk", "created_at", "updated_at", "verified_by", "verified_at"):
        values.pop(field, None)
    return values


def _snapshot(obj):
    if obj is None:
        return None
    result = {}
    for field in obj._meta.concrete_fields:
        value = getattr(obj, field.attname)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        result[field.name] = value
    return result


def _confirm(data, actor):
    if data.get("verification_status") == InventoryVerification.CONFIRMED:
        data["verified_by"] = actor
        data["verified_at"] = timezone.now()
    else:
        data["verified_by"] = None
        data["verified_at"] = None


def _preserve_verification(item, values, actor, material_fields):
    status = values.get("verification_status")
    if status == InventoryVerification.CONFIRMED:
        values["verified_by"] = actor
        values["verified_at"] = timezone.now()
        return
    if status == InventoryVerification.ESTIMATED:
        values["verified_by"] = None
        values["verified_at"] = None
        return
    changed_material = any(
        key in values and getattr(item, key) != item._meta.get_field(key).to_python(values[key])
        for key in material_fields
    )
    if item.verification_status == InventoryVerification.CONFIRMED and changed_material:
        values["verification_status"] = InventoryVerification.ESTIMATED
        values["verified_by"] = None
        values["verified_at"] = None


@transaction.atomic
def create_gear_item(*, actor, household, data):
    _validate_scope(actor, household, data)
    values = _safe_values(data)
    _confirm(values, actor)
    item = GearItem(household=household, **values)
    item.full_clean()
    item.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="created", before=None, after=_snapshot(item))
    return item


@transaction.atomic
def update_gear_item(*, actor, household, item_id, data):
    _validate_scope(actor, household, data)
    item = GearItem.objects.for_household(household).get(pk=item_id)
    before = _snapshot(item)
    values = _safe_values(data)
    _preserve_verification(item, values, actor, {"weight_grams", "quantity", "name", "category"})
    for key, value in values.items():
        setattr(item, key, value)
    item.full_clean()
    item.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="updated", before=before, after=_snapshot(item))
    return item


@transaction.atomic
def delete_gear_item(*, actor, household, item_id):
    require_household_member(actor, household.pk)
    item = GearItem.objects.for_household(household).get(pk=item_id)
    before = _snapshot(item)
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="deleted", before=before, after=None)
    item.delete()


def list_gear(*, actor, household):
    require_household_member(actor, household.pk)
    return GearItem.objects.for_household(household)


def weight_totals(*, household):
    items = GearItem.objects.for_household(household)
    totals = {"estimated_grams": 0, "confirmed_grams": 0}
    for item in items:
        key = "confirmed_grams" if item.verification_status == "confirmed" else "estimated_grams"
        totals[key] += item.total_weight_grams
    totals["total_grams"] = totals["estimated_grams"] + totals["confirmed_grams"]
    return totals


@transaction.atomic
def create_pantry_item(*, actor, household, data):
    _validate_scope(actor, household, data)
    values = _safe_values(data)
    _confirm(values, actor)
    item = PantryItem(household=household, **values)
    item.full_clean()
    item.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="created", before=None, after=_snapshot(item))
    return item


@transaction.atomic
def update_pantry_item(*, actor, household, item_id, data):
    _validate_scope(actor, household, data)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
    before = _snapshot(item)
    values = _safe_values(data)
    _preserve_verification(item, values, actor, {"quantity", "unit", "name"})
    for key, value in values.items():
        setattr(item, key, value)
    item.full_clean()
    item.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="updated", before=before, after=_snapshot(item))
    return item


@transaction.atomic
def delete_pantry_item(*, actor, household, item_id):
    require_household_member(actor, household.pk)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
    before = _snapshot(item)
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action="deleted", before=before, after=None)
    item.delete()


def list_pantry(*, actor, household):
    require_household_member(actor, household.pk)
    return PantryItem.objects.for_household(household)


@transaction.atomic
def create_nutrition_fact(*, actor, household, item_id, data):
    require_household_member(actor, household.pk)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
    values = _safe_values(data)
    if values.get("verification_status") == "confirmed":
        values["verified_by"] = actor
        values["verified_at"] = timezone.now()
    fact = NutritionFact(item=item, **values)
    fact.full_clean()
    fact.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=fact, action="nutrition_recorded", before=None,
      after={"nutrient": fact.nutrient, "amount": str(fact.amount), "unit": fact.unit, "source": fact.source,
             "source_url": fact.source_url, "confidence": str(fact.confidence) if fact.confidence is not None else None,
             "verification_status": fact.verification_status, "observed_at": fact.observed_at.isoformat()})
    return fact


@transaction.atomic
def record_price(*, actor, household, item_id, amount, currency="USD", merchant="", source="", source_url="", observed_at=None, confidence=None, verification_status=InventoryVerification.ESTIMATED):
    require_household_member(actor, household.pk)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
    from apps.inventory.models import PriceObservation
    observed_at = observed_at or timezone.now()
    observation = PriceObservation(item=item, amount=amount, currency=currency.upper(), merchant=merchant,
      source=source or merchant, source_url=source_url, observed_at=observed_at, confidence=confidence,
      verification_status=verification_status, verified_by=actor if verification_status == InventoryVerification.CONFIRMED else None,
      verified_at=timezone.now() if verification_status == InventoryVerification.CONFIRMED else None, actor=actor)
    observation.full_clean()
    observation.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=observation, action="price_observed", before=None,
      after={"amount": str(observation.amount), "currency": observation.currency, "merchant": merchant, "source_url": source_url,
             "observed_at": observation.observed_at.isoformat()})
    return observation
