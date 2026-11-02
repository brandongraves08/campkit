import base64
import struct
import zlib
from decimal import Decimal

import pytest

from apps.intake.vision import CandidatePayload, EvidenceItem

# 1x1 transparent PNG. Used only as synthetic intake bytes in tests.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def intake_settings(tmp_path, settings):
    settings.INTAKE_UPLOAD_ROOT = tmp_path / "private-intake"
    settings.INTAKE_MAX_IMAGE_BYTES = 5 * 1024 * 1024
    settings.INTAKE_MAX_ATTEMPTS = 3
    settings.INTAKE_CONFIDENCE_THRESHOLDS = {
        "identity": 0.85,
        "quantity": 0.80,
        "nutrition": 0.90,
        "allergen": 0.95,
    }
    settings.INTAKE_VISION_CLIENT = None
    settings.CAMPKIT_HERMES_NOTIFY_URL = ""
    settings.CAMPKIT_HERMES_NOTIFY_TOKEN = ""
    return settings


@pytest.fixture
def make_payload():
    def _make(**overrides):
        values = {
            "name": "Trail mix",
            "kind": "pantry",
            "category": "Snacks",
            "brand": "CampCo",
            "model_number": "",
            "barcode": "0123456789012",
            "quantity": Decimal("2"),
            "unit": "each",
            "weight_grams": None,
            "nutrition": {"calories": 120, "protein_g": 4},
            "allergens": ["peanuts"],
            "identity_confidence": 0.95,
            "quantity_confidence": 0.93,
            "nutrition_confidence": 0.91,
            "allergen_confidence": 0.96,
            "evidence": (EvidenceItem("name", "Trail mix", "label", 0.95),),
            "ambiguous_fields": (),
            "provider": "fake-vision",
        }
        values.update(overrides)
        return CandidatePayload(**values)

    return _make


class FakeVisionClient:
    """Deterministic stand-in for the Hermes vision endpoint."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def analyze(self, image_bytes, *, barcode=""):
        self.calls.append({"image": image_bytes, "barcode": barcode})
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def fake_vision():
    return FakeVisionClient


@pytest.fixture
def use_vision(settings):
    def _use(client):
        settings.INTAKE_VISION_CLIENT = client
        return client

    return _use


@pytest.fixture
def png_photo():
    return PNG_1X1


def png_with_dimensions(width, height):
    """Minimal PNG header carrying attacker-controlled dimensions."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", crc)
    return b"\x89PNG\r\n\x1a\n" + chunk


@pytest.fixture
def oversized_png_factory():
    return png_with_dimensions
