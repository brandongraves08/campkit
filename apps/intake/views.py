from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.households.permissions import current_household
from apps.intake import storage
from apps.intake.models import IntakeChannel, IntakeKind, IntakeSubmission
from apps.intake.services import (
    list_intake_submissions,
    list_verification_requests,
    resolve_intake,
    submit_intake,
)
from apps.intake.tasks import schedule_intake_processing


def _error_message(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return "; ".join(
            f"{field}: {', '.join(str(item) for item in messages_)}"
            for field, messages_ in exc.message_dict.items()
        )
    return "; ".join(str(item) for item in exc.messages)


def _form_context(request):
    return {
        "kinds": IntakeKind.choices,
        "channels": IntakeChannel.choices,
        "selected_kind": request.POST.get("kind", IntakeKind.PANTRY),
        "selected_channel": request.POST.get("channel", IntakeChannel.PHOTO),
        "barcode": request.POST.get("barcode", ""),
    }


@login_required
def intake_list(request):
    household = current_household(request)
    return render(request, "intake/list.html", {
        "nav": "intake",
        "submissions": list_intake_submissions(actor=request.user, household=household),
        "verification_requests": list_verification_requests(actor=request.user, household=household),
    })


@login_required
@require_http_methods(["GET", "POST"])
def intake_create(request):
    household = current_household(request)
    if request.method == "POST":
        upload = request.FILES.get("image")
        try:
            if upload is not None and upload.size > storage.max_image_bytes():
                raise ValidationError({"image": "Photo exceeds the configured upload limit."})
            submission, created = submit_intake(
                actor=request.user,
                household=household,
                kind=request.POST.get("kind", IntakeKind.PANTRY),
                channel=request.POST.get("channel", IntakeChannel.PHOTO),
                barcode=request.POST.get("barcode", ""),
                image_bytes=upload.read() if upload is not None else None,
                declared_content_type=getattr(upload, "content_type", "") if upload else "",
            )
        except ValidationError as exc:
            return render(request, "intake/upload.html",
                          {**_form_context(request), "nav": "intake", "error": _error_message(exc)}, status=400)
        schedule_intake_processing(submission.pk)
        if created:
            messages.info(request, "Upload stored privately and queued for review.")
        else:
            messages.info(request, "This photo or barcode was already submitted; showing the existing record.")
        return redirect("intake:detail", submission_id=submission.pk)
    return render(request, "intake/upload.html", {**_form_context(request), "nav": "intake"})


@login_required
def intake_detail(request, submission_id):
    household = current_household(request)
    submission = get_object_or_404(IntakeSubmission.objects.for_household(household), pk=submission_id)
    candidate = getattr(submission, "candidate", None)
    return render(request, "intake/detail.html", {
        "nav": "intake",
        "submission": submission,
        "candidate": candidate,
        "evidence": candidate.field_evidence.all() if candidate is not None else [],
        "verification_requests": submission.verification_requests.all(),
    })


@login_required
def intake_image(request, submission_id):
    household = current_household(request)
    submission = get_object_or_404(IntakeSubmission.objects.for_household(household), pk=submission_id)
    if not submission.image_key:
        raise Http404("No private image is stored for this intake.")
    try:
        path = storage.image_path(submission.image_key)
    except ValueError:
        raise Http404("No private image is stored for this intake.")
    if not path.is_file():
        raise Http404("No private image is stored for this intake.")
    response = FileResponse(path.open("rb"),
                            content_type=submission.image_content_type or "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    extension = storage.extension_for(submission.image_content_type)
    response["Content-Disposition"] = f'inline; filename="intake-{submission.pk}{extension}"'
    return response


@login_required
@require_POST
def intake_resolve(request, submission_id):
    household = current_household(request)
    get_object_or_404(IntakeSubmission.objects.for_household(household), pk=submission_id)
    corrections = {}
    for field_name in ("name", "category", "brand", "model_number", "barcode", "quantity", "unit",
                       "weight_grams"):
        value = request.POST.get(field_name, "").strip()
        if value:
            corrections[field_name] = value
    if "allergens" in request.POST and request.POST.get("allergens", "").strip():
        corrections["allergens"] = request.POST["allergens"]
    try:
        resolve_intake(
            submission_id,
            actor=request.user,
            decision={
                "action": request.POST.get("action", ""),
                "corrections": corrections,
                "note": request.POST.get("note", ""),
            },
        )
    except (ValidationError, PermissionDenied, IntakeSubmission.DoesNotExist) as exc:
        messages.error(request, str(exc))
    return redirect("intake:detail", submission_id=submission_id)
