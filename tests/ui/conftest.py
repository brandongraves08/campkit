from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.intake.models import IntakeCandidate, IntakeSubmission, VerificationRequest


@pytest.fixture
def gear_data(household):
    return {
        "household": household,
        "name": "Basecamp tent",
        "category": "Shelter",
        "owner": "Sam",
        "condition": "good",
        "weight_grams": 2100,
        "quantity": 1,
        "storage_location": "Garage shelf",
        "availability": "available",
        "source": "Manufacturer label",
        "source_url": "https://example.test/tent",
    }


@pytest.fixture
def pantry_data(household):
    return {
        "household": household,
        "name": "Trail mix",
        "quantity": Decimal("500"),
        "unit": "g",
        "expiry_date": date.today() + timedelta(days=30),
        "storage_location": "Pantry bin",
        "source": "Package label",
        "source_url": "https://example.test/trail-mix",
    }


@pytest.fixture
def trip_data():
    return {
        "name": "Ridge weekend",
        "destination": "Blue Ridge",
        "start_date": date.today() + timedelta(days=3),
        "end_date": date.today() + timedelta(days=5),
        "party_size": 3,
        "conditions": "Cold nights, possible rain",
    }


@pytest.fixture
def pending_review(household, owner):
    """A barcode intake that landed in the human verification queue."""
    submission = IntakeSubmission.objects.create(
        household=household,
        submitted_by=owner,
        kind="pantry",
        channel="barcode",
        barcode="0123456789012",
        dedupe_key="ui-verification-fixture",
        status="needs_review",
    )
    IntakeCandidate.objects.create(
        submission=submission,
        name="Instant noodles",
        quantity=Decimal("2"),
        unit="each",
        provider="test-vision",
        overall_confidence=Decimal("0.510"),
        identity_confidence=Decimal("0.520"),
    )
    request = VerificationRequest.objects.create(
        household=household,
        submission=submission,
        created_by=owner,
        reason="Quantity confidence below the household threshold",
        field_names=["quantity"],
    )
    return submission, request
