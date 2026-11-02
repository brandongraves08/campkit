"""Settings-selected provider registry.

Domain services ask this module for a provider; they never import a concrete
adapter. Implementations are named by dotted path (or supplied as an instance,
which keeps tests and future plugin wiring simple). The shipped defaults are
null providers, so an unconfigured deployment returns no data instead of
blocking manual entry or inventing values.

No HTTP client and no vendor module is imported here: adapters are resolved
lazily through ``django.utils.module_loading.import_string``.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

NUTRITION_PROVIDER_SETTING = "CAMPKIT_NUTRITION_PROVIDER"
PRODUCT_RESEARCHER_SETTING = "CAMPKIT_PRODUCT_RESEARCHER"
MISSING_METHOD_MESSAGE = "must provide a callable lookup() contract"

_RESOLVED: dict[tuple[str, object], object] = {}


def get_nutrition_provider():
    """Return the configured nutrition provider (a null provider by default)."""
    return _resolve(NUTRITION_PROVIDER_SETTING, default_path())


def get_product_researcher():
    """Return the configured product-research provider (null by default)."""
    return _resolve(PRODUCT_RESEARCHER_SETTING, default_research_path())


def default_path() -> str:
    return "apps.enrichment.providers.NullNutritionProvider"


def default_research_path() -> str:
    return "apps.enrichment.providers.NullProductResearcher"


def reset_provider_cache() -> None:
    """Forget resolved providers (used by tests and by settings-driven reloads)."""
    _RESOLVED.clear()


def provider_status() -> dict:
    """Describe the configured providers for the API/UI and CodeGraph summaries."""
    return {
        "nutrition": _describe(NUTRITION_PROVIDER_SETTING, default_path(), get_nutrition_provider),
        "research": _describe(PRODUCT_RESEARCHER_SETTING, default_research_path(), get_product_researcher),
    }


def _describe(setting_name: str, fallback: str, getter) -> dict:
    configured = getattr(settings, setting_name, "") or fallback
    provider = getter()
    return {
        "setting": setting_name,
        "configured": configured != fallback,
        "provider": getattr(provider, "name", type(provider).__name__),
        "implementation": configured if isinstance(configured, str) else type(provider).__name__,
    }


def _cache_key(setting_name: str, configured) -> tuple[str, object]:
    if isinstance(configured, str):
        return (setting_name, configured)
    return (setting_name, ("instance", id(configured)))


def _resolve(setting_name: str, fallback: str):
    configured = getattr(settings, setting_name, "") or fallback
    key = _cache_key(setting_name, configured)
    resolved = _RESOLVED.get(key)
    if resolved is not None:
        return resolved
    if isinstance(configured, str):
        try:
            factory = import_string(configured)
            provider = factory()
        except Exception as exc:  # configuration faults must be legible, not tracebacks in a web request
            raise ImproperlyConfigured(
                f"{setting_name} does not resolve to a usable provider ({configured!r})."
            ) from exc
    else:
        provider = configured
    if not callable(getattr(provider, "lookup", None)):
        raise ImproperlyConfigured(
            f"{setting_name} provider {type(provider).__name__} {MISSING_METHOD_MESSAGE}."
        )
    _RESOLVED[key] = provider
    return provider
