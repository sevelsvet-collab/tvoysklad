from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import DocumentHeaderMixin, BootstrapFormMixin, SkipEmptyLineMixin
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


class InvoiceForm(DocumentHeaderMixin, _HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["date", "number", "time", "organization", "warehouse", "customer", "contract", "due_date", "comment"]
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
        fields = ["product", "quantity", "serial_numbers", "price", "discount", "vat_rate"]


InvoiceLineFormSet = inlineformset_factory(
    Invoice, InvoiceLine, form=InvoiceLineForm, extra=0, can_delete=True,
)


class ShipmentForm(DocumentHeaderMixin, _HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["date", "number", "time", "organization", "warehouse", "customer", "comment"]
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
        fields = ["product", "quantity", "serial_numbers", "price", "discount", "vat_rate"]


ShipmentLineFormSet = inlineformset_factory(
    Shipment, ShipmentLine, form=ShipmentLineForm, extra=0, can_delete=True,
)


class CustomerReturnForm(DocumentHeaderMixin, _HeaderDefaultsMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CustomerReturn
        fields = ["date", "number", "time", "organization", "warehouse", "customer", "comment"]
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
        fields = ["product", "quantity", "serial_numbers", "price", "discount", "vat_rate", "shipment_line"]

    def __init__(self, *args, basis=None, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["shipment_line"]
        field.required = False
        field.label = "Строка отгрузки"
        field.empty_label = "— определить автоматически —"
        if basis is None:
            field.queryset = ShipmentLine.objects.none()
        else:
            field.queryset = basis.lines.select_related("product").order_by("pk")
            field.label_from_instance = lambda line: (
                f"{line.product.name} × {line.quantity.normalize():f} по {line.price}"
            )

    def clean(self):
        data = super().clean()
        source, product = data.get("shipment_line"), data.get("product")
        if source is not None and product is not None and source.product_id != product.pk:
            self.add_error("shipment_line", "В выбранной строке отгрузки другой товар")
        return data


class CustomerReturnLineBaseFormSet(forms.BaseInlineFormSet):
    """Строки возврата предлагают выбрать строку отгрузки-основания —
    это нужно, когда в отгрузке товар записан несколькими строками."""

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["basis"] = getattr(self.instance, "shipment", None) if self.instance.pk else None
        return kwargs


CustomerReturnLineFormSet = inlineformset_factory(
    CustomerReturn, CustomerReturnLine, form=CustomerReturnLineForm, formset=CustomerReturnLineBaseFormSet,
    extra=0, can_delete=True,
)
