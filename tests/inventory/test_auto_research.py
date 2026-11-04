from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.enrichment.models import ProductObservation
from apps.integrations.types import NutrientValue, NutritionCandidate, SourceObservation
from apps.inventory.models import GearItem, NutritionFact, PantryItem
from django.core.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from apps.inventory.services import (
    create_gear_item, create_pantry_item, update_gear_item, update_pantry_item,
    correct_nutrition_fact, create_nutrition_fact, upsert_nutrition_fact,
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind,url_name,payload,model",
    [
        ("gear", "inventory:gear_create", {
            "name": "Trail stove", "category": "Cookware", "condition": "good",
            "weight_grams": "900", "quantity": "1", "storage_location": "Closet",
            "availability": "available", "source": "", "source_url": "",
        }, GearItem),
        ("pantry", "inventory:pantry_create", {
            "name": "Trail mix", "quantity": "500", "unit": "g",
            "expiry_date": "", "storage_location": "Pantry", "source": "", "source_url": "",
        }, PantryItem),
    ],
)
def test_add_automatically_researches_saved_item(client, owner, household, kind, url_name, payload, model):
    client.force_login(owner)
    with patch("apps.inventory.services._research_new_item") as research:
        response = client.post(reverse(url_name), payload)

    assert response.status_code == 302
    item = model.objects.get(household=household)
    research.assert_called_once_with(actor=owner, household=household, item=item)


@pytest.mark.django_db
def test_provider_details_persist_with_provenance_for_pantry(owner, household, client):
    client.force_login(owner)
    fetched = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)

    class ResearchProvider:
        name = "mock-research"

        def lookup(self, identity):
            assert identity.name == "Trail mix"
            return [SourceObservation(
                source=self.name, title="Trail mix product", summary="Source-reported ingredients",
                source_url="https://example.test/product", retrieved_at=fetched, confidence=Decimal("0.91"),
            )]

    class NutritionProvider:
        name = "mock-nutrition"

        def lookup(self, query):
            assert query == "Trail mix"
            return [NutritionCandidate(
                provider=self.name, external_id="n-1", name="Trail mix",
                source_url="https://example.test/nutrition", retrieved_at=fetched,
                confidence=Decimal("0.92"), brand="Example Foods", barcode="0123456789012",
                nutrients=(NutrientValue("protein_g", Decimal("8.2"), "g"),),
            )]

    with patch("apps.enrichment.services.get_product_researcher", return_value=ResearchProvider()), \
         patch("apps.enrichment.services.get_nutrition_provider", return_value=NutritionProvider()):
        response = client.post(reverse("inventory:pantry_create"), {
            "name": "Trail mix", "quantity": "500", "unit": "g", "expiry_date": "",
            "storage_location": "Pantry", "source": "", "source_url": "",
        })
        assert response.status_code == 302
        item = PantryItem.objects.get(household=household)
    item.refresh_from_db()
    assert item.research_status == "complete"

    observation = ProductObservation.objects.get(item=item, provider="mock-research")
    assert observation.provider == "mock-research"
    assert observation.summary == "Source-reported ingredients"
    assert observation.source_url == "https://example.test/product"
    assert observation.retrieved_at == fetched
    assert observation.confidence == Decimal("0.910")
    nutrition_observation = ProductObservation.objects.get(item=item, provider="mock-nutrition")
    assert nutrition_observation.brand == "Example Foods"
    assert nutrition_observation.barcode == "0123456789012"
    assert nutrition_observation.source_url == "https://example.test/nutrition"
    assert nutrition_observation.retrieved_at == fetched
    assert nutrition_observation.confidence == Decimal("0.920")
    page = client.get(reverse("inventory:pantry_list"))
    assert b"Example Foods" in page.content
    assert b"0123456789012" in page.content
    fact = NutritionFact.objects.get(item=item)
    assert fact.source == "mock-nutrition:n-1"
    assert fact.source_url == "https://example.test/nutrition"
    assert fact.observed_at == fetched
    assert fact.confidence == Decimal("0.920")
    assert fact.verification_status == "estimated"


