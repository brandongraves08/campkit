from datetime import timedelta
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import AccessToken, get_application_model

from apps.households.models import Household, Membership, MembershipRole
from apps.inventory.models import GearItem
from apps.trips.models import Trip, TripGear

pytestmark = pytest.mark.django_db

CALLBACK = "com.graveystudios.campkit://oauth/callback"
SCOPE = "mobile:packing"
BASE = "/api/mobile/v1/"


@pytest.fixture
def member(db):
    return get_user_model().objects.create_user(username="sync-member", password="irrelevant")


@pytest.fixture
def mobile_app(member):
    return get_application_model().objects.create(
        user=member, name="CampKit", client_type="public",
        authorization_grant_type="authorization-code", redirect_uris=CALLBACK,
    )


@pytest.fixture
def token_factory(member, mobile_app):
    def create(*, user=member, token="sync-token"):
        return AccessToken.objects.create(
            user=user, application=mobile_app, token=token,
            expires=timezone.now() + timedelta(minutes=10), scope=SCOPE,
        )
    return create


@pytest.fixture
def authed_client(token_factory):
    token_factory()
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = "Bearer sync-token"
    return client


def _household(name, user):
    household = Household.objects.create(name=name)
    Membership.objects.create(household=household, user=user, role=MembershipRole.MEMBER)
    return household


def _packing(household, member, name="Weekend", status=Trip.Status.PLANNED):
    trip = Trip.objects.create(household=household, created_by=member, name=name, status=status)
    gear = GearItem.objects.create(household=household, name="Tent", category="shelter")
    entry = TripGear.objects.create(trip=trip, gear_item=gear)
    return trip, entry


def _path(household, trip, entry):
    return f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/{entry.pk}/"


def _payload(*, packed=True, operation_id=None, base_version=1):
    return {"packed": packed, "client_operation_id": str(operation_id or uuid.uuid4()), "base_version": base_version}


def _before(entry):
    entry.refresh_from_db()
    return entry.packed, entry.version


@pytest.mark.parametrize("credential", ["missing", "inactive", "userless"])
def test_packing_write_rejects_invalid_member_credentials_without_mutation(
    member, token_factory, credential
):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    headers = {}
    if credential == "inactive":
        token_factory()
        member.is_active = False
        member.save(update_fields=["is_active"])
        headers = {"HTTP_AUTHORIZATION": "Bearer sync-token"}
    elif credential == "userless":
        token_factory(user=None)
        headers = {"HTTP_AUTHORIZATION": "Bearer sync-token"}

    response = Client().put(_path(household, trip, entry), data=_payload(), content_type="application/json", **headers)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required."}}
    assert _before(entry) == (False, 1)


def test_new_operation_updates_state_once_and_same_request_replays_original_result(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    operation_id = uuid.uuid4()
    payload = _payload(operation_id=operation_id)

    first = authed_client.put(_path(household, trip, entry), data=payload, content_type="application/json")
    replay = authed_client.put(_path(household, trip, entry), data=payload, content_type="application/json")

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {
        "entry_id": str(entry.pk), "packed": True, "version": 2, "client_operation_id": str(operation_id)
    }
    assert _before(entry) == (True, 2)


def test_same_operation_id_with_changed_payload_conflicts_without_mutation(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    operation_id = uuid.uuid4()
    first = authed_client.put(
        _path(household, trip, entry), data=_payload(operation_id=operation_id), content_type="application/json"
    )
    changed = authed_client.put(
        _path(household, trip, entry),
        data=_payload(packed=False, operation_id=operation_id), content_type="application/json",
    )
    assert first.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_key_reused"
    assert _before(entry) == (True, 2)


def test_stale_base_version_returns_current_state_and_does_not_mutate(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    entry.packed = True
    entry.version = 2
    entry.save(update_fields=["packed", "version"])

    response = authed_client.put(
        _path(household, trip, entry), data=_payload(packed=False, base_version=1), content_type="application/json"
    )
    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "version_conflict", "message": "Packing entry changed.", "packed": True, "version": 2}
    }
    assert _before(entry) == (True, 2)


def test_distinct_packing_rows_have_independent_versions(member, authed_client):
    household = _household("Camp", member)
    trip, first = _packing(household, member)
    gear = GearItem.objects.create(household=household, name="Stove", category="cooking")
    second = TripGear.objects.create(trip=trip, gear_item=gear)
    first_response = authed_client.put(_path(household, trip, first), data=_payload(), content_type="application/json")
    second_response = authed_client.put(_path(household, trip, second), data=_payload(), content_type="application/json")
    assert first_response.status_code == second_response.status_code == 200
    assert (first_response.json()["version"], second_response.json()["version"]) == (2, 2)
    assert _before(first) == _before(second) == (True, 2)


@pytest.mark.parametrize("status", [Trip.Status.COMPLETED, Trip.Status.CANCELLED])
def test_completed_or_cancelled_trip_returns_stable_read_only_error(member, authed_client, status):
    household = _household("Camp", member)
    trip, entry = _packing(household, member, status=status)
    response = authed_client.put(_path(household, trip, entry), data=_payload(), content_type="application/json")
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "trip_read_only", "message": "Trip packing is read-only."}}
    assert _before(entry) == (False, 1)


