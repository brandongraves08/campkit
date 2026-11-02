"""End-to-end household acceptance flow (Task 8).

One household, one invited member, and the real surfaces the product ships:
the session web UI, the domain services behind it, the ``/api/v1`` REST layer,
and the Hermes MCP tool adapters. Only two things are substituted, both
deliberately:

* the vision provider is a scripted fake (no network, no real household photo),
* the image bytes are synthetic PNGs generated here.

Nothing else is stubbed: the member really signs up from an invitation, the
photo goes through private storage and the intake service, reservations move
confirmed stock through ``apps.meals.services``, and Hermes reads and writes
through ``apps.api.facade`` via ``apps.mcp.tools``.
"""

import json
import struct
import uuid
import zlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.api.auth import authenticate_token, issue_token
from apps.audit.models import InventoryChange
from apps.households.models import Membership, MembershipRole
from apps.intake.models import (
    IntakeStatus,
    IntakeSubmission,
    VerificationRequest,
    VerificationRequestStatus,
)
from apps.intake.vision import CandidatePayload, EvidenceItem, VisionError
from apps.inventory.models import InventoryVerification, PantryItem
from apps.inventory.services import create_pantry_item
from apps.meals.models import FoodReservation, MealVerificationRequest
from apps.mcp import tools as mcp_tools
from apps.trips.models import Trip

pytestmark = pytest.mark.django_db

MEMBER_EMAIL = "invited-member@example.test"
MEMBER_PASSWORD = "Campkit-e2e-pass-9931"


# --------------------------------------------------------------------------- #
# Synthetic uploads and scripted vision provider
# --------------------------------------------------------------------------- #


