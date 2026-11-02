from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.intake.models import (
    IntakeStatus,
    VerificationRequest,
    VerificationRequestStatus,
)
from apps.intake.services import (
    effective_threshold,
    process_submission,
    resolve_intake,
    submit_intake,
)
from apps.intake.tasks import deliver_hermes_followup
from apps.intake.vision import VisionError
from apps.inventory.models import InventoryVerification, PantryItem


def _upload(owner, household, image):
    submission, _ = submit_intake(
        actor=owner,
        household=household,
        kind="pantry",
        channel="photo",
        image_bytes=image,
        declared_content_type="image/png",
    )
    return submission


@pytest.mark.django_db
def test_high_confidence_intake_adds_estimated_item_with_field_evidence(owner, household, png_photo,
                                                                        use_vision, fake_vision, make_payload):
    client = use_vision(fake_vision(payload=make_payload()))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)

    assert submission.status == IntakeStatus.ADDED
    assert client.calls and client.calls[0]["image"] == png_photo
    candidate = submission.candidate
    assert candidate.name == "Trail mix"
    assert candidate.quantity == Decimal("2")
    assert {row.field_name for row in candidate.field_evidence.all()} >= {
        "name", "category", "brand", "barcode", "quantity", "unit", "nutrition", "allergens",
    }
    evidence = candidate.field_evidence.get(field_name="quantity")
    assert evidence.source == "label" or evidence.confidence == Decimal("0.930")
    item = PantryItem.objects.get(pk=submission.created_item_id)
    assert item.verification_status == InventoryVerification.ESTIMATED
    assert item.verified_by is None and item.confirmed_quantity == 0
    assert item.source and item.confidence is not None
    assert not VerificationRequest.objects.filter(submission=submission).exists()


@pytest.mark.django_db
def test_ambiguous_identity_goes_to_review_with_app_and_hermes_request(client, owner, household, png_photo,
                                                                        use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(identity_confidence=0.42)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)

    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0
    request = VerificationRequest.objects.get(submission=submission, status=VerificationRequestStatus.OPEN)
    assert "identity" in request.field_names
    assert request.hermes_status == "pending"
    assert request.hermes_payload["request_id"] == str(request.pk)
    assert request.hermes_payload["household_id"] == str(household.pk)

    client.force_login(owner)
    response = client.get(reverse("intake:detail", args=[submission.pk]))
    assert response.status_code == 200
    assert request.reason.encode() in response.content
    assert b"Needs review" in response.content


@pytest.mark.django_db
def test_configured_threshold_cannot_lower_the_review_floor(owner, household, png_photo, settings,
                                                             use_vision, fake_vision, make_payload):
    settings.INTAKE_CONFIDENCE_THRESHOLDS = {
        "identity": 0.0, "quantity": 0.0, "nutrition": 0.0, "allergen": 0.0,
    }
    assert effective_threshold("identity") > 0.0
    use_vision(fake_vision(payload=make_payload(
        identity_confidence=0.20,
        quantity_confidence=0.20,
        nutrition_confidence=0.20,
        allergen_confidence=0.20,
    )))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0


@pytest.mark.django_db
def test_flagged_ambiguous_allergen_blocks_automatic_add(owner, household, png_photo, use_vision,
                                                         fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(ambiguous_fields=("allergen",))))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0
    assert "allergen" in VerificationRequest.objects.get(submission=submission).field_names


