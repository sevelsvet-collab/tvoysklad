from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin, SkipEmptyLineMixin
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty

from .models import (
    CustomerReturn,
    CustomerReturnLine,
    Invoice,
    InvoiceLine,
    Shipment,
    ShipmentLine,
)


def _customer_queryset():
    return Counterparty.objects.filter(
        partner_type__in=[Counterparty.TYPE_CUSTOMER, Counterparty.TYPE_BOTH], is_active=True,
    )


class _HeaderDefaultsMixin:
    def _apply_defaults(self):
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        if not self.instance.pk:
            org = Organization.get_default()
            wh = Warehouse.get_default()
            if org:
                self.fields["organization"].initial = org
            if wh:
                self.fields["warehouse"].initial = wh


class InvoiceForm(_HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["date", "organization", "warehouse", "customer", "contract", "due_date", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_defaults()
        self.fields["due_date"].input_formats = ["%Y-%m-%d"]
        self.fields["customer"].queryset = _customer_queryset()
        self.fields["contract"].queryset = self.fields["contract"].queryset.none()
        if self.instance.pk and self.instance.customer_id:
            from apps.partners.models import Contract

            self.fields["contract"].queryset = Contract.objects.filter(counterparty_id=self.instance.customer_id)


class InvoiceLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = InvoiceLine
        fields = ["product", "quantity", "price", "discount", "vat_rate"]


InvoiceLineFormSet = inlineformset_factory(
    Invoice, InvoiceLine, form=InvoiceLineForm, extra=0, can_delete=True,
)


class ShipmentForm(_HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["date", "organization", "warehouse", "customer", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_defaults()
        self.fields["customer"].queryset = _customer_queryset()


class ShipmentLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ShipmentLine
        fields = ["product", "quantity", "price", "discount", "vat_rate"]


ShipmentLineFormSet = inlineformset_factory(
    Shipment, ShipmentLine, form=ShipmentLineForm, extra=0, can_delete=True,
)


class CustomerReturnForm(_HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CustomerReturn
        fields = ["date", "organization", "warehouse", "customer", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_defaults()
        self.fields["customer"].queryset = _customer_queryset()


class CustomerReturnLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CustomerReturnLine
        fields = ["product", "quantity", "price", "discount", "vat_rate"]


CustomerReturnLineFormSet = inlineformset_factory(
    CustomerReturn, CustomerReturnLine, form=CustomerReturnLineForm, extra=0, can_delete=True,
)
