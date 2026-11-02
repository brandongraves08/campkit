"""Domain services for private photo/barcode intake and confidence review.

Provider calls are delegated to :mod:`apps.intake.vision`; inventory writes go
through the inventory service layer (never its models), and every state change
is audited with the acting user. Automatic additions are always ESTIMATED until
a human approves, corrects, or confirms them.
"""

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.audit.services import record_change
from apps.households.permissions import require_household_member
from apps.inventory.services import (
    create_gear_item,
    create_pantry_item,
    get_gear_item,
    get_pantry_stock,
    update_gear_item,
    update_pantry_item,
)
from apps.intake import storage
from apps.intake.models import (
    HermesDeliveryStatus,
    IntakeCandidate,
    IntakeChannel,
    IntakeFieldEvidence,
    IntakeKind,
    IntakeStatus,
    IntakeSubmission,
    VerificationRequest,
    VerificationRequestStatus,
)
from apps.intake.vision import CandidatePayload, VisionError, get_vision_client

CRITICAL_FIELDS = ("identity", "quantity", "nutrition", "allergen")
DEFAULT_THRESHOLDS = {
    "identity": Decimal("0.85"),
    "quantity": Decimal("0.80"),
    "nutrition": Decimal("0.90"),
    "allergen": Decimal("0.95"),
}
# A configured threshold may tighten review but can never fall below this floor:
# ambiguous identity, quantity, nutrition, or allergens always stay reviewable.
MIN_SAFE_THRESHOLDS = {field: Decimal("0.70") for field in CRITICAL_FIELDS}

CORRECTION_FIELDS = {
    "name", "category", "brand", "model_number", "barcode", "quantity", "unit",
    "weight_grams", "nutrition", "allergens",
}
VALID_UNITS = {"g", "kg", "ml", "l", "each", "oz", "lb", "cup", "tbsp", "tsp"}
UNIT_ALIASES = {
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "milliliter": "ml", "millilitre": "ml", "milliliters": "ml", "millilitres": "ml",
    "liter": "l", "litre": "l", "liters": "l", "litres": "l",
    "count": "each", "unit": "each", "units": "each", "items": "each",
    "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb",
    "cups": "cup", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp",
}
REVIEW_ACTIONS = {"approve", "correct", "confirm", "reject"}
MAX_MANUAL_ATTEMPTS_LIMIT = 10
MAX_NOTE = 200


