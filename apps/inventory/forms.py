from django import forms

from apps.inventory.models import GEAR_CATEGORY_SUGGESTIONS, GearItem, PantryItem


class GearItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")
    category = forms.CharField(
        required=False,
        max_length=80,
        widget=forms.TextInput(attrs={"list": "gear-category-options"}),
        help_text="Durables only: the gear you own. Food and other consumables belong in Pantry.",
    )

    class Meta:
        model = GearItem
        fields = ("name", "category", "owner", "condition", "weight_grams", "quantity", "storage_location", "availability", "source", "source_url")

    @property
    def category_suggestions(self):
        return GEAR_CATEGORY_SUGGESTIONS


class PantryItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")

    class Meta:
        model = PantryItem
        fields = ("name", "quantity", "unit", "expiry_date", "storage_location", "source", "source_url")
