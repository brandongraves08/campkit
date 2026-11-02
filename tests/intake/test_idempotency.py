from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.intake.models import IntakeCandidate, IntakeStatus, IntakeSubmission
from apps.intake.services import (
    add_intake_quantity,
    process_submission,
    reject_intake,
    resolve_intake,
    submit_intake,
)
from apps.intake.vision import CandidatePayload, EvidenceItem, VisionError
from apps.inventory.models import GearItem, PantryItem


def _submit_barcode(actor, household, *, image, barcode, kind="pantry", client=None):
    return submit_intake(
        actor=actor,
        household=household,
        kind=kind,
        channel="barcode",
        barcode=barcode,
        image_bytes=image,
        declared_content_type="image/png",
    )


@pytest.mark.django_db
def test_duplicate_photo_returns_the_same_submission_and_adds_once(owner, household, png_photo,
                                                                    use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    first, created_first = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    second, created_second = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk
    assert IntakeSubmission.objects.count() == 1

    submission = process_submission(first.pk)
    process_submission(first.pk)
    assert submission.status == IntakeStatus.ADDED
    assert PantryItem.objects.count() == 1
    assert IntakeCandidate.objects.filter(submission=submission).count() == 1


@pytest.mark.django_db
def test_duplicate_barcode_does_not_double_add_stock(owner, household, png_photo, use_vision,
                                                      fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    barcode = "0123456789012"
    first, _ = _submit_barcode(owner, household, image=png_photo, barcode=barcode)
    second, created = _submit_barcode(owner, household, image=png_photo + b"\n", barcode=barcode)
    assert created is False
    assert first.pk == second.pk
    assert IntakeSubmission.objects.count() == 1

    submission = process_submission(first.pk)
    assert PantryItem.objects.count() == 1
    item = PantryItem.objects.get()
    assert item.quantity == Decimal("2")

    add_intake_quantity(submission.pk, actor=owner, quantity="3")
    item.refresh_from_db()
    assert item.quantity == Decimal("5")
    from apps.audit.models import InventoryChange
    assert InventoryChange.objects.filter(action="intake_quantity_reconciled", actor=owner).exists()


@pytest.mark.django_db
def test_rejected_submission_can_be_resubmitted(owner, household, png_photo, use_vision,
                                                 fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload(identity_confidence=0.2)))
    first, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    process_submission(first.pk)
    reject_intake(first.pk, actor=owner)
    first.refresh_from_db()
    assert first.status == IntakeStatus.REJECTED

    second, created = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    assert created is True
    assert second.pk != first.pk
    assert IntakeSubmission.objects.count() == 2


@pytest.mark.django_db
def test_processing_after_added_is_idempotent(owner, household, png_photo, use_vision,
                                               fake_vision, make_payload):
    client = use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    submission = process_submission(submission.pk)
    attempts = submission.attempts
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    assert submission.attempts == attempts
    assert len(client.calls) == 1
    assert PantryItem.objects.count() == 1


@pytest.mark.django_db
def test_same_photo_in_another_household_is_isolated(owner, household, other_owner, other_household,
                                                      png_photo, use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    first, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    second, created = submit_intake(
        actor=other_owner, household=other_household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    assert created is True
    assert first.pk != second.pk
    process_submission(first.pk)
    process_submission(second.pk)
    assert PantryItem.objects.for_household(household).count() == 1
    assert PantryItem.objects.for_household(other_household).count() == 1


@pytest.mark.django_db
def test_gear_intake_creates_estimated_gear_through_inventory_boundary(owner, household, png_photo,
                                                                       use_vision, fake_vision,
                                                                       make_payload):
    use_vision(fake_vision(payload=make_payload(
        kind="gear", name="Titanium pot", category="Cookware", quantity=Decimal("1"), unit="",
        weight_grams=180, nutrition={}, allergens=[], nutrition_confidence=0.0, allergen_confidence=0.0,
    )))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="gear", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    item = GearItem.objects.get(pk=submission.created_item_id)
    assert item.name == "Titanium pot"
    assert item.weight_grams == 180
    assert item.verification_status == "estimated"
    assert item.verified_by is None


@pytest.mark.django_db
def test_confirm_is_idempotent_and_never_double_audits(owner, household, png_photo, use_vision,
                                                       fake_vision, make_payload):
    from apps.audit.models import InventoryChange

    use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    submission = process_submission(submission.pk)
    resolve_intake(submission.pk, actor=owner, decision={"action": "confirm"})
    resolve_intake(submission.pk, actor=owner, decision={"action": "confirm"})
    assert InventoryChange.objects.filter(action="intake_confirmed").count() == 1


@pytest.mark.django_db
def test_fake_vision_client_contract_and_payload_bounds(owner, household, png_photo, use_vision,
                                                        fake_vision, make_payload):
    client = use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="barcode",
        barcode="0123456789012", image_bytes=png_photo, declared_content_type="image/png",
    )
    process_submission(submission.pk)
    assert client.calls[0]["image"] == png_photo
    assert client.calls[0]["barcode"] == "0123456789012"

    with pytest.raises(ValueError):
        CandidatePayload(name="x" * 500)
    with pytest.raises(ValueError):
        CandidatePayload(name="Rice", identity_confidence=1.5)
    with pytest.raises(ValueError):
        CandidatePayload(name="Rice", quantity=Decimal("-1"))
    with pytest.raises(ValueError):
        CandidatePayload(name="Rice", allergens=["peanut"] * 40)
    with pytest.raises(ValueError):
        CandidatePayload(name="Rice", evidence=(EvidenceItem("name", "Rice", "label", 2.0),))


@pytest.mark.django_db
def test_provider_payload_is_never_persisted_when_analysis_fails(owner, household, png_photo,
                                                                  use_vision, fake_vision, make_payload):
    use_vision(fake_vision(error=VisionError("bad gateway")))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    process_submission(submission.pk)
    assert IntakeCandidate.objects.count() == 0
    assert PantryItem.objects.count() == 0
    assert submission.status == IntakeStatus.PENDING


@pytest.mark.django_db
def test_barcode_only_submission_uses_the_provider_contract(owner, household, use_vision,
                                                            fake_vision, make_payload):
    client = use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="barcode",
        barcode="0123456789012",
    )
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    assert client.calls == [{"image": b"", "barcode": "0123456789012"}]


@pytest.mark.django_db
def test_rejecting_an_added_intake_is_refused(owner, household, png_photo, use_vision,
                                              fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    submission = process_submission(submission.pk)
    with pytest.raises(ValidationError):
        reject_intake(submission.pk, actor=owner)
    submission.refresh_from_db()
    assert submission.status == IntakeStatus.ADDED
    assert PantryItem.objects.count() == 1


@pytest.mark.django_db
def test_stalled_processing_can_be_reclaimed_after_the_lease(owner, household, png_photo, settings,
                                                              use_vision, fake_vision, make_payload):
    from datetime import timedelta

    from django.utils import timezone

    use_vision(fake_vision(payload=make_payload()))
    submission, _ = submit_intake(
        actor=owner, household=household, kind="pantry", channel="photo",
        image_bytes=png_photo, declared_content_type="image/png",
    )
    IntakeSubmission.objects.filter(pk=submission.pk).update(
        status=IntakeStatus.PROCESSING,
        updated_at=timezone.now() - timedelta(seconds=600),
    )
    submission = process_submission(submission.pk)
    assert submission.status == IntakeStatus.ADDED
    assert PantryItem.objects.count() == 1
