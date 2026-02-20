from __future__ import annotations

from django import forms
from django.forms import inlineformset_factory

from .models import Client, Invoice, InvoiceItem, WhatsAppFollowUp, WhatsAppSettings


INVOICE_SHARED_FIELDS = ("client", "invoice_type", "date", "chassis_no", "engine_no")
INVOICE_GENERAL_FIELDS = ("vehicle", "lic_no")
INVOICE_PROFORMA_FIELDS = (
    "proforma_make",
    "proforma_model",
    "proforma_year",
    "proforma_colour",
    "proforma_cc_rating",
    "proforma_price",
    "proforma_currency",
)


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["name", "email", "phone", "address"]


class WhatsAppSettingsForm(forms.ModelForm):
    class Meta:
        model = WhatsAppSettings
        fields = ["business_name", "global_follow_up_days"]
        widgets = {
            "business_name": forms.TextInput(attrs={"class": "form-control"}),
            "global_follow_up_days": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }


class WhatsAppEnrollmentForm(forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    last_service_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    follow_up_days_override = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        help_text="Leave blank to use the global default",
    )

    def __init__(self, *args, eligible_clients=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = eligible_clients if eligible_clients is not None else Client.objects.all()
        self.fields["client"].queryset = queryset.order_by("name")


class WhatsAppFollowUpForm(forms.ModelForm):
    class Meta:
        model = WhatsAppFollowUp
        fields = ["is_active", "last_service_date", "follow_up_days_override"]
        widgets = {
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "last_service_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "follow_up_days_override": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def clean_follow_up_days_override(self):
        value = self.cleaned_data.get("follow_up_days_override")
        if value is not None and value < 1:
            raise forms.ValidationError("Follow-up days must be at least 1.")
        return value


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = list(INVOICE_SHARED_FIELDS) + list(INVOICE_GENERAL_FIELDS) + list(INVOICE_PROFORMA_FIELDS)
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "invoice_type": forms.Select(),
            "client": forms.Select(),
            "proforma_year": forms.NumberInput(attrs={"min": 0}),
            "proforma_price": forms.NumberInput(attrs={"step": "0.01", "min": 0}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        for name, field in self.fields.items():
            if isinstance(field.widget, forms.widgets.Select):
                css_class = "form-select"
            else:
                css_class = "form-control"
            existing = field.widget.attrs.get("class", "")
            if css_class not in existing.split():
                field.widget.attrs["class"] = (existing + " " + css_class).strip()

        for name in INVOICE_PROFORMA_FIELDS:
            if name in self.fields:
                self.fields[name].widget.attrs["data-proforma-field"] = "1"

        for name in ("proforma_make", "proforma_model", "proforma_price"):
            if name in self.fields:
                self.fields[name].widget.attrs["data-proforma-required"] = "1"

    def clean(self):
        cleaned_data = super().clean()
        invoice_type = cleaned_data.get("invoice_type")
        if invoice_type == Invoice.Type.PROFORMA:
            required_fields = {
                "proforma_make": "Make",
                "proforma_model": "Model",
                "proforma_price": "Total Cost",
            }
            for field_name, label in required_fields.items():
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, f"{label} is required for proforma invoices.")
        return cleaned_data


ItemFormSet = inlineformset_factory(
    Invoice,
    InvoiceItem,
    fields=["description", "labour_cost", "parts_cost"],
    extra=1,
    can_delete=True,
)
