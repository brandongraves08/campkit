"""Issue, revoke, and authenticate household-scoped API tokens.

Both the REST views and the MCP server authenticate through
:func:`authenticate_token`, so there is exactly one definition of "usable
credential". Every rejected credential raises the same :class:`TokenRejected`
error with no detail, and a rejected call performs no write.
"""

import hmac
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.api.models import PREFIX_LENGTH, TOKEN_PREFIX, ApiToken
from apps.households.models import Membership, MembershipRole


class TokenRejected(Exception):
    """Raised for missing, malformed, unknown, revoked, or expired tokens.

    Callers deliberately cannot distinguish these cases and must not leak which
    one applied: the response is identical for all of them.
    """


@dataclass(frozen=True)
class ServiceIdentity:
    """The household and service actor a valid token resolves to."""

    household: object
    actor: object
    token: ApiToken | None = None

    @property
    def actor_name(self) -> str:
        return self.actor.get_username()


def service_username() -> str:
    return str(getattr(settings, "CAMPKIT_SERVICE_USERNAME", "hermes") or "hermes")


@transaction.atomic
def service_user():
    """Return the single dedicated service identity used by API/MCP writes."""
    username = service_username()
    user, created = get_user_model().objects.get_or_create(
        username=username,
        defaults={"email": f"{username}@campkit.invalid", "is_active": True},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


@transaction.atomic
def issue_token(*, household, label="", expires_at=None):
    """Create a token for ``household`` bound to the service identity.

    Returns ``(token_row, raw_token)``. The raw value is shown once; only its
    digest is stored. The service identity is added as a household member so the
    existing membership boundary keeps applying to every wrapped service call.
    """
    actor = service_user()
    Membership.objects.get_or_create(
        household=household, user=actor, defaults={"role": MembershipRole.MEMBER}
    )
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(
        household=household,
        service_user=actor,
        label=str(label or "")[:80],
        key_prefix=raw_token[:PREFIX_LENGTH],
        key_hash=ApiToken.hash_key(raw_token),
        expires_at=expires_at,
    )
    return token, raw_token


def revoke_token(token, *, when=None):
    """Revoke a token. Idempotent; a revoked token can never authenticate again."""
    if token.revoked_at is None:
        token.revoked_at = when or timezone.now()
        token.save(update_fields=["revoked_at"])
    return token


def authenticate_token(raw_token) -> ServiceIdentity:
    """Resolve a bearer token to its household and service actor.

    Raises :class:`TokenRejected` for every unusable value. The actor's current
    membership is re-checked on each call, so removing the service identity from
    a household disables that household's tokens immediately.
    """
    if not isinstance(raw_token, str):
        raise TokenRejected
    candidate = raw_token.strip()
    if not candidate.startswith(TOKEN_PREFIX) or len(candidate) <= PREFIX_LENGTH:
        raise TokenRejected
    token = (
        ApiToken.objects.select_related("household", "service_user")
        .filter(key_prefix=candidate[:PREFIX_LENGTH], revoked_at__isnull=True)
        .first()
    )
    if token is None:
        raise TokenRejected
    if not hmac.compare_digest(token.key_hash, ApiToken.hash_key(candidate)):
        raise TokenRejected
    if token.is_expired:
        raise TokenRejected
    if not Membership.objects.filter(
        user_id=token.service_user_id, household_id=token.household_id
    ).exists():
        raise TokenRejected
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return ServiceIdentity(household=token.household, actor=token.service_user, token=token)


def bearer_value(header_value) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not isinstance(header_value, str):
        raise TokenRejected
    scheme, _, value = header_value.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise TokenRejected
    return value.strip()
