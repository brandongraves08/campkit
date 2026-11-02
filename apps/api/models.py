"""Credentials for the household-scoped API and Hermes MCP adapter.

A token is stored as a SHA-256 digest, never in clear text. It is bound to one
household and to the dedicated service identity, so every write made through
the adapter is attributable to ``hermes`` rather than to a household human.
"""

import hashlib
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

#: Prefix that makes a leaked token recognisable to secret scanners.
TOKEN_PREFIX = "ck_"
#: Length of the non-secret lookup fragment kept for operator display.
PREFIX_LENGTH = 12


class ApiToken(models.Model):
    """A revocable, household-scoped credential for the REST/MCP surface."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey(
        "households.Household", on_delete=models.CASCADE, related_name="api_tokens"
    )
    service_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="api_tokens"
    )
    label = models.CharField(max_length=80, blank=True)
    key_prefix = models.CharField(max_length=16)
    key_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("key_prefix",), name="unique_api_token_prefix"),
        ]

    def __str__(self):
        return f"{self.label or 'API token'} for {self.household_id}"

    @staticmethod
    def hash_key(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()
