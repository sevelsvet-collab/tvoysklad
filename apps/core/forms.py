from decimal import Decimal

from django import forms

from .models import Organization, Warehouse


class BootstrapFormMixin:
    """Проставляет bootstrap-классы всем полям формы."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class SkipEmptyLineMixin:
    """Строка табличной части без выбранного товара считается пустой и не сохраняется.

    Нужно, чтобы внизу документа всегда «жила» пустая строка для ввода (как в МойСклад),
    и при сохранении она не вызывала ошибку «обязательное поле — товар».
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "discount" in self.fields:
            self.fields["discount"].required = False

    def clean_discount(self):
        return self.cleaned_data.get("discount") or Decimal("0")

    def has_changed(self):
        product_name = self.add_prefix("product")
        if not (self.data.get(product_name) or "").strip():
            return False
        return super().has_changed()


class ImportForm(forms.Form):
    file = forms.FileField(
        label="Файл Excel (.xlsx)",
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".xlsx"}),
    )


class OrganizationForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Organization
        fields = [
            "name", "full_name", "inn", "kpp", "ogrn",
            "legal_address", "actual_address", "phone", "email",
            "bank_name", "bik", "bank_account", "corr_account",
            "director_position", "director_name", "accountant_name",
            "signature", "stamp", "logo",
            "vat_payer", "default_vat_rate", "allow_negative_stock", "is_default", "is_active",
        ]


class WarehouseForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ["name", "address", "is_default", "is_active"]
