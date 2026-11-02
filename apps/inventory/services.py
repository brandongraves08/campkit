from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.households.permissions import require_household_member
from apps.inventory.models import (
    GearItem,
    InventoryVerification,
    NutritionFact,
    PantryItem,
    PriceObservation,
)

# Pantry items expiring inside this window are surfaced for planning.
PANTRY_EXPIRY_WINDOW_DAYS = 7


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


def list_gear(*, actor, household, query=""):
    require_household_member(actor, household.pk)
    items = GearItem.objects.for_household(household)
    query = str(query or "").strip()
    if query:
        items = items.filter(
            Q(name__icontains=query)
            | Q(category__icontains=query)
            | Q(owner__icontains=query)
            | Q(storage_location__icontains=query)
        )
    return items


def weight_totals(*, actor, household):
    require_household_member(actor, household.pk)
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


def list_pantry(*, actor, household, query=""):
    require_household_member(actor, household.pk)
    items = PantryItem.objects.for_household(household)
    query = str(query or "").strip()
    if query:
        items = items.filter(
            Q(name__icontains=query)
            | Q(storage_location__icontains=query)
            | Q(source__icontains=query)
        )
    return items


def expiring_pantry(*, actor, household, days=PANTRY_EXPIRY_WINDOW_DAYS):
    """Items whose stored expiry date is inside the planning window (soonest first).

    Estimated and confirmed rows are both returned; the caller must keep the
    two states visibly distinct rather than collapsing them into one number.
    """
    require_household_member(actor, household.pk)
    cutoff = timezone.localdate() + timedelta(days=max(int(days), 0))
    return (
        PantryItem.objects.for_household(household)
        .filter(expiry_date__isnull=False, expiry_date__lte=cutoff)
        .order_by("expiry_date", "name")
    )


def verification_backlog(*, actor, household):
    """Estimated facts a household member still has to confirm."""
    require_household_member(actor, household.pk)
    gear = list(GearItem.objects.for_household(household)
                .filter(verification_status=InventoryVerification.ESTIMATED).order_by("name"))
    pantry = list(PantryItem.objects.for_household(household)
                  .filter(verification_status=InventoryVerification.ESTIMATED).order_by("name"))
    return {"gear": gear, "pantry": pantry, "total": len(gear) + len(pantry)}


def get_pantry_item(*, actor, item_id, household=None):
    """Return one pantry item the actor may access, never a cross-household row.

    ``household`` optionally pins the lookup: a caller that already resolved a
    household cannot be handed an item from a different one.
    """
    item = PantryItem.objects.select_related("household").filter(pk=item_id).first()
    if item is None:
        raise PantryItem.DoesNotExist(f"Pantry item {item_id} does not exist.")
    require_household_member(actor, item.household_id)
    if household is not None and item.household_id != household.pk:
        raise PermissionDenied("Pantry item belongs to a different household.")
    return item


def get_gear_item(*, actor, household, item_id):
    require_household_member(actor, household.pk)
    return GearItem.objects.for_household(household).get(pk=item_id)


def packed_gear_weight_totals(*, actor, household, entries):
    require_household_member(actor, household.pk)
    totals = {"estimated_grams": 0, "confirmed_grams": 0}
    entries = list(entries)
    gear_ids = {item_id for item_id, _quantity in entries}
    gear = {str(item.pk): item for item in GearItem.objects.for_household(household).filter(pk__in=gear_ids)}
    for item_id, quantity in entries:
        item = gear.get(str(item_id))
        if item is None:
            raise PermissionDenied("Packed gear must belong to the trip household.")
        key = "confirmed_grams" if item.verification_status == InventoryVerification.CONFIRMED else "estimated_grams"
        totals[key] += item.total_weight_grams * quantity
    totals["total_grams"] = totals["estimated_grams"] + totals["confirmed_grams"]
    return totals


def get_pantry_stock(*, actor, household, item_ids=None, for_update=False):
    require_household_member(actor, household.pk)
    items = PantryItem.objects.for_household(household)
    if item_ids is not None:
        items = items.filter(pk__in=item_ids)
    if for_update:
        items = items.select_for_update()
    return {
        str(item.pk): {
            "id": item.pk, "name": item.name, "quantity": item.quantity, "unit": item.unit,
            "verification_status": item.verification_status,
        }
        for item in items.order_by("pk")
    }


def record_pantry_reservation_change(*, actor, household, item_id, action, before, after):
    require_household_member(actor, household.pk)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
    from apps.audit.services import record_change
    return record_change(actor=actor, household=household, object=item, action=action,
                         before=before, after=after)


@transaction.atomic
def move_confirmed_pantry_stock(*, actor, household, item_id, delta, action):
    """Apply an audited reservation/release/reconciliation movement to confirmed stock."""
    require_household_member(actor, household.pk)
    if action not in {"food_reserved", "food_reservation_released", "food_reconciled"}:
        raise ValidationError("Unsupported confirmed pantry stock movement.")
    item = PantryItem.objects.for_household(household).select_for_update().get(pk=item_id)
    if item.verification_status != InventoryVerification.CONFIRMED:
        raise ValidationError("Estimated pantry stock cannot be moved as guaranteed food.")
    before = _snapshot(item)
    item.quantity += delta
    item.full_clean()
    item.save(update_fields=["quantity", "updated_at"])
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=item, action=action,
                  before=before, after=_snapshot(item))
    return item


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


def list_nutrition_facts(*, actor, household, item_id):
    """Return the stored nutrition facts for one household-scoped pantry item."""
    require_household_member(actor, household.pk)
    item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    return NutritionFact.objects.filter(item=item).order_by("nutrient")


@transaction.atomic
def upsert_nutrition_fact(*, actor, household, item_id, data):
    """Create or refresh one sourced nutrition fact for a pantry item.

    ``NutritionFact`` is unique per ``(item, nutrient)``, so a re-enrichment
    updates the existing row deliberately instead of colliding on insert. A
    human-confirmed fact is never overwritten by sourced data: the confirmed
    value wins and ``(fact, False)`` tells the caller nothing changed.
    """
    require_household_member(actor, household.pk)
    item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    values = _safe_values(data)
    nutrient = str(values.get("nutrient") or "").strip()
    if not nutrient:
        raise ValidationError({"nutrient": "A nutrient name is required."})
    values["nutrient"] = nutrient
    existing = NutritionFact.objects.filter(item=item, nutrient=nutrient).first()
    if existing is None:
        return create_nutrition_fact(actor=actor, household=household, item_id=item_id, data=values), True
    if existing.verification_status == InventoryVerification.CONFIRMED:
        return existing, False
    before = _snapshot(existing)
    for key, value in values.items():
        setattr(existing, key, value)
    existing.full_clean()
    existing.save()
    from apps.audit.services import record_change
    record_change(actor=actor, household=household, object=existing, action="nutrition_refreshed",
                  before=before, after=_snapshot(existing))
    return existing, True


@transaction.atomic
def record_price(*, actor, household, item_id, amount, currency="USD", merchant="", source="", source_url="", observed_at=None, confidence=None, verification_status=InventoryVerification.ESTIMATED):
    require_household_member(actor, household.pk)
    item = PantryItem.objects.for_household(household).get(pk=item_id)
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


def list_price_observations(*, actor, household, item_id):
    """Dated price observations for one household-scoped item, newest first."""
    require_household_member(actor, household.pk)
    item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    return PriceObservation.objects.filter(item=item).order_by("-observed_at")
