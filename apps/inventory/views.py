import datetime
from decimal import Decimal
from types import SimpleNamespace

from django.contrib import messages
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.catalog.models import Product, ProductGroup
from apps.core import roles
from apps.core.constants import DOC_STATUS_CHOICES
from apps.core.bulk import bulk_delete_view
from apps.core.document_edit import LineDocumentMixin
from apps.core.listing import (
    ChoiceFilter, FilteredListMixin, PeriodFilter, delete_action, document_filters,
    organizations, warehouses,
)
from apps.core.models import Warehouse
from apps.core.permissions import RoleRequiredMixin, role_required
from apps.inventory import costing
from apps.inventory.services import StockError

from .forms import (
    AdjustmentForm,
    AdjustmentLineFormSet,
    TransferForm,
    TransferLineFormSet,
)
from .models import StockAdjustment, StockBalance, StockMovement, Transfer

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER]
VIEW_ROLES = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER, roles.ROLE_MANAGER, roles.ROLE_ACCOUNTANT]


# ---------- Остатки ----------

class BalanceListView(RoleRequiredMixin, ListView):
    """Текущие остатки, а с параметром ?on=ГГГГ-ММ-ДД — остатки на дату.

    Строка — «организация + склад + товар»: у каждой организации свои
    количество, стоимость и средняя. Фильтр ?organization= — одна
    организация; без него видны все, организация — отдельной колонкой.
    Дефицит одной организации (продано раньше поступления, когда ей минус
    разрешён) не вычитается из товара другой: строки показываются отдельно,
    а сводка по складу (справочно) называет «в наличии» и «дефицит» порознь,
    средняя в ней — только по имеющемуся товару.

    Остаток на дату не выводится из текущего: история товара проигрывается
    заново только по документам с датой не позже выбранной (costing.state_on).
    Пока переход на учёт по организациям не завершён, остатков нет —
    показывается предупреждение (transition.is_ready).
    """

    allowed_roles = VIEW_ROLES
    template_name = "inventory/balance_list.html"
    context_object_name = "balances"
    paginate_by = 100

    def get_queryset(self):
        from apps.inventory import transition

        self.on_date = _parse_date(self.request.GET.get("on"))
        self.ready = transition.is_ready()
        organization = self.request.GET.get("organization", "")
        self.organization_id = int(organization) if organization.isdigit() else None
        if not self.ready:
            return []
        if self.on_date:
            return self._rows_on_date()
        qs = (
            StockBalance.objects
            .select_related("product", "product__unit", "product__group", "warehouse", "organization")
            .exclude(quantity=0)
        )
        q = self.request.GET.get("q", "").strip()
        warehouse_id = self.request.GET.get("warehouse")
        group_ids = self._group_ids()
        if q:
            qs = qs.filter(Q(product__name__icontains=q) | Q(product__article__icontains=q))
        if warehouse_id:
            qs = qs.filter(warehouse_id=warehouse_id)
        if self.organization_id:
            qs = qs.filter(organization_id=self.organization_id)
        if group_ids is not None:
            qs = qs.filter(product__group_id__in=group_ids)
        return qs.order_by("product__name", "warehouse__name", "organization__name")

    def _group_ids(self):
        group = ProductGroup.objects.filter(pk=self.request.GET.get("group") or None).first()
        return group.descendant_ids() if group else None

    def _rows_on_date(self):
        if hasattr(self, "_rows"):
            return self._rows
        from apps.core.models import Organization

        movements = StockMovement.objects.filter(date__lte=self.on_date)
        q = self.request.GET.get("q", "").strip()
        warehouse_id = self.request.GET.get("warehouse")
        group_ids = self._group_ids()
        if q:
            movements = movements.filter(Q(product__name__icontains=q) | Q(product__article__icontains=q))
        if group_ids is not None:
            movements = movements.filter(product__group_id__in=group_ids)
        if self.organization_id:
            movements = movements.filter(organization_id=self.organization_id)
        product_ids = sorted(set(movements.values_list("product_id", flat=True)))
        products = Product.objects.select_related("unit", "group").in_bulk(product_ids)
        stores = Warehouse.objects.in_bulk()
        owners = Organization.objects.in_bulk()
        rows = []
        for product_id in product_ids:
            for (org_id, store_id), (quantity, value, open_qty) in costing.state_on(product_id, self.on_date).items():
                if warehouse_id and str(store_id) != warehouse_id or not quantity:
                    continue
                if self.organization_id and org_id != self.organization_id:
                    continue
                rows.append(SimpleNamespace(
                    product=products[product_id], warehouse=stores[store_id], organization=owners.get(org_id),
                    quantity=quantity, total_value=value, open_qty=open_qty,
                    avg_cost=costing.unit_cost(value, quantity) if quantity > 0 else Decimal("0"),
                ))
        rows.sort(key=lambda row: (row.product.name, row.warehouse.name,
                                   row.organization.name if row.organization else ""))
        self._rows = rows
        return rows

    @staticmethod
    def _summaries(rows):
        """Справочная сводка по «складу + товару», где строк организаций
        несколько: имеющийся товар и дефицит — порознь, средняя — только по
        имеющемуся товару. Для проведения не используется."""
        groups = {}
        for row in rows:
            groups.setdefault((row.product.pk, row.warehouse.pk), []).append(row)
        for items in groups.values():
            if len(items) < 2:
                continue
            positive = [row for row in items if row.quantity > 0]
            qty = sum((row.quantity for row in positive), Decimal("0"))
            value = sum((row.total_value or Decimal("0") for row in positive), Decimal("0"))
            deficit = sum((-row.quantity for row in items if row.quantity < 0), Decimal("0"))
            items[-1].summary = SimpleNamespace(
                quantity=qty, value=value, deficit=deficit,
                average=costing.unit_cost(value, qty) if qty > 0 else None,
            )

    def get_context_data(self, **kwargs):
        from apps.core.models import Organization

        ctx = super().get_context_data(**kwargs)
        ctx["warehouses"] = Warehouse.objects.filter(is_active=True)
        ctx["organizations"] = Organization.objects.order_by("name")
        ctx["groups"] = ProductGroup.objects.filter(parent=None).prefetch_related("children")
        ctx["active_warehouse"] = self.request.GET.get("warehouse", "")
        ctx["active_organization"] = str(self.organization_id or "")
        ctx["active_group"] = self.request.GET.get("group", "")
        ctx["on_date"] = self.on_date
        ctx["stock_ready"] = self.ready
        page = list(ctx["balances"])
        self._summaries(page)
        if not self.ready:
            ctx["total_value"] = None
            return ctx
        rows = self._rows_on_date() if self.on_date else self.get_queryset()
        if self.on_date:
            value = sum((row.total_value for row in rows if row.quantity > 0), Decimal("0"))
            deficit = [row for row in rows if row.quantity < 0]
        else:
            money = DecimalField(max_digits=18, decimal_places=2)
            value = rows.filter(quantity__gt=0).aggregate(
                # записанная стоимость остатка; «количество × средняя» — только для
                # остатков, ещё не пересчитанных после обновления
                value=Sum(Coalesce(
                    F("value"),
                    ExpressionWrapper(F("quantity") * F("avg_cost"), output_field=money),
                    output_field=money,
                )),
            )["value"] or Decimal("0")
            deficit = list(rows.filter(quantity__lt=0))
        ctx["total_value"] = value
        ctx["deficit_rows"] = len(deficit)
        ctx["deficit_quantity"] = sum((-row.quantity for row in deficit), Decimal("0"))
        return ctx


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------- Перемещения ----------

