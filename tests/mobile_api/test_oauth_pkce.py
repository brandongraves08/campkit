from urllib.parse import urlencode

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client
from oauth2_provider.models import get_application_model
from oauth2_provider.settings import oauth2_settings

pytestmark = pytest.mark.django_db

CALLBACK = "com.graveystudios.campkit://oauth/callback"
SCOPE = "mobile:packing"


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="member", password="irrelevant")


@pytest.fixture
def oauth_application(db, user):
    return get_application_model().objects.create(
        user=user,
        name="CampKit native app",
        client_type="public",
        authorization_grant_type="authorization-code",
        redirect_uris=CALLBACK,
    )


def _authorize_params(application, **overrides):
    params = {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": CALLBACK,
        "scope": SCOPE,
        "code_challenge": "A" * 43,
        "code_challenge_method": "S256",
    }
    return {**params, **overrides}


def test_provider_settings_are_minimal_and_secure():
    assert oauth2_settings.OAUTH2_GRANT_TYPES_SUPPORTED == ["authorization_code", "refresh_token"]
    assert oauth2_settings.OAUTH2_RESPONSE_TYPES_SUPPORTED == ["code"]
    assert oauth2_settings.PKCE_REQUIRED is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_PKCE_METHOD is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_PASSWORD_GRANT is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_PKCE_REQUIRED is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_REDIRECT_URI_MATCHING is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME is True
    assert oauth2_settings.COMPLIANT_BCP_RFC9700_REFRESH_TOKEN is True
    assert oauth2_settings.ALLOW_URI_WILDCARDS is False
    assert oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES == ["com.graveystudios.campkit"]
    assert oauth2_settings.SCOPES == {SCOPE: "Mobile packing access"}
    assert oauth2_settings.DEFAULT_SCOPES == [SCOPE]
    assert settings.OAUTH2_PROVIDER["ACCESS_TOKEN_EXPIRE_SECONDS"] == 900
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_EXPIRE_SECONDS"] == 30 * 24 * 60 * 60
    assert oauth2_settings.ROTATE_REFRESH_TOKEN is True
    assert oauth2_settings.REFRESH_TOKEN_REUSE_PROTECTION is True
    assert oauth2_settings.REFRESH_TOKEN_GRACE_PERIOD_SECONDS == 0
    assert oauth2_settings.DCR_ENABLED is False


def test_authorization_uses_existing_member_browser_login(oauth_application):
    response = Client().get("/o/authorize/?" + urlencode(_authorize_params(oauth_application)))
    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/?")


def test_public_client_is_authorization_code_only_and_has_exact_callback(oauth_application):
    assert oauth_application.client_type == "public"
    assert oauth_application.authorization_grant_type == "authorization-code"
    assert oauth_application.redirect_uris.split() == [CALLBACK]


@pytest.mark.parametrize("credential_state", ["valid", "unknown", "expired", "revoked"])
def test_bearer_authentication_resolves_member_and_denies_invalid_credentials_without_writes(
    user, oauth_application, credential_state
):
    from datetime import timedelta
    from django.test import RequestFactory
    from django.utils import timezone
    from oauth2_provider.models import AccessToken
    from apps.mobile_api.auth import authenticate_mobile_request

    if credential_state == "valid":
        token_value = "valid-member-token"
        token = AccessToken.objects.create(
            user=user, application=oauth_application, token=token_value,
            expires=timezone.now() + timedelta(minutes=10), scope=SCOPE,
        )
    elif credential_state == "unknown":
        token_value = "unknown-member-token"
    else:
        token_value = f"{credential_state}-member-token"
        token = AccessToken.objects.create(
            user=user, application=oauth_application, token=token_value,
            expires=(timezone.now() - timedelta(seconds=1) if credential_state == "expired"
                     else timezone.now() + timedelta(minutes=10)),
            scope=SCOPE,
        )
        if credential_state == "revoked":
            token.delete()
    request = RequestFactory().get("/api/v1/", HTTP_AUTHORIZATION=f"Bearer {token_value}")
    before = AccessToken.objects.count()

    authenticated_token = authenticate_mobile_request(request)

    if credential_state == "valid":
        assert authenticated_token == token
        assert request.user.pk == user.pk
        assert request.user.__class__ is user.__class__
    else:
        assert authenticated_token is None
        assert not hasattr(request, "user") or request.user.is_anonymous
        assert AccessToken.objects.count() == before


@pytest.mark.parametrize("redirect_uri", ["com.graveystudios.campkit://oauth/callback.evil", "https://attacker.example/callback"])
def test_authorization_rejects_unregistered_redirect_uri(oauth_application, user, redirect_uri):
    client = Client()
    client.force_login(user)
    response = client.get("/o/authorize/?" + urlencode(_authorize_params(oauth_application, redirect_uri=redirect_uri)))
    assert response.status_code == 400


@pytest.mark.parametrize("pkce_values", [{"code_challenge": ""}, {"code_challenge_method": "plain"}])
def test_authorization_rejects_missing_or_plain_pkce(oauth_application, user, pkce_values):
    client = Client()
    client.force_login(user)
    response = client.get("/o/authorize/?" + urlencode(_authorize_params(oauth_application, **pkce_values)))
    assert response.status_code == 400


@pytest.mark.parametrize("grant_type", ["password", "client_credentials"])
def test_token_endpoint_rejects_unapproved_grant_types(oauth_application, grant_type):
    response = Client().post("/o/token/", {
        "grant_type": grant_type,
        "client_id": oauth_application.client_id,
        "username": "member",
        "password": "not-a-client-secret",
    })
    assert response.status_code == 400


def test_authorize_endpoint_rejects_implicit_response(oauth_application, user):
    client = Client()
    client.force_login(user)
    response = client.get("/o/authorize/?" + urlencode(_authorize_params(oauth_application, response_type="token")))
    assert response.status_code == 400


def test_authorization_issues_mobile_scope_and_rejects_unregistered_scope(oauth_application, user):
    import base64
    import hashlib

    client = Client()
    client.force_login(user)
    verifier = "A" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = _authorize_params(oauth_application, code_challenge=challenge)

    consent = client.get("/o/authorize/?" + urlencode(params))
    assert consent.status_code == 200
    approved = client.post("/o/authorize/", {**params, "allow": "Authorize"})
    assert approved.status_code == 302
    assert approved["Location"].startswith(CALLBACK + "?")
    code = approved["Location"].split("code=", 1)[1].split("&", 1)[0]
    issued = client.post("/o/token/", {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": oauth_application.client_id,
        "code_verifier": verifier,
    })
    assert issued.status_code == 200
    assert issued.json()["scope"] == SCOPE

    invalid = client.get("/o/authorize/?" + urlencode(_authorize_params(oauth_application, scope="write")))
    assert invalid.status_code == 400
