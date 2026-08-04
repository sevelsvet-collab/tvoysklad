from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin, SkipEmptyLineMixin
from apps.core.models import Organization, Warehouse

from .models import AdjustmentLine, StockAdjustment, Transfer, TransferLine


class _DefaultsMixin:
    def _apply_defaults(self):
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org


class TransferForm(_DefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Transfer
        fields = ["date", "organization", "warehouse_from", "warehouse_to", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self._apply_defaults()

    def clean(self):
        data = super().clean()
        if data.get("warehouse_from") and data.get("warehouse_from") == data.get("warehouse_to"):
            raise forms.ValidationError("Склад-отправитель и склад-получатель должны отличаться")
        return data


class TransferLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = TransferLine
        fields = ["product", "quantity"]


TransferLineFormSet = inlineformset_factory(
    Transfer, TransferLine, form=TransferLineForm, extra=0, can_delete=True,
)


class AdjustmentForm(_DefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["date", "organization", "warehouse", "reason", "comment"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self._apply_defaults()


class AdjustmentLineForm(SkipEmptyLineMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = AdjustmentLine
        fields = ["product", "quantity", "price"]


AdjustmentLineFormSet = inlineformset_factory(
    StockAdjustment, AdjustmentLine, form=AdjustmentLineForm, extra=0, can_delete=True,
)
