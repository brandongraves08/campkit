"""Enrichment provider boundary.

Protocols and null implementations live here so a deployment can run with no
provider configured. Null providers return no data: they never raise, never
fabricate a nutrient, and never block manual entry. Concrete adapters live in
:mod:`apps.enrichment.usda` and :mod:`apps.enrichment.research`.
"""

from apps.integrations.types import (
    NutritionCandidate,
    NutritionProvider,
    ProductIdentity,
    ProductResearcher,
    SourceObservation,
)

__all__ = [
    "NullNutritionProvider",
    "NullProductResearcher",
    "NutritionProvider",
    "ProductResearcher",
    "NutritionCandidate",
    "ProductIdentity",
    "SourceObservation",
]


class NullNutritionProvider:
    """Unconfigured nutrition provider: no data, no failure."""

    name = "null-nutrition"

    def lookup(self, barcode_or_query: str) -> list[NutritionCandidate]:
        return []


class NullProductResearcher:
    """Unconfigured research provider: no observations, no failure."""

    name = "null-research"

    def lookup(self, candidate: ProductIdentity) -> list[SourceObservation]:
        return []
