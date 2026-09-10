from decimal import Decimal

from django import forms

from .models import Organization, Warehouse, current_time

DOC_FORM_ID = "doc-form"  # id основной формы документа: поля шапки стоят в заголовке страницы


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


class DocumentHeaderMixin:
    """Номер, дата и время документа в заголовке — как в МойСклад:
    «Счёт покупателю № [___] от [дата] [время]».

    Номер можно ввести вручную; пустой присвоится автоматически при сохранении.
    Занятый номер в той же серии не пропускаем.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        number = self.fields["number"]
        number.required = False
        number.help_text = ""
        number.widget.attrs.update({
            "placeholder": "авто", "form": DOC_FORM_ID, "autocomplete": "off",
            "class": "form-control form-control-sm doc-number",
            "title": "Оставьте пустым — номер присвоится при сохранении",
        })
        self.fields["date"].widget.attrs.update({
            "form": DOC_FORM_ID, "class": "form-control form-control-sm doc-date",
        })
        time = self.fields["time"]
        time.required = False
        time.widget = forms.TimeInput(
            attrs={"type": "time", "form": DOC_FORM_ID, "class": "form-control form-control-sm doc-time"},
            format="%H:%M",
        )
        time.input_formats = ["%H:%M", "%H:%M:%S"]

    def clean_time(self):
        return self.cleaned_data.get("time") or self.instance.time or current_time()

    def number_scope(self):
        """Серия номеров: у видов (приход/расход) — своя."""
        kind = getattr(self.instance, "kind", "")
        return {"kind": kind} if kind else {}

    def clean(self):
        cleaned = super().clean()
        number = (cleaned.get("number") or "").strip()
        cleaned["number"] = number
        org = cleaned.get("organization")
        if org is None and self.instance.organization_id:
            org = self.instance.organization
        date = cleaned.get("date")
        if number and org and date:
            taken = self._meta.model.objects.filter(
                organization=org, number=number, date__year=date.year, **self.number_scope(),
            )
            if self.instance.pk:
                taken = taken.exclude(pk=self.instance.pk)
            if taken.exists():
                self.add_error(
                    "number",
                    f"Номер {number} уже есть в {date.year} году — укажите другой "
                    "или оставьте пустым, он присвоится сам",
                )
        return cleaned


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
