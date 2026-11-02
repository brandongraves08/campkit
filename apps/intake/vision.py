"""Replaceable vision provider boundary.

Everything provider-specific (HTTP, credentials, response shape) lives here.
Domain services only see the :class:`VisionClient` protocol and validated
:class:`CandidatePayload` values, so a failed or different provider can never
bypass intake review or invent candidate fields.
"""

import base64
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

MAX_RESPONSE_BYTES = 256 * 1024
MAX_NAME = 160
MAX_CATEGORY = 80
MAX_BRAND = 120
MAX_MODEL = 120
MAX_BARCODE = 64
MAX_UNIT = 20
MAX_WEIGHT_GRAMS = 500_000
MAX_QUANTITY = Decimal("1000000")
MAX_ALLERGENS = 32
MAX_NUTRITION_KEYS = 32
MAX_ATTRIBUTE_KEYS = 32
MAX_EVIDENCE = 32

FIELD_NAMES = ("name", "category", "brand", "model_number", "barcode", "quantity", "unit",
               "weight_grams", "nutrition", "allergens", "attributes")
CRITICAL_FIELDS = ("identity", "quantity", "nutrition", "allergen")


class VisionError(Exception):
    """A retryable provider failure. Never carries credentials or image data."""


def _text(value, limit, *, allow_empty=True, strict=False) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not allow_empty and not text:
        raise ValueError("Required text field is empty.")
    if len(text) > limit:
        if strict:
            raise ValueError(f"Text field exceeds {limit} characters.")
        text = text[:limit]
    return text


def _confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Confidence must be a number between 0 and 1.") from exc
    if math.isnan(number) or math.isinf(number) or not 0.0 <= number <= 1.0:
        raise ValueError("Confidence must be a number between 0 and 1.")
    return number


