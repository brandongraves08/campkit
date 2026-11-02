"""Domain services for sourced product research, nutrition, and price history.

Providers are obtained through :mod:`apps.integrations.registry`, so these
services never import a vendor adapter. Inventory writes (nutrition facts, price
observations) go through :mod:`apps.inventory.services`, which owns the
provenance shape, household scoping, and audit trail.

Rules this module enforces:

* a provider value is stored only when the provider supplied it — missing
  nutrients stay unset and no field is ever filled with a guess;
* every stored value keeps its citation, confidence, and observation time;
* a human-confirmed fact is never overwritten by a provider;
* a failed or rate-limited provider falls back to a stale cache (clearly marked
  stale) or reports the failure, and never invents a payload;
* price observations are dated manual/receipt entries; no retailer page is
  fetched, ever.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.services import record_change
from apps.enrichment.models import EnrichmentCache, ProductObservation
from apps.households.permissions import require_household_member
from apps.integrations.registry import get_nutrition_provider, get_product_researcher
from apps.integrations.types import (
    MAX_NUTRIENTS,
    MAX_SUMMARY,
    NutrientValue,
    NutritionCandidate,
    ProductIdentity,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    SourceObservation,
)
from apps.inventory.services import (
    get_pantry_item,
    list_nutrition_facts,
    list_price_observations,
    record_price as record_inventory_price,
    upsert_nutrition_fact,
)

NUTRITION_KIND = EnrichmentCache.NUTRITION
PRICE_MAX = Decimal("99999999.99")
PRICE_QUANTUM = Decimal("0.01")
PRICE_SOURCES = {"manual", "receipt", "api", "feed"}
FUTURE_TOLERANCE = timedelta(minutes=5)

_ERROR_STATUS = (
    (ProviderRateLimited, "rate_limited"),
    (ProviderNotConfigured, "not_configured"),
    (ProviderUnavailable, "unavailable"),
    (ProviderResponseError, "invalid_response"),
)


def _bounded(text, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _provider_name(provider) -> str:
    return _bounded(getattr(provider, "name", "") or type(provider).__name__, 80)


def _query_key(query: str) -> str:
    return _bounded(" ".join(str(query or "").split()).lower(), 200)


def _status_for(exc: ProviderError) -> str:
    for error_type, status in _ERROR_STATUS:
        if isinstance(exc, error_type):
            return status
    return "failed"


def _cache_ttl_seconds() -> int:
    return int(getattr(settings, "CAMPKIT_ENRICHMENT_CACHE_TTL_SECONDS", 30 * 24 * 3600))


def _min_confidence() -> Decimal:
    try:
        value = Decimal(str(getattr(settings, "CAMPKIT_ENRICHMENT_MIN_CONFIDENCE", "0.300")))
    except (InvalidOperation, TypeError, ValueError):
        value = Decimal("0.300")
    return min(max(value, Decimal("0")), Decimal("1"))


def _price_stale_days() -> int:
    return max(int(getattr(settings, "CAMPKIT_PRICE_STALE_DAYS", 30)), 0)


# --------------------------------------------------------------------------- #
# Cache serialization
# --------------------------------------------------------------------------- #

def _candidate_to_dict(candidate: NutritionCandidate) -> dict:
    return {
        "provider": candidate.provider,
        "external_id": candidate.external_id,
        "name": candidate.name,
        "brand": candidate.brand,
        "barcode": candidate.barcode,
        "data_type": candidate.data_type,
        "source_url": candidate.source_url,
        "retrieved_at": candidate.retrieved_at.isoformat(),
        "confidence": str(candidate.confidence),
        "nutrients": [
            {
                "nutrient": nutrient.nutrient,
                "amount": str(nutrient.amount),
                "unit": nutrient.unit,
                "source_name": nutrient.source_name,
            }
            for nutrient in candidate.nutrients
        ],
    }


def _candidate_from_dict(data):
    """Rebuild a cached candidate; ``None`` means the row is unusable."""
    if not isinstance(data, dict):
        return None

    try:
        retrieved_at = datetime.fromisoformat(str(data.get("retrieved_at") or ""))
    except ValueError:
        return None
    nutrients = []
    for entry in data.get("nutrients") or []:
        if not isinstance(entry, dict):
            return None
        try:
            nutrients.append(
                NutrientValue(
                    nutrient=str(entry.get("nutrient") or ""),
                    amount=entry.get("amount"),
                    unit=str(entry.get("unit") or ""),
                    source_name=str(entry.get("source_name") or ""),
                )
            )
        except ValueError:
            return None
        if len(nutrients) > MAX_NUTRIENTS:
            return None
    try:
        return NutritionCandidate(
            provider=str(data.get("provider") or ""),
            external_id=str(data.get("external_id") or ""),
            name=str(data.get("name") or ""),
            brand=str(data.get("brand") or ""),
            barcode=str(data.get("barcode") or ""),
            data_type=str(data.get("data_type") or ""),
            nutrients=tuple(nutrients),
            source_url=str(data.get("source_url") or ""),
            retrieved_at=retrieved_at,
            confidence=data.get("confidence"),
        )
    except ValueError:
        return None


def _candidates_from_cache(row: EnrichmentCache):
    if not isinstance(row.payload, list):
        return None
    candidates = []
    for entry in row.payload:
        candidate = _candidate_from_dict(entry)
        if candidate is None:
            return None
        candidates.append(candidate)
    return candidates


def _validated_candidates(raw) -> list[NutritionCandidate]:
    if not isinstance(raw, (list, tuple)):
        raise ProviderResponseError("Nutrition provider returned an unsupported result.")
    for item in raw:
        if not isinstance(item, NutritionCandidate):
            raise ProviderResponseError("Nutrition provider returned an unsupported candidate.")
    return list(raw)


def _public_candidate(candidate: NutritionCandidate) -> dict:
    return {
        "provider": candidate.provider,
        "external_id": candidate.external_id,
        "name": candidate.name,
        "brand": candidate.brand,
        "barcode": candidate.barcode,
        "data_type": candidate.data_type,
        "source_url": candidate.source_url,
        "retrieved_at": candidate.retrieved_at.isoformat(),
        "confidence": str(candidate.confidence),
        "nutrients": [
            {
                "nutrient": nutrient.nutrient,
                "amount": str(nutrient.amount),
                "unit": nutrient.unit,
                "source_name": nutrient.source_name,
            }
            for nutrient in candidate.nutrients
        ],
    }


def _result(status, provider_name, query, candidates, fetched_at, from_cache, stale, item, error) -> dict:
    return {
        "status": status,
        "provider": provider_name,
        "query": query,
        "item_id": str(item.pk) if item is not None else None,
        "fetched_at": fetched_at,
        "from_cache": from_cache,
        "stale": stale,
        "error": _bounded(error, 200),
        "candidates": [_public_candidate(candidate) for candidate in candidates],
    }


# --------------------------------------------------------------------------- #
# Nutrition lookup and enrichment
# --------------------------------------------------------------------------- #

def _lookup(*, actor, household, item=None, item_id=None, query="", refresh=False, provider=None):
    """Return ``(public_result, candidates)`` for a nutrition lookup."""
    require_household_member(actor, household.pk)
    if item is None and item_id is not None:
        item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    query = _bounded(query or (item.name if item is not None else ""), 120).strip()
    if not query:
        raise ValidationError({"query": "Enter a barcode or product name to look up."})
    provider = provider or get_nutrition_provider()
    provider_name = _provider_name(provider)
    now = timezone.now()
    key = _query_key(query)
    cached = (
        EnrichmentCache.objects.filter(kind=NUTRITION_KIND, provider=provider_name, query_key=key).first()
    )
    if cached is not None and not refresh and cached.is_fresh(now, _cache_ttl_seconds()):
        fresh = _candidates_from_cache(cached)
        if fresh is not None:
            return (
                _result("ok", provider_name, query, fresh, cached.fetched_at, True, False, item, ""),
                fresh,
            )
    try:
        candidates = _validated_candidates(provider.lookup(query))
    except ProviderError as exc:
        return _failure(exc, cached, provider_name, query, item)
    EnrichmentCache.objects.update_or_create(
        kind=NUTRITION_KIND,
        provider=provider_name,
        query_key=key,
        defaults={"payload": [_candidate_to_dict(candidate) for candidate in candidates], "fetched_at": now},
    )
    return (
        _result("ok", provider_name, query, candidates, now, False, False, item, ""),
        candidates,
    )


def _failure(exc: ProviderError, cached, provider_name, query, item):
    status = _status_for(exc)
    error = _bounded(str(exc) or type(exc).__name__, 200)
    if cached is not None:
        stale = _candidates_from_cache(cached)
        if stale:
            # Keep serving the older sourced values, explicitly marked stale.
            return (
                _result(status, provider_name, query, stale, cached.fetched_at, True, True, item, error),
                stale,
            )
    return (_result(status, provider_name, query, [], None, False, False, item, error), [])


def lookup_nutrition(*, actor, household, item_id=None, query="", refresh=False, provider=None) -> dict:
    """Look up nutrition candidates for a barcode/query, using the dated cache."""
    result, _candidates = _lookup(
        actor=actor,
        household=household,
        item_id=item_id,
        query=query,
        refresh=refresh,
        provider=provider,
    )
    return result


@transaction.atomic
def enrich_nutrition(*, actor, household, item_id, query="", refresh=False, provider=None) -> dict:
    """Write the best sourced candidate onto a pantry item as estimated facts.

    A cached (even stale) candidate is stored with its original fetch time, never
    back-dated to now, and every stored fact keeps its citation and confidence.
    """
    require_household_member(actor, household.pk)
    item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    result, candidates = _lookup(
        actor=actor,
        household=household,
        item=item,
        query=query,
        refresh=refresh,
        provider=provider,
    )
    summary = {
        key: result[key]
        for key in ("status", "provider", "query", "item_id", "fetched_at", "from_cache", "stale",
                    "error", "candidates")
    }
    summary.update(
        {"candidate": None, "written": [], "skipped_confirmed": [], "skipped": []}
    )
    if not candidates:
        return summary

    best = max(candidates, key=lambda candidate: candidate.confidence)
    summary["candidate"] = _public_candidate(best)
    if best.confidence < _min_confidence():
        summary["skipped"].append(
            {
                "reason": "Candidate confidence is below the enrichment threshold.",
                "confidence": str(best.confidence),
            }
        )
        return summary
    if best.nutrients:
        for nutrient in best.nutrients:
            _fact, changed = upsert_nutrition_fact(
                actor=actor,
                household=household,
                item_id=item.pk,
                data={
                    "nutrient": nutrient.nutrient,
                    "amount": nutrient.amount,
                    "unit": nutrient.unit,
                    "source": _bounded(f"{best.provider}:{best.external_id}", 255),
                    "source_url": best.source_url,
                    "observed_at": result["fetched_at"] or timezone.now(),
                    "confidence": best.confidence,
                    "verification_status": "estimated",
                },
            )
            if changed:
                summary["written"].append(nutrient.nutrient)
            else:
                summary["skipped_confirmed"].append(nutrient.nutrient)
    return summary


def nutrition_facts(*, actor, household, item_id) -> list[dict]:
    """Return stored nutrition facts with their provenance and freshness data."""
    facts = list_nutrition_facts(actor=actor, household=household, item_id=item_id)
    return [
        {
            "nutrient": fact.nutrient,
            "amount": str(fact.amount),
            "unit": fact.unit,
            "source": fact.source,
            "source_url": fact.source_url,
            "observed_at": fact.observed_at.isoformat(),
            "confidence": str(fact.confidence) if fact.confidence is not None else None,
            "verification_status": fact.verification_status,
        }
        for fact in facts
    ]


# --------------------------------------------------------------------------- #
# Product research
# --------------------------------------------------------------------------- #

def _validated_observations(raw) -> list[SourceObservation]:
    if not isinstance(raw, (list, tuple)):
        raise ProviderResponseError("Research provider returned an unsupported result.")
    for item in raw:
        if not isinstance(item, SourceObservation):
            raise ProviderResponseError("Research provider returned an unsupported observation.")
    return list(raw)


def _store_observation(item, observation: SourceObservation) -> ProductObservation:
    row = ProductObservation.objects.filter(item=item, source_url=observation.source_url).first()
    if row is None:
        row = ProductObservation(item=item, source_url=observation.source_url)
    row.provider = _bounded(observation.source, 80) or "unknown"
    row.title = _bounded(observation.title, 200)
    row.summary = _bounded(observation.summary, MAX_SUMMARY)
    row.retrieved_at = observation.retrieved_at
    row.confidence = observation.confidence
    row.full_clean()
    row.save()
    return row


def _public_observation(row: ProductObservation) -> dict:
    return {
        "id": str(row.pk),
        "provider": row.provider,
        "title": row.title,
        "summary": row.summary,
        "source_url": row.source_url,
        "retrieved_at": row.retrieved_at.isoformat(),
        "confidence": str(row.confidence),
    }


@transaction.atomic
def research_product(*, actor, household, item_id, identity=None, provider=None) -> dict:
    """Fetch and store dated, cited observations for one pantry item."""
    item = get_pantry_item(actor=actor, item_id=item_id, household=household)
    provider = provider or get_product_researcher()
    provider_name = _provider_name(provider)
    identity = identity or ProductIdentity(name=item.name)
    result = {
        "status": "ok",
        "provider": provider_name,
        "item_id": str(item.pk),
        "error": "",
        "observations": [],
    }
    try:
        observations = _validated_observations(provider.lookup(identity))
    except ProviderError as exc:
        result["status"] = _status_for(exc)
        result["error"] = _bounded(str(exc) or type(exc).__name__, 200)
        return result
    stored = [_store_observation(item, observation) for observation in observations]
    if stored:
        record_change(
            actor=actor,
            household=household,
            object=item,
            action="product_researched",
            before=None,
            after={"provider": provider_name, "sources": [row.source_url for row in stored]},
        )
    result["observations"] = [_public_observation(row) for row in stored]
    return result


# --------------------------------------------------------------------------- #
# Dated price observations and price history
# --------------------------------------------------------------------------- #

def _price_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"amount": "Price must be a decimal amount."}) from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({"amount": "Price must be greater than zero."})
    if amount > PRICE_MAX:
        raise ValidationError({"amount": "Price is outside the accepted range."})
    if amount != amount.quantize(PRICE_QUANTUM):
        raise ValidationError({"amount": "Prices support at most two decimal places."})
    return amount.quantize(PRICE_QUANTUM)


def _price_currency(value) -> str:
    currency = str(value or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError({"currency": "Currency must be a three-letter code."})
    return currency


def _price_merchant(value) -> str:
    merchant = str(value or "").strip()
    if len(merchant) > 160:
        raise ValidationError({"merchant": "Merchant name is too long."})
    return merchant


def _price_source_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 200:
        raise ValidationError({"source_url": "Source links must be 200 characters or fewer."})
    if not url.lower().startswith(("https://", "http://")):
        raise ValidationError({"source_url": "Source links must be http(s) URLs."})
    return url


def _price_source(value) -> str:
    source = str(value or "manual").strip().lower()
    if source not in PRICE_SOURCES:
        raise ValidationError({"source": "Unsupported price source."})
    return source


def _price_observed_at(value) -> datetime:
    if value in (None, ""):
        return timezone.now()
    if isinstance(value, str):
        parsed = parse_datetime(value.strip())
        if parsed is None:
            raise ValidationError({"observed_at": "Observation time must be an ISO-8601 timestamp."})
        value = parsed
    if not isinstance(value, datetime):
        raise ValidationError({"observed_at": "Observation time must be a date and time."})
    if timezone.is_naive(value):
        raise ValidationError({"observed_at": "Observation time must include a timezone."})
    if value > timezone.now() + FUTURE_TOLERANCE:
        raise ValidationError({"observed_at": "Observation time cannot be in the future."})
    return value


def _price_confidence(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        confidence = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"confidence": "Confidence must be a number between 0 and 1."}) from exc
    if not confidence.is_finite() or not 0 <= confidence <= 1:
        raise ValidationError({"confidence": "Confidence must be between 0 and 1."})
    return confidence


def record_price(*, item_id, amount, currency="USD", merchant="", source_url="",
                 observed_at=None, actor, source="manual", confidence=None):
    """Record one dated manual/receipt price observation for a pantry item.

    The item's household is derived from the item and the actor's membership is
    enforced, so a price can never be written across households. No network call
    is made: the citation link is stored for reading, never fetched.
    """
    item = get_pantry_item(actor=actor, item_id=item_id)
    amount_value = _price_amount(amount)
    return record_inventory_price(
        actor=actor,
        household=item.household,
        item_id=item.pk,
        amount=amount_value,
        currency=_price_currency(currency),
        merchant=_price_merchant(merchant),
        source=_price_source(source),
        source_url=_price_source_url(source_url),
        observed_at=_price_observed_at(observed_at),
        confidence=_price_confidence(confidence),
    )


def _public_price(observation, stale_cutoff) -> dict:
    return {
        "id": str(observation.pk),
        "amount": str(observation.amount),
        "currency": observation.currency,
        "merchant": observation.merchant,
        "source": observation.source,
        "source_url": observation.source_url,
        "observed_at": observation.observed_at.isoformat(),
        "confidence": str(observation.confidence) if observation.confidence is not None else None,
        "verification_status": observation.verification_status,
        "is_stale": observation.observed_at < stale_cutoff,
    }


def price_history(*, actor, household, item_id) -> list[dict]:
    """Dated price observations, newest first, each flagging its own freshness."""
    stale_cutoff = timezone.now() - timedelta(days=_price_stale_days())
    return [
        _public_price(observation, stale_cutoff)
        for observation in list_price_observations(actor=actor, household=household, item_id=item_id)
    ]


def latest_price(*, actor, household, item_id) -> dict | None:
    """The most recent dated observation, or ``None`` when nothing was recorded."""
    history = price_history(actor=actor, household=household, item_id=item_id)
    return history[0] if history else None