def _png(width, height, colour):
    """Build a minimal valid PNG so two uploads have two distinct digests."""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    scanlines = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class ScriptedVisionClient:
    """Deterministic stand-in for the Hermes vision endpoint (no network)."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def analyze(self, image_bytes, *, barcode=""):
        self.calls.append({"bytes": len(image_bytes), "barcode": barcode})
        if not self._payloads:
            raise AssertionError("The flow made an unexpected extra vision call.")
        return self._payloads.pop(0)


class UnavailableVisionClient:
    def analyze(self, image_bytes, *, barcode=""):
        raise VisionError("Vision provider unavailable in this acceptance run.")


def confident_trail_mix():
    """Everything the provider read is above the configured review floor."""

    return CandidatePayload(
        name="Trail mix",
        kind="pantry",
        category="Snacks",
        brand="CampCo",
        barcode="0123456789012",
        quantity=Decimal("2"),
        unit="each",
        nutrition={"calories": 120, "protein_g": 4},
        allergens=("peanuts",),
        identity_confidence=0.95,
        quantity_confidence=0.93,
        nutrition_confidence=0.91,
        allergen_confidence=0.96,
        evidence=(EvidenceItem("name", "Trail mix", "package label", 0.95),),
        provider="fake-vision",
    )


def uncertain_tin():
    """Identity is below the review floor, so nothing may be added automatically."""

    return CandidatePayload(
        name="Noodle tin, maybe",
        kind="pantry",
        category="",
        brand="",
        quantity=Decimal("2"),
        unit="each",
        nutrition={},
        allergens=(),
        identity_confidence=0.55,
        quantity_confidence=0.62,
        nutrition_confidence=0.0,
        allergen_confidence=0.0,
        provider="fake-vision",
    )


@pytest.fixture(autouse=True)
def e2e_settings(tmp_path, settings):
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


def _upload_photo(client, name, payload):
    return client.post(
        "/intake/new/",
        {
            "kind": "pantry",
            "channel": "photo",
            "image": SimpleUploadedFile(name, payload, content_type="image/png"),
        },
    )


def _pantry_named(household, name):
    return PantryItem.objects.get(household=household, name=name)


# --------------------------------------------------------------------------- #
# The acceptance flow
# --------------------------------------------------------------------------- #


def test_invited_member_household_flow_end_to_end(client, owner, household, other_household, settings):
    settings.INTAKE_VISION_CLIENT = ScriptedVisionClient([confident_trail_mix(), uncertain_tin()])

    # 1. Invitation-only sign-up, then a real session login. ----------------- #
    client.force_login(owner)
    invitation = client.post("/accounts/invitations/", {"email": MEMBER_EMAIL})
    assert invitation.status_code == 201
    token = invitation.content.decode().split("Invitation created: ", 1)[1].strip()
    client.logout()

    signup = client.post(
        f"/accounts/signup/{token}/",
        {"email": MEMBER_EMAIL, "password1": MEMBER_PASSWORD, "password2": MEMBER_PASSWORD},
    )
    assert signup.status_code == 302
    member = get_user_model().objects.get(email=MEMBER_EMAIL)
    assert Membership.objects.filter(
        user=member, household=household, role=MembershipRole.MEMBER
    ).exists()
    login = client.post("/accounts/login/", {"username": MEMBER_EMAIL, "password": MEMBER_PASSWORD})
    assert login.status_code == 302
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Smith household" in dashboard.content, "the member sees their own household only"
    assert b'aria-label="Primary"' in dashboard.content

    # 2. Food photo with confident data is auto-added as ESTIMATED. ---------- #
    first_photo = _upload_photo(client, "trail-mix.png", _png(2, 2, (10, 20, 30)))
    assert first_photo.status_code == 302
    second_photo = _upload_photo(client, "noodle-tin.png", _png(3, 2, (40, 50, 60)))
    assert second_photo.status_code == 302

    submissions = list(
        IntakeSubmission.objects.filter(household=household).order_by("created_at", "pk")
    )
    assert len(submissions) == 2, "each distinct photo must create one submission"
    confident, uncertain = submissions[0], submissions[1]

    confident.refresh_from_db()
    assert confident.status == IntakeStatus.ADDED
    assert confident.created_item_kind == "pantry"
    assert confident.image_key and confident.image_sha256
    stored_photo = settings.INTAKE_UPLOAD_ROOT / confident.image_key
    assert stored_photo.is_file(), "the private upload must be stored on disk"

    trail_mix = PantryItem.objects.get(pk=confident.created_item_id)
    assert trail_mix.name == "Trail mix"
    assert trail_mix.verification_status == InventoryVerification.ESTIMATED
    assert trail_mix.verified_by_id is None and trail_mix.verified_at is None
    assert trail_mix.quantity == Decimal("2.000")
    assert trail_mix.estimated_quantity == Decimal("2.000")
    assert trail_mix.confirmed_quantity == Decimal("0.000")
    assert "fake-vision" in trail_mix.source
    assert trail_mix.confidence is not None
    assert not VerificationRequest.objects.filter(
        submission=confident, status=VerificationRequestStatus.OPEN
    ).exists()

    # 3. Uncertain data triggers a verification request, adds nothing. ------- #
    uncertain.refresh_from_db()
    assert uncertain.status == IntakeStatus.NEEDS_REVIEW
    assert uncertain.created_item_id == ""
    assert not PantryItem.objects.filter(household=household, name="Noodle tin, maybe").exists()
    open_request = VerificationRequest.objects.get(
        submission=uncertain, status=VerificationRequestStatus.OPEN
    )
    assert "identity" in open_request.field_names
    assert open_request.hermes_payload["source"] == "campkit.intake"
    assert open_request.reason

    # 4. The member corrects it; correction creates CONFIRMED stock. --------- #
    corrected = client.post(
        f"/intake/{uncertain.pk}/resolve/",
        {
            "action": "correct",
            "name": "Instant noodles",
            "quantity": "3",
            "unit": "each",
            "note": "Read the printed label",
        },
    )
    assert corrected.status_code == 302
    uncertain.refresh_from_db()
    assert uncertain.status == IntakeStatus.ADDED
    noodles = PantryItem.objects.get(pk=uncertain.created_item_id)
    assert noodles.name == "Instant noodles"
    assert noodles.verification_status == InventoryVerification.CONFIRMED
    assert noodles.verified_by_id == member.pk
    assert noodles.verified_at is not None
    assert noodles.quantity == Decimal("3.000")
    assert not VerificationRequest.objects.filter(
        submission=uncertain, status=VerificationRequestStatus.OPEN
    ).exists()
    quantity_evidence = uncertain.candidate.field_evidence.get(field_name="quantity")
    assert quantity_evidence.source == "manual"

    # 5. A meal plan may not reserve estimated stock. ------------------------ #
    start = date.today() + timedelta(days=3)
    trip_response = client.post(
        "/trips/",
        {
            "name": "Ridge weekend",
            "destination": "Blue Ridge",
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=2)).isoformat(),
            "party_size": "3",
            "conditions": "Cold nights, possible rain",
        },
    )
    assert trip_response.status_code == 302
    trip = Trip.objects.get(household=household, name="Ridge weekend")
    assert trip.status == Trip.Status.PLANNED

    noodle_meal = client.post(
        f"/meals/trips/{trip.pk}/",
        {"name": "Noodle dinner", "servings": "2", "pantry_item_id": str(noodles.pk), "quantity": "2"},
    )
    assert noodle_meal.status_code == 302
    trail_meal = client.post(
        f"/meals/trips/{trip.pk}/",
        {"name": "Trail snack", "servings": "2", "pantry_item_id": str(trail_mix.pk), "quantity": "2"},
    )
    assert trail_meal.status_code == 302

    blocked = client.post(f"/meals/trips/{trip.pk}/confirm/", {})
    assert blocked.status_code == 400
    assert b"Estimated pantry stock" in blocked.content
    noodles.refresh_from_db()
    trail_mix.refresh_from_db()
    assert noodles.quantity == Decimal("3.000"), "a refused plan must not move confirmed stock"
    assert trail_mix.quantity == Decimal("2.000")
    assert not FoodReservation.objects.filter(meal__trip=trip).exists()
    assert MealVerificationRequest.objects.filter(
        trip=trip, pantry_item=trail_mix, resolved_at__isnull=True
    ).exists()
    trip.refresh_from_db()
    assert trip.status == Trip.Status.PLANNED

    # 6. The member confirms the auto-added estimate. ------------------------ #
    confirmed = client.post(f"/intake/{confident.pk}/resolve/", {"action": "confirm"})
    assert confirmed.status_code == 302
    trail_mix.refresh_from_db()
    assert trail_mix.verification_status == InventoryVerification.CONFIRMED
    assert trail_mix.verified_by_id == member.pk
    assert trail_mix.verified_at is not None
    assert not VerificationRequest.objects.filter(
        submission=confident, status=VerificationRequestStatus.OPEN
    ).exists()

    # 7. Confirming the plan reserves only confirmed quantities. ------------- #
    plan_confirmed = client.post(f"/meals/trips/{trip.pk}/confirm/", {})
    assert plan_confirmed.status_code == 302
    trip.refresh_from_db()
    noodles.refresh_from_db()
    trail_mix.refresh_from_db()
    assert trip.status == Trip.Status.CONFIRMED
    assert noodles.quantity == Decimal("1.000")
    assert trail_mix.quantity == Decimal("0.000")
    reservations = FoodReservation.objects.filter(meal__trip=trip)
    assert reservations.count() == 2
    assert all(row.status == FoodReservation.Status.RESERVED for row in reservations)
    assert all(row.consumed_quantity == Decimal("0") for row in reservations)

    # 8. Hermes reads and updates the same records without misreporting. ----- #
    _, raw_token = issue_token(household=household, label="e2e-hermes")
    identity = authenticate_token(raw_token)

    listing, is_error = mcp_tools.call_tool("campkit_list_pantry", {}, identity)
    assert is_error is False
    rows = {row["name"]: row for row in listing["pantry"]}
    confirmed_row = rows["Trail mix"]
    assert confirmed_row["is_guaranteed"] is True
    assert confirmed_row["confirmed_quantity"] == "0.000"
    assert confirmed_row["estimated_quantity"] == "0.000"

    added, is_error = mcp_tools.call_tool(
        "campkit_add_pantry_item", {"name": "Oat bars", "quantity": "6", "unit": "each"}, identity
    )
    assert is_error is False
    oat_row = added["pantry_item"]
    assert oat_row["is_guaranteed"] is False
    assert oat_row["estimated_quantity"] == "6.000"
    assert oat_row["confirmed_quantity"] == "0.000"
    assert oat_row["verified_by"] is None

    updated, is_error = mcp_tools.call_tool(
        "campkit_update_pantry_item", {"pantry_item_id": oat_row["id"], "quantity": "7"}, identity
    )
    assert is_error is False
    assert updated["pantry_item"]["quantity"] == "7.000"
    assert updated["pantry_item"]["is_guaranteed"] is False
    assert updated["pantry_item"]["confirmed_quantity"] == "0.000"
    oat_bars = PantryItem.objects.get(pk=oat_row["id"])
    assert oat_bars.verification_status == InventoryVerification.ESTIMATED

    gated, is_error = mcp_tools.call_tool(
        "campkit_confirm_inventory_fact", {"kind": "pantry", "item_id": oat_row["id"]}, identity
    )
    assert is_error is False
    assert gated["status"] == "confirmation_required"
    oat_bars.refresh_from_db()
    assert oat_bars.verification_status == InventoryVerification.ESTIMATED

    promoted, is_error = mcp_tools.call_tool(
        "campkit_confirm_inventory_fact",
        {"kind": "pantry", "item_id": oat_row["id"], "confirm": True},
        identity,
    )
    assert is_error is False
    assert promoted["changed"] is True
    assert promoted["pantry_item"]["is_guaranteed"] is True
    assert promoted["pantry_item"]["confirmed_quantity"] == "7.000"
    assert promoted["pantry_item"]["verified_by"] == "hermes"

    # 9. The token's household pins every read and write. -------------------- #
    _, other_token = issue_token(household=other_household, label="e2e-other")
    other_identity = authenticate_token(other_token)

    other_listing, is_error = mcp_tools.call_tool("campkit_list_pantry", {}, other_identity)
    assert is_error is False
    assert other_listing["count"] == 0

    foreign_trip, is_error = mcp_tools.call_tool(
        "campkit_get_trip", {"trip_id": str(trip.pk)}, other_identity
    )
    assert is_error is True
    assert foreign_trip["error"]["code"] == "not_found"

    foreign_api = client.get(
        f"/api/v1/trips/{trip.pk}/", HTTP_AUTHORIZATION=f"Bearer {other_token}"
    )
    assert foreign_api.status_code == 404
    assert foreign_api.json()["error"]["code"] == "not_found"
    missing_api = client.get(
        f"/api/v1/trips/{uuid.uuid4()}/", HTTP_AUTHORIZATION=f"Bearer {other_token}"
    )
    assert missing_api.status_code == 404
    assert missing_api.json() == foreign_api.json(), (
        "a foreign id and a nonexistent id must stay indistinguishable"
    )

    foreign_patch = client.patch(
        f"/api/v1/pantry/{oat_bars.pk}/",
        data=json.dumps({"quantity": "1"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {other_token}",
    )
    assert foreign_patch.status_code == 404
    oat_bars.refresh_from_db()
    assert oat_bars.quantity == Decimal("7.000")

    # 10. Post-trip reconciliation returns unused stock to the household. ---- #
    by_name = {
        PantryItem.objects.get(pk=row.pantry_item_id).name: row for row in reservations
    }
    reconcile = client.post(
        f"/meals/trips/{trip.pk}/reconcile/",
        {
            f"consumed_{by_name['Instant noodles'].pk}": "1",
            f"consumed_{by_name['Trail mix'].pk}": "2",
        },
    )
    assert reconcile.status_code == 302
    trip.refresh_from_db()
    noodles.refresh_from_db()
    trail_mix.refresh_from_db()
    assert trip.status == Trip.Status.COMPLETED
    assert noodles.quantity == Decimal("2.000"), "one unused portion returns to stock"
    assert trail_mix.quantity == Decimal("0.000"), "a fully consumed item returns nothing"
    assert set(
        FoodReservation.objects.filter(meal__trip=trip).values_list("status", flat=True)
    ) == {FoodReservation.Status.RECONCILED}
    assert InventoryChange.objects.filter(
        household=household, action="food_reconciled"
    ).count() == 1, "only the item with unused stock is written back"

    # 11. Editing a confirmed quantity through the API re-opens verification. #
    energy_bars = create_pantry_item(
        actor=member,
        household=household,
        data={
            "name": "Energy bars",
            "quantity": Decimal("4"),
            "unit": "each",
            "verification_status": InventoryVerification.CONFIRMED,
        },
    )
    assert energy_bars.verified_by_id == member.pk
    edited = client.patch(
        f"/api/v1/pantry/{energy_bars.pk}/",
        data=json.dumps({"quantity": "5"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw_token}",
    )
    assert edited.status_code == 200
    edited_row = edited.json()["pantry_item"]
    assert edited_row["quantity"] == "5.000"
    assert edited_row["verification_status"] == InventoryVerification.ESTIMATED
    assert edited_row["is_guaranteed"] is False
    assert edited_row["confirmed_quantity"] == "0.000"
    assert edited_row["verified_by"] is None
    energy_bars.refresh_from_db()
    assert energy_bars.verification_status == InventoryVerification.ESTIMATED
    assert energy_bars.verified_at is None

    not_writable = client.patch(
        f"/api/v1/pantry/{energy_bars.pk}/",
        data=json.dumps({"verification_status": "confirmed"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw_token}",
    )
    assert not_writable.status_code == 400
    assert not_writable.json()["error"]["code"] == "invalid_request"
    energy_bars.refresh_from_db()
    assert energy_bars.verification_status == InventoryVerification.ESTIMATED

    # 12. The whole flow left an attributable audit trail. ------------------- #
    actions = set(
        InventoryChange.objects.filter(household=household).values_list("action", flat=True)
    )
    assert {
        "intake_added",
        "intake_needs_review",
        "intake_corrected",
        "intake_confirmed",
        "food_reserved",
        "food_reconciled",
        "food_reservation_reconciled",
    } <= actions
    assert not InventoryChange.objects.exclude(household=household).exists()


def test_absent_vision_provider_never_invents_inventory(client, owner, household, settings):
    """With no provider configured, an upload waits for a human instead of guessing."""

    settings.INTAKE_VISION_CLIENT = UnavailableVisionClient()
    settings.INTAKE_MAX_ATTEMPTS = 1
    client.force_login(owner)

    response = _upload_photo(client, "opaque.png", _png(2, 2, (1, 2, 3)))
    assert response.status_code == 302
    submission = IntakeSubmission.objects.get(household=household)
    assert submission.status == IntakeStatus.NEEDS_REVIEW
    assert submission.created_item_id == ""
    assert not PantryItem.objects.filter(household=household).exists()
    request = VerificationRequest.objects.get(
        submission=submission, status=VerificationRequestStatus.OPEN
    )
    assert request.submission_id == submission.pk
    assert timezone.now() >= request.created_at
