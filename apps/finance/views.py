from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.core import roles, submit_once
from apps.core.constants import DOC_STATUS_CHOICES
from apps.core.bulk import bulk_delete_view
from apps.core.listing import (
    ChoiceFilter, FilteredListMixin, PeriodFilter, TextFilter, delete_action, organizations,
)
from apps.core.permissions import RoleRequiredMixin, role_required
from apps.sales.models import Invoice

from . import settlements
from .forms import AccountCorrectionForm, AccountForm, PaymentForm, SettlementCorrectionForm
from .models import Account, AccountCorrection, Payment, SettlementCorrection
from .services import PaymentError

MONEY_ROLES = [roles.ROLE_ADMIN, roles.ROLE_ACCOUNTANT, roles.ROLE_MANAGER]


# ---------- Счета и кассы ----------

class AccountListView(RoleRequiredMixin, ListView):
    allowed_roles = MONEY_ROLES
    model = Account
    template_name = "finance/account_list.html"
    context_object_name = "accounts"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["total"] = sum((a.balance for a in ctx["accounts"]), Decimal("0"))
        return ctx


class AccountCreateView(RoleRequiredMixin, CreateView):
    allowed_roles = [roles.ROLE_ADMIN, roles.ROLE_ACCOUNTANT]
    model = Account
    form_class = AccountForm
    template_name = "finance/account_form.html"
    success_url = reverse_lazy("account_list")


class AccountUpdateView(RoleRequiredMixin, UpdateView):
    allowed_roles = [roles.ROLE_ADMIN, roles.ROLE_ACCOUNTANT]
    model = Account
    form_class = AccountForm
    template_name = "finance/account_form.html"
    success_url = reverse_lazy("account_list")


# ---------- Платежи ----------

def _accounts():
    return Account.objects.filter(is_active=True).values_list("pk", "name")


class PaymentListView(FilteredListMixin, RoleRequiredMixin, ListView):
    allowed_roles = MONEY_ROLES
    model = Payment
    template_name = "finance/payment_list.html"
    context_object_name = "payments"
    search_fields = ("number", "counterparty__name", "purpose")
    search_placeholder = "Номер, контрагент, назначение"
    list_filters = [
        PeriodFilter(),
        ChoiceFilter("kind", "Вид", choices=Payment.KIND_CHOICES),
        ChoiceFilter("settlement_purpose", "Назначение для взаиморасчётов", choices=Payment.PURPOSE_CHOICES),
        TextFilter("partner", "Контрагент", "counterparty__name", placeholder="Наименование"),
        ChoiceFilter("status", "Статус", choices=DOC_STATUS_CHOICES),
        ChoiceFilter("organization", "Организация", "organization_id", organizations),
        ChoiceFilter("account", "Счёт/касса", "account_id", _accounts),
    ]
    bulk_actions = [delete_action("payment_bulk_delete", "платежи")]

    def get_queryset(self):
        return self.filter_queryset(Payment.objects.select_related("counterparty", "account", "organization"))


class PaymentEditBase(RoleRequiredMixin):
    allowed_roles = MONEY_ROLES
    model = Payment
    form_class = PaymentForm
    template_name = "finance/payment_form.html"

    def get_success_url(self):
        return reverse("payment_edit", args=[self.object.pk])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["posting"] = self.request.method == "POST" and self.request.POST.get("action") == "save_post"
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        kind = self.object.kind if self.object else self.kind
        ctx["is_incoming"] = kind == Payment.KIND_IN
        ctx["kind"] = kind
        ctx["submit_token"] = submit_once.new_token()
        return ctx

    def form_valid(self, form):
        """Сохранение, проведение и пересчёт оплаты счетов — одна транзакция:
        при любой ошибке не остаётся ни изменённого платежа, ни старой оплаты."""
        creating = form.instance.pk is None
        try:
            with transaction.atomic():
                return self._save(form)
        except PaymentError as exc:
            for field, text in exc.errors.items():
                form.add_error(field if field in form.fields else None, text)
            if creating:
                form.instance.pk = None      # сохранение откатилось — платежа в базе нет
                self.object = None
            return self.form_invalid(form)

    def _save(self, form):
        creating = form.instance.pk is None
        if creating:
            duplicate = submit_once.claim(self.request)
            if duplicate:
                return duplicate   # повторная отправка — платёж уже создан
        response = super().form_valid(form)
        if self.request.POST.get("action") == "save_post":
            self.object.post()
            messages.success(self.request, f"{self.object} — проведён")
        else:
            messages.success(self.request, f"{self.object} — сохранён")
        if creating:
            submit_once.remember(self.request, self.get_success_url())
        return response


