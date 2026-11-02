"""Shared provider contracts for sourced enrichment.

This module holds the vocabulary every enrichment adapter speaks: typed provider
failures, normalized nutrient values, and validated nutrition/research results.
It deliberately contains no HTTP client and no vendor import, so domain services
can depend on it without becoming provider-specific. The integrations registry
(:mod:`apps.integrations.registry`) picks the implementation from settings, and
the adapters (:mod:`apps.enrichment.usda`, :mod:`apps.enrichment.research`)
live behind these protocols.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol

# Inventory provenance stores a URLField of 200 characters; enrichment never
# invents or stores a longer citation link.
PROVENANCE_URL_LIMIT = 200
MAX_NAME = 200
MAX_BRAND = 120
MAX_IDENTIFIER = 64
MAX_UNIT = 8
MAX_SUMMARY = 500
MAX_NUTRIENTS = 64


class ProviderError(Exception):
    """Base class for provider failures. Messages never carry credentials."""


class ProviderNotConfigured(ProviderError):
    """No usable provider is configured (missing endpoint or rejected key)."""


class ProviderUnavailable(ProviderError):
    """The provider could not be reached or returned a 5xx; retry later."""


class ProviderRateLimited(ProviderError):
    """The provider asked us to slow down; retry after a delay."""


class ProviderResponseError(ProviderError):
    """The provider answered with something this adapter cannot trust."""


NUTRIENT_ALIASES = {
    "energy": "energy_kcal",
    "energy (kcal)": "energy_kcal",
    "energy (kj)": "energy_kcal",
    "calories": "energy_kcal",
    "protein": "protein_g",
    "total lipid (fat)": "fat_g",
    "total lipid": "fat_g",
    "fat": "fat_g",
    "carbohydrate, by difference": "carbohydrate_g",
    "carbohydrate": "carbohydrate_g",
    "carbohydrates": "carbohydrate_g",
    "fiber, total dietary": "fiber_g",
    "fiber": "fiber_g",
    "sugars, total including nlea": "sugar_g",
    "sugars, total": "sugar_g",
    "total sugars": "sugar_g",
    "sugars": "sugar_g",
    "sodium, na": "sodium_mg",
    "sodium": "sodium_mg",
    "fatty acids, total saturated": "saturated_fat_g",
    "saturated fat": "saturated_fat_g",
}

# Canonical storage unit per canonical nutrient name.
NUTRIENT_UNITS = {
    "energy_kcal": "kcal",
    "protein_g": "g",
    "fat_g": "g",
    "carbohydrate_g": "g",
    "fiber_g": "g",
    "sugar_g": "g",
    "sodium_mg": "mg",
    "saturated_fat_g": "g",
}

UNIT_TOKENS = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "kg": "kg",
    "kilogram": "kg",
    "ug": "ug",
    "µg": "ug",
    "μg": "ug",
    "mcg": "ug",
    "microgram": "ug",
    "kcal": "kcal",
    "cal": "kcal",
    "kilocalorie": "kcal",
    "kj": "kj",
    "kilojoule": "kj",
}

# (source unit, target unit) -> ("multiply" | "divide", factor)
_UNIT_FACTORS = {
    ("kj", "kcal"): ("divide", Decimal("4.184")),
    ("kcal", "kj"): ("multiply", Decimal("4.184")),
    ("g", "mg"): ("multiply", Decimal("1000")),
    ("mg", "g"): ("divide", Decimal("1000")),
    ("ug", "mg"): ("divide", Decimal("1000")),
    ("ug", "g"): ("divide", Decimal("1000000")),
    ("kg", "g"): ("multiply", Decimal("1000")),
}

_QUANTUM = Decimal("0.001")


def _text(value, limit: int, *, field_name: str, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValueError(f"{field_name} is required.")
    return text[:limit]


def _url(value, *, required: bool, field_name: str = "source_url") -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise ValueError(f"{field_name} is required.")
        return ""
    if len(text) > PROVENANCE_URL_LIMIT:
        raise ValueError(f"{field_name} must be {PROVENANCE_URL_LIMIT} characters or fewer.")
    if not text.lower().startswith(("https://", "http://")):
        raise ValueError(f"{field_name} must be an http(s) URL.")
    return text


def _confidence(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Confidence must be a number between 0 and 1.") from exc
    if not number.is_finite() or not 0 <= number <= 1:
        raise ValueError("Confidence must be a number between 0 and 1.")
    return number.quantize(_QUANTUM)


def _aware(value, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone.")
    return value


def _slug(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


@dataclass(frozen=True)
class NutrientValue:
    """One nutrient measurement expressed in the canonical unit we store."""

    nutrient: str
    amount: Decimal
    unit: str
    source_name: str = ""

    def __post_init__(self):
        nutrient = _text(self.nutrient, 80, field_name="nutrient", required=True)
        unit = _text(self.unit, MAX_UNIT, field_name="unit", required=True)
        if nutrient not in NUTRIENT_UNITS:
            raise ValueError("Unsupported nutrient name.")
        if unit != NUTRIENT_UNITS[nutrient]:
            raise ValueError("Nutrient unit does not match the canonical storage unit.")
        try:
            amount = Decimal(str(self.amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Nutrient amount must be a decimal number.") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("Nutrient amounts cannot be negative or non-finite.")
        object.__setattr__(self, "nutrient", nutrient)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "amount", amount.quantize(_QUANTUM, rounding=ROUND_HALF_UP))
        object.__setattr__(self, "source_name", _text(self.source_name, 80, field_name="source_name"))


def _convert(amount: Decimal, source_unit: str, target_unit: str) -> Decimal | None:
    if source_unit == target_unit:
        return amount
    rule = _UNIT_FACTORS.get((source_unit, target_unit))
    if rule is None:
        return None
    operation, factor = rule
    return amount * factor if operation == "multiply" else amount / factor


def normalize_nutrient(name, amount, unit) -> NutrientValue | None:
    """Map a provider nutrient to a canonical key and storage unit.

    Returns ``None`` when the nutrient is unknown, the unit cannot be converted
    to the canonical unit, or the value is unusable. Nothing is guessed: an
    unmappable label is dropped rather than stored under an invented name.
    """
    canonical = NUTRIENT_ALIASES.get(_slug(name))
    if canonical is None:
        return None
    source_unit = UNIT_TOKENS.get(_slug(unit))
    if source_unit is None:
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    converted = _convert(value, source_unit, NUTRIENT_UNITS[canonical])
    if converted is None:
        return None
    return NutrientValue(
        nutrient=canonical,
        amount=converted.quantize(_QUANTUM, rounding=ROUND_HALF_UP),
        unit=NUTRIENT_UNITS[canonical],
        source_name=str(name or ""),
    )


@dataclass(frozen=True)
class NutritionCandidate:
    """Validated provider result for one product, with its citation."""

    provider: str
    external_id: str
    name: str
    source_url: str
    retrieved_at: datetime
    confidence: Decimal
    brand: str = ""
    barcode: str = ""
    data_type: str = ""
    nutrients: tuple[NutrientValue, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "provider", _text(self.provider, 80, field_name="provider", required=True))
        object.__setattr__(self, "external_id", _text(self.external_id, MAX_IDENTIFIER, field_name="external_id"))
        object.__setattr__(self, "name", _text(self.name, MAX_NAME, field_name="name", required=True))
        object.__setattr__(self, "brand", _text(self.brand, MAX_BRAND, field_name="brand"))
        object.__setattr__(self, "barcode", _text(self.barcode, MAX_IDENTIFIER, field_name="barcode"))
        object.__setattr__(self, "data_type", _text(self.data_type, 40, field_name="data_type"))
        object.__setattr__(self, "source_url", _url(self.source_url, required=True))
        object.__setattr__(self, "retrieved_at", _aware(self.retrieved_at, field_name="retrieved_at"))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        nutrients = self.nutrients or ()
        if not isinstance(nutrients, (list, tuple)):
            raise ValueError("Nutrients must be a sequence of NutrientValue entries.")
        if len(nutrients) > MAX_NUTRIENTS:
            raise ValueError("Too many nutrient entries in a provider result.")
        for entry in nutrients:
            if not isinstance(entry, NutrientValue):
                raise ValueError("Nutrients must be NutrientValue entries.")
        object.__setattr__(self, "nutrients", tuple(nutrients))


@dataclass(frozen=True)
class SourceObservation:
    """One dated, cited research observation about a product."""

    source: str
    source_url: str
    retrieved_at: datetime
    confidence: Decimal
    title: str = ""
    summary: str = ""

    def __post_init__(self):
        object.__setattr__(self, "source", _text(self.source, 80, field_name="source", required=True))
        object.__setattr__(self, "source_url", _url(self.source_url, required=True))
        object.__setattr__(self, "retrieved_at", _aware(self.retrieved_at, field_name="retrieved_at"))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "title", _text(self.title, MAX_NAME, field_name="title"))
        object.__setattr__(self, "summary", _text(self.summary, MAX_SUMMARY, field_name="summary"))


@dataclass(frozen=True)
class ProductIdentity:
    """The only product data a research provider may receive.

    House id, stock, quantity, and household identity are deliberately absent so
    an external researcher cannot learn household state.
    """

    name: str
    brand: str = ""
    barcode: str = ""
    category: str = ""

    def __post_init__(self):
        object.__setattr__(self, "name", _text(self.name, 160, field_name="name", required=True))
        object.__setattr__(self, "brand", _text(self.brand, MAX_BRAND, field_name="brand"))
        object.__setattr__(self, "barcode", _text(self.barcode, MAX_IDENTIFIER, field_name="barcode"))
        object.__setattr__(self, "category", _text(self.category, 80, field_name="category"))


class NutritionProvider(Protocol):
    name: str

    def lookup(self, barcode_or_query: str) -> list[NutritionCandidate]:
        """Return validated candidates for a barcode or product query."""
        ...


class ProductResearcher(Protocol):
    name: str

    def lookup(self, candidate: ProductIdentity) -> list[SourceObservation]:
        """Return dated, cited observations for a product identity."""
        ...
