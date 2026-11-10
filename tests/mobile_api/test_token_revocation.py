from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import AccessToken, RefreshToken, get_application_model

pytestmark = pytest.mark.django_db

CALLBACK = "com.graveystudios.campkit://oauth/callback"
SCOPE = "mobile:packing"


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="member", password="irrelevant")


@pytest.fixture
def oauth_application(user):
    return get_application_model().objects.create(
        user=user,
        name="CampKit native app",
        client_type="public",
        authorization_grant_type="authorization-code",
        redirect_uris=CALLBACK,
    )


def test_access_token_expiry_is_finite_and_oauth_provider_rejects_expired_token(user, oauth_application):
    from oauth2_provider.settings import oauth2_settings

    assert oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS == 900
    access_token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token="expired-member-token",
        expires=timezone.now() - timedelta(seconds=1),
        scope=SCOPE,
    )
    assert access_token.is_expired()


def test_revocation_endpoint_invalidates_access_token(user, oauth_application):
    token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token="member-token-to-revoke",
        expires=timezone.now() + timedelta(minutes=10),
        scope=SCOPE,
    )
    response = Client().post("/o/revoke_token/", {"token": token.token, "client_id": oauth_application.client_id})
    assert response.status_code == 200
    assert not AccessToken.objects.filter(pk=token.pk).exists()


def test_refresh_reuse_protection_is_enabled():
    from oauth2_provider.settings import oauth2_settings

    assert oauth2_settings.REFRESH_TOKEN_REUSE_PROTECTION is True
    assert oauth2_settings.ROTATE_REFRESH_TOKEN is True


def test_refresh_token_rotates_and_invalidates_the_previous_token(user, oauth_application):
    access_token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token="old-access-token",
        expires=timezone.now() + timedelta(minutes=10),
        scope=SCOPE,
    )
    refresh_token = RefreshToken.objects.create(
        user=user,
        application=oauth_application,
        access_token=access_token,
        token="old-refresh-token",
    )
    response = Client().post("/o/token/", {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.token,
        "client_id": oauth_application.client_id,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["refresh_token"] != refresh_token.token
    refresh_token.refresh_from_db()
    assert refresh_token.revoked is not None


def test_mobile_tokens_cannot_receive_non_mobile_scope(user, oauth_application):
    from oauth2_provider.settings import oauth2_settings

    assert set(oauth2_settings.SCOPES) == {SCOPE}
    assert oauth2_settings.DEFAULT_SCOPES == [SCOPE]
    assert "write" not in oauth2_settings.SCOPES