@pytest.mark.django_db
def test_manual_nutrition_correction_survives_later_research_with_audit(owner, household):
    from apps.enrichment.services import enrich_nutrition
    from apps.audit.models import InventoryChange

    item = PantryItem.objects.create(household=household, name="Trail mix", quantity=1, unit="each")
    provider = type("Provider", (), {"name": "catalog", "lookup": lambda self, query: [NutritionCandidate(
        provider="catalog", external_id="new", name="Trail mix", source_url="https://example.test/new",
        retrieved_at=datetime(2026, 11, 3, tzinfo=timezone.utc), confidence=Decimal("0.95"),
        nutrients=(NutrientValue("protein_g", Decimal("99"), "g"),),
    )]})()
    fact = NutritionFact.objects.create(
        item=item, nutrient="protein_g", amount=Decimal("8"), unit="g",
        source="old-provider:1", source_url="https://example.test/old", confidence=Decimal("0.8"),
    )

    corrected = correct_nutrition_fact(
        actor=owner, household=household, item_id=item.pk, fact_id=fact.pk,
        data={"amount": Decimal("12.5"), "unit": "g"},
    )
    enrich_nutrition(actor=owner, household=household, item_id=item.pk, provider=provider, refresh=True)

    corrected.refresh_from_db()
    assert corrected.amount == Decimal("12.500")
    assert corrected.source == "manual"
    assert corrected.source_url == ""
    assert corrected.verification_status == "estimated"
    event = InventoryChange.objects.filter(object_id=str(fact.pk), action="nutrition_corrected").latest("created_at")
    assert event.actor_id == owner.pk
    assert event.after["amount"] == "12.5"
    assert event.after["source"] == "manual"


@pytest.mark.django_db
@pytest.mark.parametrize("nutrient,unit", list(NutritionFact.CANONICAL_UNITS.items()))
def test_nutrition_service_accepts_every_canonical_nutrient_unit(owner, household, nutrient, unit):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")

    fact = create_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
        "nutrient": nutrient, "amount": Decimal("2"), "unit": unit,
    })

    assert fact.nutrient == nutrient
    assert fact.unit == unit


@pytest.mark.django_db
def test_nutrition_service_rejects_unsupported_nutrient(owner, household):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")

    with pytest.raises(ValidationError):
        create_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
            "nutrient": "vitamin_z", "amount": Decimal("2"), "unit": "mg",
        })

    assert not NutritionFact.objects.filter(item=item).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("nutrient,unit", [("protein_g", "widgets"), ("protein_g", "kcal"),
                                           ("vitamin_z", "mg")])
def test_nutrition_provider_upsert_rejects_invalid_new_fact_without_persisting(
    owner, household, nutrient, unit
):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")

    with pytest.raises(ValidationError):
        upsert_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
            "nutrient": nutrient, "amount": Decimal("2"), "unit": unit, "source": "provider",
        })

    assert not NutritionFact.objects.filter(item=item).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("unit", ["widgets", "kcal"])
def test_nutrition_provider_upsert_rejects_invalid_update_without_changing_existing_fact(
    owner, household, unit
):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")
    fact = NutritionFact.objects.create(item=item, nutrient="protein_g", amount=1, unit="g", source="old")

    with pytest.raises(ValidationError):
        upsert_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
            "nutrient": "protein_g", "amount": Decimal("9"), "unit": unit, "source": "new",
        })

    fact.refresh_from_db()
    assert fact.amount == Decimal("1")
    assert fact.unit == "g"
    assert fact.source == "old"


@pytest.mark.django_db
@pytest.mark.parametrize("unit", ["widgets", "kcal"])
def test_create_nutrition_fact_rejects_noncanonical_units(owner, household, unit):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")

    with pytest.raises(ValidationError):
        create_nutrition_fact(actor=owner, household=household, item_id=item.pk, data={
            "nutrient": "protein_g", "amount": Decimal("2"), "unit": unit,
        })

    assert not NutritionFact.objects.filter(item=item).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("unit", ["widgets", "kcal", " ", "milligrams"])
def test_manual_nutrition_correction_rejects_noncanonical_or_mismatched_units(owner, household, unit):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")
    fact = NutritionFact.objects.create(item=item, nutrient="protein_g", amount=1, unit="g")

    with pytest.raises(ValidationError):
        correct_nutrition_fact(actor=owner, household=household, item_id=item.pk, fact_id=fact.pk,
                               data={"amount": Decimal("2"), "unit": unit})

    fact.refresh_from_db()
    assert fact.amount == Decimal("1")
    assert fact.unit == "g"
    assert fact.source != "manual"