class PaymentCreateView(PaymentEditBase, CreateView):
    kind = None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["kind"] = self.kind
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        partner = self.request.GET.get("partner")  # предзаполнение из карточки контрагента
        if partner:
            initial["counterparty"] = partner
        return initial

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.instance.kind = self.kind  # серия номеров входящих и исходящих своя
        return form

    def form_valid(self, form):
        form.instance.kind = self.kind
        return super().form_valid(form)


class PaymentUpdateView(PaymentEditBase, UpdateView):
    def get_context_data(self, **kwargs):
        self.kind = self.object.kind
        return super().get_context_data(**kwargs)


@require_POST
@role_required(*MONEY_ROLES)
def payment_post(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    try:
        payment.post()
    except PaymentError as exc:
        messages.error(request, f"Платёж не проведён: {exc}")
        return redirect("payment_edit", pk=pk)
    messages.success(request, f"{payment} — проведён")
    return redirect("payment_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def payment_unpost(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.unpost()
    messages.info(request, f"{payment} — снят с проведения")
    return redirect("payment_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.delete()        # оплату счёта пересчитывает сам платёж, в той же транзакции
    messages.info(request, "Платёж удалён")
    return redirect("payment_list")


@require_POST
@role_required(*MONEY_ROLES)
def invoice_to_payment(request, pk, account_kind=Account.KIND_BANK):
    """Платёж по счёту: на расчётный счёт или наличными в кассу (приходный ордер)."""
    invoice = get_object_or_404(Invoice, pk=pk)
    # счёт или касса — только организации, выставившей счёт; нет — заводится для неё
    account, created = Account.ensure_default(account_kind, invoice.organization)
    if account is None:
        messages.error(request, "Сначала заведите организацию в настройках")
        return redirect("invoice_edit", pk=pk)
    if created:
        messages.info(request, f"Создана «{account.name}» — проверьте реквизиты "
                               "в разделе «Деньги → Счета и кассы»")
    remaining = invoice.total - invoice.paid_amount
    if remaining <= 0:
        # нулевой платёж «на всякий случай» не создаём; переплату можно
        # оформить вручную обычным входящим платежом
        messages.info(request, f"{invoice} уже оплачен полностью — неоплаченного остатка нет. "
                               "Если нужна оплата сверх суммы счёта, создайте входящий платёж вручную")
        return redirect("invoice_edit", pk=pk)
    payment = Payment.objects.create(
        kind=Payment.KIND_IN, organization=invoice.organization,
        account=account, counterparty=invoice.customer, invoice=invoice,
        amount=remaining,
        purpose=f"Оплата по счёту № {invoice.number} от {invoice.date:%d.%m.%Y}",
        settlement_purpose=Payment.PURPOSE_CUSTOMER_PAYMENT,     # счёт — продажа
    )
    what = "приходный ордер" if account_kind == Account.KIND_CASH else "входящий платёж"
    messages.success(request, f"Создан {what} № {payment.number} — проверьте и проведите")
    return redirect("payment_edit", pk=payment.pk)


# ---------- Взаиморасчёты ----------

class SettlementsView(RoleRequiredMixin, ListView):
    """Взаиморасчёты: строки — пары «организация + контрагент», расчёт —
    apps/finance/settlements.py (тот же, что в карточке контрагента).
    Только чтение."""

    allowed_roles = MONEY_ROLES
    template_name = "finance/settlements.html"
    context_object_name = "rows"

    def get_queryset(self):
        return settlements.settlement_pairs()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["totals"] = settlements.totals(ctx["rows"])
        return ctx


# ---------- Корректировки ----------

class CorrectionListView(RoleRequiredMixin, ListView):
    allowed_roles = MONEY_ROLES
    template_name = "finance/correction_list.html"
    context_object_name = "rows"

    def get_queryset(self):
        rows = []
        for c in AccountCorrection.objects.select_related("account", "organization"):
            kind_label = "Остаток в кассе" if c.account.kind == Account.KIND_CASH else "Остаток на счёте"
            rows.append({
                "number": c.number, "date": c.date, "type_label": kind_label,
                "target": c.account.name, "amount": c.amount, "comment": c.comment,
                "is_posted": c.is_posted, "url": reverse("account_correction_edit", args=[c.pk]),
                "sort": (c.date, c.pk),
            })
        for c in SettlementCorrection.objects.select_related("counterparty", "organization"):
            rows.append({
                "number": c.number, "date": c.date, "type_label": "Взаиморасчёты",
                "target": c.counterparty.name, "amount": c.signed_amount, "comment": c.comment,
                "is_posted": c.is_posted, "url": reverse("settlement_correction_edit", args=[c.pk]),
                "sort": (c.date, c.pk),
            })
        rows.sort(key=lambda r: r["sort"], reverse=True)
        return rows


class _CorrectionEditMixin(RoleRequiredMixin):
    allowed_roles = MONEY_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["submit_token"] = submit_once.new_token()
        return ctx

    def post(self, request, *args, **kwargs):
        # Повторная отправка той же формы создания — корректировка уже есть
        with transaction.atomic():
            creating = "pk" not in kwargs
            if creating:
                duplicate = submit_once.claim(request)
                if duplicate:
                    return duplicate
            response = super().post(request, *args, **kwargs)
            if creating and getattr(self, "object", None) is not None and self.object.pk:
                submit_once.remember(request, self.get_success_url())
            return response

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("action") == "save_post":
            self.object.post()
            messages.success(self.request, f"{self.object} — проведена")
        else:
            messages.success(self.request, f"{self.object} — сохранена")
        return response


class AccountCorrectionEditBase(_CorrectionEditMixin):
    model = AccountCorrection
    form_class = AccountCorrectionForm
    template_name = "finance/account_correction_form.html"

    def get_success_url(self):
        return reverse("account_correction_edit", args=[self.object.pk])

    def form_valid(self, form):
        # Расчётный остаток и разницу считает сама модель при сохранении и
        # проведении — по истории кассы на момент документа (правило «остаток
        # на момент»). Отказ модели (касса другой организации — например, её
        # успели передать после проверки формы) откатывает всё сохранение.
        creating = form.instance.pk is None
        try:
            with transaction.atomic():
                self.object = form.save()
                if self.request.POST.get("action") == "save_post":
                    self.object.post()
        except PaymentError as exc:
            for field, text in exc.errors.items():
                form.add_error(field if field in form.fields else None, text)
            if creating:
                form.instance.pk = None      # сохранение откатилось — корректировки в базе нет
                self.object = None
            return self.form_invalid(form)
        if self.request.POST.get("action") == "save_post":
            messages.success(self.request, f"{self.object} — проведена")
        else:
            messages.success(self.request, f"{self.object} — сохранена")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["account_kind"] = self.account_kind
        ctx["is_cash"] = self.account_kind == Account.KIND_CASH
        return ctx


class AccountCorrectionCreateView(AccountCorrectionEditBase, CreateView):
    account_kind = None  # задаётся в urls (cash/bank)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["account_kind"] = self.account_kind
        return kwargs


class AccountCorrectionUpdateView(AccountCorrectionEditBase, UpdateView):
    @property
    def account_kind(self):
        return self.object.account.kind if self.object else None


class SettlementCorrectionEditBase(_CorrectionEditMixin):
    model = SettlementCorrection
    form_class = SettlementCorrectionForm
    template_name = "finance/settlement_correction_form.html"

    def get_success_url(self):
        return reverse("settlement_correction_edit", args=[self.object.pk])


class SettlementCorrectionCreateView(SettlementCorrectionEditBase, CreateView):
    pass


class SettlementCorrectionUpdateView(SettlementCorrectionEditBase, UpdateView):
    pass


@require_POST
@role_required(*MONEY_ROLES)
def account_correction_post(request, pk):
    obj = get_object_or_404(AccountCorrection, pk=pk)
    try:
        obj.post()
    except PaymentError as exc:
        messages.error(request, f"Не удалось провести: {exc}")
        return redirect("account_correction_edit", pk=pk)
    messages.success(request, f"{obj} — проведена")
    return redirect("account_correction_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def account_correction_unpost(request, pk):
    obj = get_object_or_404(AccountCorrection, pk=pk)
    obj.unpost()
    messages.info(request, f"{obj} — снята с проведения")
    return redirect("account_correction_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def account_correction_delete(request, pk):
    get_object_or_404(AccountCorrection, pk=pk).delete()
    messages.info(request, "Корректировка удалена")
    return redirect("correction_list")


@require_POST
@role_required(*MONEY_ROLES)
def settlement_correction_post(request, pk):
    obj = get_object_or_404(SettlementCorrection, pk=pk)
    obj.post()
    messages.success(request, f"{obj} — проведена")
    return redirect("settlement_correction_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def settlement_correction_unpost(request, pk):
    obj = get_object_or_404(SettlementCorrection, pk=pk)
    obj.unpost()
    messages.info(request, f"{obj} — снята с проведения")
    return redirect("settlement_correction_edit", pk=pk)


@require_POST
@role_required(*MONEY_ROLES)
def settlement_correction_delete(request, pk):
    get_object_or_404(SettlementCorrection, pk=pk).delete()
    messages.info(request, "Корректировка удалена")
    return redirect("correction_list")


# ---------- Удаление отмеченных в списках ----------

payment_bulk_delete = bulk_delete_view(Payment, "payment_list", MONEY_ROLES, "платежей")
