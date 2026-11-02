"""The integrations registry is the only place provider implementations are chosen."""

import ast
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.enrichment.providers import NullNutritionProvider, NullProductResearcher
from apps.integrations import registry
from apps.integrations.types import ProductIdentity


@pytest.fixture(autouse=True)
def registry_settings(settings):
    settings.CAMPKIT_NUTRITION_PROVIDER = ""
    settings.CAMPKIT_PRODUCT_RESEARCHER = ""
    settings.CAMPKIT_USDA_API_KEY = "usda-secret-value"
    registry.reset_provider_cache()
    yield settings
    registry.reset_provider_cache()


def test_unconfigured_providers_return_no_data(settings):
    nutrition = registry.get_nutrition_provider()
    research = registry.get_product_researcher()

    assert isinstance(nutrition, NullNutritionProvider)
    assert isinstance(research, NullProductResearcher)
    assert nutrition.lookup("0123456789012") == []
    assert research.lookup(ProductIdentity(name="Camp stove")) == []


def test_dotted_setting_paths_resolve_adapters(settings):
    settings.CAMPKIT_NUTRITION_PROVIDER = "apps.enrichment.usda.UsdaNutritionProvider"
    settings.CAMPKIT_PRODUCT_RESEARCHER = "apps.enrichment.research.HermesResearchProvider"

    nutrition = registry.get_nutrition_provider()
    research = registry.get_product_researcher()

    assert nutrition.name == "usda-fooddata-central"
    assert nutrition.base_url == "https://api.nal.usda.gov/fdc/v1"
    assert research.name == "hermes-research"
    assert callable(nutrition.lookup) and callable(research.lookup)


def test_provider_instances_are_used_as_is(settings):
    class Inline:
        name = "inline"

        def lookup(self, barcode_or_query):
            return []

    inline = Inline()
    settings.CAMPKIT_NUTRITION_PROVIDER = inline

    assert registry.get_nutrition_provider() is inline
    assert registry.get_nutrition_provider() is inline


def test_unknown_provider_path_is_a_configuration_error_without_secrets(settings):
    settings.CAMPKIT_NUTRITION_PROVIDER = "apps.enrichment.missing.NotAProvider"

    with pytest.raises(ImproperlyConfigured) as excinfo:
        registry.get_nutrition_provider()

    message = str(excinfo.value)
    assert "CAMPKIT_NUTRITION_PROVIDER" in message
    assert "usda-secret-value" not in message


def test_provider_without_lookup_contract_is_rejected(settings):
    settings.CAMPKIT_PRODUCT_RESEARCHER = "builtins.object"

    with pytest.raises(ImproperlyConfigured) as excinfo:
        registry.get_product_researcher()

    assert "lookup" in str(excinfo.value)


def test_registry_source_never_imports_vendor_adapters():
    source = Path(registry.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name.startswith("apps.enrichment")]


def test_provider_status_reports_configuration(settings):
    assert registry.provider_status()["nutrition"]["configured"] is False
    assert registry.provider_status()["nutrition"]["provider"] == "null-nutrition"

    settings.CAMPKIT_NUTRITION_PROVIDER = "apps.enrichment.usda.UsdaNutritionProvider"
    status = registry.provider_status()["nutrition"]
    assert status["configured"] is True
    assert status["provider"] == "usda-fooddata-central"
    assert status["setting"] == "CAMPKIT_NUTRITION_PROVIDER"


def test_registry_cache_is_cleared_between_settings(settings):
    first = registry.get_nutrition_provider()
    settings.CAMPKIT_NUTRITION_PROVIDER = "apps.enrichment.usda.UsdaNutritionProvider"
    registry.reset_provider_cache()

    assert registry.get_nutrition_provider() is not first