@pytest.mark.django_db
@pytest.mark.parametrize("nutrient,unit", [("protein_g", "g"), ("sodium_mg", "mg"), ("energy_kcal", "kcal")])
def test_manual_nutrition_correction_accepts_canonical_units(owner, household, nutrient, unit):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")
    fact = NutritionFact.objects.create(item=item, nutrient=nutrient, amount=1, unit=unit)

    corrected = correct_nutrition_fact(actor=owner, household=household, item_id=item.pk, fact_id=fact.pk,
                                       data={"amount": Decimal("2"), "unit": unit})

    assert corrected.amount == Decimal("2")
    assert corrected.unit == unit
    assert corrected.source == "manual"


@pytest.mark.django_db
def test_manual_nutrition_correction_is_household_scoped(owner, household, other_household):
    item = PantryItem.objects.create(household=household, name="Beans", quantity=1, unit="each")
    fact = NutritionFact.objects.create(item=item, nutrient="protein_g", amount=1, unit="g")
    with pytest.raises(PermissionDenied):
        correct_nutrition_fact(actor=owner, household=other_household, item_id=item.pk, fact_id=fact.pk,
                               data={"amount": Decimal("2"), "unit": "g"})
    fact.refresh_from_db()
    assert fact.amount == Decimal("1")


@pytest.mark.django_db
def test_provider_estimates_refresh_but_confirmed_nutrition_does_not(owner, household):
    from apps.inventory.models import InventoryVerification
    from apps.inventory.services import upsert_nutrition_fact

    item = PantryItem.objects.create(household=household, name="Oats", quantity=1, unit="each")
    estimated = NutritionFact.objects.create(item=item, nutrient="protein_g", amount=1, unit="g",
                                              source="provider:old")
    refreshed, changed = upsert_nutrition_fact(actor=owner, household=household, item_id=item.pk,
        data={"nutrient": "protein_g", "amount": Decimal("2"), "unit": "g", "source": "provider:new",
              "verification_status": "estimated"})
    assert changed and refreshed.amount == Decimal("2") and refreshed.source == "provider:new"

    confirmed = NutritionFact.objects.create(item=item, nutrient="fiber_g", amount=3, unit="g",
        verification_status=InventoryVerification.CONFIRMED, verified_by=owner, verified_at=datetime.now(timezone.utc))
    same, changed = upsert_nutrition_fact(actor=owner, household=household, item_id=item.pk,
        data={"nutrient": "fiber_g", "amount": Decimal("99"), "unit": "g", "source": "provider:new",
              "verification_status": "estimated"})
    assert not changed and same.pk == confirmed.pk and same.amount == Decimal("3")


@pytest.mark.django_db
def test_research_empty_or_failed_does_not_undo_inventory_save(client, owner, household):
    client.force_login(owner)
    with patch("apps.enrichment.services.research_inventory_item", side_effect=RuntimeError("provider down")):
        response = client.post(reverse("inventory:gear_create"), {
            "name": "Trail stove", "category": "Cookware", "condition": "good",
            "weight_grams": "900", "quantity": "1", "storage_location": "Closet",
            "availability": "available", "source": "", "source_url": "",
        })
    assert response.status_code == 302
    item = GearItem.objects.get(household=household, name="Trail stove")
    assert item.research_status == "failed"


@pytest.mark.django_db
def test_manual_edits_are_not_overwritten_by_research(client, owner, household):
    item = PantryItem.objects.create(household=household, name="Hand entered", quantity=Decimal("2"), unit="each")
    client.force_login(owner)
    with patch("apps.inventory.services._research_new_item") as research:
        response = client.post(reverse("inventory:pantry_edit", args=[item.pk]), {
            "name": "Corrected name", "quantity": "3", "unit": "each", "expiry_date": "",
            "storage_location": "Shelf", "source": "", "source_url": "",
        })
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.name == "Corrected name"
    assert item.quantity == Decimal("3")
    research.assert_not_called()


@pytest.mark.django_db
def test_observation_can_belong_to_gear_without_cross_household_leak(owner, household, other_household):
    item = GearItem.objects.create(household=household, name="Stove")
    observation = ProductObservation.objects.create(
        gear_item=item, provider="catalog", title="Stove specifications",
        summary="Source-provided description", source_url="https://example.test/stove",
        retrieved_at="2026-11-03T12:00:00+00:00", confidence=Decimal("0.9"),
    )
    assert observation.gear_item.household_id == household.pk
    assert observation.item_id is None
    other = GearItem.objects.create(household=other_household, name="Other stove")
    assert not ProductObservation.objects.filter(gear_item=other).exists()


