import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Meal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trip = models.ForeignKey("trips.Trip", on_delete=models.CASCADE, related_name="meals")
    name = models.CharField(max_length=160)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    servings = models.PositiveSmallIntegerField(default=1)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if self.servings < 1:
            raise ValidationError({"servings": "Servings must be at least one."})


class MealIngredient(models.Model):
    meal = models.ForeignKey(Meal, on_delete=models.CASCADE, related_name="ingredients")
    pantry_item = models.ForeignKey("inventory.PantryItem", on_delete=models.PROTECT, related_name="meal_ingredients")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=20)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("meal", "pantry_item"), name="unique_meal_ingredient_item")]

    def clean(self):
        super().clean()
        if self.quantity <= 0:
            raise ValidationError({"quantity": "Ingredient quantity must be greater than zero."})
        if self.meal_id and self.pantry_item_id and self.meal.trip.household_id != self.pantry_item.household_id:
            raise ValidationError("Meal ingredients must belong to the trip household.")
        if self.pantry_item_id and self.unit != self.pantry_item.unit:
            raise ValidationError({"unit": "Ingredient and pantry units must match."})


class FoodReservation(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        RELEASED = "released", "Released"
        RECONCILED = "reconciled", "Reconciled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meal = models.ForeignKey(Meal, on_delete=models.CASCADE, related_name="reservations")
    pantry_item = models.ForeignKey("inventory.PantryItem", on_delete=models.PROTECT, related_name="food_reservations")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    consumed_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RESERVED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="food_reservations")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("meal", "pantry_item"), name="unique_food_reservation_item")]

    def clean(self):
        super().clean()
        if self.quantity <= 0:
            raise ValidationError({"quantity": "Reserved quantity must be positive."})
        if self.consumed_quantity < 0 or self.consumed_quantity > self.quantity:
            raise ValidationError({"consumed_quantity": "Consumed quantity must be between zero and reserved quantity."})
        if self.meal_id and self.pantry_item_id and self.meal.trip.household_id != self.pantry_item.household_id:
            raise ValidationError("Reservation pantry stock must belong to the trip household.")


class MealVerificationRequest(models.Model):
    trip = models.ForeignKey("trips.Trip", on_delete=models.CASCADE, related_name="food_verification_requests")
    pantry_item = models.ForeignKey("inventory.PantryItem", on_delete=models.CASCADE, related_name="meal_verification_requests")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="meal_verification_requests")
    reason = models.CharField(max_length=200, default="Pantry quantity must be confirmed before meal planning.")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="resolved_meal_verification_requests")

    class Meta:
        constraints = [models.UniqueConstraint(fields=("trip", "pantry_item"), condition=models.Q(resolved_at__isnull=True), name="unique_open_meal_verification")]
