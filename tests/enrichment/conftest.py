"""Fixtures for enrichment tests.

Every provider here is a deterministic fake or the shipped null provider: no
test performs a live FoodData Central, Hermes research, retailer, or price
request.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.integrations import registry
from apps.integrations.types import (
    NutrientValue,
    NutritionCandidate,
    SourceObservation,
)
from apps.inventory.services import create_pantry_item


@pytest.fixture(autouse=True)
def enrichment_settings(settings):
    settings.CAMPKIT_NUTRITION_PROVIDER = ""
    settings.CAMPKIT_PRODUCT_RESEARCHER = ""
    settings.CAMPKIT_ENRICHMENT_CACHE_TTL_SECONDS = 3600
    settings.CAMPKIT_PRICE_STALE_DAYS = 30
    settings.CAMPKIT_USDA_API_KEY = ""
    settings.CAMPKIT_USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"
    settings.CAMPKIT_USDA_TIMEOUT = 5.0
    settings.CAMPKIT_HERMES_RESEARCH_URL = ""
    settings.CAMPKIT_HERMES_RESEARCH_TOKEN = ""
    settings.CAMPKIT_HERMES_RESEARCH_TIMEOUT = 5.0
    registry.reset_provider_cache()
    yield settings
    registry.reset_provider_cache()


@pytest.fixture
def pantry_item(owner, household):
    return create_pantry_item(
        actor=owner,
        household=household,
        data={"name": "Trail mix", "quantity": Decimal("2"), "unit": "each"},
    )


@pytest.fixture
def citation_url():
    return "https://fdc.nal.usda.gov/fdc-app.html#/food-details/2039158/nutrients"


@pytest.fixture
def make_candidate(citation_url):
    def _make(**overrides):
        values = {
            "provider": "fake-nutrition",
            "external_id": "2039158",
            "name": "TRAIL MIX",
            "brand": "CampCo",
            "barcode": "0123456789012",
            "data_type": "Branded",
            "nutrients": (
                NutrientValue(nutrient="protein_g", amount=Decimal("3.500"), unit="g"),
                NutrientValue(nutrient="energy_kcal", amount=Decimal("120.000"), unit="kcal"),
            ),
            "source_url": citation_url,
            "retrieved_at": timezone.now(),
            "confidence": Decimal("0.900"),
        }
        values.update(overrides)
        return NutritionCandidate(**values)

    return _make


@pytest.fixture
def make_observation():
    def _make(**overrides):
        values = {
            "source": "hermes-research",
            "source_url": "https://example.test/reviews/trail-mix",
            "retrieved_at": timezone.now(),
            "confidence": Decimal("0.700"),
            "title": "Trail mix review",
            "summary": "Synthetic fixture summary.",
        }
        values.update(overrides)
        return SourceObservation(**values)

    return _make


class FakeNutritionProvider:
    """Deterministic nutrition provider used for parity and failure tests."""

    name = "fake-nutrition"

    def __init__(self, candidates=(), error=None, name=None):
        self.candidates = list(candidates)
        self.error = error
        self.calls = []
        if name:
            self.name = name

    def lookup(self, barcode_or_query):
        self.calls.append(barcode_or_query)
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class FakeProductResearcher:
    """Deterministic product-research provider that records what it receives."""

    name = "fake-research"

    def __init__(self, observations=(), error=None, name=None):
        self.observations = list(observations)
        self.error = error
        self.calls = []
        if name:
            self.name = name

    def lookup(self, candidate):
        self.calls.append(candidate)
        if self.error is not None:
            raise self.error
        return list(self.observations)


@pytest.fixture
def fake_provider():
    return FakeNutritionProvider


@pytest.fixture
def fake_researcher():
    return FakeProductResearcher


@pytest.fixture
def use_provider(settings):
    def _use(provider):
        settings.CAMPKIT_NUTRITION_PROVIDER = provider
        registry.reset_provider_cache()
        return provider

    return _use


@pytest.fixture
def use_researcher(settings):
    def _use(researcher):
        settings.CAMPKIT_PRODUCT_RESEARCHER = researcher
        registry.reset_provider_cache()
        return researcher

    return _use
