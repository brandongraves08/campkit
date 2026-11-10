import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower, Trim
from django.utils import timezone


class HouseholdTripManager(models.Manager):
    def for_household(self, household):
        return self.get_queryset().filter(household=household)


class Trip(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        CONFIRMED = "confirmed", "Confirmed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="trips")
    name = models.CharField(max_length=160)
    destination = models.CharField(max_length=200, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    party_size = models.PositiveSmallIntegerField(default=1)
    conditions = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_trips")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdTripManager()

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "Trip end date cannot precede its start date."})
        if self.party_size < 1:
            raise ValidationError({"party_size": "Party size must be at least one."})


class TripGear(models.Model):
    trip = models.ForeignKey(Trip, on_delete=models.CASCADE, related_name="packed_gear")
    gear_item = models.ForeignKey("inventory.GearItem", on_delete=models.PROTECT, related_name="trip_packing_entries")
    quantity = models.PositiveSmallIntegerField(default=1)
    packed = models.BooleanField(default=False)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("trip", "gear_item"), name="unique_trip_gear_item")]

    def clean(self):
        super().clean()
        if self.trip_id and self.gear_item_id and self.trip.household_id != self.gear_item.household_id:
            raise ValidationError("Packed gear must belong to the trip household.")
        if self.quantity < 1:
            raise ValidationError({"quantity": "Packed quantity must be at least one."})


class HouseholdGearKitManager(models.Manager):
    def for_household(self, household):
        return self.get_queryset().filter(household=household)


class GearKit(models.Model):
    """A named, household-owned packing list that references shared gear.

    A kit is a saved set of gear references and quantities; it never copies
    gear metadata or provenance, and it never changes a trip or a GearItem.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="gear_kits")
    name = models.CharField(max_length=160)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_gear_kits")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdGearKitManager()

    class Meta:
        ordering = ("name", "pk")
        constraints = [
            models.UniqueConstraint(
                Lower(Trim("name")),
                "household",
                name="unique_gear_kit_name_per_household",
                violation_error_message="A kit with this name already exists in this household.",
            ),
        ]

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Kit name is required."})

    def __str__(self):
        return self.name


class GearKitItem(models.Model):
    kit = models.ForeignKey(GearKit, on_delete=models.CASCADE, related_name="items")
    # Protected: deleting shared gear must not silently remove kit membership.
    gear_item = models.ForeignKey("inventory.GearItem", on_delete=models.PROTECT, related_name="gear_kit_entries")
    quantity = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("kit", "gear_item"), name="unique_gear_kit_item"),
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="gear_kit_item_quantity_positive"),
        ]

    def clean(self):
        super().clean()
        if self.kit_id and self.gear_item_id and self.kit.household_id != self.gear_item.household_id:
            raise ValidationError("Kit gear must belong to the kit household.")
        if self.quantity < 1:
            raise ValidationError({"quantity": "Kit quantity must be at least one."})

    def __str__(self):
        return f"{self.gear_item} x{self.quantity}"
