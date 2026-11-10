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
    return get_user_model().objects.create_user(username="mobile-member", password="irrelevant")


@pytest.fixture
def mobile_app(member):
    return get_application_model().objects.create(
        user=member,
        name="CampKit native app",
        client_type="public",
        authorization_grant_type="authorization-code",
        redirect_uris=CALLBACK,
    )


@pytest.fixture
def token_factory(member, mobile_app):
    def create(*, scope=SCOPE, expires=None, token="member-token", user=member):
        return AccessToken.objects.create(
            user=user,
            application=mobile_app,
            token=token,
            expires=expires or timezone.now() + timedelta(minutes=10),
            scope=scope,
        )
    return create


@pytest.fixture
def authed_client(token_factory):
    token_factory()
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = "Bearer member-token"
    return client


def _household(name, user):
    household = Household.objects.create(name=name)
    Membership.objects.create(household=household, user=user, role=MembershipRole.MEMBER)
    return household


def _trip(household, actor, name="Weekend", **kwargs):
    return Trip.objects.create(household=household, created_by=actor, name=name, **kwargs)


def test_household_discovery_returns_only_member_households_and_empty_list(member, token_factory):
    first = _household("First", member)
    second = _household("Second", member)
    outsider = get_user_model().objects.create_user(username="outsider")
    _household("Private", outsider)
    client = Client()
    token_factory()
    client.defaults["HTTP_AUTHORIZATION"] = "Bearer member-token"

    response = client.get(BASE + "households/")
    assert response.status_code == 200
    assert response.json() == {"households": [
        {"id": str(first.pk), "name": "First"},
        {"id": str(second.pk), "name": "Second"},
    ]}

    Membership.objects.filter(user=member).delete()
    assert client.get(BASE + "households/").json() == {"households": []}


def test_trip_list_and_packing_snapshot_have_only_contract_fields(member, authed_client):
    household = _household("Camp", member)
    trip = _trip(household, member, destination="Ridge", conditions="Cold", party_size=3)
    gear = GearItem.objects.create(household=household, name="Tent", category="shelter", owner="Alex")
    entry = TripGear.objects.create(trip=trip, gear_item=gear, quantity=2, packed=True)

    trips = authed_client.get(f"{BASE}households/{household.pk}/trips/")
    assert trips.status_code == 200
    assert trips.json() == {"trips": [{
        "id": str(trip.pk), "name": "Weekend", "destination": "Ridge",
        "start_date": None, "end_date": None, "party_size": 3,
        "conditions": "Cold", "status": Trip.Status.PLANNED,
    }]}

    snapshot = authed_client.get(f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/")
    assert snapshot.status_code == 200
    assert snapshot.json() == {"trip": {"id": str(trip.pk), "name": "Weekend"}, "packing": [{
        "id": entry.pk, "packed": True, "quantity": 2,
        "gear": {"id": str(gear.pk), "name": "Tent", "category": "shelter"},
    }]}
    payload = str(snapshot.json()).lower()
    assert "meal" not in payload and "pantry" not in payload
    assert "provenance" not in payload and "version" not in payload
    assert "owner" not in payload and "conditions" not in str(snapshot.json())


def test_empty_trip_and_packing_lists_are_empty(member, authed_client):
    household = _household("Camp", member)
    trip = _trip(household, member)
    assert authed_client.get(f"{BASE}households/{household.pk}/trips/").json() == {"trips": [{
        "id": str(trip.pk), "name": "Weekend", "destination": "", "start_date": None,
        "end_date": None, "party_size": 1, "conditions": "", "status": Trip.Status.PLANNED,
    }]}
    assert authed_client.get(f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/").json() == {
        "trip": {"id": str(trip.pk), "name": "Weekend"}, "packing": []
    }


def test_mobile_read_endpoints_reject_non_get_methods(member, authed_client):
    household = _household("Camp", member)
    trip = _trip(household, member)
    for path in (f"{BASE}households/", f"{BASE}households/{household.pk}/trips/",
                 f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/"):
        assert authed_client.post(path).status_code == 405


@pytest.mark.parametrize("credential", ["missing", "unknown", "expired", "revoked", "malformed", "underscoped"])
def test_invalid_credentials_share_401_and_do_not_write(member, mobile_app, token_factory, credential):
    household = _household("Camp", member)
    trip = _trip(household, member)
    if credential == "expired":
        token_factory(expires=timezone.now() - timedelta(seconds=1), token="expired-token")
    elif credential == "revoked":
        AccessToken.objects.create(user=member, application=mobile_app, token="revoked-token",
                                   expires=timezone.now() + timedelta(minutes=10), scope=SCOPE).delete()
    elif credential == "underscoped":
        token_factory(scope="", token="underscoped-token")
    tokens = {
        "unknown": "unknown-token", "expired": "expired-token", "revoked": "revoked-token",
        "malformed": "Bearer invalid extra", "underscoped": "underscoped-token",
    }
    headers = {} if credential == "missing" else {"HTTP_AUTHORIZATION": tokens[credential]}
    paths = [
        f"{BASE}households/",
        f"{BASE}households/{household.pk}/trips/",
        f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/",
    ]
    before = (Household.objects.count(), Trip.objects.count(), GearItem.objects.count(), TripGear.objects.count())

    for path in paths:
        response = Client().get(path, **headers)
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required."}}
    assert (Household.objects.count(), Trip.objects.count(), GearItem.objects.count(), TripGear.objects.count()) == before


@pytest.mark.parametrize("route", ["households", "trips", "packing"])
def test_deactivated_user_cannot_read_mobile_resources_without_writes(member, token_factory, route):
    household = _household("Camp", member)
    trip = _trip(household, member)
    gear = GearItem.objects.create(household=household, name="Tent", category="shelter")
    TripGear.objects.create(trip=trip, gear_item=gear)
    token_factory()
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = "Bearer member-token"
    paths = {
        "households": f"{BASE}households/",
        "trips": f"{BASE}households/{household.pk}/trips/",
        "packing": f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/",
    }
    member.is_active = False
    member.save(update_fields=["is_active"])
    before = (Household.objects.count(), Membership.objects.count(), Trip.objects.count(),
              GearItem.objects.count(), TripGear.objects.count())

    response = client.get(paths[route])

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required."}}
    assert b"Camp" not in response.content and b"Weekend" not in response.content and b"Tent" not in response.content
    assert (Household.objects.count(), Membership.objects.count(), Trip.objects.count(),
            GearItem.objects.count(), TripGear.objects.count()) == before


@pytest.mark.parametrize("route", ["households", "trips", "packing"])
def test_token_without_member_user_cannot_read_mobile_resources_without_writes(member, token_factory, route):
    household = _household("Camp", member)
    trip = _trip(household, member)
    gear = GearItem.objects.create(household=household, name="Tent", category="shelter")
    TripGear.objects.create(trip=trip, gear_item=gear)
    token_factory(user=None)
    client = Client()
    client.defaults["HTTP_AUTHORIZATION"] = "Bearer member-token"
    paths = {
        "households": f"{BASE}households/",
        "trips": f"{BASE}households/{household.pk}/trips/",
        "packing": f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/",
    }
    before = (Household.objects.count(), Membership.objects.count(), Trip.objects.count(),
              GearItem.objects.count(), TripGear.objects.count())

    response = client.get(paths[route])

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "unauthorized", "message": "Authentication required."}}
    assert b"Camp" not in response.content and b"Weekend" not in response.content and b"Tent" not in response.content
    assert (Household.objects.count(), Membership.objects.count(), Trip.objects.count(),
            GearItem.objects.count(), TripGear.objects.count()) == before


