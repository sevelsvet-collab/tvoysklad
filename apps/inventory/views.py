from decimal import Decimal

from django.contrib import messages
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.catalog.models import ProductGroup
from apps.core import roles
from apps.core.constants import DOC_POSTED
from apps.core.document_edit import LineDocumentMixin
from apps.core.models import Warehouse
from apps.core.permissions import RoleRequiredMixin

from .forms import (
    AdjustmentForm,
    AdjustmentLineFormSet,
    TransferForm,
    TransferLineFormSet,
)
from .models import StockAdjustment, StockBalance, Transfer

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER]
VIEW_ROLES = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]


# ---------- Остатки ----------

class BalanceListView(RoleRequiredMixin, ListView):
    allowed_roles = VIEW_ROLES
    template_name = "inventory/balance_list.html"
    context_object_name = "balances"
    paginate_by = 100

    def get_queryset(self):
        qs = (
            StockBalance.objects
            .select_related("product", "product__unit", "product__group", "warehouse")
            .filter(quantity__gt=0)
        )
        q = self.request.GET.get("q", "").strip()
        warehouse_id = self.request.GET.get("warehouse")
        group_id = self.request.GET.get("group")
        if q:
            qs = qs.filter(Q(product__name__icontains=q) | Q(product__article__icontains=q))
        if warehouse_id:
            qs = qs.filter(warehouse_id=warehouse_id)
        if group_id:
            group = ProductGroup.objects.filter(pk=group_id).first()
            if group:
                qs = qs.filter(product__group_id__in=group.descendant_ids())
        return qs.order_by("product__name", "warehouse__name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["warehouses"] = Warehouse.objects.filter(is_active=True)
        ctx["groups"] = ProductGroup.objects.filter(parent=None).prefetch_related("children")
        ctx["active_warehouse"] = self.request.GET.get("warehouse", "")
        ctx["active_group"] = self.request.GET.get("group", "")
        total = self.get_queryset().aggregate(
            value=Sum(ExpressionWrapper(
                F("quantity") * F("avg_cost"), output_field=DecimalField(max_digits=18, decimal_places=2),
            )),
        )
        ctx["total_value"] = total["value"] or Decimal("0")
        return ctx


# ---------- Перемещения ----------

class TransferListView(RoleRequiredMixin, ListView):
    allowed_roles = VIEW_ROLES
    model = Transfer
    template_name = "inventory/transfer_list.html"
    context_object_name = "transfers"
    paginate_by = 50

    def get_queryset(self):
        qs = Transfer.objects.select_related("warehouse_from", "warehouse_to", "organization")
        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)
        return qs


class TransferCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = EDIT_ROLES
    model = Transfer
    form_class = TransferForm
    formset_class = TransferLineFormSet
    template_name = "inventory/transfer_form.html"

    def get_success_url(self):
        return reverse("transfer_list")

    def get_edit_url(self):
        return reverse("transfer_edit", args=[self.object.pk])


class TransferUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = EDIT_ROLES
    model = Transfer
    form_class = TransferForm
    formset_class = TransferLineFormSet
    template_name = "inventory/transfer_form.html"

    def get_success_url(self):
        return reverse("transfer_list")

    def get_edit_url(self):
        return reverse("transfer_edit", args=[self.object.pk])


@require_POST
def transfer_post(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        transfer.post()
        messages.success(request, f"{transfer} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("transfer_edit", pk=pk)


@require_POST
def transfer_unpost(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    transfer.unpost()
    messages.info(request, f"{transfer} — снят с проведения")
    return redirect("transfer_edit", pk=pk)


@require_POST
def transfer_delete(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    if transfer.status == DOC_POSTED:
        transfer.unpost()
    transfer.delete()
    messages.info(request, "Перемещение удалено")
    return redirect("transfer_list")


# ---------- Оприходования / Списания ----------

class AdjustmentListView(RoleRequiredMixin, ListView):
    allowed_roles = VIEW_ROLES
    model = StockAdjustment
    template_name = "inventory/adjustment_list.html"
    context_object_name = "adjustments"
    paginate_by = 50
    kind = None  # приходит из URL (<str:kind>)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        if kwargs.get("kind"):
            self.kind = kwargs["kind"]

    def get_queryset(self):
        qs = StockAdjustment.objects.filter(kind=self.kind).select_related("warehouse", "organization")
        status = self.request.GET.get("status", "")
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["kind"] = self.kind
        ctx["is_income"] = self.kind == StockAdjustment.KIND_INCOME
        return ctx


class AdjustmentEditMixin(LineDocumentMixin):
    model = StockAdjustment
    form_class = AdjustmentForm
    formset_class = AdjustmentLineFormSet
    template_name = "inventory/adjustment_form.html"
    kind = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        kind = self.object.kind if self.object else self.kind
        ctx["is_income"] = kind == StockAdjustment.KIND_INCOME
        ctx["kind"] = kind
        return ctx

    def get_success_url(self):
        kind = self.object.kind
        return reverse("adjustment_list", args=[kind])

    def get_edit_url(self):
        return reverse("adjustment_edit", args=[self.object.pk])


class AdjustmentCreateView(RoleRequiredMixin, AdjustmentEditMixin, CreateView):
    allowed_roles = EDIT_ROLES

    def form_valid(self, form):
        form.instance.kind = self.kind
        return super().form_valid(form)


class AdjustmentUpdateView(RoleRequiredMixin, AdjustmentEditMixin, UpdateView):
    allowed_roles = EDIT_ROLES


@require_POST
def adjustment_post(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    try:
        adj.post()
        messages.success(request, f"{adj} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("adjustment_edit", pk=pk)


@require_POST
def adjustment_unpost(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    adj.unpost()
    messages.info(request, f"{adj} — снят с проведения")
    return redirect("adjustment_edit", pk=pk)


@require_POST
def adjustment_delete(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    kind = adj.kind
    if adj.status == DOC_POSTED:
        adj.unpost()
    adj.delete()
    messages.info(request, "Документ удалён")
    return redirect("adjustment_list", kind=kind)
