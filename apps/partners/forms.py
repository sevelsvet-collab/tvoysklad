from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from apps.core.forms import BootstrapFormMixin
from apps.core.models import Organization

from .models import BankAccount, ContactPerson, Contract, ContractTemplate, Counterparty


class CounterpartyForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Counterparty
        fields = [
            "name", "full_name", "kind", "partner_type",
            "inn", "kpp", "ogrn", "okpo",
            "legal_address", "actual_address",
            "phone", "email", "contact_person", "director_name",
            "comment", "is_active",
        ]
        widgets = {"comment": forms.Textarea(attrs={"rows": 3})}


class BankAccountForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["bank_name", "bik", "account", "corr_account", "is_default"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        # Если реквизиты начали заполнять — расчётный счёт обязателен
        filled = any(cleaned.get(f) for f in ("bank_name", "bik", "corr_account", "account"))
        if filled and not cleaned.get("account"):
            self.add_error("account", "Укажите расчётный счёт")
        return cleaned


class ContractForm(BootstrapFormMixin, forms.ModelForm):
    """Карточка договора: шаблон, стороны, срок, суммы, скан."""

    class Meta:
        model = Contract
        fields = [
            "template", "date", "number", "place", "name", "organization", "counterparty", "subject",
            "valid_from", "valid_to", "is_perpetual", "auto_renew",
            "amount", "payment_delay_days", "prepayment_percent",
            "delivery_place", "delivery_days", "warranty_months",
            "penalty_rate", "penalty_cap_percent", "claim_days",
            "goods_condition", "goods_details",
            "scan", "comment",
        ]
        widgets = {
            "goods_details": forms.Textarea(attrs={"rows": 2}),
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "valid_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "valid_to": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "subject": forms.Textarea(attrs={"rows": 2}),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("date", "valid_from", "valid_to"):
            self.fields[name].input_formats = ["%Y-%m-%d"]
        self.fields["template"].queryset = ContractTemplate.objects.filter(is_active=True)
        self.fields["number"].help_text = "Оставьте пустым — присвоится автоматически"
        # Без вида договора не из чего собрать текст, без организации — нет
        # реквизитов и автонумерации, без даты договор недействителен.
        for name in ("template", "organization", "date"):
            self.fields[name].required = True
        # Подписи в сводке ошибок должны совпадать с подписями полей на форме
        for name, label in (("template", "Вид договора"), ("organization", "Наша организация"),
                            ("counterparty", "Контрагент"), ("date", "Дата")):
            self.fields[name].label = label
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org
            if not self.initial.get("date"):
                self.fields["date"].initial = timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        valid_from, valid_to = cleaned.get("valid_from"), cleaned.get("valid_to")
        if valid_from and valid_to and valid_to < valid_from:
            self.add_error("valid_to", "Дата окончания раньше даты начала")
        return cleaned


class ContractTemplateForm(BootstrapFormMixin, forms.ModelForm):
    """Настройки шаблона: шапка договора и блок реквизитов.
    Условия договора задаются вопросами конструктора."""

    class Meta:
        model = ContractTemplate
        fields = ["name", "kind", "title", "intro", "outro", "is_active"]
        widgets = {
            "intro": forms.Textarea(attrs={"rows": 6, "style": "font-family:monospace"}),
            "outro": forms.Textarea(attrs={"rows": 14, "style": "font-family:monospace"}),
        }


class ContactPersonForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = ContactPerson
        fields = ["full_name", "position", "phone", "email", "comment"]


BankAccountFormSet = inlineformset_factory(
    Counterparty, BankAccount, form=BankAccountForm, extra=1, can_delete=True,
)
ContactPersonFormSet = inlineformset_factory(
    Counterparty, ContactPerson, form=ContactPersonForm, extra=1, can_delete=True,
)
