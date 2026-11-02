"""Hermes product-research adapter.

The research provider is asked for dated, cited observations about a product
identity. It never receives household state, stock, or quantities, and this
module never scrapes a retailer page: it speaks one configured Hermes endpoint
and only accepts observations that arrive with an http(s) citation, a retrieval
time, and a confidence. Anything less is dropped rather than filled in.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils.dateparse import parse_datetime

from apps.integrations.types import (
    ProductIdentity,
    ProductResearcher,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    SourceObservation,
)

MAX_RESPONSE_BYTES = 256 * 1024
MAX_OBSERVATIONS = 25


class HermesResearchProvider:
    """Research provider backed by a configured Hermes research endpoint."""

    name = "hermes-research"

    def __init__(self, endpoint=None, token=None, timeout=None, transport=None):
        self.endpoint = (
            getattr(settings, "CAMPKIT_HERMES_RESEARCH_URL", "") if endpoint is None else endpoint
        )
        self._token = (
            getattr(settings, "CAMPKIT_HERMES_RESEARCH_TOKEN", "") if token is None else token
        )
        self.timeout = float(
            timeout if timeout is not None else getattr(settings, "CAMPKIT_HERMES_RESEARCH_TIMEOUT", 10.0)
        )
        self._transport = transport or urllib.request.urlopen

    def lookup(self, candidate: ProductIdentity) -> list[SourceObservation]:
        if not self.endpoint:
            raise ProviderNotConfigured(
                "Hermes research endpoint is not configured; product research is disabled."
            )
        if not isinstance(candidate, ProductIdentity):
            raise ProviderResponseError("Research lookups require a product identity.")
        body = json.dumps(
            {
                "name": candidate.name,
                "brand": candidate.brand,
                "barcode": candidate.barcode,
                "category": candidate.category,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        raw = self._send(request)
        return self._parse(raw)

    def _send(self, request) -> bytes:
        try:
            with self._transport(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Never chain: the request URL/headers must not reach a traceback.
            raise self._http_error(exc.code) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ProviderUnavailable("Hermes research provider is unavailable; retry later.") from None
        except Exception:
            raise ProviderUnavailable("Hermes research response could not be read; retry later.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderResponseError("Hermes research returned an oversized response.")
        return raw

    @staticmethod
    def _http_error(code: int):
        if code == 429:
            return ProviderRateLimited("Hermes research rate limit reached; retry later.")
        if code in (401, 403):
            return ProviderNotConfigured("Hermes research rejected the configured credentials.")
        if code >= 500:
            return ProviderUnavailable("Hermes research is unavailable; retry later.")
        return ProviderResponseError("Hermes research rejected the lookup request.")

    def _parse(self, raw: bytes) -> list[SourceObservation]:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProviderResponseError("Hermes research returned an unreadable response.") from None
        if not isinstance(data, dict) or not isinstance(data.get("observations"), list):
            raise ProviderResponseError("Hermes research response did not contain observations.")
        observations = []
        for entry in data["observations"][:MAX_OBSERVATIONS]:
            observation = self._observation(entry)
            if observation is not None:
                observations.append(observation)
        return observations

    def _observation(self, entry) -> SourceObservation | None:
        if not isinstance(entry, dict):
            return None
        retrieved_at = self._retrieved_at(entry.get("retrieved_at"))
        if retrieved_at is None:
            return None
        try:
            return SourceObservation(
                source=str(entry.get("source") or self.name),
                source_url=str(entry.get("source_url") or ""),
                retrieved_at=retrieved_at,
                confidence=entry.get("confidence"),
                title=str(entry.get("title") or ""),
                summary=str(entry.get("summary") or ""),
            )
        except ValueError:
            # Missing citation, unusable confidence, or an unsafe URL: drop the
            # entry instead of inventing a plausible value.
            return None

    @staticmethod
    def _retrieved_at(value) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else None
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = parse_datetime(value.strip())
        if parsed is None or parsed.tzinfo is None:
            return None
        return parsed
