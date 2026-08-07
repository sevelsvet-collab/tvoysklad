from django import forms

from apps.core.forms import BootstrapFormMixin
from apps.core.models import Organization
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from .models import Account, AccountCorrection, Payment, SettlementCorrection


class AccountForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Account
        fields = ["organization", "name", "kind", "bank_account", "opening_balance", "is_default", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org


class PaymentForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["date", "organization", "account", "counterparty", "invoice", "amount", "purpose"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "purpose": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind or (self.instance.kind if self.instance.pk else Payment.KIND_IN)
        self.fields["date"].input_formats = ["%Y-%m-%d"]

        if not self.instance.pk:
            org = Organization.get_default()
            account = Account.get_default()
            if org:
                self.fields["organization"].initial = org
            if account:
                self.fields["account"].initial = account

        # Входящий — от покупателей и привязка к счёту; исходящий — поставщикам, без счёта
        if self.kind == Payment.KIND_IN:
            self.fields["counterparty"].queryset = Counterparty.objects.filter(
                partner_type__in=[Counterparty.TYPE_CUSTOMER, Counterparty.TYPE_BOTH], is_active=True,
            )
            self.fields["invoice"].queryset = Invoice.objects.filter(status=Invoice.STATUS_ISSUED)
        else:
            self.fields["counterparty"].queryset = Counterparty.objects.filter(
                partner_type__in=[Counterparty.TYPE_SUPPLIER, Counterparty.TYPE_BOTH], is_active=True,
            )
            del self.fields["invoice"]


class AccountCorrectionForm(BootstrapFormMixin, forms.ModelForm):
    """Корректировка остатка кассы/счёта: вводится фактический остаток."""

    class Meta:
        model = AccountCorrection
        fields = ["date", "organization", "account", "actual_balance", "comment"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, account_kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        # account_kind ("cash"/"bank") ограничивает выбор — касса или счёт
        kind = account_kind or (self.instance.account.kind if self.instance.pk else None)
        qs = Account.objects.filter(is_active=True)
        if kind:
            qs = qs.filter(kind=kind)
        self.fields["account"].queryset = qs
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org
            default = qs.filter(is_default=True).first() or qs.first()
            if default:
                self.fields["account"].initial = default


class SettlementCorrectionForm(BootstrapFormMixin, forms.ModelForm):
    """Корректировка взаиморасчётов: контрагент, направление, сумма."""

    class Meta:
        model = SettlementCorrection
        fields = ["date", "organization", "counterparty", "direction", "amount", "comment"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.fields["counterparty"].queryset = Counterparty.objects.filter(is_active=True)
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("Сумма должна быть больше нуля.")
        return amount
