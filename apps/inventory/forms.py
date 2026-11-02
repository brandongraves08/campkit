from django import forms

from apps.inventory.models import GearItem, PantryItem


class GearItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")

    class Meta:
        model = GearItem
        fields = ("name", "category", "owner", "condition", "weight_grams", "quantity", "storage_location", "availability", "source", "source_url")


class PantryItemForm(forms.ModelForm):
    source_url = forms.URLField(required=False, assume_scheme="https")

    class Meta:
        model = PantryItem
        fields = ("name", "quantity", "unit", "expiry_date", "storage_location", "source", "source_url")
