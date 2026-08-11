from decimal import Decimal

from django.contrib import messages
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.core import roles
from apps.core.constants import DOC_POSTED
from apps.core.permissions import RoleRequiredMixin
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import Invoice

from .forms import AccountCorrectionForm, AccountForm, PaymentForm, SettlementCorrectionForm
from .models import Account, AccountCorrection, Payment, SettlementCorrection

MONEY_ROLES = [roles.ROLE_ADMIN, roles.ROLE_ACCOUNTANT, roles.ROLE_MANAGER]
_ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=18, decimal_places=2))


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

class PaymentListView(RoleRequiredMixin, ListView):
    allowed_roles = MONEY_ROLES
    model = Payment
    template_name = "finance/payment_list.html"
    context_object_name = "payments"
    paginate_by = 50

    def get_queryset(self):
        qs = Payment.objects.select_related("counterparty", "account", "organization")
        kind = self.request.GET.get("kind", "")
        q = self.request.GET.get("q", "").strip()
        if kind:
            qs = qs.filter(kind=kind)
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(counterparty__name__icontains=q) | Q(purpose__icontains=q))
        return qs


class PaymentEditBase(RoleRequiredMixin):
    allowed_roles = MONEY_ROLES
    model = Payment
    form_class = PaymentForm
    template_name = "finance/payment_form.html"

    def get_success_url(self):
        return reverse("payment_edit", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        kind = self.object.kind if self.object else self.kind
        ctx["is_incoming"] = kind == Payment.KIND_IN
        ctx["kind"] = kind
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("action") == "save_post":
            self.object.post()
            messages.success(self.request, f"{self.object} — проведён")
        else:
            messages.success(self.request, f"{self.object} — сохранён")
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

    def form_valid(self, form):
        form.instance.kind = self.kind
        return super().form_valid(form)


class PaymentUpdateView(PaymentEditBase, UpdateView):
    def get_context_data(self, **kwargs):
        self.kind = self.object.kind
        return super().get_context_data(**kwargs)


@require_POST
def payment_post(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.post()
    messages.success(request, f"{payment} — проведён")
    return redirect("payment_edit", pk=pk)


@require_POST
def payment_unpost(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.unpost()
    messages.info(request, f"{payment} — снят с проведения")
    return redirect("payment_edit", pk=pk)


@require_POST
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    invoice = payment.invoice
    payment.delete()
    if invoice:
        from .models import recompute_invoice_paid

        recompute_invoice_paid(invoice)
    messages.info(request, "Платёж удалён")
    return redirect("payment_list")


@require_POST
def invoice_to_payment(request, pk):
    """Создать входящий платёж на основании счёта (остаток к оплате)."""
    invoice = get_object_or_404(Invoice, pk=pk)
    remaining = invoice.total - invoice.paid_amount
    payment = Payment.objects.create(
        kind=Payment.KIND_IN, organization=invoice.organization,
        account=Account.get_default(), counterparty=invoice.customer, invoice=invoice,
        amount=remaining if remaining > 0 else Decimal("0"),
        purpose=f"Оплата по счёту № {invoice.number} от {invoice.date:%d.%m.%Y}",
    )
    messages.success(request, f"Создан входящий платёж № {payment.number} — проверьте и проведите")
    return redirect("payment_edit", pk=payment.pk)


# ---------- Взаиморасчёты ----------

class SettlementsView(RoleRequiredMixin, ListView):
    allowed_roles = MONEY_ROLES
    template_name = "finance/settlements.html"
    context_object_name = "rows"

    def get_queryset(self):
        rows = []
        for cp in Counterparty.objects.all():
            # Invoice.total / Receipt.total — Python-свойства (сумма строк), считаем перебором
            sales_total = _sum_invoice_totals(cp)
            purch_total = _sum_receipt_totals(cp)
            paid_in = Payment.objects.filter(counterparty=cp, kind=Payment.KIND_IN, status=DOC_POSTED).aggregate(
                s=Coalesce(Sum("amount"), _ZERO))["s"]
            paid_out = Payment.objects.filter(counterparty=cp, kind=Payment.KIND_OUT, status=DOC_POSTED).aggregate(
                s=Coalesce(Sum("amount"), _ZERO))["s"]

            cust_return = _sum_customer_returns(cp)   # мы вернули покупателю (уменьшает его долг)
            supp_return = _sum_supplier_returns(cp)   # мы вернули поставщику (уменьшает наш долг)

            correction = SettlementCorrection.objects.filter(
                counterparty=cp, status=DOC_POSTED).aggregate(
                s=Coalesce(Sum("amount", filter=Q(direction=SettlementCorrection.DIR_THEY_OWE)), _ZERO)
                - Coalesce(Sum("amount", filter=Q(direction=SettlementCorrection.DIR_WE_OWE)), _ZERO))["s"]

            they_owe = sales_total - cust_return - paid_in   # долг покупателя нам
            we_owe = purch_total - supp_return - paid_out    # наш долг поставщику
            balance = they_owe - we_owe + correction         # >0 — нам должны, <0 — мы должны
            if sales_total or paid_in or purch_total or paid_out or cust_return or supp_return or correction:
                rows.append({
                    "cp": cp, "sales": sales_total, "paid_in": paid_in,
                    "purch": purch_total, "paid_out": paid_out,
                    "cust_return": cust_return, "supp_return": supp_return,
                    "correction": correction,
                    "they_owe": they_owe, "we_owe": we_owe, "balance": balance,
                })
        rows.sort(key=lambda r: abs(r["balance"]), reverse=True)
        return rows


def _sum_invoice_totals(counterparty):
    total = Decimal("0")
    for inv in Invoice.objects.filter(customer=counterparty, status=Invoice.STATUS_ISSUED).prefetch_related("lines"):
        total += inv.total
    return total


def _sum_receipt_totals(counterparty):
    total = Decimal("0")
    for r in Receipt.objects.filter(supplier=counterparty, status=DOC_POSTED).prefetch_related("lines"):
        total += r.total
    return total


def _sum_customer_returns(counterparty):
    from apps.sales.models import CustomerReturn

    total = Decimal("0")
    for r in CustomerReturn.objects.filter(customer=counterparty, status=DOC_POSTED).prefetch_related("lines"):
        total += r.total
    return total


def _sum_supplier_returns(counterparty):
    from apps.purchases.models import SupplierReturn

    total = Decimal("0")
    for r in SupplierReturn.objects.filter(supplier=counterparty, status=DOC_POSTED).prefetch_related("lines"):
        total += r.total
    return total


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
        # Сохраняем, затем пересчитываем разницу от актуального остатка счёта.
        self.object = form.save()
        self.object.recalc()
        self.object.save(update_fields=["balance_before", "amount"])
        if self.request.POST.get("action") == "save_post":
            self.object.post()
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
def account_correction_post(request, pk):
    obj = get_object_or_404(AccountCorrection, pk=pk)
    obj.post()
    messages.success(request, f"{obj} — проведена")
    return redirect("account_correction_edit", pk=pk)


@require_POST
def account_correction_unpost(request, pk):
    obj = get_object_or_404(AccountCorrection, pk=pk)
    obj.unpost()
    messages.info(request, f"{obj} — снята с проведения")
    return redirect("account_correction_edit", pk=pk)


@require_POST
def account_correction_delete(request, pk):
    get_object_or_404(AccountCorrection, pk=pk).delete()
    messages.info(request, "Корректировка удалена")
    return redirect("correction_list")


@require_POST
def settlement_correction_post(request, pk):
    obj = get_object_or_404(SettlementCorrection, pk=pk)
    obj.post()
    messages.success(request, f"{obj} — проведена")
    return redirect("settlement_correction_edit", pk=pk)


@require_POST
def settlement_correction_unpost(request, pk):
    obj = get_object_or_404(SettlementCorrection, pk=pk)
    obj.unpost()
    messages.info(request, f"{obj} — снята с проведения")
    return redirect("settlement_correction_edit", pk=pk)


@require_POST
def settlement_correction_delete(request, pk):
    get_object_or_404(SettlementCorrection, pk=pk).delete()
    messages.info(request, "Корректировка удалена")
    return redirect("correction_list")
