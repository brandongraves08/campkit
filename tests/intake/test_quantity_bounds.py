"""Regression tests: provider-legal quantities inventory cannot store.

``VisionClient`` accepts any ``Decimal`` in 0..1,000,000, but gear stock is a
whole-number count and pantry stock stores three decimal places. Such payloads
must route to human review (never crash processing, never auto-add, never
strand the submission in ``PROCESSING``).
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.intake.models import (
    IntakeStatus,
    VerificationRequest,
    VerificationRequestStatus,
)
from apps.intake.services import process_submission, resolve_intake, submit_intake
from apps.inventory.models import GearItem, InventoryVerification, PantryItem


def _submit(actor, household, image, kind="pantry"):
    submission, _created = submit_intake(
        actor=actor,
        household=household,
        kind=kind,
        channel="photo",
        image_bytes=image,
        declared_content_type="image/png",
    )
    return submission


def _assert_never_processing(submission):
    submission.refresh_from_db()
    assert submission.status != IntakeStatus.PROCESSING
    assert submission.status != IntakeStatus.PENDING


@pytest.mark.django_db
def test_fractional_gear_quantity_routes_to_review_instead_of_crashing(
    owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(
        kind="gear", quantity=Decimal("1.5"), unit="", nutrition={}, allergens=(),
        nutrition_confidence=0.0, allergen_confidence=0.0,
    )))
    submission = _submit(owner, household, png_photo, kind="gear")

    submission = process_submission(submission.pk)  # must not raise

    _assert_never_processing(submission)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert submission.processed_at is not None
    assert GearItem.objects.count() == 0

    request = VerificationRequest.objects.get(
        submission=submission, status=VerificationRequestStatus.OPEN
    )
    assert "quantity" in request.field_names
    assert "whole number" in request.reason

    # The candidate stays available so a human can correct the count.
    candidate = submission.candidate
    assert candidate.quantity == Decimal("1.500")
    raw = candidate.field_evidence.get(field_name="quantity")
    assert raw.value == "1.5"  # unrounded provider value survives for the reviewer


@pytest.mark.django_db
def test_pantry_quantity_beyond_three_decimals_routes_to_review(
    owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(quantity=Decimal("2.5001"))))
    submission = _submit(owner, household, png_photo)

    submission = process_submission(submission.pk)  # must not raise

    _assert_never_processing(submission)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0

    request = VerificationRequest.objects.get(
        submission=submission, status=VerificationRequestStatus.OPEN
    )
    assert "quantity" in request.field_names
    assert "three decimal places" in request.reason

    candidate = submission.candidate
    assert candidate.quantity == Decimal("2.500")
    assert candidate.field_evidence.get(field_name="quantity").value == "2.5001"

    # A reviewed submission is stable: retrying the job neither crashes nor adds stock.
    again = process_submission(submission.pk)
    assert again.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0


@pytest.mark.django_db
def test_representable_fractional_pantry_quantity_still_auto_adds(
    owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(quantity=Decimal("2.5"))))
    submission = _submit(owner, household, png_photo)
    submission = process_submission(submission.pk)

    assert submission.status == IntakeStatus.ADDED
    item = PantryItem.objects.get(pk=submission.created_item_id)
    assert item.quantity == Decimal("2.5")
    assert item.verification_status == InventoryVerification.ESTIMATED


@pytest.mark.django_db
def test_upload_view_survives_a_provider_legal_fractional_quantity(
    client, owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(
        kind="gear", quantity=Decimal("1.5"), unit="", nutrition={}, allergens=(),
        nutrition_confidence=0.0, allergen_confidence=0.0,
    )))
    client.force_login(owner)

    response = client.post(reverse("intake:create"), {
        "kind": "gear",
        "channel": "photo",
        "image": SimpleUploadedFile("gear.png", png_photo, content_type="image/png"),
    })

    assert response.status_code == 302
    assert GearItem.objects.count() == 0
    reviewed = VerificationRequest.objects.get(status=VerificationRequestStatus.OPEN)
    assert reviewed.submission.status == IntakeStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_inventory_validation_error_never_strands_a_submission(
    owner, household, png_photo, use_vision, fake_vision, make_payload, monkeypatch
):
    """Defence in depth: the inventory layer itself rejecting a value is reviewable."""
    use_vision(fake_vision(payload=make_payload()))

    def rejecting_create_pantry_item(**kwargs):
        raise ValidationError({"quantity": "Ensure that there are no more than 3 decimal places."})

    monkeypatch.setattr("apps.intake.services.create_pantry_item", rejecting_create_pantry_item)
    submission = _submit(owner, household, png_photo)

    submission = process_submission(submission.pk)  # must not raise

    _assert_never_processing(submission)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert "decimal places" in submission.failure_reason
    assert PantryItem.objects.count() == 0
    request = VerificationRequest.objects.get(
        submission=submission, status=VerificationRequestStatus.OPEN
    )
    assert {"identity", "quantity"}.issubset(set(request.field_names))

    # Manual correction remains a working recovery path out of review.
    monkeypatch.undo()  # restore the real inventory service for the recovery step
    submission = resolve_intake(
        submission.pk,
        actor=owner,
        decision={"action": "correct",
                  "corrections": {"name": "Hand-entered mix", "quantity": "3", "unit": "each"}},
    )
    assert submission.status == IntakeStatus.ADDED
    assert PantryItem.objects.get(pk=submission.created_item_id).name == "Hand-entered mix"


@pytest.mark.django_db
def test_unexpected_processing_error_still_reaches_human_review(
    owner, household, png_photo, use_vision, fake_vision, make_payload, monkeypatch
):
    use_vision(fake_vision(payload=make_payload()))

    def exploding_create_pantry_item(**kwargs):
        raise RuntimeError("inventory backend exploded")

    monkeypatch.setattr("apps.intake.services.create_pantry_item", exploding_create_pantry_item)
    submission = _submit(owner, household, png_photo)

    submission = process_submission(submission.pk)  # safety net: never strands the row

    _assert_never_processing(submission)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert "RuntimeError" in submission.failure_reason
    assert PantryItem.objects.count() == 0
    assert VerificationRequest.objects.filter(
        submission=submission, status=VerificationRequestStatus.OPEN
    ).exists()


@pytest.mark.django_db
def test_gear_manual_correction_requires_a_whole_number(
    owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(
        kind="gear", quantity=Decimal("1.5"), unit="", nutrition={}, allergens=(),
        nutrition_confidence=0.0, allergen_confidence=0.0,
    )))
    submission = _submit(owner, household, png_photo, kind="gear")
    submission = process_submission(submission.pk)

    with pytest.raises(ValidationError) as excinfo:
        resolve_intake(submission.pk, actor=owner, decision={
            "action": "correct", "corrections": {"quantity": "1.5"},
        })
    assert "whole numbers" in str(excinfo.value)
    assert GearItem.objects.count() == 0

    submission = resolve_intake(submission.pk, actor=owner, decision={
        "action": "correct", "corrections": {"quantity": "2"},
    })
    assert submission.status == IntakeStatus.ADDED
    item = GearItem.objects.get(pk=submission.created_item_id)
    assert item.quantity == 2
    assert item.verification_status == InventoryVerification.CONFIRMED


@pytest.mark.django_db
def test_pantry_manual_correction_requires_three_decimal_places(
    owner, household, png_photo, use_vision, fake_vision, make_payload
):
    use_vision(fake_vision(payload=make_payload(quantity=Decimal("2.5001"))))
    submission = _submit(owner, household, png_photo)
    submission = process_submission(submission.pk)

    with pytest.raises(ValidationError) as excinfo:
        resolve_intake(submission.pk, actor=owner, decision={
            "action": "correct", "corrections": {"quantity": "2.5001"},
        })
    assert "three decimal places" in str(excinfo.value)
    assert PantryItem.objects.count() == 0
