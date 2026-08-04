from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import BootstrapFormMixin

from .models import BankAccount, Contract, Counterparty


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


class ContractForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["organization", "number", "date", "name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}


BankAccountFormSet = inlineformset_factory(
    Counterparty, BankAccount, form=BankAccountForm, extra=1, can_delete=True,
)
ContractFormSet = inlineformset_factory(
    Counterparty, Contract, form=ContractForm, extra=1, can_delete=True,
)
