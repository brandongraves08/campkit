from django import forms


class SaveGearKitForm(forms.Form):
    name = forms.CharField(max_length=160, label="Kit name")


class GearKitForm(forms.Form):
    name = forms.CharField(max_length=160, label="Kit name")

    def __init__(self, *args, gear_items, quantities=None, **kwargs):
        super().__init__(*args, **kwargs)
        quantities = quantities or {}
        self.gear_by_id = {}
        for gear in gear_items:
            key = str(gear.pk)
            self.gear_by_id[key] = gear
            self.fields[f"include_{key}"] = forms.BooleanField(
                required=False, label=gear.name,
                initial=key in quantities,
            )
            self.fields[f"quantity_{key}"] = forms.IntegerField(
                required=False, min_value=1, max_value=32767,
                label=f"Quantity for {gear.name}",
                initial=quantities.get(key, 1),
            )

    def clean(self):
        cleaned = super().clean()
        for gear_id in self.gear_by_id:
            include_key = f"include_{gear_id}"
            quantity_key = f"quantity_{gear_id}"
            if cleaned.get(include_key):
                if cleaned.get(quantity_key) is None:
                    self.add_error(quantity_key, "Enter a whole-number quantity of at least one.")
            elif cleaned.get(quantity_key) is not None and cleaned.get(quantity_key) != self.get_initial_for_field(
                self.fields[quantity_key], quantity_key
            ):
                self.add_error(quantity_key, "Select this gear to save its quantity.")
        return cleaned

    def selected_items(self):
        return [
            (gear.pk, self.cleaned_data[f"quantity_{gear_id}"])
            for gear_id, gear in self.gear_by_id.items()
            if self.cleaned_data.get(f"include_{gear_id}")
        ]
