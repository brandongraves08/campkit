"""Shared helpers for the household API tests."""

import json

import pytest

from apps.api.auth import issue_token


@pytest.fixture
def tokens(household, other_household):
    """Two household-scoped tokens issued for the shared service identity.

    The service identity is a member of *both* households here on purpose: it
    proves that the token's household — not the actor's membership list — is
    what pins every read and write.
    """
    _, primary = issue_token(household=household, label="primary")
    _, other = issue_token(household=other_household, label="other")
    return {"primary": primary, "other": other, "primary_id": primary[:12]}


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def get(client, path, token):
    return client.get(path, **auth(token))


def post(client, path, payload, token):
    return client.post(
        path, data=json.dumps(payload), content_type="application/json", **auth(token)
    )


def patch(client, path, payload, token):
    return client.patch(
        path, data=json.dumps(payload), content_type="application/json", **auth(token)
    )
