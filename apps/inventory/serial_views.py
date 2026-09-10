"""Серийные номера: номера на остатке (для выбора в документе), ввод номеров
старого остатка из карточки товара и отчёт с историей каждого номера."""
from collections import defaultdict

from django.apps import apps as django_apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.core import roles
from apps.core.pagination import PageSizeMixin
from apps.core.permissions import RoleRequiredMixin, role_required

from . import serials
from .models import SerialMovement, SerialNumber
from .services import StockError

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER]

# Вид документа → (подпись, маршрут карточки, модель, поле контрагента)
DOC_INFO = {
    "receipt": ("Приёмка", "receipt_edit", "purchases.Receipt", "supplier"),
    "supplierreturn": ("Возврат поставщику", "supplier_return_edit", "purchases.SupplierReturn", "supplier"),
    "shipment": ("Отгрузка", "shipment_edit", "sales.Shipment", "customer"),
    "customerreturn": ("Возврат покупателя", "customer_return_edit", "sales.CustomerReturn", "customer"),
    "transfer": ("Перемещение", "transfer_edit", "inventory.Transfer", None),
    "stockadjustment": ("Оприходование/списание", "adjustment_edit", "inventory.StockAdjustment", None),
}


def describe(movements):
    """Подпись документа, ссылка и контрагент для каждого движения номера."""
    ids = defaultdict(set)
    for m in movements:
        ids[m.doc_type].add(m.doc_id)
    docs = {}
    for doc_type, doc_ids in ids.items():
        info = DOC_INFO.get(doc_type)
        if not info:
            continue
        model = django_apps.get_model(info[2])
        related = [info[3]] if info[3] else []
        for doc in model.objects.filter(pk__in=doc_ids).select_related(*related):
            docs[(doc_type, doc.pk)] = doc

    rows = []
    for m in movements:
        info = DOC_INFO.get(m.doc_type)
        doc = docs.get((m.doc_type, m.doc_id))
        if info is None:   # ввод номеров старого остатка из карточки товара
            label, url, partner = "Ввод остатка", reverse("product_edit", args=[m.serial.product_id]), None
        else:
            label = doc.get_kind_display() if m.doc_type == "stockadjustment" and doc else info[0]
            url = reverse(info[1], args=[m.doc_id]) if doc else ""
            partner = getattr(doc, info[3]) if doc and info[3] else None
        rows.append({"m": m, "label": label, "url": url, "partner": partner})
    return rows


@login_required
def serials_available(request):
    """Номера товара на складе — для выбора при расходе."""
    product_id = request.GET.get("product", "")
    warehouse_id = request.GET.get("warehouse", "")
    if not (product_id.isdigit() and warehouse_id.isdigit()):
        return JsonResponse({"serials": []})
    return JsonResponse({"serials": serials.available(int(product_id), int(warehouse_id))})


@require_POST
@login_required
@role_required(*EDIT_ROLES)
def product_serial_stock(request, pk):
    """Ввод номеров для товара, который лежал на складе до включения учёта."""
    from apps.catalog.models import Product
    from apps.core.models import Warehouse

    product = get_object_or_404(Product, pk=pk)
    warehouse = get_object_or_404(Warehouse, pk=request.POST.get("warehouse") or 0)
    try:
        count = serials.enter_stock(product, warehouse, request.POST.get("numbers", ""))
        messages.success(request, f"Добавлено серийных номеров: {count} (склад «{warehouse}»)")
    except StockError as exc:
        messages.error(request, str(exc))
    url = reverse("product_edit", args=[pk])
    if request.GET.get("embed"):
        url += "?embed=1"
    return redirect(url + "#serials")


class SerialReportView(PageSizeMixin, RoleRequiredMixin, ListView):
    """Все серийные номера: где сейчас, откуда пришёл и кому ушёл."""

    template_name = "reports/serials.html"
    context_object_name = "serials"

    def get_queryset(self):
        qs = SerialNumber.objects.select_related("product", "warehouse").order_by("product__name", "number")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(product__name__icontains=q))
        status = self.request.GET.get("status", "")
        if status in dict(SerialNumber.STATUS_CHOICES):
            qs = qs.filter(status=status)
        product = self.request.GET.get("product", "")
        if product.isdigit():
            qs = qs.filter(product_id=product)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page = list(ctx["serials"])
        moves = describe(list(
            SerialMovement.objects.filter(serial__in=page).select_related("serial", "warehouse")
            .order_by("date", "id")
        ))
        by_serial = defaultdict(list)
        for row in moves:
            by_serial[row["m"].serial_id].append(row)
        ctx["rows"] = []
        for serial in page:
            history = by_serial.get(serial.pk, [])
            came = next((r for r in history if r["m"].quantity > 0), None)
            left = next((r for r in reversed(history) if r["m"].quantity < 0), None)
            if serial.status == SerialNumber.STATUS_IN_STOCK:
                left = None
            ctx["rows"].append({"serial": serial, "came": came, "left": left})
        ctx["statuses"] = SerialNumber.STATUS_CHOICES
        product = self.request.GET.get("product", "")
        if product.isdigit():
            from apps.catalog.models import Product

            ctx["product_filter"] = Product.objects.filter(pk=product).first()
        return ctx


class SerialDetailView(RoleRequiredMixin, DetailView):
    """История одного серийного номера по документам."""

    model = SerialNumber
    template_name = "reports/serial_detail.html"
    context_object_name = "serial"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["history"] = describe(list(
            self.object.movements.select_related("serial", "warehouse").order_by("date", "id")
        ))
        return ctx
