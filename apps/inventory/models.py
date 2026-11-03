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


#: Words that mark a category as edible supplies. Consumables are PantryItems
#: (quantity + unit, expiry, price, nutrition); keeping them in Gear splits the
#: same stock across two models and keeps it out of meal planning.
FOOD_CATEGORY_WORDS = frozenset(
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
        "grocery",
        "groceries",
        "provision",
        "provisions",
        "pantry",
        "beverage",
        "beverages",
        "drink",
        "drinks",
    }
)

#: Multi-word food phrases whose individual words are too generic to flag alone.
FOOD_CATEGORY_PHRASES = ("freeze dried", "freeze dry", "meal replacement", "trail food")

#: The only categories a gear item may carry. A hard vocabulary, so a meal
#: bucket cannot be filed as gear under some novel label: food lives in Pantry,
#: and this list names durable kinds only. Add a category here (with a test)
#: when a household needs one.
class GearCategory(models.TextChoices):
    BACKPACK = "Backpack", "Backpack"
    SHELTER = "Shelter", "Shelter"
    SLEEP = "Sleep system", "Sleep system"
    COOKWARE = "Cookware", "Cookware"
    KITCHEN = "Kitchen", "Kitchen"
    WATER = "Water", "Water"
    LIGHTING = "Lighting", "Lighting"
    NAVIGATION = "Navigation", "Navigation"
    TOOLS = "Tools & repair", "Tools & repair"
    CLOTHING = "Clothing", "Clothing"
    CLIMBING = "Climbing", "Climbing"
    SAFETY = "Safety", "Safety"
    FOOD_STORAGE = "Food storage", "Food storage"
    OTHER = "Other", "Other"


#: Categories offered in the gear form (derived, so the two can never drift).
GEAR_CATEGORY_SUGGESTIONS = tuple(label for _, label in GearCategory.choices)

#: Durable nouns that legitimately appear next to a food word: a cooler, a
#: storage bin, or a food-grade scale is gear, not stock. Their presence means
#: the category names a thing you own rather than something you eat.
DURABLE_CATEGORY_WORDS = frozenset(
    {
        "storage",
        "container",
        "containers",
        "cooler",
        "coolers",
        "bin",
        "bins",
        "bag",
        "bags",
        "box",
        "boxes",
        "crate",
        "crates",
        "jar",
        "jars",
        "canister",
        "canisters",
        "wrap",
        "wraps",
        "grade",
        "safe",
        "scale",
        "scales",
        "thermometer",
        "dehydrator",
        "processor",
        "cookware",
        "kitchen",
    }
)


def _normalize_category(category) -> str:
    return " ".join(
        str(category or "").lower().replace("_", " ").replace("-", " ").split()
    )


_CATEGORY_BY_NORMALIZED = {
    _normalize_category(value): value for value, _ in GearCategory.choices
}


def canonical_gear_category(category) -> str:
    """Map a case/space/hyphen variant onto the canonical vocabulary value.

    "shelter", "SHELTER" and "sleep-system" become "Shelter" and "Sleep system";
    anything outside the vocabulary is returned unchanged so field validation can
    report it as an invalid choice.
    """
    raw = str(category or "").strip()
    if not raw:
        return ""
    return _CATEGORY_BY_NORMALIZED.get(_normalize_category(raw), raw)


def _category_is_food_error(category):
    """The validation error for a food category, or None when it is durable."""
    if not _category_is_food(category):
        return None
    return {
        "category": (
            "Food and other consumables belong in Pantry, not Gear. "
            "Add this item to the pantry instead."
        )
    }


def _category_is_food(category) -> bool:
    """True when a gear category names food rather than durables.

    Word-level, not substring: "Food storage" and "Food-grade scale" stay valid
    because a durable noun is present, while "Food", "Meals", "MRE rations" and
    "Trail snacks" are rejected.
    """
    normalized = _normalize_category(category)
    words = set(normalized.split())
    if not words:
        return False
    if words & DURABLE_CATEGORY_WORDS:
        return False
    if any(phrase in normalized for phrase in FOOD_CATEGORY_PHRASES):
        return True
    return bool(words & FOOD_CATEGORY_WORDS)


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
    category = models.CharField(max_length=80, blank=True, choices=GearCategory.choices)
    owner = models.CharField(max_length=120, blank=True)
    condition = models.CharField(max_length=40, blank=True)
    weight_grams = models.PositiveIntegerField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    storage_location = models.CharField(max_length=160, blank=True)
    availability = models.CharField(max_length=32, default="available")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = HouseholdInventoryManager()

    def clean_fields(self, exclude=None):
        """Canonicalize the category, then refuse food before choice validation.

        Running the food check first means a food category gets the "belongs in
        Pantry" message rather than a generic "not a valid choice", while any
        other value outside the vocabulary is still rejected by the field
        choices.
        """
        self.category = canonical_gear_category(self.category)
        food_error = _category_is_food_error(self.category)
        if food_error is not None:
            raise ValidationError(food_error)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        if self.weight_grams is not None and self.weight_grams < 0:
            raise ValidationError({"weight_grams": "Weight cannot be negative."})
        if self.quantity < 0:
            raise ValidationError({"quantity": "Quantity cannot be negative."})

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
