"""USDA FoodData Central nutrition adapter.

Only this module knows the FoodData Central HTTP contract. The API key is read
from the app setting ``CAMPKIT_USDA_API_KEY`` (a server secret), is never
written to code, logs, citations, or exception messages, and no live call is
made unless a real key is configured.

Failure discipline follows the intake provider boundary: every transport or
shape problem becomes a typed :mod:`apps.integrations.types` error so callers
can retry or fall back to a stale cache, and no payload is ever fabricated.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.integrations.types import (
    MAX_NUTRIENTS,
    NutritionCandidate,
    NutritionProvider,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    normalize_nutrient,
)

DEFAULT_BASE_URL = "https://api.nal.usda.gov/fdc/v1"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_FOODS = 10
MAX_QUERY = 120
# A barcode match is a strong identity match; a free-text search is not. Both
# stay well below 1.0 so a sourced value is never presented as verified.
BARCODE_CONFIDENCE = Decimal("0.900")
TEXT_CONFIDENCE = Decimal("0.500")
FDC_DETAILS_URL = "https://fdc.nal.usda.gov/fdc-app.html#/food-details/{fdc_id}/nutrients"


class UsdaNutritionProvider:
    """Nutrition provider backed by the FoodData Central ``/foods/search`` API."""

    name = "usda-fooddata-central"

    def __init__(self, api_key=None, base_url=None, timeout=None, transport=None):
        self._api_key = getattr(settings, "CAMPKIT_USDA_API_KEY", "") if api_key is None else api_key
        self.base_url = (
            base_url or getattr(settings, "CAMPKIT_USDA_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = float(
            timeout if timeout is not None else getattr(settings, "CAMPKIT_USDA_TIMEOUT", 10.0)
        )
        self._transport = transport or urllib.request.urlopen

    def lookup(self, barcode_or_query: str) -> list[NutritionCandidate]:
        query = str(barcode_or_query or "").strip()[:MAX_QUERY]
        if not query:
            return []
        if not self._api_key:
            raise ProviderNotConfigured(
                "USDA FoodData Central API key is not configured; nutrition lookup is disabled."
            )
        body = json.dumps(
            {"query": query, "pageSize": MAX_FOODS, "dataType": ["Branded"]}
        ).encode("utf-8")
        # FoodData Central documents the key as the ``api_key`` query parameter.
        # This module never logs, stores, or re-raises the request URL, so the key
        # cannot reach a citation, a household-visible field, or an error message.
        url = f"{self.base_url}/foods/search?api_key={urllib.parse.quote(str(self._api_key), safe='')}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        raw = self._send(request)
        return self._parse(raw, query=query)

    def _send(self, request) -> bytes:
        try:
            with self._transport(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Do not chain: ``HTTPError`` carries the request URL (and therefore
            # the API key) in its repr.
            raise self._http_error(exc.code) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ProviderUnavailable(
                "USDA FoodData Central could not be reached; retry the lookup later."
            ) from None
        except Exception:
            raise ProviderUnavailable(
                "USDA FoodData Central response could not be read; retry the lookup later."
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderResponseError("USDA FoodData Central returned an oversized response.")
        return raw

    @staticmethod
    def _http_error(code: int):
        if code == 429:
            return ProviderRateLimited(
                "USDA FoodData Central rate limit reached; retry the lookup later."
            )
        if code in (401, 403):
            return ProviderNotConfigured(
                "USDA FoodData Central rejected the configured API key."
            )
        if code >= 500:
            return ProviderUnavailable(
                "USDA FoodData Central is unavailable; retry the lookup later."
            )
        return ProviderResponseError(
            "USDA FoodData Central rejected the lookup request."
        )

    def _parse(self, raw: bytes, *, query: str) -> list[NutritionCandidate]:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProviderResponseError(
                "USDA FoodData Central returned an unreadable response."
            ) from None
        if not isinstance(data, dict) or not isinstance(data.get("foods"), list):
            raise ProviderResponseError(
                "USDA FoodData Central response did not contain a food list."
            )
        retrieved_at = timezone.now()
        candidates = []
        for food in data["foods"][:MAX_FOODS]:
            candidate = self._candidate(food, query=query, retrieved_at=retrieved_at)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _candidate(self, food, *, query: str, retrieved_at) -> NutritionCandidate | None:
        """Build one candidate, or ``None`` when the record is not trustworthy.

        A record without a description or a numeric FoodData Central id is
        skipped rather than filled with a placeholder; unknown or unconvertible
        nutrients are dropped instead of stored under an invented name.
        """
        if not isinstance(food, dict):
            return None
        name = str(food.get("description") or "").strip()
        fdc_id = str(food.get("fdcId") or "").strip()
        if not name or not fdc_id.isdigit():
            return None
        nutrients = []
        seen = set()
        for entry in (food.get("foodNutrients") or [])[:MAX_NUTRIENTS]:
            if not isinstance(entry, dict):
                continue
            value = normalize_nutrient(entry.get("nutrientName"), entry.get("value"), entry.get("unitName"))
            if value is None or value.nutrient in seen:
                continue
            seen.add(value.nutrient)
            nutrients.append(value)
        barcode = str(food.get("gtinUpc") or "").strip()
        confidence = BARCODE_CONFIDENCE if barcode and barcode == query else TEXT_CONFIDENCE
        return NutritionCandidate(
            provider=self.name,
            external_id=fdc_id,
            name=name,
            brand=str(food.get("brandOwner") or food.get("brandName") or ""),
            barcode=barcode,
            data_type=str(food.get("dataType") or ""),
            nutrients=tuple(nutrients),
            source_url=FDC_DETAILS_URL.format(fdc_id=fdc_id),
            retrieved_at=retrieved_at,
            confidence=confidence,
        )
