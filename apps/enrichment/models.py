"""Sourced enrichment state owned by this app.

Nutrition facts and price observations stay in :mod:`apps.inventory` (Task 2's
provenance shape) and are written through its services. This app only owns what
is specific to enrichment: cached provider responses with their fetch time, and
the dated citations produced by product research.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models


class EnrichmentCache(models.Model):
    """A provider response kept with the time it was fetched.

    The payload stores validated provider output as JSON so a cache hit returns
    exactly what the provider said, with the original fetch time, and never a
    re-derived value. Freshness is decided by the caller's TTL setting.
    """

    NUTRITION = "nutrition"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32, default=NUTRITION)
    provider = models.CharField(max_length=80)
    query_key = models.CharField(max_length=200)
    payload = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "provider", "query_key"), name="unique_enrichment_cache_entry"
            )
        ]
        verbose_name_plural = "enrichment cache entries"

    def is_fresh(self, now, ttl_seconds: int) -> bool:
        return (now - self.fetched_at).total_seconds() <= max(int(ttl_seconds), 0)

    def __str__(self):
        return f"{self.provider}: {self.query_key}"


class ProductObservation(models.Model):
    """One dated, cited research observation attached to one inventory item.

    Every row keeps its source URL, retrieval time, and confidence so the UI and
    API can show provenance and freshness instead of an unsourced claim.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey("inventory.PantryItem", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="research_observations")
    gear_item = models.ForeignKey("inventory.GearItem", null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="research_observations")
    provider = models.CharField(max_length=80)
    title = models.CharField(max_length=200, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    brand = models.CharField(max_length=120, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    source_url = models.URLField(max_length=200)
    retrieved_at = models.DateTimeField()
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("item", "source_url"), name="unique_item_research_source"),
            models.UniqueConstraint(fields=("gear_item", "source_url"), name="unique_gear_research_source"),
            models.CheckConstraint(condition=(models.Q(item__isnull=False, gear_item__isnull=True) |
                                              models.Q(item__isnull=True, gear_item__isnull=False)),
                                   name="research_observation_exactly_one_inventory_item"),
        ]
        ordering = ("-retrieved_at",)

    def clean(self):
        super().clean()
        if not str(self.source_url or "").strip():
            raise ValidationError({"source_url": "Research observations require a citation link."})
        if self.confidence is None or not 0 <= self.confidence <= 1:
            raise ValidationError({"confidence": "Confidence must be between 0 and 1."})
        if self.retrieved_at is not None and self.retrieved_at.tzinfo is None:
            raise ValidationError({"retrieved_at": "Retrieval time must include a timezone."})

    def __str__(self):
        return f"{self.provider}: {self.source_url}"