@pytest.mark.django_db
def test_uncertain_quantity_blocks_automatic_add(owner, household, png_photo, use_vision,
                                                  fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(quantity=Decimal("0"), quantity_confidence=0.40)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert "quantity" in VerificationRequest.objects.get(submission=submission).field_names


@pytest.mark.django_db
def test_missing_nutrition_still_adds_estimated_item_but_opens_followup(owner, household, png_photo,
                                                                         use_vision, fake_vision,
                                                                         make_payload):
    use_vision(fake_vision(payload=make_payload(nutrition={}, allergens=[], nutrition_confidence=0.0,
                                                allergen_confidence=0.0)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    request = VerificationRequest.objects.get(submission=submission, status=VerificationRequestStatus.OPEN)
    assert {"nutrition", "allergen"}.issubset(set(request.field_names))


@pytest.mark.django_db
def test_provider_failure_stays_retryable_then_succeeds(owner, household, png_photo, use_vision,
                                                         fake_vision, make_payload):
    use_vision(fake_vision(error=VisionError("vision endpoint timed out")))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.PENDING
    assert submission.attempts == 1
    assert "timed out" in submission.failure_reason
    assert not PantryItem.objects.exists()

    use_vision(fake_vision(payload=make_payload()))
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    assert submission.attempts == 2
    assert PantryItem.objects.count() == 1


@pytest.mark.django_db
def test_provider_failure_exhausts_attempts_and_falls_back_to_manual_entry(owner, household, png_photo,
                                                                           settings, use_vision,
                                                                           fake_vision, make_payload):
    settings.INTAKE_MAX_ATTEMPTS = 2
    use_vision(fake_vision(error=VisionError("provider down")))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    request = VerificationRequest.objects.get(submission=submission)
    assert "provider" in request.reason.lower()

    submission = resolve_intake(
        submission.pk,
        actor=owner,
        decision={"action": "correct", "corrections": {"name": "Hand-entered mix", "quantity": "3", "unit": "each"}},
    )
    assert submission.status == IntakeStatus.ADDED
    item = PantryItem.objects.get(pk=submission.created_item_id)
    assert item.name == "Hand-entered mix"
    assert item.quantity == Decimal("3")
    assert item.verification_status == InventoryVerification.CONFIRMED
    assert item.verified_by == owner


@pytest.mark.django_db
def test_manual_correction_and_confirmation_are_audited(owner, household, png_photo, use_vision,
                                                         fake_vision, make_payload):
    from apps.audit.models import InventoryChange

    use_vision(fake_vision(payload=make_payload(identity_confidence=0.2)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)

    submission = resolve_intake(
        submission.pk,
        actor=owner,
        decision={"action": "correct", "corrections": {"name": "Corrected mix", "quantity": "4", "unit": "each"}},
    )
    candidate = submission.candidate
    candidate.refresh_from_db()
    assert candidate.name == "Corrected mix"
    assert candidate.quantity == Decimal("4")
    manual = candidate.field_evidence.get(field_name="name")
    assert manual.source == "manual" and manual.confidence == Decimal("1.0")

    change = InventoryChange.objects.get(action="intake_corrected")
    assert change.actor == owner
    assert change.before["name"] == "Trail mix"
    assert change.after["name"] == "Corrected mix"
    request = VerificationRequest.objects.get(submission=submission)
    assert request.status == VerificationRequestStatus.RESOLVED
    assert request.resolution == "corrected"
    assert request.resolved_by == owner and request.resolved_at is not None


@pytest.mark.django_db
def test_confirming_an_estimated_auto_add_upgrades_the_fact(owner, household, png_photo, use_vision,
                                                            fake_vision, make_payload):
    from apps.audit.models import InventoryChange

    use_vision(fake_vision(payload=make_payload()))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    item = PantryItem.objects.get(pk=submission.created_item_id)
    assert item.verification_status == InventoryVerification.ESTIMATED

    submission = resolve_intake(submission.pk, actor=owner, decision={"action": "confirm"})
    item.refresh_from_db()
    assert item.verification_status == InventoryVerification.CONFIRMED
    assert item.verified_by == owner and item.verified_at is not None
    assert InventoryChange.objects.filter(action="intake_confirmed", actor=owner).exists()


@pytest.mark.django_db
def test_reject_marks_submission_rejected_and_dismisses_requests(owner, household, png_photo, use_vision,
                                                                 fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(identity_confidence=0.2)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    submission = resolve_intake(submission.pk, actor=owner, decision={"action": "reject"})
    assert submission.status == IntakeStatus.REJECTED
    request = VerificationRequest.objects.get(submission=submission)
    assert request.status == VerificationRequestStatus.RESOLVED
    assert request.resolution == "rejected"


@pytest.mark.django_db
def test_other_household_member_cannot_resolve_or_confirm(owner, household, other_owner, png_photo,
                                                          use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    with pytest.raises(PermissionDenied):
        resolve_intake(submission.pk, actor=other_owner, decision={"action": "confirm"})
    with pytest.raises(ValidationError):
        resolve_intake(submission.pk, actor=owner, decision={"action": "explode"})


@pytest.mark.django_db
def test_raising_a_threshold_can_tighten_review(owner, household, png_photo, settings, use_vision,
                                                 fake_vision, make_payload):
    settings.INTAKE_CONFIDENCE_THRESHOLDS = {
        "identity": 0.99, "quantity": 0.99, "nutrition": 0.99, "allergen": 0.99,
    }
    use_vision(fake_vision(payload=make_payload()))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0


@pytest.mark.django_db
def test_hermes_followup_delivery_retries_and_records_attempts(owner, household, png_photo, use_vision,
                                                               fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(identity_confidence=0.2)))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    request = VerificationRequest.objects.get(submission=submission)
    sent = []

    def failing_transport(payload):
        raise RuntimeError("hermes offline")

    request = deliver_hermes_followup(request.pk, transport=failing_transport)
    assert request.hermes_status == "failed"
    assert request.hermes_attempts == 1
    assert "offline" in request.hermes_last_error

    request = deliver_hermes_followup(request.pk, transport=lambda payload: sent.append(payload))
    assert request.hermes_status == "sent"
    assert request.hermes_attempts == 2
    assert request.hermes_delivered_at is not None
    assert sent and sent[0]["request_id"] == str(request.pk)


@pytest.mark.django_db
def test_correcting_an_unknown_quantity_requires_an_explicit_unit(owner, household, png_photo,
                                                                 use_vision, fake_vision, make_payload):
    """A correction may not silently become 'each' for a multi-item quantity.

    The candidate has no usable unit and more than one unit of stock, so the
    household has to say what the number counts. A single item still resolves
    to 'each', which is unambiguous.
    """

    use_vision(fake_vision(payload=make_payload(quantity=Decimal("2"), unit="")))
    submission = _upload(owner, household, png_photo)
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert PantryItem.objects.count() == 0

    with pytest.raises(ValidationError) as excinfo:
        resolve_intake(submission.pk, actor=owner, decision={
            "action": "correct", "corrections": {"name": "Mystery tin", "quantity": "2"},
        })
    assert "supported unit" in str(excinfo.value)
    assert PantryItem.objects.count() == 0

    resolved = resolve_intake(submission.pk, actor=owner, decision={
        "action": "correct", "corrections": {"name": "Mystery tin", "quantity": "2", "unit": "each"},
    })
    item = PantryItem.objects.get(pk=resolved.created_item_id)
    assert item.quantity == Decimal("2.000") and item.unit == "each"
