from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin, SkipEmptyLineMixin
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty

from .models import Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine


def _supplier_queryset():
    return Counterparty.objects.filter(
        partner_type__in=[Counterparty.TYPE_SUPPLIER, Counterparty.TYPE_BOTH], is_active=True,
    )


class ReceiptForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Receipt
        fields = [
            "date", "organization", "warehouse", "supplier",
            "contract", "supplier_invoice", "comment",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.fields["supplier"].queryset = Counterparty.objects.filter(
            partner_type__in=[Counterparty.TYPE_SUPPLIER, Counterparty.TYPE_BOTH], is_active=True,
        )
        self.fields["contract"].queryset = self.fields["contract"].queryset.none()
        if not self.instance.pk:
            org = Organization.get_default()
            wh = Warehouse.get_default()
            if org:
                self.fields["organization"].initial = org
            if wh:
                self.fields["warehouse"].initial = wh
        if self.instance.pk and self.instance.supplier_id:
            from apps.partners.models import Contract

            self.fields["contract"].queryset = Contract.objects.filter(counterparty_id=self.instance.supplier_id)


class ReceiptLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ReceiptLine
        fields = ["product", "quantity", "price", "discount", "vat_rate"]


ReceiptLineFormSet = inlineformset_factory(
    Receipt, ReceiptLine, form=ReceiptLineForm, extra=0, can_delete=True,
)


class SupplierReturnForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierReturn
        fields = ["date", "organization", "warehouse", "supplier", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.fields["supplier"].queryset = _supplier_queryset()
        if not self.instance.pk:
            org = Organization.get_default()
            wh = Warehouse.get_default()
            if org:
                self.fields["organization"].initial = org
            if wh:
                self.fields["warehouse"].initial = wh


class SupplierReturnLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = SupplierReturnLine
        fields = ["product", "quantity", "price", "discount", "vat_rate"]


SupplierReturnLineFormSet = inlineformset_factory(
    SupplierReturn, SupplierReturnLine, form=SupplierReturnLineForm, extra=0, can_delete=True,
)