def _quantize(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"))


def effective_threshold(field_name: str) -> Decimal:
    configured = getattr(settings, "INTAKE_CONFIDENCE_THRESHOLDS", {}) or {}
    raw = configured.get(field_name, DEFAULT_THRESHOLDS.get(field_name, Decimal("1")))
    try:
        configured_value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        configured_value = DEFAULT_THRESHOLDS.get(field_name, Decimal("1"))
    value = max(configured_value, MIN_SAFE_THRESHOLDS.get(field_name, Decimal("0.70")))
    return min(value, Decimal("1"))


def _normalize_unit(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text in VALID_UNITS else UNIT_ALIASES.get(text, "")


def _normalize_barcode(value) -> str:
    text = str(value or "").strip().replace(" ", "").replace("-", "")
    if not text:
        return ""
    if not text.isdigit() or not 6 <= len(text) <= 32:
        raise ValidationError({"barcode": "Enter a 6-32 digit barcode."})
    return text


def _validate_kind(value) -> str:
    value = str(value or "").strip().lower()
    if value not in {IntakeKind.GEAR, IntakeKind.PANTRY}:
        raise ValidationError({"kind": "Intake kind must be gear or pantry."})
    return value


def _validate_channel(value) -> str:
    value = str(value or "").strip().lower()
    if value not in {IntakeChannel.PHOTO, IntakeChannel.BARCODE}:
        raise ValidationError({"channel": "Upload a photo or a barcode."})
    return value


def _dedupe_key(sha256: str, kind: str, barcode: str) -> str:
    if sha256:
        return sha256
    return hashlib.sha256(f"barcode:{kind}:{barcode}".encode("utf-8")).hexdigest()


def _bounded(text, limit=240) -> str:
    return str(text or "").strip()[:limit]


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def evaluate_candidate(payload: CandidatePayload) -> tuple[list[dict], list[dict]]:
    """Return the blocking and advisory review reasons for a payload.

    Identity and quantity problems block automatic addition; an explicitly
    ambiguous or low-confidence allergen claim does too. Missing nutrition or
    allergen data permits an ESTIMATED addition but always opens a follow-up
    verification request, so no fact silently becomes trusted.
    """
    blocking: list[dict] = []
    flagged: list[dict] = []

    identity_confidence = _quantize(payload.identity_confidence)
    if not payload.name.strip():
        blocking.append({"field": "identity", "reason": "Item name is missing."})
    elif "identity" in payload.ambiguous_fields:
        blocking.append({"field": "identity", "reason": "The provider flagged the item identity as ambiguous."})
    elif identity_confidence < effective_threshold("identity"):
        blocking.append({
            "field": "identity",
            "reason": f"Identity confidence {identity_confidence} is below the review floor of {effective_threshold('identity')}.",
        })

    quantity_confidence = _quantize(payload.quantity_confidence)
    if payload.quantity <= 0:
        blocking.append({"field": "quantity", "reason": "Extracted quantity must be greater than zero."})
    elif "quantity" in payload.ambiguous_fields:
        blocking.append({"field": "quantity", "reason": "The provider flagged the quantity as ambiguous."})
    elif quantity_confidence < effective_threshold("quantity"):
        blocking.append({
            "field": "quantity",
            "reason": f"Quantity confidence {quantity_confidence} is below the review floor of {effective_threshold('quantity')}.",
        })
    elif payload.kind == IntakeKind.PANTRY and payload.quantity > 1 and not _normalize_unit(payload.unit):
        blocking.append({"field": "quantity", "reason": "The unit for the extracted quantity is unknown."})

    if payload.kind == IntakeKind.PANTRY:
        nutrition_confidence = _quantize(payload.nutrition_confidence)
        if payload.nutrition:
            if "nutrition" in payload.ambiguous_fields:
                blocking.append({"field": "nutrition", "reason": "The provider flagged the nutrition values as ambiguous."})
            elif nutrition_confidence < effective_threshold("nutrition"):
                flagged.append({
                    "field": "nutrition",
                    "reason": f"Nutrition confidence {nutrition_confidence} is below the preferred threshold.",
                })
        else:
            flagged.append({"field": "nutrition", "reason": "Nutrition data is missing."})

        allergen_confidence = _quantize(payload.allergen_confidence)
        if payload.allergens:
            if "allergen" in payload.ambiguous_fields:
                blocking.append({"field": "allergen", "reason": "The provider flagged the allergen information as ambiguous."})
            elif allergen_confidence < effective_threshold("allergen"):
                blocking.append({
                    "field": "allergen",
                    "reason": f"Allergen confidence {allergen_confidence} is below the review floor of {effective_threshold('allergen')}.",
                })
        else:
            flagged.append({"field": "allergen", "reason": "No allergen information was supplied."})

    return blocking, flagged


def _review_reason(blocking: list[dict], flagged: list[dict]) -> str:
    parts = [item["reason"] for item in blocking + flagged]
    return _bounded("Intake needs review: " + "; ".join(parts), 255)


def _hermes_payload(request: VerificationRequest) -> dict:
    return {
        "request_id": str(request.pk),
        "household_id": str(request.household_id),
        "submission_id": str(request.submission_id) if request.submission_id else None,
        "title": "CampKit intake needs verification",
        "reason": request.reason,
        "fields": list(request.field_names or []),
        "review_path": f"/intake/{request.submission_id}/" if request.submission_id else None,
        "created_at": timezone.now().isoformat(),
        "source": "campkit.intake",
    }


def _open_verification_request(*, submission: IntakeSubmission, fields, reason: str) -> VerificationRequest:
    cleaned = sorted({field for field in fields if field in CRITICAL_FIELDS})
    reason = _bounded(reason, 255) or "Intake needs a human review."
    existing = VerificationRequest.objects.filter(
        submission=submission, status=VerificationRequestStatus.OPEN
    ).first()
    if existing is not None:
        merged = sorted(set(existing.field_names or []) | set(cleaned))
        if merged != (existing.field_names or []):
            existing.field_names = merged
            existing.hermes_payload = _hermes_payload(existing)
            existing.save(update_fields=["field_names", "hermes_payload"])
        return existing
    request = VerificationRequest(
        household=submission.household,
        submission=submission,
        created_by=submission.submitted_by,
        reason=reason,
        field_names=cleaned,
        channel="app_and_hermes",
        hermes_status=HermesDeliveryStatus.PENDING,
        hermes_queued_at=timezone.now(),
    )
    request.hermes_payload = _hermes_payload(request)
    request.full_clean()
    request.save()
    return request


def _resolve_verification_requests(submission: IntakeSubmission, actor, resolution: str) -> None:
    now = timezone.now()
    for request in submission.verification_requests.filter(status=VerificationRequestStatus.OPEN):
        request.status = VerificationRequestStatus.RESOLVED
        request.resolution = resolution
        request.resolved_by = actor
        request.resolved_at = now
        request.full_clean()
        request.save(update_fields=["status", "resolution", "resolved_by", "resolved_at"])


def _audit(submission: IntakeSubmission, action: str, *, actor, before=None, after=None):
    return record_change(
        actor=actor,
        household=submission.household,
        object=submission,
        action=action,
        before=before,
        after=after,
    )


def submit_intake(*, actor, household, kind, channel, barcode="", image_bytes=None,
                  declared_content_type="", process=False):
    """Store and validate an intake upload.

    Returns ``(submission, created)``. A duplicate photo digest or barcode for
    the same household returns the existing submission instead of creating a
    second one, so repeated scans cannot double-add stock.
    """
    actual_household = require_household_member(actor, household.pk)
    kind = _validate_kind(kind)
    channel = _validate_channel(channel)
    barcode = _normalize_barcode(barcode) if barcode else ""
    if channel == IntakeChannel.BARCODE and not barcode:
        raise ValidationError({"barcode": "Barcode intake requires a barcode value."})
    if channel == IntakeChannel.PHOTO and not image_bytes:
        raise ValidationError({"image": "Choose a JPEG, PNG, or WebP photo to upload."})

    info = storage.validate_upload(image_bytes, declared_content_type) if image_bytes else None
    # Barcode submissions dedupe on the barcode itself so the same product
    # cannot be added twice from two slightly different photos. Photo
    # submissions dedupe on the exact image digest.
    dedupe_key = _dedupe_key(
        info.sha256 if info and channel == IntakeChannel.PHOTO else "",
        kind,
        barcode,
    )
    existing = (
        IntakeSubmission.objects.for_household(actual_household)
        .exclude(status=IntakeStatus.REJECTED)
        .filter(dedupe_key=dedupe_key)
        .first()
    )
    if existing is not None:
        return existing, False

    image_key = ""
    if image_bytes and info is not None:
        image_key = storage.save_private_image(actual_household.pk, image_bytes, info.mime)
    try:
        with transaction.atomic():
            submission = IntakeSubmission(
                household=actual_household,
                submitted_by=actor,
                kind=kind,
                channel=channel,
                barcode=barcode,
                dedupe_key=dedupe_key,
                image_key=image_key,
                image_content_type=info.mime if info else "",
                image_size_bytes=info.size if info else 0,
                image_sha256=info.sha256 if info else "",
            )
            submission.full_clean()
            submission.save()
    except IntegrityError:
        if image_key:
            storage.delete_private_image(image_key)
        existing = (
            IntakeSubmission.objects.for_household(actual_household)
            .exclude(status=IntakeStatus.REJECTED)
            .filter(dedupe_key=dedupe_key)
            .first()
        )
        if existing is None:
            raise
        return existing, False
    except Exception:
        if image_key:
            storage.delete_private_image(image_key)
        raise

    if process:
        submission = process_submission(submission.pk)
    return submission, True


def process_submission(submission_id):
    """Analyze one submission; safe to retry after provider failures."""
    submission = IntakeSubmission.objects.filter(pk=submission_id).select_related("household").first()
    if submission is None:
        raise IntakeSubmission.DoesNotExist(f"Intake submission {submission_id} does not exist.")
    if submission.status in {IntakeStatus.ADDED, IntakeStatus.REJECTED}:
        return submission

    lease_seconds = max(30, int(getattr(settings, "INTAKE_PROCESSING_LEASE_SECONDS", 300)))
    cutoff = timezone.now() - timedelta(seconds=lease_seconds)
    if submission.status == IntakeStatus.PROCESSING and submission.updated_at > cutoff:
        # Another worker still owns this submission.
        return submission
    claimable = Q(status=IntakeStatus.PENDING) | Q(status=IntakeStatus.PROCESSING, updated_at__lte=cutoff)
    claimed = (
        IntakeSubmission.objects.filter(pk=submission.pk)
        .filter(claimable)
        .update(status=IntakeStatus.PROCESSING, attempts=F("attempts") + 1, updated_at=timezone.now())
    )
    if not claimed:
        submission.refresh_from_db()
        return submission
    submission.refresh_from_db()

    image = b""
    if submission.image_key:
        try:
            image = storage.read_private_image(submission.image_key)
        except (OSError, ValueError):
            return _finish_failure(
                submission,
                "The private upload could not be read; submit the photo again.",
                retryable=False,
            )

    try:
        payload = get_vision_client().analyze(image, barcode=submission.barcode)
    except VisionError as exc:
        return _finish_failure(submission, str(exc) or "Vision provider unavailable.", retryable=True)

    if payload.kind != submission.kind:
        return _finish_failure(
            submission,
            "Vision provider extracted a different item type than requested.",
            retryable=False,
        )
    return _apply_payload(submission, payload)


def _finish_failure(submission: IntakeSubmission, message: str, *, retryable: bool):
    limit = max(1, min(int(getattr(settings, "INTAKE_MAX_ATTEMPTS", 3)), MAX_MANUAL_ATTEMPTS_LIMIT))
    message = _bounded(message, 240)
    with transaction.atomic():
        submission.refresh_from_db()
        submission.failure_reason = message
        if retryable and submission.attempts < limit:
            submission.status = IntakeStatus.PENDING
            submission.save(update_fields=["status", "failure_reason", "updated_at"])
            _audit(submission, "intake_processing_failed", actor=submission.submitted_by,
                   after={"attempts": submission.attempts, "retryable": True, "reason": message})
            return submission
        submission.status = IntakeStatus.NEEDS_REVIEW
        submission.processed_at = timezone.now()
        submission.save(update_fields=["status", "failure_reason", "processed_at", "updated_at"])
        _audit(submission, "intake_processing_failed", actor=submission.submitted_by,
               after={"attempts": submission.attempts, "retryable": False, "reason": message})
        _open_verification_request(
            submission=submission,
            fields=["identity", "quantity"],
            reason=f"Vision provider could not read this upload; enter the item manually. ({message})",
        )
    submission.refresh_from_db()
    return submission


def _candidate_values(submission: IntakeSubmission, payload: CandidatePayload) -> dict:
    nutrition = dict(payload.nutrition or {})
    allergens = list(payload.allergens or ())
    confidences = [payload.identity_confidence, payload.quantity_confidence]
    if payload.nutrition:
        confidences.append(payload.nutrition_confidence)
    if payload.allergens:
        confidences.append(payload.allergen_confidence)
    return {
        "name": payload.name.strip()[:160],
        "category": payload.category[:80],
        "brand": payload.brand[:120],
        "model_number": payload.model_number[:120],
        "barcode": (payload.barcode or submission.barcode)[:64],
        "quantity": payload.quantity,
        "unit": _normalize_unit(payload.unit),
        "weight_grams": payload.weight_grams,
        "nutrition": nutrition,
        "allergens": allergens,
        "attributes": dict(payload.attributes or {}),
        "provider": payload.provider[:80],
        "model_version": payload.model_version[:80],
        "overall_confidence": _quantize(min(confidences) if confidences else 0),
        "identity_confidence": _quantize(payload.identity_confidence),
        "quantity_confidence": _quantize(payload.quantity_confidence),
        "nutrition_confidence": _quantize(payload.nutrition_confidence),
        "allergen_confidence": _quantize(payload.allergen_confidence),
    }


def _evidence_rows(submission: IntakeSubmission, payload: CandidatePayload) -> dict:
    source = payload.provider[:64] or "vision"
    default_url = next((item.source_url for item in payload.evidence if item.source_url), "")
    rows = {
        "name": (payload.name, source, payload.identity_confidence, default_url),
        "quantity": (str(payload.quantity), source, payload.quantity_confidence, default_url),
        "unit": (payload.unit, source, payload.quantity_confidence, default_url),
    }
    if payload.category:
        rows["category"] = (payload.category, source, payload.identity_confidence, default_url)
    if payload.brand:
        rows["brand"] = (payload.brand, source, payload.identity_confidence, default_url)
    if payload.model_number:
        rows["model_number"] = (payload.model_number, source, payload.identity_confidence, default_url)
    if payload.barcode:
        rows["barcode"] = (payload.barcode, source, payload.identity_confidence, default_url)
    if payload.weight_grams is not None:
        rows["weight_grams"] = (payload.weight_grams, source, payload.quantity_confidence, default_url)
    if payload.nutrition:
        rows["nutrition"] = (payload.nutrition, source, payload.nutrition_confidence, default_url)
    if payload.allergens:
        rows["allergens"] = (list(payload.allergens), source, payload.allergen_confidence, default_url)
    for item in payload.evidence:
        rows[item.field_name] = (item.value, item.source, item.confidence, item.source_url)
    cleaned = {}
    for field_name, (value, row_source, confidence, source_url) in rows.items():
        if value in (None, "", [], {}):
            continue
        cleaned[field_name] = {
            "value": _json_value(value),
            "source": row_source[:64] or source,
            "source_url": _safe_evidence_url(source_url),
            "confidence": _quantize(confidence),
        }
    return cleaned


def _safe_evidence_url(value) -> str:
    text = str(value or "").strip()[:255]
    return text if text.lower().startswith(("https://", "http://")) else ""


def _store_candidate(submission: IntakeSubmission, payload: CandidatePayload) -> IntakeCandidate:
    values = _candidate_values(submission, payload)
    candidate, _created = IntakeCandidate.objects.get_or_create(
        submission=submission,
        defaults={**values, "name": values["name"] or "Unidentified item"},
    )
    for field_name, value in values.items():
        setattr(candidate, field_name, value)
    candidate.full_clean()
    candidate.save()
    candidate.field_evidence.all().delete()
    now = timezone.now()
    for field_name, row in _evidence_rows(submission, payload).items():
        evidence = IntakeFieldEvidence(
            candidate=candidate,
            field_name=field_name,
            value=row["value"],
            source=row["source"],
            source_url=row["source_url"],
            confidence=row["confidence"],
            observed_at=now,
        )
        evidence.full_clean()
        evidence.save()
    return candidate


def _source_for(candidate: IntakeCandidate) -> str:
    return f"{candidate.provider} / smart intake"[:255]


def _primary_source_url(candidate: IntakeCandidate) -> str:
    for evidence in candidate.field_evidence.all():
        if evidence.source_url:
            return evidence.source_url
    return ""


def _inventory_values(submission: IntakeSubmission, candidate: IntakeCandidate) -> dict:
    base = {
        "name": candidate.name[:160],
        "source": _source_for(candidate),
        "source_url": _primary_source_url(candidate),
        "observed_at": timezone.now(),
        "confidence": candidate.overall_confidence,
    }
    if submission.kind == IntakeKind.GEAR:
        quantity = int(candidate.quantity)
        if Decimal(quantity) != candidate.quantity:
            raise ValidationError({"quantity": "Gear quantities must be whole numbers."})
        return {
            **base,
            "category": candidate.category[:80],
            "owner": "",
            "condition": "",
            "weight_grams": candidate.weight_grams,
            "quantity": max(quantity, 1),
            "storage_location": "",
            "availability": "available",
        }
    return {
        **base,
        "quantity": candidate.quantity,
        "unit": candidate.unit or "each",
        "storage_location": "",
    }


def _create_inventory_item(submission: IntakeSubmission, candidate: IntakeCandidate, *, actor,
                           confirmed: bool):
    values = _inventory_values(submission, candidate)
    values["verification_status"] = "confirmed" if confirmed else "estimated"
    if submission.kind == IntakeKind.GEAR:
        return create_gear_item(actor=actor, household=submission.household, data=values)
    return create_pantry_item(actor=actor, household=submission.household, data=values)


def _apply_payload(submission: IntakeSubmission, payload: CandidatePayload):
    blocking, flagged = evaluate_candidate(payload)
    with transaction.atomic():
        submission = IntakeSubmission.objects.select_for_update().get(pk=submission.pk)
        if submission.status in {IntakeStatus.ADDED, IntakeStatus.REJECTED}:
            return submission
        candidate = _store_candidate(submission, payload)
        if blocking:
            submission.status = IntakeStatus.NEEDS_REVIEW
            submission.failure_reason = ""
            submission.processed_at = timezone.now()
            submission.save(update_fields=["status", "failure_reason", "processed_at", "updated_at"])
            _audit(submission, "intake_needs_review", actor=submission.submitted_by,
                   after={"blocking": [item["field"] for item in blocking],
                          "flagged": [item["field"] for item in flagged]})
            _open_verification_request(
                submission=submission,
                fields=[item["field"] for item in blocking + flagged],
                reason=_review_reason(blocking, flagged),
            )
            return submission

        item = _create_inventory_item(submission, candidate, actor=submission.submitted_by, confirmed=False)
        submission.created_item_kind = submission.kind
        submission.created_item_id = str(item.pk)
        submission.status = IntakeStatus.ADDED
        submission.failure_reason = ""
        submission.processed_at = timezone.now()
        submission.save(update_fields=["status", "failure_reason", "processed_at", "created_item_kind",
                                       "created_item_id", "updated_at"])
        _audit(submission, "intake_added", actor=submission.submitted_by,
               after={"item_id": str(item.pk), "item_kind": submission.kind, "estimated": True,
                      "provider": candidate.provider})
        if flagged:
            _open_verification_request(
                submission=submission,
                fields=[item["field"] for item in flagged],
                reason=_review_reason([], flagged),
            )
    submission.refresh_from_db()
    return submission


def _parse_decision(decision):
    if decision is None:
        raise ValidationError({"decision": "A review decision is required."})
    if isinstance(decision, Mapping):
        action = decision.get("action")
        corrections = decision.get("corrections") or {}
        note = decision.get("note", "")
    else:
        action = getattr(decision, "action", None)
        corrections = getattr(decision, "corrections", {}) or {}
        note = getattr(decision, "note", "")
    action = str(action or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise ValidationError({"action": "Decision must be approve, correct, confirm, or reject."})
    if not isinstance(corrections, Mapping):
        raise ValidationError({"corrections": "Corrections must be a mapping of field values."})
    return action, dict(corrections), _bounded(note, MAX_NOTE)


def _validate_corrections(submission: IntakeSubmission, corrections: dict) -> dict:
    if not corrections:
        raise ValidationError({"corrections": "Manual review needs at least one corrected field."})
    cleaned = {}
    for field_name, raw in corrections.items():
        if field_name not in CORRECTION_FIELDS:
            raise ValidationError({field_name: "Unsupported correction field."})
        if field_name == "name":
            value = _bounded(raw, 160)
            if not value:
                raise ValidationError({"name": "Item name cannot be blank."})
        elif field_name in {"category", "brand", "model_number"}:
            limit = 80 if field_name == "category" else 120
            value = _bounded(raw, limit)
        elif field_name == "barcode":
            value = _normalize_barcode(raw)
        elif field_name == "quantity":
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError({"quantity": "Quantity must be a decimal number."}) from exc
            if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
                raise ValidationError({"quantity": "Quantity must be between 0 and 1,000,000."})
        elif field_name == "unit":
            value = _normalize_unit(raw)
            if not value:
                raise ValidationError({"unit": "Choose a supported unit."})
        elif field_name == "weight_grams":
            if raw in (None, ""):
                value = None
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValidationError({"weight_grams": "Weight must be whole grams."}) from exc
                if not 0 <= value <= 500_000:
                    raise ValidationError({"weight_grams": "Weight is outside the accepted range."})
        elif field_name == "nutrition":
            if not isinstance(raw, Mapping) or len(raw) > 32:
                raise ValidationError({"nutrition": "Nutrition data must be a small mapping."})
            value = {_bounded(key, 64): _bounded(item, 255) for key, item in raw.items()}
        else:  # allergens
            if isinstance(raw, str):
                raw = [part.strip() for part in raw.split(",") if part.strip()]
            if not isinstance(raw, (list, tuple)) or len(raw) > 32:
                raise ValidationError({"allergens": "Provide up to 32 allergen names."})
            value = [_bounded(item, 80) for item in raw if _bounded(item, 80)]
        cleaned[field_name] = value
    if submission.kind == IntakeKind.PANTRY and "unit" not in cleaned:
        candidate = getattr(submission, "candidate", None)
        current_unit = candidate.unit if candidate is not None else ""
        quantity = cleaned.get("quantity", candidate.quantity if candidate is not None else Decimal("1"))
        if not _normalize_unit(current_unit) and Decimal(quantity) == 1:
            cleaned["unit"] = "each"
    return cleaned


def _apply_corrections(submission: IntakeSubmission, candidate, corrections: dict):
    before = {
        "name": candidate.name if candidate is not None else "",
        "quantity": str(candidate.quantity) if candidate is not None else "",
        "unit": candidate.unit if candidate is not None else "",
    }
    for field_name, value in corrections.items():
        before.setdefault(field_name, _json_value(getattr(candidate, field_name, "")) if candidate else "")
    if candidate is None:
        candidate = IntakeCandidate(
            submission=submission,
            name=corrections.get("name") or "Manual item",
            quantity=corrections.get("quantity", Decimal("1")),
            unit=corrections.get("unit", "each"),
            barcode=corrections.get("barcode", submission.barcode),
            provider="manual",
            overall_confidence=Decimal("1.000"),
            identity_confidence=Decimal("1.000"),
            quantity_confidence=Decimal("1.000"),
            nutrition_confidence=Decimal("1.000"),
            allergen_confidence=Decimal("1.000"),
        )
    for field_name, value in corrections.items():
        setattr(candidate, field_name, value)
    if candidate.provider == "manual":
        candidate.overall_confidence = Decimal("1.000")
    candidate.full_clean()
    candidate.save()
    now = timezone.now()
    for field_name, value in corrections.items():
        evidence, _ = IntakeFieldEvidence.objects.update_or_create(
            candidate=candidate,
            field_name=field_name,
            defaults={
                "value": _json_value(value),
                "source": "manual",
                "source_url": "",
                "confidence": Decimal("1.000"),
                "observed_at": now,
            },
        )
        evidence.full_clean()
    after = {field_name: _json_value(value) for field_name, value in corrections.items()}
    return candidate, before, after


def _require_status(submission: IntakeSubmission, allowed: set[str], action: str):
    if submission.status not in allowed:
        raise ValidationError(
            {"action": f"Cannot {action} an intake that is {submission.get_status_display().lower()}."}
        )


def resolve_intake(submission_id, actor, decision):
    """Approve, correct, confirm, or reject a submission after review."""
    action, corrections, note = _parse_decision(decision)
    submission = IntakeSubmission.objects.select_related("household").filter(pk=submission_id).first()
    if submission is None:
        raise IntakeSubmission.DoesNotExist(f"Intake submission {submission_id} does not exist.")
    require_household_member(actor, submission.household_id)
    if action == "reject":
        return reject_intake(submission_id, actor=actor, note=note)

    with transaction.atomic():
        submission = IntakeSubmission.objects.select_for_update().get(pk=submission_id)
        if submission.status == IntakeStatus.REJECTED:
            raise ValidationError({"action": "Rejected intake cannot be resolved."})
        candidate = getattr(submission, "candidate", None)

        if action == "approve":
            _require_status(submission, {IntakeStatus.NEEDS_REVIEW}, "approve")
            if candidate is None:
                raise ValidationError({"action": "There is no extracted candidate to approve."})
            item = _create_inventory_item(submission, candidate, actor=actor, confirmed=True)
            _mark_added(submission, item, actor=actor, action="intake_approved",
                        after={"item_id": str(item.pk), "note": note})
            _resolve_verification_requests(submission, actor, "confirmed")
            return submission

        if action == "correct":
            _require_status(submission, {IntakeStatus.PENDING, IntakeStatus.NEEDS_REVIEW}, "correct")
            cleaned = _validate_corrections(submission, corrections)
            candidate, before, after = _apply_corrections(submission, candidate, cleaned)
            item = _create_inventory_item(submission, candidate, actor=actor, confirmed=True)
            _mark_added(submission, item, actor=actor, action="intake_corrected",
                        before=before, after={**after, "item_id": str(item.pk), "note": note})
            _resolve_verification_requests(submission, actor, "corrected")
            return submission

        # action == "confirm"
        _require_status(submission, {IntakeStatus.ADDED}, "confirm")
        if not submission.created_item_id:
            raise ValidationError({"action": "This intake has no created inventory item to confirm."})
        if submission.created_item_kind == IntakeKind.GEAR:
            item = get_gear_item(actor=actor, household=submission.household,
                                 item_id=submission.created_item_id)
            already_confirmed = item.verification_status == "confirmed"
        else:
            stock = get_pantry_stock(actor=actor, household=submission.household,
                                     item_ids=[submission.created_item_id], for_update=True)
            current = stock.get(str(submission.created_item_id))
            if current is None:
                raise ValidationError({"action": "The created inventory item no longer exists."})
            already_confirmed = current["verification_status"] == "confirmed"
        if already_confirmed:
            return submission
        if submission.created_item_kind == IntakeKind.GEAR:
            update_gear_item(actor=actor, household=submission.household,
                             item_id=submission.created_item_id,
                             data={"verification_status": "confirmed"})
        else:
            update_pantry_item(actor=actor, household=submission.household,
                               item_id=submission.created_item_id,
                               data={"verification_status": "confirmed"})
        _audit(submission, "intake_confirmed", actor=actor,
               after={"item_id": submission.created_item_id, "note": note})
        _resolve_verification_requests(submission, actor, "confirmed")
        return submission


def _mark_added(submission: IntakeSubmission, item, *, actor, action: str, before=None, after=None):
    submission.created_item_kind = submission.kind
    submission.created_item_id = str(item.pk)
    submission.status = IntakeStatus.ADDED
    submission.failure_reason = ""
    submission.processed_at = timezone.now()
    submission.save(update_fields=["status", "failure_reason", "processed_at", "created_item_kind",
                                   "created_item_id", "updated_at"])
    _audit(submission, action, actor=actor, before=before, after=after)


def reject_intake(submission_id, actor, note=""):
    submission = IntakeSubmission.objects.select_related("household").filter(pk=submission_id).first()
    if submission is None:
        raise IntakeSubmission.DoesNotExist(f"Intake submission {submission_id} does not exist.")
    require_household_member(actor, submission.household_id)
    with transaction.atomic():
        submission = IntakeSubmission.objects.select_for_update().get(pk=submission_id)
        if submission.status == IntakeStatus.REJECTED:
            return submission
        if submission.status == IntakeStatus.ADDED:
            raise ValidationError(
                {"action": "Rejecting an added intake would leave its inventory item behind; remove the item instead."}
            )
        submission.status = IntakeStatus.REJECTED
        submission.save(update_fields=["status", "updated_at"])
        _audit(submission, "intake_rejected", actor=actor, after={"note": _bounded(note, MAX_NOTE)})
        _resolve_verification_requests(submission, actor, "rejected")
    submission.refresh_from_db()
    return submission


def add_intake_quantity(submission_id, actor, quantity):
    """Explicitly reconcile stock for an already-added intake (never implicit)."""
    submission = IntakeSubmission.objects.select_related("household").filter(pk=submission_id).first()
    if submission is None:
        raise IntakeSubmission.DoesNotExist(f"Intake submission {submission_id} does not exist.")
    require_household_member(actor, submission.household_id)
    if submission.status != IntakeStatus.ADDED or not submission.created_item_id:
        raise ValidationError({"action": "Only added intake can be reconciled."})
    try:
        delta = Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"quantity": "Quantity must be a decimal number."}) from exc
    if not delta.is_finite() or delta <= 0:
        raise ValidationError({"quantity": "Added quantity must be greater than zero."})
    with transaction.atomic():
        submission = IntakeSubmission.objects.select_for_update().get(pk=submission_id)
        if submission.created_item_kind == IntakeKind.GEAR:
            item = get_gear_item(actor=actor, household=submission.household,
                                 item_id=submission.created_item_id)
            delta_int = int(delta)
            if Decimal(delta_int) != delta:
                raise ValidationError({"quantity": "Gear quantities must be whole numbers."})
            before = {"quantity": item.quantity}
            item = update_gear_item(actor=actor, household=submission.household,
                                    item_id=submission.created_item_id,
                                    data={"quantity": item.quantity + delta_int})
            after = {"quantity": item.quantity}
        else:
            stock = get_pantry_stock(actor=actor, household=submission.household,
                                     item_ids=[submission.created_item_id], for_update=True)
            current = stock.get(str(submission.created_item_id))
            if current is None:
                raise ValidationError({"action": "The created inventory item no longer exists."})
            before = {"quantity": str(current["quantity"])}
            item = update_pantry_item(actor=actor, household=submission.household,
                                      item_id=submission.created_item_id,
                                      data={"quantity": current["quantity"] + delta})
            after = {"quantity": str(item.quantity)}
        _audit(submission, "intake_quantity_reconciled", actor=actor, before=before, after=after)
    return item


def get_submission(*, actor, household, submission_id):
    require_household_member(actor, household.pk)
    submission = IntakeSubmission.objects.for_household(household).filter(pk=submission_id).first()
    if submission is None:
        raise IntakeSubmission.DoesNotExist(f"Intake submission {submission_id} does not exist.")
    return submission


def list_intake_submissions(*, actor, household):
    require_household_member(actor, household.pk)
    return IntakeSubmission.objects.for_household(household).order_by("-created_at")


def list_verification_requests(*, actor, household, open_only=True):
    require_household_member(actor, household.pk)
    requests = VerificationRequest.objects.filter(household=household)
    if open_only:
        requests = requests.filter(status=VerificationRequestStatus.OPEN)
    return requests.select_related("submission").order_by("-created_at")
