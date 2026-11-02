import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.intake import storage
from apps.intake.models import IntakeSubmission
from apps.intake.services import process_submission, submit_intake


def _submit(actor, household, *, image, content_type="image/png", kind="pantry",
            channel="photo", barcode="", process=False):
    return submit_intake(
        actor=actor,
        household=household,
        kind=kind,
        channel=channel,
        barcode=barcode,
        image_bytes=image,
        declared_content_type=content_type,
        process=process,
    )


class _Upload:
    """Minimal in-memory upload object accepted by the intake view."""

    def __init__(self, data, name, content_type):
        self._data = data
        self.name = name
        self.content_type = content_type
        self.size = len(data)

    def read(self, *args):
        return self._data


@pytest.mark.django_db
def test_rejects_unsupported_or_spoofed_image_types(owner, household, png_photo, use_vision,
                                                     fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    with pytest.raises(ValidationError):
        _submit(owner, household, image=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    with pytest.raises(ValidationError):
        # PNG bytes that lie about being a JPEG.
        _submit(owner, household, image=png_photo, content_type="image/jpeg")
    assert IntakeSubmission.objects.count() == 0


@pytest.mark.django_db
def test_rejects_uploads_over_the_configured_size_limit(owner, household, png_photo, settings,
                                                         use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    settings.INTAKE_MAX_IMAGE_BYTES = 1024
    with pytest.raises(ValidationError):
        _submit(owner, household, image=png_photo + b"\x00" * 2048)
    assert IntakeSubmission.objects.count() == 0


@pytest.mark.django_db
def test_rejects_images_with_absurd_dimensions(owner, household, oversized_png_factory,
                                                use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    with pytest.raises(ValidationError):
        _submit(owner, household, image=oversized_png_factory(20000, 20000))
    assert IntakeSubmission.objects.count() == 0


@pytest.mark.django_db
def test_upload_requires_authentication(client, household):
    response = client.get(reverse("intake:create"))
    assert response.status_code == 302
    assert reverse("login") in response["Location"]
    response = client.post(reverse("intake:create"), {"kind": "pantry", "channel": "photo"})
    assert response.status_code == 302
    assert IntakeSubmission.objects.count() == 0


@pytest.mark.django_db
def test_upload_rejects_callers_outside_the_household(owner, household, png_photo, other_household,
                                                       use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    with pytest.raises(PermissionDenied):
        _submit(owner, other_household, image=png_photo)
    assert IntakeSubmission.objects.count() == 0


@pytest.mark.django_db
def test_private_image_cannot_be_fetched_by_other_households(client, owner, household, other_owner,
                                                             png_photo, use_vision, fake_vision,
                                                             make_payload):
    use_vision(fake_vision(payload=make_payload()))
    submission, _ = _submit(owner, household, image=png_photo)
    process_submission(submission.pk)
    submission.refresh_from_db()
    assert submission.image_key

    client.force_login(other_owner)
    assert client.get(reverse("intake:image", args=[submission.pk])).status_code == 404
    assert client.get(reverse("intake:detail", args=[submission.pk])).status_code == 404
    assert client.post(reverse("intake:resolve", args=[submission.pk]), {"action": "reject"}).status_code == 404

    client.logout()
    response = client.get(reverse("intake:image", args=[submission.pk]))
    assert response.status_code == 302
    assert reverse("login") in response["Location"]


@pytest.mark.django_db
def test_household_member_can_fetch_the_private_original(client, owner, member, household, png_photo,
                                                          use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    submission, _ = _submit(owner, household, image=png_photo)
    client.force_login(member)
    response = client.get(reverse("intake:image", args=[submission.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response["Cache-Control"]
    assert b"".join(response.streaming_content) == png_photo


@pytest.mark.django_db
def test_uploads_are_private_on_disk_and_traversal_safe(owner, household, png_photo, settings,
                                                         use_vision, fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    submission, _ = _submit(owner, household, image=png_photo)
    path = storage.image_path(submission.image_key)
    assert path.is_file()
    assert settings.INTAKE_UPLOAD_ROOT.resolve() in path.resolve().parents
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        storage.image_path("../escape.png")
    with pytest.raises(ValueError):
        storage.image_path("/etc/passwd")


@pytest.mark.django_db
def test_upload_view_reports_validation_errors_without_storing_anything(client, owner, use_vision,
                                                                        fake_vision, make_payload):
    use_vision(fake_vision(payload=make_payload()))
    client.force_login(owner)
    response = client.post(
        reverse("intake:create"),
        {"kind": "pantry", "channel": "photo", "image": _Upload(b"not-an-image", "fake.png", "image/png")},
    )
    assert response.status_code == 400
    assert IntakeSubmission.objects.count() == 0