@pytest.mark.django_db
def test_gear_add_persists_cited_research_without_exposing_household_data(client, owner, household):
    from datetime import datetime, timezone
    from apps.integrations.types import SourceObservation

    class Provider:
        name = "mock-gear"

        def lookup(self, identity):
            assert identity.name == "Trail stove"
            assert identity.category == "Cookware"
            assert not hasattr(identity, "household")
            return [SourceObservation(
                source=self.name, title="Stove specs", summary="Manufacturer-listed weight is 900g",
                source_url="https://example.test/stove", retrieved_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
                confidence=Decimal("0.88"),
            )]

    client.force_login(owner)
    with patch("apps.enrichment.services.get_product_researcher", return_value=Provider()):
        response = client.post(reverse("inventory:gear_create"), {
            "name": "Trail stove", "category": "Cookware", "condition": "good",
            "weight_grams": "", "quantity": "1", "storage_location": "Closet",
            "availability": "available", "source": "", "source_url": "",
        })
    assert response.status_code == 302
    item = GearItem.objects.get(household=household)
    item.refresh_from_db()
    assert item.weight_grams is None  # free-text source claims are not parsed/guessed into fields
    assert item.research_status == "complete"
    observation = ProductObservation.objects.get(gear_item=item)
    assert observation.summary == "Manufacturer-listed weight is 900g"
    assert observation.confidence == Decimal("0.880")
    page = client.get(reverse("inventory:gear_list"))
    assert b"Stove specs" in page.content
    assert b"https://example.test/stove" in page.content


@pytest.mark.django_db
def test_research_status_vocabulary_rejects_unknown_values_on_create_and_update(owner, household):
    data = {"name": "Stove", "category": "Cookware", "quantity": 1, "research_status": "invented"}
    with pytest.raises(ValidationError):
        create_gear_item(actor=owner, household=household, data=data)
    with patch("apps.inventory.services._research_new_item"):
        item = create_gear_item(actor=owner, household=household, data={"name": "Stove", "category": "Cookware"})
    with pytest.raises(ValidationError):
        update_gear_item(actor=owner, household=household, item_id=item.pk, data={"research_status": "invented"})
    item.refresh_from_db()
    assert item.research_status == "not_started"


@pytest.mark.django_db
def test_pantry_research_status_vocabulary_rejects_unknown_values_on_create_and_update(owner, household):
    data = {"name": "Beans", "quantity": Decimal("1"), "unit": "each", "research_status": "other"}
    with pytest.raises(ValidationError):
        create_pantry_item(actor=owner, household=household, data=data)
    with patch("apps.inventory.services._research_new_item"):
        item = create_pantry_item(actor=owner, household=household,
                                  data={"name": "Beans", "quantity": Decimal("1"), "unit": "each"})
    with pytest.raises(ValidationError):
        update_pantry_item(actor=owner, household=household, item_id=item.pk,
                           data={"research_status": "other"})
    item.refresh_from_db()
    assert item.research_status == "not_started"


@pytest.mark.django_db
def test_retry_research_is_post_only_and_household_scoped(client, owner, household, other_household):
    item = GearItem.objects.create(household=household, name="Own stove")
    foreign = GearItem.objects.create(household=other_household, name="Foreign stove")
    client.force_login(owner)
    assert client.get(reverse("inventory:gear_research_retry", args=[item.pk])).status_code == 405
    with patch("apps.inventory.views._research_new_item") as research:
        response = client.post(reverse("inventory:gear_research_retry", args=[item.pk]))
    assert response.status_code == 302
    research.assert_called_once_with(actor=owner, household=household, item=item)
    with patch("apps.inventory.views._research_new_item") as research:
        response = client.post(reverse("inventory:gear_research_retry", args=[foreign.pk]))
    assert response.status_code == 404
    research.assert_not_called()


@pytest.mark.django_db
def test_empty_provider_result_is_visible_and_does_not_invent_item_details(client, owner, household):
    class EmptyProvider:
        name = "empty"

        def lookup(self, identity):
            return []

    client.force_login(owner)
    with patch("apps.enrichment.services.get_product_researcher", return_value=EmptyProvider()):
        response = client.post(reverse("inventory:gear_create"), {
            "name": "Unknown stove", "category": "Cookware", "condition": "",
            "weight_grams": "", "quantity": "1", "storage_location": "",
            "availability": "available", "source": "", "source_url": "",
        })
    item = GearItem.objects.get(household=household)
    item.refresh_from_db()
    assert response.status_code == 302
    assert item.research_status == "no_results"
    assert item.weight_grams is None
    assert not ProductObservation.objects.filter(gear_item=item).exists()
    page = client.get(reverse("inventory:gear_list"))
    assert b"No results" in page.content
