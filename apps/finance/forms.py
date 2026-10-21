from django import forms

from apps.core.forms import DocumentHeaderMixin, BootstrapFormMixin
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


class PaymentForm(DocumentHeaderMixin, BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["date", "number", "time", "organization", "account", "counterparty", "invoice", "amount",
                  "settlement_purpose", "purpose"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "purpose": forms.Textarea(attrs={"rows": 2}),
        }

    AUTO_PURPOSE_LABEL = {
        Payment.KIND_IN: "Определить по счёту: со счётом — оплата от покупателя, без счёта — не распределено",
        Payment.KIND_OUT: "Не выбрано (будет «Не распределено»)",
    }

    def __init__(self, *args, kind=None, posting=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind or (self.instance.kind if self.instance.pk else Payment.KIND_IN)
        self.posting = posting      # нажато «Сохранить и провести»
        self.fields["date"].input_formats = ["%Y-%m-%d"]

        if not self.instance.pk:
            org = Organization.get_default()
            # счёт предлагаем только свой: платёж по чужому счёту сервер не сохранит
            account = Account.get_default(organization=org) if org else None
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

        # Назначение для взаиморасчётов: только подходящие к направлению денег;
        # пустое — «не указано», определяется при записи (Payment.clean/save).
        # Сохранённое значение показывается, даже если оно уже не подходит, —
        # чтобы ошибка была видна у поля, а не подменялась молча.
        labels = dict(Payment.PURPOSE_CHOICES)
        allowed = list(Payment.PURPOSES_BY_KIND[self.kind])
        stored = self.instance.settlement_purpose if self.instance.pk else ""
        if stored and stored not in allowed:
            allowed.append(stored)
        field = self.fields["settlement_purpose"]
        field.choices = [("", self.AUTO_PURPOSE_LABEL[self.kind])] + [(v, labels.get(v, v)) for v in allowed]
        field.required = False

    def _post_clean(self):
        # вид платежа задаётся адресом страницы, а не полем формы; проверке
        # связей (Payment.clean) он нужен до сохранения
        if not self.instance.pk:
            self.instance.kind = self.kind
        super()._post_clean()
        amount = self.cleaned_data.get("amount")
        if self.posting and amount is not None and amount == 0 and "amount" not in self.errors:
            from .services import ZERO_POSTED

            self.add_error("amount", ZERO_POSTED)


class AccountCorrectionForm(DocumentHeaderMixin, BootstrapFormMixin, forms.ModelForm):
    """Корректировка остатка кассы/счёта: вводится фактический остаток."""

    class Meta:
        model = AccountCorrection
        fields = ["date", "number", "time", "organization", "account", "actual_balance", "comment"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, account_kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        # account_kind ("cash"/"bank") ограничивает выбор — касса или счёт
        kind = account_kind or (self.instance.account.kind if self.instance.pk else None)
        qs = Account.objects.filter(is_active=True).select_related("organization")
        if kind:
            qs = qs.filter(kind=kind)
        self.fields["account"].queryset = qs
        # касса должна быть своей организации (проверяет AccountCorrection.clean и save)
        self.fields["account"].label_from_instance = lambda account: f"{account} — {account.organization}"
        if not self.instance.pk:
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org
            default = qs.filter(is_default=True).first() or qs.first()
            if default:
                self.fields["account"].initial = default


class SettlementCorrectionForm(DocumentHeaderMixin, BootstrapFormMixin, forms.ModelForm):
    """Корректировка взаиморасчётов: контрагент, направление, сторона, сумма.

    У новой корректировки сторону выбирают явно (по умолчанию ничего не
    подставляется): «продажи», «закупки» или «не распределено»."""

    class Meta:
        model = SettlementCorrection
        fields = ["date", "number", "time", "organization", "counterparty", "direction", "side", "amount",
                  "comment"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.fields["counterparty"].queryset = Counterparty.objects.filter(is_active=True)
        side = self.fields["side"]
        side.required = True
        side.error_messages["required"] = "Укажите сторону расчётов: продажи, закупки или «не распределено»"
        if not self.instance.pk:
            side.choices = [("", "— выберите сторону —")] + list(SettlementCorrection.SIDE_CHOICES)
            side.initial = ""
            org = Organization.get_default()
            if org:
                self.fields["organization"].initial = org

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("Сумма должна быть больше нуля.")
        return amount
