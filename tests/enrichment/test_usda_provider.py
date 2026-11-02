"""USDA FoodData Central adapter contract: positive, missing, and rate-limit responses."""

import json
import urllib.error
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.enrichment.usda import UsdaNutritionProvider
from apps.integrations.types import (
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)

FDC_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
API_KEY = "usda-secret-value"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, size=-1):
        return self.body if size in (-1, None) else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Transporter:
    """Records each request the adapter would send; never touches the network."""

    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({"request": request, "timeout": timeout})
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode("utf-8")
        return FakeResponse(raw)

    @property
    def request(self):
        return self.requests[0]["request"]

    @property
    def payload(self):
        return json.loads(self.request.data.decode("utf-8"))


def branded_payload(nutrients=None):
    return {
        "totalHits": 1,
        "foods": [
            {
                "fdcId": 2039158,
                "description": "TRAIL MIX",
                "brandOwner": "CampCo Foods",
                "brandName": "CampCo",
                "gtinUpc": "0123456789012",
                "dataType": "Branded",
                "foodNutrients": nutrients
                if nutrients is not None
                else [
                    {"nutrientName": "Energy", "unitName": "kJ", "value": 418.4},
                    {"nutrientName": "Protein", "unitName": "g", "value": 3.5},
                    {"nutrientName": "Sodium, Na", "unitName": "mg", "value": 140},
                    {"nutrientName": "Ash", "unitName": "g", "value": 1.2},
                ],
            }
        ],
    }


def provider(transporter, api_key=API_KEY):
    return UsdaNutritionProvider(api_key=api_key, transport=transporter)


def http_error(code, message="error"):
    return urllib.error.HTTPError(FDC_URL, code, message, {}, None)


def test_branded_result_is_normalized_with_citation():
    transporter = Transporter(branded_payload())

    candidates = provider(transporter).lookup("0123456789012")

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provider == "usda-fooddata-central"
    assert candidate.external_id == "2039158"
    assert candidate.name == "TRAIL MIX"
    assert candidate.brand == "CampCo Foods"
    assert candidate.barcode == "0123456789012"
    assert candidate.confidence == Decimal("0.900")
    assert candidate.retrieved_at is not None and candidate.retrieved_at.tzinfo is not None
    assert "2039158" in candidate.source_url
    assert candidate.source_url.startswith("https://fdc.nal.usda.gov/")
    nutrients = {item.nutrient: (str(item.amount), item.unit) for item in candidate.nutrients}
    assert nutrients == {
        "energy_kcal": ("100.000", "kcal"),
        "protein_g": ("3.500", "g"),
        "sodium_mg": ("140.000", "mg"),
    }


def test_query_is_sent_in_the_documented_search_body():
    transporter = Transporter(branded_payload())

    provider(transporter).lookup("trail mix")

    assert transporter.request.full_url.startswith(FDC_URL)
    assert transporter.request.method == "POST"
    assert transporter.payload["query"] == "trail mix"
    assert transporter.payload["dataType"] == ["Branded"]


def test_blank_query_makes_no_request():
    transporter = Transporter(branded_payload())

    assert provider(transporter).lookup("   ") == []
    assert transporter.requests == []


def test_missing_results_return_an_empty_list():
    transporter = Transporter({"totalHits": 0, "foods": []})

    assert provider(transporter).lookup("unknown barcode") == []


def test_entries_without_a_description_are_skipped():
    payload = branded_payload()
    payload["foods"][0]["description"] = "   "
    transporter = Transporter(payload)

    assert provider(transporter).lookup("0123456789012") == []


def test_unknown_or_unconvertible_nutrients_are_not_invented():
    payload = branded_payload(
        nutrients=[
            {"nutrientName": "Ash", "unitName": "g", "value": 1.2},
            {"nutrientName": "Protein", "unitName": "IU", "value": 400},
            {"nutrientName": "Protein", "unitName": "g", "value": "not a number"},
            {"nutrientName": "Protein", "unitName": "g", "value": -1},
        ]
    )
    transporter = Transporter(payload)

    candidates = provider(transporter).lookup("0123456789012")

    assert candidates[0].nutrients == ()


def test_nutrients_without_a_value_are_omitted():
    payload = branded_payload(
        nutrients=[
            {"nutrientName": "Protein", "unitName": "g"},
            {"nutrientName": "Energy", "unitName": "kcal", "value": 120},
        ]
    )
    candidates = provider(Transporter(payload)).lookup("0123456789012")

    assert [item.nutrient for item in candidates[0].nutrients] == ["energy_kcal"]


def test_rate_limit_response_is_a_typed_error():
    transporter = Transporter(error=http_error(429, "Too Many Requests"))

    with pytest.raises(ProviderRateLimited):
        provider(transporter).lookup("0123456789012")


def test_server_error_is_a_retryable_unavailable_error():
    transporter = Transporter(error=http_error(503))

    with pytest.raises(ProviderUnavailable):
        provider(transporter).lookup("0123456789012")


def test_transport_failure_is_a_retryable_unavailable_error():
    transporter = Transporter(error=urllib.error.URLError("dns failure"))

    with pytest.raises(ProviderUnavailable):
        provider(transporter).lookup("0123456789012")


def test_invalid_key_response_does_not_become_a_candidate():
    transporter = Transporter(error=http_error(403, "Forbidden"))

    with pytest.raises(ProviderNotConfigured):
        provider(transporter).lookup("0123456789012")


def test_unreadable_payload_is_a_response_error():
    transporter = Transporter(b"<html>not json</html>")

    with pytest.raises(ProviderResponseError):
        provider(transporter).lookup("0123456789012")


def test_payload_without_foods_is_a_response_error():
    transporter = Transporter({"unexpected": []})

    with pytest.raises(ProviderResponseError):
        provider(transporter).lookup("0123456789012")


def test_oversized_response_is_rejected_without_parsing():
    transporter = Transporter(b"{" + b"0" * (512 * 1024))

    with pytest.raises(ProviderResponseError):
        provider(transporter).lookup("0123456789012")


def test_missing_api_key_never_reaches_the_network():
    def explode(request, timeout=None):
        raise AssertionError("no network call is allowed without a configured key")

    with pytest.raises(ProviderNotConfigured):
        UsdaNutritionProvider(api_key="", transport=explode).lookup("0123456789012")


def test_credentials_never_leak_into_errors_or_citations():
    transporter = Transporter(error=http_error(429))
    with pytest.raises(ProviderRateLimited) as excinfo:
        provider(transporter).lookup("0123456789012")
    assert API_KEY not in str(excinfo.value)

    candidate = provider(Transporter(branded_payload())).lookup("0123456789012")[0]
    assert API_KEY not in candidate.source_url
    assert API_KEY not in repr(candidate)


def test_api_key_is_read_from_settings(settings):
    settings.CAMPKIT_USDA_API_KEY = "settings-key"

    assert UsdaNutritionProvider()._api_key == "settings-key"


def test_candidates_carry_a_retrieval_time_no_earlier_than_the_call_started():
    started = timezone.now()

    candidate = provider(Transporter(branded_payload())).lookup("0123456789012")[0]

    assert candidate.retrieved_at >= started
