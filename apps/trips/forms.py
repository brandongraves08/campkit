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


class SeedTripGearForm(forms.Form):
    source_kind = forms.ChoiceField(label="Source type")
    source_id = forms.ChoiceField(label="Source")

    def __init__(self, *args, sources, source_rows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sources = list(sources)
        self.fields["source_kind"].choices = [("kit", "Saved gear kit"), ("completed_trip", "Completed trip")]
        self.fields["source_id"].choices = [(str(pk), label) for _kind, pk, label in self.sources]
        self.gear_by_id = {}
        for row in source_rows or []:
            gear = row.gear_item
            key = str(gear.pk)
            self.gear_by_id[key] = gear
            self.fields[f"include_{key}"] = forms.BooleanField(
                required=False, label=gear.name, initial=True, widget=forms.CheckboxInput(attrs={"id": f"include-{key}"}),
            )
            self.fields[f"quantity_{key}"] = forms.IntegerField(
                required=False, min_value=1, max_value=32767, initial=row.quantity,
                label=f"Quantity for {gear.name}", widget=forms.NumberInput(attrs={"id": f"quantity-{key}"}),
            )

    def clean(self):
        cleaned = super().clean()
        choosing_source = bool(self.data.get("choose_source")) if self.is_bound else False
        for gear_id in self.gear_by_id:
            include_key, quantity_key = f"include_{gear_id}", f"quantity_{gear_id}"
            if not choosing_source and cleaned.get(include_key) and cleaned.get(quantity_key) is None:
                self.add_error(quantity_key, "Enter a whole number of at least one.")
        if not choosing_source and self.gear_by_id and not any(cleaned.get(f"include_{key}") for key in self.gear_by_id):
            raise forms.ValidationError("Select at least one gear item to add.")
        return cleaned

    def selected_items(self):
        return [(self.gear_by_id[key].pk, self.cleaned_data[f"quantity_{key}"])
                for key in self.gear_by_id if self.cleaned_data.get(f"include_{key}")]