def _quantity(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Quantity must be a decimal number.") from exc
    if not number.is_finite() or number < 0 or number > MAX_QUANTITY:
        raise ValueError("Quantity must be between 0 and 1,000,000.")
    return number


def _weight(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Weight must be an integer number of grams.") from exc
    if not 0 <= number <= MAX_WEIGHT_GRAMS:
        raise ValueError("Weight is outside the accepted range.")
    return number


def _string_list(value, *, limit_count, max_length) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("Expected a list of short text values.")
    if len(value) > limit_count:
        raise ValueError("Too many list entries in the candidate payload.")
    return tuple(_text(item, max_length, allow_empty=False) for item in value)


def _mapping(value, *, max_keys) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected a mapping in the candidate payload.")
    if len(value) > max_keys:
        raise ValueError("Too many mapping entries in the candidate payload.")
    cleaned = {}
    for key, item in value.items():
        key = _text(key, 64, allow_empty=False)
        if isinstance(item, (dict, list, tuple)):
            raise ValueError("Nested candidate values are not supported.")
        if isinstance(item, bool) or not isinstance(item, (str, int, float, Decimal)):
            raise ValueError("Candidate values must be short scalars.")
        cleaned[key] = str(item)[:255] if not isinstance(item, (int, float)) else item
    return cleaned


@dataclass(frozen=True)
class EvidenceItem:
    field_name: str
    value: object
    source: str = "vision"
    confidence: float = 0.0
    source_url: str = ""

    def __post_init__(self):
        if self.field_name not in FIELD_NAMES:
            raise ValueError("Unsupported evidence field.")
        _confidence(self.confidence)
        object.__setattr__(self, "source", _text(self.source, 64, allow_empty=False))
        object.__setattr__(self, "source_url", _safe_url(self.source_url))


def _safe_url(value) -> str:
    text = _text(value, 255)
    if not text:
        return ""
    lowered = text.lower()
    if not lowered.startswith(("https://", "http://")):
        return ""
    return text


@dataclass(frozen=True)
class CandidatePayload:
    """Validated provider output for one submitted photo/barcode.

    ``analyze(image_bytes) -> CandidatePayload`` is the only provider contract;
    the optional ``barcode`` keyword lets a barcode-only submission reuse it.
    """

    name: str
    kind: str = "pantry"
    category: str = ""
    brand: str = ""
    model_number: str = ""
    barcode: str = ""
    quantity: Decimal = Decimal("1")
    unit: str = ""
    weight_grams: int | None = None
    nutrition: dict = field(default_factory=dict)
    allergens: tuple[str, ...] = ()
    attributes: dict = field(default_factory=dict)
    identity_confidence: float = 0.0
    quantity_confidence: float = 0.0
    nutrition_confidence: float = 0.0
    allergen_confidence: float = 0.0
    evidence: tuple[EvidenceItem, ...] = ()
    ambiguous_fields: tuple[str, ...] = ()
    provider: str = "hermes-vision"
    model_version: str = ""

    def __post_init__(self):
        if self.kind not in {"gear", "pantry"}:
            raise ValueError("Candidate kind must be 'gear' or 'pantry'.")
        object.__setattr__(self, "name", _text(self.name, MAX_NAME, allow_empty=False, strict=True))
        object.__setattr__(self, "category", _text(self.category, MAX_CATEGORY, strict=True))
        object.__setattr__(self, "brand", _text(self.brand, MAX_BRAND, strict=True))
        object.__setattr__(self, "model_number", _text(self.model_number, MAX_MODEL, strict=True))
        object.__setattr__(self, "barcode", _text(self.barcode, MAX_BARCODE, strict=True))
        object.__setattr__(self, "unit", _text(self.unit, MAX_UNIT, strict=True))
        object.__setattr__(self, "provider", _text(self.provider, 80, allow_empty=False, strict=True))
        object.__setattr__(self, "model_version", _text(self.model_version, 80, strict=True))
        object.__setattr__(self, "quantity", _quantity(self.quantity))
        object.__setattr__(self, "weight_grams", _weight(self.weight_grams))
        object.__setattr__(self, "nutrition", _mapping(self.nutrition, max_keys=MAX_NUTRITION_KEYS))
        object.__setattr__(self, "attributes", _mapping(self.attributes, max_keys=MAX_ATTRIBUTE_KEYS))
        object.__setattr__(self, "allergens", _string_list(self.allergens, limit_count=MAX_ALLERGENS,
                                                           max_length=80))
        for name in ("identity_confidence", "quantity_confidence", "nutrition_confidence",
                     "allergen_confidence"):
            object.__setattr__(self, name, _confidence(getattr(self, name)))
        if self.evidence in (None, ""):
            object.__setattr__(self, "evidence", ())
        elif not isinstance(self.evidence, (list, tuple)):
            raise ValueError("Evidence must be a sequence of EvidenceItem values.")
        elif len(self.evidence) > MAX_EVIDENCE:
            raise ValueError("Too many evidence entries.")
        else:
            for item in self.evidence:
                if not isinstance(item, EvidenceItem):
                    raise ValueError("Evidence must be EvidenceItem values.")
            object.__setattr__(self, "evidence", tuple(self.evidence))
        ambiguous = _string_list(self.ambiguous_fields, limit_count=4, max_length=16)
        for name in ambiguous:
            if name not in CRITICAL_FIELDS:
                raise ValueError("Ambiguous field names must be identity, quantity, nutrition, or allergen.")
        object.__setattr__(self, "ambiguous_fields", ambiguous)

    @classmethod
    def from_mapping(cls, data: dict) -> "CandidatePayload":
        if not isinstance(data, dict):
            raise ValueError("Vision response must be a JSON object.")
        evidence = []
        for item in data.get("evidence") or ():
            if not isinstance(item, dict):
                raise ValueError("Vision evidence entries must be objects.")
            evidence.append(EvidenceItem(
                field_name=item.get("field_name", ""),
                value=item.get("value"),
                source=item.get("source", "vision"),
                confidence=item.get("confidence", 0.0),
                source_url=item.get("source_url", ""),
            ))
        return cls(
            name=data.get("name", ""),
            kind=data.get("kind", "pantry"),
            category=data.get("category", ""),
            brand=data.get("brand", ""),
            model_number=data.get("model_number", ""),
            barcode=data.get("barcode", ""),
            quantity=data.get("quantity", 1),
            unit=data.get("unit", ""),
            weight_grams=data.get("weight_grams"),
            nutrition=data.get("nutrition") or {},
            allergens=data.get("allergens") or (),
            attributes=data.get("attributes") or {},
            identity_confidence=data.get("identity_confidence", 0.0),
            quantity_confidence=data.get("quantity_confidence", 0.0),
            nutrition_confidence=data.get("nutrition_confidence", 0.0),
            allergen_confidence=data.get("allergen_confidence", 0.0),
            evidence=tuple(evidence),
            ambiguous_fields=data.get("ambiguous_fields") or (),
            provider=data.get("provider", "hermes-vision"),
            model_version=data.get("model_version", ""),
        )


class VisionClient(Protocol):
    def analyze(self, image_bytes: bytes, *, barcode: str = "") -> CandidatePayload:
        """Extract a bounded candidate payload from image bytes."""
        ...


class HermesVisionClient:
    """Adapter for the configured Hermes vision endpoint.

    Endpoint and token come from CampKit environment settings; the Hermes
    runtime configuration is never read or modified.
    """

    def __init__(self, endpoint: str = "", token: str = "", timeout: float = 10.0):
        self.endpoint = endpoint
        self._token = token
        self.timeout = timeout

    def analyze(self, image_bytes: bytes, *, barcode: str = "") -> CandidatePayload:
        if not self.endpoint:
            raise VisionError("Vision provider endpoint is not configured.")
        body = json.dumps({
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "barcode": barcode,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise VisionError("Vision provider request failed; retry the intake later.") from exc
        except Exception as exc:  # malformed transport responses must stay retryable, never crash a worker
            raise VisionError("Vision provider response could not be read; retry the intake later.") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise VisionError("Vision provider returned an oversized response.")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise VisionError("Vision provider returned an unreadable response.") from exc
        try:
            return CandidatePayload.from_mapping(payload)
        except ValueError as exc:
            raise VisionError("Vision provider returned an invalid candidate payload.") from exc


def get_vision_client() -> VisionClient:
    configured = getattr(settings, "INTAKE_VISION_CLIENT", None)
    if configured:
        if isinstance(configured, str):
            return import_string(configured)()
        return configured
    return HermesVisionClient(
        endpoint=getattr(settings, "CAMPKIT_VISION_ENDPOINT", ""),
        token=getattr(settings, "CAMPKIT_VISION_TOKEN", ""),
        timeout=float(getattr(settings, "CAMPKIT_VISION_TIMEOUT", 10.0)),
    )
