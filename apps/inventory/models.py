import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone



class InventoryVerification(models.TextChoices):
    ESTIMATED = "estimated", "Estimated"
    CONFIRMED = "confirmed", "Confirmed"


class HouseholdInventoryManager(models.Manager):
    def for_household(self, household):
        return self.get_queryset().filter(household=household)


#: Gear categories that actually describe edible supplies. Consumables are
#: PantryItems (quantity + unit, expiry, price, nutrition); keeping them in Gear
#: splits the same stock across two models and breaks trip/meal planning.
FOOD_GEAR_CATEGORIES = frozenset(
    {
        "food",
        "foods",
        "meal",
        "meals",
        "ration",
        "rations",
        "mre",
        "mres",
        "snack",
        "snacks",
        "groceries",
        "grocery",
        "provisions",
        "pantry",
        "freeze dried",
        "beverage",
        "beverages",
        "drink",
        "drinks",
    }
)


def _category_is_food(category) -> bool:
    """True when a gear category names food rather than durables.

    Matches the whole category, not a substring, so legitimate durable
    categories such as "Food storage" (a cooler or container) stay valid.
    """
    normalized = " ".join(
        str(category or "").lower().replace("_", " ").replace("-", " ").split()
    )
    return bool(normalized) and normalized in FOOD_GEAR_CATEGORIES


class ProvenanceModel(models.Model):
    source = models.CharField(max_length=255, blank=True)
    source_url = models.URLField(blank=True)
    observed_at = models.DateTimeField(default=timezone.now)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    verification_status = models.CharField(
        max_length=16, choices=InventoryVerification.choices, default=InventoryVerification.ESTIMATED
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="%(class)s_verified"
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.confidence is not None and not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValidationError({"confidence": "Confidence must be between 0 and 1."})
        if self.verification_status == InventoryVerification.CONFIRMED:
            if not self.verified_by_id or not self.verified_at:
                raise ValidationError("Confirmed inventory facts require a verifier and verification time.")
        elif self.verified_by_id or self.verified_at:
            raise ValidationError("Estimated inventory facts cannot carry confirmation attribution.")


class GearItem(ProvenanceModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="gear_items")
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80, blank=True)
    owner = models.CharField(max_length=120, blank=True)
    condition = models.CharField(max_length=40, blank=True)
    weight_grams = models.PositiveIntegerField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    storage_location = models.CharField(max_length=160, blank=True)
    availability = models.CharField(max_length=32, default="available")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdInventoryManager()

    def clean(self):
        super().clean()
        if self.weight_grams is not None and self.weight_grams < 0:
            raise ValidationError({"weight_grams": "Weight cannot be negative."})
        if self.quantity < 0:
            raise ValidationError({"quantity": "Quantity cannot be negative."})
        if _category_is_food(self.category):
            raise ValidationError(
                {
                    "category": (
                        "Food and other consumables belong in Pantry, not Gear. "
                        "Add this item to the pantry instead."
                    )
                }
            )

    @property
    def total_weight_grams(self):
        return (self.weight_grams or 0) * self.quantity

    def __str__(self):
        return self.name


class PantryItem(ProvenanceModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    household = models.ForeignKey("households.Household", on_delete=models.CASCADE, related_name="pantry_items")
    name = models.CharField(max_length=160)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=20)
    expiry_date = models.DateField(null=True, blank=True)
    storage_location = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdInventoryManager()

    VALID_UNITS = {"g", "kg", "ml", "l", "each", "oz", "lb", "cup", "tbsp", "tsp"}

    def clean(self):
        super().clean()
        if self.quantity < 0:
            raise ValidationError({"quantity": "Quantity cannot be negative."})
        if self.unit not in self.VALID_UNITS:
            raise ValidationError({"unit": "Choose a supported pantry unit."})

    @property
    def estimated_quantity(self):
        return self.quantity if self.verification_status == InventoryVerification.ESTIMATED else Decimal("0")

    @property
    def confirmed_quantity(self):
        return self.quantity if self.verification_status == InventoryVerification.CONFIRMED else Decimal("0")

    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"


class PriceObservation(ProvenanceModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(PantryItem, on_delete=models.CASCADE, related_name="price_observations")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    merchant = models.CharField(max_length=160, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="price_observations")

    def clean(self):
        super().clean()
        if self.amount <= 0:
            raise ValidationError({"amount": "Price must be greater than zero."})
        if len(self.currency) != 3:
            raise ValidationError({"currency": "Currency must be a three-letter code."})


class NutritionFact(ProvenanceModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(PantryItem, on_delete=models.CASCADE, related_name="nutrition_facts")
    nutrient = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=20)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("item", "nutrient"), name="unique_item_nutrition_fact")]

    def clean(self):
        super().clean()
        if self.amount < 0:
            raise ValidationError({"amount": "Nutrition amount cannot be negative."})