class TransferListView(FilteredListMixin, RoleRequiredMixin, ListView):
    allowed_roles = VIEW_ROLES
    model = Transfer
    template_name = "inventory/transfer_list.html"
    context_object_name = "transfers"
    search_fields = ("number", "comment")
    search_placeholder = "Номер или комментарий"
    list_filters = [
        PeriodFilter(),
        ChoiceFilter("status", "Статус", choices=DOC_STATUS_CHOICES),
        ChoiceFilter("organization", "Организация", "organization_id", organizations),
        ChoiceFilter("warehouse_from", "Со склада", "warehouse_from_id", warehouses),
        ChoiceFilter("warehouse_to", "На склад", "warehouse_to_id", warehouses),
    ]
    bulk_actions = [delete_action("transfer_bulk_delete", "перемещения")]

    def get_queryset(self):
        return self.filter_queryset(Transfer.objects.select_related("warehouse_from", "warehouse_to", "organization"))


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
@role_required(*EDIT_ROLES)
def transfer_post(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        transfer.post()
        messages.success(request, f"{transfer} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("transfer_edit", pk=pk)


@require_POST
@role_required(*EDIT_ROLES)
def transfer_unpost(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        transfer.unpost()
    except StockError as exc:
        messages.error(request, f"Не удалось снять с проведения: {exc}")
        return redirect("transfer_edit", pk=pk)
    messages.info(request, f"{transfer} — снят с проведения")
    return redirect("transfer_edit", pk=pk)


@require_POST
@role_required(*EDIT_ROLES)
def transfer_delete(request, pk):
    transfer = get_object_or_404(Transfer, pk=pk)
    try:
        transfer.delete()   # проведённый снимается с проведения — с проверкой остатков
    except StockError as exc:
        messages.error(request, f"Не удалось удалить: {exc}")
        return redirect("transfer_edit", pk=pk)
    messages.info(request, "Перемещение удалено")
    return redirect("transfer_list")


# ---------- Оприходования / Списания ----------

class AdjustmentListView(FilteredListMixin, RoleRequiredMixin, ListView):
    allowed_roles = VIEW_ROLES
    model = StockAdjustment
    template_name = "inventory/adjustment_list.html"
    context_object_name = "adjustments"
    search_fields = ("number", "reason", "comment")
    search_placeholder = "Номер, причина или комментарий"
    list_filters = document_filters()
    bulk_actions = [delete_action("adjustment_bulk_delete", "документы")]
    kind = None  # приходит из URL (<str:kind>)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        if kwargs.get("kind"):
            self.kind = kwargs["kind"]

    def get_queryset(self):
        qs = StockAdjustment.objects.filter(kind=self.kind).select_related("warehouse", "organization")
        return self.filter_queryset(qs)

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

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.instance.kind = self.kind  # серия номеров приходов и расходов своя
        return form

    def form_valid(self, form):
        form.instance.kind = self.kind
        return super().form_valid(form)


class AdjustmentUpdateView(RoleRequiredMixin, AdjustmentEditMixin, UpdateView):
    allowed_roles = EDIT_ROLES


@require_POST
@role_required(*EDIT_ROLES)
def adjustment_post(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    try:
        adj.post()
        messages.success(request, f"{adj} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("adjustment_edit", pk=pk)


@require_POST
@role_required(*EDIT_ROLES)
def adjustment_unpost(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    try:
        adj.unpost()
    except StockError as exc:
        messages.error(request, f"Не удалось снять с проведения: {exc}")
        return redirect("adjustment_edit", pk=pk)
    messages.info(request, f"{adj} — снят с проведения")
    return redirect("adjustment_edit", pk=pk)


@require_POST
@role_required(*EDIT_ROLES)
def adjustment_delete(request, pk):
    adj = get_object_or_404(StockAdjustment, pk=pk)
    kind = adj.kind
    try:
        adj.delete()   # проведённый снимается с проведения — с проверкой остатков
    except StockError as exc:
        messages.error(request, f"Не удалось удалить: {exc}")
        return redirect("adjustment_edit", pk=pk)
    messages.info(request, "Документ удалён")
    return redirect("adjustment_list", kind=kind)


# ---------- Удаление отмеченных в списках ----------

transfer_bulk_delete = bulk_delete_view(Transfer, "transfer_list", EDIT_ROLES, "перемещений")
adjustment_bulk_delete = bulk_delete_view(StockAdjustment, "adjustment_list_income", EDIT_ROLES, "документов")
