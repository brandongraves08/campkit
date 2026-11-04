"""Sourced nutrition enrichment, cache freshness, citations, and provider parity."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.enrichment.models import EnrichmentCache, ProductObservation
from apps.enrichment.research import HermesResearchProvider
from apps.enrichment.services import (
    enrich_nutrition,
    lookup_nutrition,
    nutrition_facts,
    research_product,
)
from apps.integrations.types import (
    NutrientValue,
    ProductIdentity,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    SourceObservation,
    normalize_nutrient,
)
from apps.inventory.models import InventoryVerification, NutritionFact
from apps.inventory.services import create_nutrition_fact


def test_normalization_cases():
    cases = [
        ("Protein", "3.5", "g", ("protein_g", Decimal("3.500"), "g")),
        ("PROTEIN", "3500", "mg", ("protein_g", Decimal("3.500"), "g")),
        ("Sodium, Na", "0.14", "g", ("sodium_mg", Decimal("140.000"), "mg")),
        ("Sodium", "140", "mg", ("sodium_mg", Decimal("140.000"), "mg")),
        ("Energy", "418.4", "kJ", ("energy_kcal", Decimal("100.000"), "kcal")),
        ("Energy", "100", "kcal", ("energy_kcal", Decimal("100.000"), "kcal")),
        ("Total lipid (fat)", "12", "g", ("fat_g", Decimal("12.000"), "g")),
        ("Carbohydrate, by difference", "20", "g", ("carbohydrate_g", Decimal("20.000"), "g")),
        ("Fiber, total dietary", "4", "g", ("fiber_g", Decimal("4.000"), "g")),
        ("Sugars, total including NLEA", "5", "g", ("sugar_g", Decimal("5.000"), "g")),
        ("Fatty acids, total saturated", "2", "g", ("saturated_fat_g", Decimal("2.000"), "g")),
    ]
    for name, amount, unit, expected in cases:
        value = normalize_nutrient(name, amount, unit)
        assert value is not None, (name, unit)
        assert (value.nutrient, value.amount, value.unit) == expected

    skipped = [
        ("Ash", "1", "g"),
        ("Protein", "1", "IU"),
        ("Protein", "abc", "g"),
        ("Protein", "-1", "g"),
        ("Protein", "NaN", "g"),
        ("", "1", "g"),
    ]
    for name, amount, unit in skipped:
        assert normalize_nutrient(name, amount, unit) is None, (name, amount, unit)


def test_nutrition_enrichment_writes_only_supplied_values(owner, household, pantry_item,
                                                          fake_provider, use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))

    result = enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "ok"
    assert provider.calls == ["Trail mix"]
    facts = {fact.nutrient: fact for fact in NutritionFact.objects.filter(item=pantry_item)}
    assert set(facts) == {"protein_g", "energy_kcal"}
    protein = facts["protein_g"]
    assert protein.amount == Decimal("3.500")
    assert protein.unit == "g"
    assert protein.verification_status == InventoryVerification.ESTIMATED
    assert protein.verified_by is None and protein.verified_at is None
    assert protein.confidence == Decimal("0.900")
    assert protein.source_url.startswith("https://fdc.nal.usda.gov/")
    assert "fake-nutrition" in protein.source and "2039158" in protein.source
    assert result["candidate"]["retrieved_at"] == protein.observed_at.isoformat()
    assert result["written"] == ["protein_g", "energy_kcal"]
    # Missing nutrients stay unset: no invented zero rows.
    assert not NutritionFact.objects.filter(item=pantry_item, nutrient="fat_g").exists()


def test_enrichment_caches_the_response_with_its_fetch_time(owner, household, pantry_item,
                                                            fake_provider, use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))

    first = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)
    second = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert provider.calls == ["Trail mix"]
    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["fetched_at"] == first["fetched_at"]
    cache = EnrichmentCache.objects.get()
    assert cache.fetched_at == first["fetched_at"]
    assert cache.provider == "fake-nutrition"


def test_refresh_forces_a_new_provider_call(owner, household, pantry_item, fake_provider,
                                            use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))
    lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk, refresh=True)

    assert len(provider.calls) == 2


def test_expired_cache_is_refetched(owner, household, pantry_item, fake_provider, use_provider,
                                    make_candidate, settings):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))
    lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    settings.CAMPKIT_ENRICHMENT_CACHE_TTL_SECONDS = 0
    result = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert len(provider.calls) == 2
    assert result["from_cache"] is False


def test_rate_limit_returns_the_stale_cache_instead_of_fabricating(owner, household, pantry_item,
                                                                   fake_provider, use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))
    first = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    provider.error = ProviderRateLimited("USDA rate limit reached; retry later.")
    result = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk, refresh=True)

    assert result["status"] == "rate_limited"
    assert result["stale"] is True
    assert result["from_cache"] is True
    assert result["fetched_at"] == first["fetched_at"]
    assert [row["external_id"] for row in result["candidates"]] == ["2039158"]
    assert NutritionFact.objects.count() == 0


def test_provider_failure_without_cache_reports_unavailable_and_writes_nothing(
    owner, household, pantry_item, fake_provider, use_provider
):
    use_provider(fake_provider(error=ProviderUnavailable("USDA is unreachable.")))

    result = enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "unavailable"
    assert result["candidates"] == [] and result["written"] == []
    assert NutritionFact.objects.count() == 0


def test_unconfigured_provider_is_explicit_and_harmless(owner, household, pantry_item):
    result = enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "ok"
    assert result["provider"] == "null-nutrition"
    assert result["candidates"] == []
    assert NutritionFact.objects.count() == 0


def test_confirmed_nutrition_is_never_overwritten_by_a_provider(owner, household, pantry_item,
                                                                fake_provider, use_provider, make_candidate):
    create_nutrition_fact(
        actor=owner,
        household=household,
        item_id=pantry_item.pk,
        data={
            "nutrient": "protein_g",
            "amount": Decimal("10.000"),
            "unit": "g",
            "source": "household label check",
            "source_url": "",
            "observed_at": timezone.now(),
            "confidence": Decimal("1.000"),
            "verification_status": InventoryVerification.CONFIRMED,
        },
    )
    use_provider(fake_provider(candidates=[make_candidate()]))

    result = enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    fact = NutritionFact.objects.get(item=pantry_item, nutrient="protein_g")
    assert fact.amount == Decimal("10.000")
    assert fact.verification_status == InventoryVerification.CONFIRMED
    assert fact.verified_by == owner and fact.verified_at is not None
    assert result["skipped_confirmed"] == ["protein_g"]
    assert "energy_kcal" in result["written"]


def test_repeated_enrichment_updates_the_same_estimated_fact(owner, household, pantry_item,
                                                             fake_provider, use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))
    enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    provider.candidates = [
        make_candidate(nutrients=(NutrientValue("protein_g", Decimal("4.000"), "g"),))
    ]
    enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk, refresh=True)

    facts = NutritionFact.objects.filter(item=pantry_item, nutrient="protein_g")
    assert facts.count() == 1
    assert facts.get().amount == Decimal("4.000")


def test_two_fake_providers_produce_identical_results(owner, household, pantry_item, fake_provider,
                                                       use_provider, make_candidate):
    def run(provider_name):
        use_provider(fake_provider(candidates=[make_candidate(provider=provider_name)],
                                   name=provider_name))
        item = pantry_item.__class__.objects.create(
            household=pantry_item.household, name=f"Item {provider_name}", quantity=Decimal("1"),
            unit="each",
        )
        enrich_nutrition(actor=owner, household=household, item_id=item.pk)
        return {
            fact.nutrient: (str(fact.amount), fact.unit, str(fact.confidence),
                            fact.source_url.rsplit("/", 2)[-2])
            for fact in NutritionFact.objects.filter(item=item).order_by("nutrient")
        }, item.pk

    first, first_item = run("fake-a")
    second, second_item = run("fake-b")

    assert first == second
    assert NutritionFact.objects.filter(item_id__in=[first_item, second_item]).count() == 4


def test_enrichment_is_household_scoped(other_owner, household, pantry_item):
    with pytest.raises(PermissionDenied):
        enrich_nutrition(actor=other_owner, household=household, item_id=pantry_item.pk)
    with pytest.raises(PermissionDenied):
        lookup_nutrition(actor=other_owner, household=household, item_id=pantry_item.pk)


def test_corrupt_cache_rows_are_treated_as_a_miss(owner, household, pantry_item, fake_provider,
                                                  use_provider, make_candidate):
    provider = use_provider(fake_provider(candidates=[make_candidate()]))
    EnrichmentCache.objects.create(
        kind="nutrition",
        provider="fake-nutrition",
        query_key="trail mix",
        payload=[{"unexpected": True}],
        fetched_at=timezone.now(),
    )

    result = lookup_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert provider.calls == ["Trail mix"]
    assert result["from_cache"] is False


def test_research_retains_citations_and_confidence(owner, household, pantry_item, fake_researcher,
                                                   use_researcher, make_observation):
    observations = [
        make_observation(source_url="https://example.test/a"),
        make_observation(source_url="https://example.test/b", confidence=Decimal("0.550"),
                         title="Second source"),
    ]
    researcher = use_researcher(fake_researcher(observations=observations))

    result = research_product(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "ok"
    assert researcher.calls[0] == ProductIdentity(name="Trail mix")
    assert len(result["observations"]) == 2
    rows = ProductObservation.objects.filter(item=pantry_item).order_by("source_url")
    assert [row.source_url for row in rows] == ["https://example.test/a", "https://example.test/b"]
    assert all(row.retrieved_at is not None for row in rows)
    assert [row.confidence for row in rows] == [Decimal("0.700"), Decimal("0.550")]


def test_research_is_deduplicated_by_source_url(owner, household, pantry_item, fake_researcher,
                                                use_researcher, make_observation):
    use_researcher(fake_researcher(observations=[make_observation()]))
    research_product(actor=owner, household=household, item_id=pantry_item.pk)
    research_product(actor=owner, household=household, item_id=pantry_item.pk)

    assert ProductObservation.objects.filter(item=pantry_item).count() == 1


def test_research_without_a_configured_provider_returns_nothing(owner, household, pantry_item):
    result = research_product(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "ok"
    assert result["provider"] == "null-research"
    assert result["observations"] == []
    assert ProductObservation.objects.count() == 0


def test_research_provider_failure_is_reported_not_raised(owner, household, pantry_item,
                                                          fake_researcher, use_researcher):
    use_researcher(fake_researcher(error=ProviderUnavailable("research provider down")))

    result = research_product(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["status"] == "unavailable"
    assert result["observations"] == []
    assert "down" in result["error"]
    assert ProductObservation.objects.count() == 0


def test_research_identity_never_carries_household_or_stock_data(owner, household, pantry_item,
                                                                fake_researcher, use_researcher,
                                                                make_observation):
    researcher = use_researcher(fake_researcher(observations=[make_observation()]))

    research_product(actor=owner, household=household, item_id=pantry_item.pk)

    sent = researcher.calls[0]
    assert sent.name == "Trail mix"
    assert sent.brand == "" and sent.barcode == ""
    assert "household" not in repr(sent) and "quantity" not in repr(sent)


def test_observation_objects_require_citations_and_times():
    now = timezone.now()
    with pytest.raises(ValueError):
        SourceObservation(source="x", source_url="", retrieved_at=now, confidence=Decimal("0.5"))
    with pytest.raises(ValueError):
        SourceObservation(source="x", source_url="javascript:alert(1)", retrieved_at=now,
                          confidence=Decimal("0.5"))
    with pytest.raises(ValueError):
        SourceObservation(source="x", source_url="https://example.test/x",
                          retrieved_at=timezone.make_naive(now), confidence=Decimal("0.5"))
    with pytest.raises(ValueError):
        SourceObservation(source="x", source_url="https://example.test/x", retrieved_at=now,
                          confidence=Decimal("1.2"))


class ResearchTransporter:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode("utf-8")
        return _Response(raw)

    @property
    def payload(self):
        return json.loads(self.requests[0].data.decode("utf-8"))


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def research_provider(transporter, endpoint="https://hermes.example.test/research"):
    return HermesResearchProvider(endpoint=endpoint, token="research-token", transport=transporter)


def test_research_adapter_sends_only_product_identity():
    transporter = ResearchTransporter(body={"observations": []})

    research_provider(transporter).lookup(ProductIdentity(name="Stove", brand="Jetboil"))

    assert transporter.payload == {"name": "Stove", "brand": "Jetboil", "barcode": "", "category": ""}


def test_research_adapter_skips_entries_without_a_citation_time_or_confidence():
    now = timezone.now().isoformat()
    transporter = ResearchTransporter(body={"observations": [
        {"title": "no url", "source_url": "", "retrieved_at": now, "confidence": "0.5"},
        {"title": "bad url", "source_url": "ftp://example.test/x", "retrieved_at": now, "confidence": "0.5"},
        {"title": "no time", "source_url": "https://example.test/a", "confidence": "0.5"},
        {"title": "bad time", "source_url": "https://example.test/b", "retrieved_at": "yesterday",
         "confidence": "0.5"},
        {"title": "no confidence", "source_url": "https://example.test/c", "retrieved_at": now},
        {"title": "ok", "source_url": "https://example.test/d", "retrieved_at": now, "confidence": "0.6"},
    ]})

    observations = research_provider(transporter).lookup(ProductIdentity(name="Stove"))

    assert [item.source_url for item in observations] == ["https://example.test/d"]
    assert observations[0].confidence == Decimal("0.600")
    assert observations[0].title == "ok"


def test_research_adapter_requires_an_endpoint():
    with pytest.raises(ProviderNotConfigured):
        HermesResearchProvider(endpoint="").lookup(ProductIdentity(name="Stove"))


def test_research_adapter_maps_rate_limits_and_transport_failures():
    import urllib.error

    with pytest.raises(ProviderRateLimited):
        research_provider(ResearchTransporter(
            error=urllib.error.HTTPError("https://hermes.example.test", 429, "slow", {}, None)
        )).lookup(ProductIdentity(name="Stove"))
    with pytest.raises(ProviderUnavailable):
        research_provider(ResearchTransporter(error=urllib.error.URLError("down"))).lookup(
            ProductIdentity(name="Stove")
        )
    with pytest.raises(ProviderResponseError):
        research_provider(ResearchTransporter(body=b"<html>")).lookup(ProductIdentity(name="Stove"))
    with pytest.raises(ProviderResponseError):
        research_provider(ResearchTransporter(body={"unexpected": []})).lookup(
            ProductIdentity(name="Stove")
        )


def test_research_adapter_never_leaks_its_token_into_errors():
    import urllib.error

    provider = research_provider(ResearchTransporter(
        error=urllib.error.HTTPError("https://hermes.example.test", 500, "boom", {}, None)
    ))
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.lookup(ProductIdentity(name="Stove"))
    assert "research-token" not in str(excinfo.value)


def test_last_observation_wins_only_when_the_same_citation_repeats(owner, household, pantry_item,
                                                                   fake_researcher, use_researcher,
                                                                   make_observation):
    use_researcher(fake_researcher(observations=[
        make_observation(confidence=Decimal("0.400")),
        make_observation(confidence=Decimal("0.800"), summary="refreshed"),
    ]))

    research_product(actor=owner, household=household, item_id=pantry_item.pk)

    row = ProductObservation.objects.get(item=pantry_item)
    assert row.confidence == Decimal("0.800")
    assert row.summary == "refreshed"


def test_low_confidence_candidates_are_not_stored(owner, household, pantry_item, fake_provider,
                                                  use_provider, make_candidate, settings):
    settings.CAMPKIT_ENRICHMENT_MIN_CONFIDENCE = "0.950"
    use_provider(fake_provider(candidates=[make_candidate(confidence=Decimal("0.500"))]))

    result = enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    assert result["written"] == []
    assert result["skipped"] and "threshold" in result["skipped"][0]["reason"]
    assert NutritionFact.objects.count() == 0


def test_nutrition_facts_report_provenance_and_household_scope(owner, household, pantry_item,
                                                              fake_provider, use_provider,
                                                              make_candidate, other_owner,
                                                              other_household):
    use_provider(fake_provider(candidates=[make_candidate()]))
    enrich_nutrition(actor=owner, household=household, item_id=pantry_item.pk)

    facts = {
        row["nutrient"]: row
        for row in nutrition_facts(actor=owner, household=household, item_id=pantry_item.pk)
    }
    assert facts["protein_g"]["amount"] == "3.500"
    assert facts["protein_g"]["source_url"].startswith("https://fdc.nal.usda.gov/")
    assert facts["protein_g"]["confidence"] == "0.900"
    assert facts["protein_g"]["verification_status"] == "estimated"
    assert facts["protein_g"]["observed_at"]

    with pytest.raises(PermissionDenied):
        nutrition_facts(actor=other_owner, household=other_household, item_id=pantry_item.pk)


def test_task_entry_points_reuse_the_domain_services(owner, household, pantry_item, fake_provider,
                                                     use_provider, make_candidate):
    from apps.enrichment.tasks import enrich_pantry_item, research_pantry_item

    use_provider(fake_provider(candidates=[make_candidate()]))

    result = enrich_pantry_item(pantry_item.pk, actor=owner)
    assert result["written"] == ["protein_g", "energy_kcal"]
    assert NutritionFact.objects.filter(item=pantry_item).count() == 2

    research = research_pantry_item(pantry_item.pk, actor=owner)
    assert research["status"] == "ok" and research["provider"] == "null-research"


def test_task_entry_points_refuse_foreign_items(other_owner, pantry_item):
    from apps.enrichment.tasks import enrich_pantry_item, research_pantry_item

    with pytest.raises(PermissionDenied):
        enrich_pantry_item(pantry_item.pk, actor=other_owner)
    with pytest.raises(PermissionDenied):
        research_pantry_item(pantry_item.pk, actor=other_owner)
