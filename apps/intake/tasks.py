"""Intake job entry points.

No broker is installed in this milestone, so intake analysis runs in-process
immediately after an upload. ``process_intake_submission`` stays a plain,
idempotent callable so a future Celery/task runner can wrap it without changing
domain services or views.

Hermes follow-up notifications use a separate app setting and token; the Hermes
runtime configuration is never read or modified.
"""

import json
import urllib.request

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

MAX_RESPONSE_BYTES = 8192


class HermesNotificationNotConfigured(Exception):
    """Raised when no Hermes notification transport is configured."""


def process_intake_submission(submission_id):
    from apps.intake.services import process_submission

    return process_submission(submission_id)


def schedule_intake_processing(submission_id):
    """Run intake analysis for a stored submission."""
    return process_intake_submission(submission_id)


def retry_pending_intake(limit=50):
    """Retry queued submissions; provider failures stay queued, never fabricated."""
    from apps.intake.models import IntakeStatus, IntakeSubmission

    submission_ids = list(
        IntakeSubmission.objects.filter(status=IntakeStatus.PENDING)
        .order_by("created_at")
        .values_list("pk", flat=True)[:limit]
    )
    return [process_intake_submission(submission_id) for submission_id in submission_ids]


def resolve_transport():
    configured = getattr(settings, "CAMPKIT_HERMES_NOTIFY_TRANSPORT", "")
    if configured:
        return import_string(configured) if isinstance(configured, str) else configured
    return _default_transport


def _default_transport(payload):
    endpoint = getattr(settings, "CAMPKIT_HERMES_NOTIFY_URL", "")
    if not endpoint:
        raise HermesNotificationNotConfigured(
            "Hermes notification endpoint is not configured; the request stays queued."
        )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = getattr(settings, "CAMPKIT_HERMES_NOTIFY_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    timeout = float(getattr(settings, "CAMPKIT_HERMES_NOTIFY_TIMEOUT", 5.0))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(MAX_RESPONSE_BYTES)
    except OSError as exc:
        raise RuntimeError("Hermes notification request failed.") from exc


def deliver_hermes_followup(verification_request_id, *, transport=None):
    """Best-effort delivery of an open verification request to Hermes."""
    from apps.intake.models import HermesDeliveryStatus, VerificationRequest, VerificationRequestStatus

    request = VerificationRequest.objects.filter(pk=verification_request_id).first()
    if request is None:
        return None
    if request.status != VerificationRequestStatus.OPEN:
        return request
    transport = transport or resolve_transport()
    payload = dict(request.hermes_payload or {})
    try:
        transport(payload)
    except HermesNotificationNotConfigured as exc:
        request.hermes_last_error = str(exc)[:240]
        request.save(update_fields=["hermes_last_error"])
        return request
    except Exception as exc:  # delivery is best effort; never lose the request
        request.hermes_attempts += 1
        request.hermes_status = HermesDeliveryStatus.FAILED
        request.hermes_last_error = str(exc)[:240]
        request.save(update_fields=["hermes_attempts", "hermes_status", "hermes_last_error"])
        return request
    request.hermes_attempts += 1
    request.hermes_status = HermesDeliveryStatus.SENT
    request.hermes_delivered_at = timezone.now()
    request.hermes_last_error = ""
    request.save(update_fields=["hermes_attempts", "hermes_status", "hermes_delivered_at",
                                "hermes_last_error"])
    return request