def test_deleted_foreign_and_cross_trip_rows_share_not_found_without_mutation(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    other_trip = Trip.objects.create(household=household, created_by=member, name="Other")
    foreign_user = get_user_model().objects.create_user(username="foreign-sync")
    foreign_household = _household("Private", foreign_user)
    foreign_trip, foreign_entry = _packing(foreign_household, foreign_user)
    deleted_trip, deleted_entry = _packing(household, member, name="Deleted")
    deleted_entry_id = deleted_entry.pk
    deleted_entry.delete()
    paths = [
        _path(household, other_trip, entry),
        f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/999999/",
        _path(household, trip, foreign_entry),
        f"{BASE}households/{household.pk}/trips/{deleted_trip.pk}/packing/{deleted_entry_id}/",
    ]
    for path in paths:
        response = authed_client.put(path, data=_payload(), content_type="application/json")
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "Not found."}}
    assert _before(entry) == (False, 1)
    assert _before(foreign_entry) == (False, 1)
    foreign_trip.refresh_from_db()


def test_operation_idempotency_is_scoped_per_member(member, authed_client, token_factory):
    household = _household("Camp", member)
    trip, first = _packing(household, member)
    second_gear = GearItem.objects.create(household=household, name="Stove", category="cooking")
    second = TripGear.objects.create(trip=trip, gear_item=second_gear)
    operation_id = uuid.uuid4()
    first_result = authed_client.put(
        _path(household, trip, first), data=_payload(operation_id=operation_id), content_type="application/json"
    )

    other_member = get_user_model().objects.create_user(username="second-sync-member")
    Membership.objects.create(household=household, user=other_member, role=MembershipRole.MEMBER)
    token_factory(user=other_member, token="second-sync-token")
    second_client = Client()
    second_client.defaults["HTTP_AUTHORIZATION"] = "Bearer second-sync-token"
    second_result = second_client.put(
        _path(household, trip, second), data=_payload(operation_id=operation_id), content_type="application/json"
    )

    assert first_result.status_code == second_result.status_code == 200
    assert first_result.json()["entry_id"] != second_result.json()["entry_id"]
    assert _before(first) == _before(second) == (True, 2)


def test_packing_sync_accepts_only_put(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    response = authed_client.post(_path(household, trip, entry), data=_payload(), content_type="application/json")
    assert response.status_code == 405
    assert _before(entry) == (False, 1)


def test_invalid_sync_shape_is_rejected_without_mutation(member, authed_client):
    household = _household("Camp", member)
    trip, entry = _packing(household, member)
    invalid_payloads = [
        {"packed": "true", "client_operation_id": str(uuid.uuid4()), "base_version": 1},
        {"packed": True, "client_operation_id": "not-a-uuid", "base_version": 1},
        {"packed": True, "client_operation_id": str(uuid.uuid4()), "base_version": 0},
        {"packed": True, "client_operation_id": str(uuid.uuid4()), "base_version": True},
        {"packed": True, "client_operation_id": str(uuid.uuid4())},
    ]
    for payload in invalid_payloads:
        response = authed_client.put(_path(household, trip, entry), data=payload, content_type="application/json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert _before(entry) == (False, 1)
