from django import forms

from apps.inventory.models import GearCategory, GearItem, PantryItem


class GearItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")
    category = forms.ChoiceField(
        required=False,
        choices=(("", "---------"), *GearCategory.choices),
        help_text="Durables only: the gear you own. Food and other consumables belong in Pantry.",
    )

    class Meta:
        model = GearItem
        fields = ("name", "category", "owner", "condition", "weight_grams", "quantity", "storage_location", "availability", "source", "source_url")


class PantryItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")

    class Meta:
        model = PantryItem
        fields = ("name", "quantity", "unit", "expiry_date", "storage_location", "source", "source_url")