def test_active_user_can_read_authorized_mobile_household_routes(member, authed_client):
    household = _household("Camp", member)
    trip = _trip(household, member)

    assert member.is_active is True
    assert authed_client.get(f"{BASE}households/").status_code == 200
    assert authed_client.get(f"{BASE}households/{household.pk}/trips/").status_code == 200
    assert authed_client.get(f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/").status_code == 200


def test_revoked_membership_denies_household_trip_and_packing_reads(member, authed_client):
    household = _household("Camp", member)
    trip = _trip(household, member)
    Membership.objects.filter(household=household, user=member).delete()

    for path in (f"{BASE}households/{household.pk}/trips/",
                 f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/"):
        response = authed_client.get(path)
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "Not found."}}


def test_foreign_missing_and_malformed_ids_are_indistinguishable(member, authed_client):
    household = _household("Mine", member)
    foreign_user = get_user_model().objects.create_user(username="foreign")
    foreign_household = _household("Theirs", foreign_user)
    foreign_trip = _trip(foreign_household, foreign_user, "Secret")
    missing_household_id = uuid.uuid4()
    missing_trip_id = uuid.uuid4()
    responses = [
        authed_client.get(f"{BASE}households/{foreign_household.pk}/trips/"),
        authed_client.get(f"{BASE}households/{missing_household_id}/trips/"),
        authed_client.get(f"{BASE}households/not-a-uuid/trips/"),
        authed_client.get(f"{BASE}households/{household.pk}/trips/{foreign_trip.pk}/packing/"),
        authed_client.get(f"{BASE}households/{household.pk}/trips/{missing_trip_id}/packing/"),
        authed_client.get(f"{BASE}households/{household.pk}/trips/not-a-uuid/packing/"),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "Not found."}}


def test_canceled_and_completed_trips_remain_readable(member, authed_client):
    household = _household("Camp", member)
    for status in (Trip.Status.CANCELLED, Trip.Status.COMPLETED):
        trip = _trip(household, member, name=status.value, status=status)
        response = authed_client.get(f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/")
        assert response.status_code == 200
        assert response.json()["trip"] == {"id": str(trip.pk), "name": status}


def test_foreign_household_gear_never_leaks_into_snapshot(member, authed_client):
    household = _household("Camp", member)
    foreign_user = get_user_model().objects.create_user(username="foreign")
    foreign_household = _household("Other", foreign_user)
    trip = _trip(household, member)
    foreign_gear = GearItem.objects.create(household=foreign_household, name="Secret Stove", category="cooking")
    # Simulate legacy/inconsistent data that bypassed model.clean().
    entry = TripGear.objects.create(trip=trip, gear_item=foreign_gear)

    response = authed_client.get(f"{BASE}households/{household.pk}/trips/{trip.pk}/packing/")
    assert response.status_code == 200
    assert entry.pk not in [row["id"] for row in response.json()["packing"]]
    assert "Secret Stove" not in response.content.decode()
