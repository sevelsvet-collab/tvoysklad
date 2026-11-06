import json
from functools import cached_property

from django import forms
from django.core.exceptions import ValidationError

from apps.core.forms import DocumentHeaderMixin, BootstrapFormMixin
from apps.core.models import Organization
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from . import services
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

        # Сторону контрагента задаёт назначение, а не направление денег: возврат
        # покупателю — исходящий платёж покупателю, возврат от поставщика —
        # входящий от поставщика. Здесь — все действующие, тип проверяет clean();
        # живой поиск сужен тем же правилом (counterparty_rules).
        self.fields["counterparty"].queryset = Counterparty.objects.filter(is_active=True)
        # Привязка к счёту покупателю — только у входящего
        if self.kind == Payment.KIND_IN:
            self.fields["invoice"].queryset = Invoice.objects.filter(status=Invoice.STATUS_ISSUED)
        else:
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

    # ----- контрагент: тип по назначению

    def _rule(self, purpose, has_invoice):
        """Правило выбора контрагента при этом назначении: тип для поиска
        ("" — любой), тип нового контрагента и текст отказа ({name}, {type} —
        название и тип неподходящего)."""
        values = {"kind": self.kind, "settlement_purpose": purpose,
                  "invoice_id": has_invoice or None}     # какой счёт — правилу не важно
        partner_type = services.counterparty_type(values)
        effective = purpose or services.auto_purpose(values)
        label = dict(Payment.PURPOSE_CHOICES).get(effective, effective)
        if self.kind == Payment.KIND_IN:
            default = Counterparty.TYPE_CUSTOMER
        else:
            default = Counterparty.TYPE_SUPPLIER
        needed = {Counterparty.TYPE_CUSTOMER: "покупателя", Counterparty.TYPE_SUPPLIER: "поставщика"}
        message = ""
        if partner_type:
            message = (
                f"«{{name}}» — {{type}}. «{label}» оформляется на {needed[partner_type]} или на "
                f"контрагента с типом «Покупатель и поставщик». Выберите другого контрагента или другое "
                f"назначение; тип контрагента меняется в его карточке."
            )
        return {
            "type": partner_type,
            "create": partner_type or default,     # новый — как раньше по направлению
            "message": message,
        }

    def _bound_rule(self):
        purpose = self["settlement_purpose"].value() or ""
        if purpose not in dict(self.fields["settlement_purpose"].choices):
            purpose = ""        # не из списка — на странице выбрано «не указано»
        has_invoice = "invoice" in self.fields and bool(self["invoice"].value())
        return self._rule(purpose, has_invoice)

    @property
    def counterparty_rules(self):
        """Правила для страницы (payment-counterparty.js): при каждом назначении,
        со счётом-основанием и без. Из той же services.counterparty_type, что
        проверяет clean()."""
        states = {"none": False, "invoice": True} if "invoice" in self.fields else {"none": False}
        return {
            "purposes": {
                value: {state: self._rule(value, has_invoice) for state, has_invoice in states.items()}
                for value, _ in self.fields["settlement_purpose"].choices
            },
            "type_labels": {value: text.lower() for value, text in Counterparty.TYPE_CHOICES},
        }

    @property
    def counterparty_search_params(self):
        """Параметры живого поиска для того, что сейчас выбрано в форме."""
        return json.dumps({"type": self._bound_rule()["type"]})

    @cached_property
    def selected_counterparty(self):
        """Контрагент, чей id сейчас в поле. При повторном показе формы — присланный,
        а не прежний из документа: иначе название рядом с полем и скрытое значение
        расходятся. Неизвестный id — None: поле пустое, а не со скрытым значением."""
        value = self["counterparty"].value()
        if value in (None, ""):
            return None
        try:
            return Counterparty.objects.filter(pk=value).first()
        except (TypeError, ValueError, ValidationError):
            return None

    def clean(self):
        cleaned = super().clean()
        counterparty = cleaned.get("counterparty")
        if counterparty is None or {"settlement_purpose", "invoice"} & set(self.errors):
            return cleaned
        rule = self._rule(cleaned.get("settlement_purpose") or "", bool(cleaned.get("invoice")))
        if rule["type"] and counterparty.partner_type not in (rule["type"], Counterparty.TYPE_BOTH):
            text = rule["message"].format(name=counterparty, type=counterparty.get_partner_type_display().lower())
            self.add_error("counterparty", ValidationError(text, code="partner_type"))
        return cleaned

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
