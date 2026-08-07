from decimal import Decimal

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.core import roles
from apps.core.constants import DOC_POSTED
from apps.core.document_edit import LineDocumentMixin
from apps.core.permissions import RoleRequiredMixin

from .forms import (
    CustomerReturnForm,
    CustomerReturnLineFormSet,
    InvoiceForm,
    InvoiceLineFormSet,
    ShipmentForm,
    ShipmentLineFormSet,
)
from .models import CustomerReturn, CustomerReturnLine, Invoice, Shipment, ShipmentLine

SALES_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER]
SHIP_ROLES = [roles.ROLE_ADMIN, roles.ROLE_MANAGER, roles.ROLE_STOREKEEPER]


# ---------- Счета покупателям ----------

class InvoiceListView(RoleRequiredMixin, ListView):
    model = Invoice
    template_name = "sales/invoice_list.html"
    context_object_name = "invoices"

    def get_paginate_by(self, qs):
        try:
            return int(self.request.GET.get("per_page", 50))
        except (ValueError, TypeError):
            return 50

    def get_queryset(self):
        qs = Invoice.objects.select_related("customer", "warehouse", "organization").prefetch_related("lines", "shipments__lines")
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        _zero = Decimal("0")
        for inv in ctx["invoices"]:
            total = sum((line.total for line in inv.lines.all()), _zero)
            shipped = sum(
                sum((line.total for line in s.lines.all()), _zero)
                for s in inv.shipments.all() if s.status == "posted"
            )
            inv.row_total = total
            inv.row_shipped = shipped
            inv.row_unpaid = max(_zero, total - inv.paid_amount)
            if total > 0:
                inv.row_paid_pct = int(min(100, float(inv.paid_amount / total * 100)))
                inv.row_shipped_pct = int(min(100, float(shipped / total * 100)))
            else:
                inv.row_paid_pct = inv.row_shipped_pct = 0
        ctx["perpage_choices"] = [25, 50, 100]
        try:
            ctx["current_per_page"] = int(self.request.GET.get("per_page", 50))
        except (ValueError, TypeError):
            ctx["current_per_page"] = 50
        return ctx


class InvoiceCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = SALES_ROLES
    model = Invoice
    form_class = InvoiceForm
    formset_class = InvoiceLineFormSet
    template_name = "sales/invoice_form.html"

    def get_success_url(self):
        return reverse("invoice_list")

    def get_edit_url(self):
        return reverse("invoice_edit", args=[self.object.pk])


class InvoiceUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = SALES_ROLES
    model = Invoice
    form_class = InvoiceForm
    formset_class = InvoiceLineFormSet
    template_name = "sales/invoice_form.html"

    def get_success_url(self):
        return reverse("invoice_list")

    def get_edit_url(self):
        return reverse("invoice_edit", args=[self.object.pk])


@require_POST
def invoice_post(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    invoice.post()
    messages.success(request, f"{invoice} — выставлен")
    return redirect("invoice_edit", pk=pk)


@require_POST
def invoice_unpost(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    invoice.unpost()
    messages.info(request, f"{invoice} — возвращён в черновики")
    return redirect("invoice_edit", pk=pk)


@require_POST
def invoice_bulk_delete(request):
    ids = request.POST.getlist("ids")
    if ids:
        deleted, _ = Invoice.objects.filter(pk__in=ids).delete()
        messages.success(request, f"Удалено счетов: {deleted}")
    else:
        messages.warning(request, "Не выбрано ни одного счёта")
    return redirect("invoice_list")


@require_POST
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    invoice.delete()
    messages.info(request, "Счёт удалён")
    return redirect("invoice_list")


@require_POST
def invoice_to_shipment(request, pk):
    """Создать отгрузку на основании счёта (копирует шапку и строки)."""
    invoice = get_object_or_404(Invoice, pk=pk)
    shipment = Shipment.objects.create(
        organization=invoice.organization, warehouse=invoice.warehouse,
        customer=invoice.customer, invoice=invoice,
    )
    for line in invoice.lines.all():
        ShipmentLine.objects.create(
            shipment=shipment, product=line.product, quantity=line.quantity,
            price=line.price, vat_rate=line.vat_rate,
        )
    messages.success(request, f"Создана отгрузка № {shipment.number} — проверьте и проведите")
    return redirect("shipment_edit", pk=shipment.pk)


# ---------- Отгрузки ----------

class ShipmentListView(RoleRequiredMixin, ListView):
    model = Shipment
    template_name = "sales/shipment_list.html"
    context_object_name = "shipments"
    paginate_by = 50

    def get_queryset(self):
        qs = Shipment.objects.select_related("customer", "warehouse", "organization").prefetch_related("lines")
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs


class ShipmentCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = SHIP_ROLES
    model = Shipment
    form_class = ShipmentForm
    formset_class = ShipmentLineFormSet
    template_name = "sales/shipment_form.html"

    def get_success_url(self):
        return reverse("shipment_list")

    def get_edit_url(self):
        return reverse("shipment_edit", args=[self.object.pk])


class ShipmentUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = SHIP_ROLES
    model = Shipment
    form_class = ShipmentForm
    formset_class = ShipmentLineFormSet
    template_name = "sales/shipment_form.html"

    def get_success_url(self):
        return reverse("shipment_list")

    def get_edit_url(self):
        return reverse("shipment_edit", args=[self.object.pk])


@require_POST
def shipment_post(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    try:
        shipment.post()
        messages.success(request, f"{shipment} — проведена")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("shipment_edit", pk=pk)


@require_POST
def shipment_unpost(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    shipment.unpost()
    messages.info(request, f"{shipment} — снята с проведения")
    return redirect("shipment_edit", pk=pk)


@require_POST
def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    if shipment.status == DOC_POSTED:
        shipment.unpost()
    shipment.delete()
    messages.info(request, "Отгрузка удалена")
    return redirect("shipment_list")


# ---------- Возвраты покупателей ----------

class CustomerReturnListView(RoleRequiredMixin, ListView):
    model = CustomerReturn
    template_name = "sales/customer_return_list.html"
    context_object_name = "returns"
    paginate_by = 50

    def get_queryset(self):
        qs = CustomerReturn.objects.select_related("customer", "warehouse", "organization").prefetch_related("lines")
        status = self.request.GET.get("status", "")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs


class CustomerReturnCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = SHIP_ROLES
    model = CustomerReturn
    form_class = CustomerReturnForm
    formset_class = CustomerReturnLineFormSet
    template_name = "sales/customer_return_form.html"

    def get_success_url(self):
        return reverse("customer_return_list")

    def get_edit_url(self):
        return reverse("customer_return_edit", args=[self.object.pk])


class CustomerReturnUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = SHIP_ROLES
    model = CustomerReturn
    form_class = CustomerReturnForm
    formset_class = CustomerReturnLineFormSet
    template_name = "sales/customer_return_form.html"

    def get_success_url(self):
        return reverse("customer_return_list")

    def get_edit_url(self):
        return reverse("customer_return_edit", args=[self.object.pk])


@require_POST
def customer_return_post(request, pk):
    doc = get_object_or_404(CustomerReturn, pk=pk)
    try:
        doc.post()
        messages.success(request, f"{doc} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("customer_return_edit", pk=pk)


@require_POST
def customer_return_unpost(request, pk):
    doc = get_object_or_404(CustomerReturn, pk=pk)
    doc.unpost()
    messages.info(request, f"{doc} — снят с проведения")
    return redirect("customer_return_edit", pk=pk)


@require_POST
def customer_return_delete(request, pk):
    doc = get_object_or_404(CustomerReturn, pk=pk)
    if doc.status == DOC_POSTED:
        doc.unpost()
    doc.delete()
    messages.info(request, "Возврат покупателя удалён")
    return redirect("customer_return_list")


@require_POST
def shipment_to_return(request, pk):
    """Создать возврат покупателя на основании отгрузки (копирует шапку и строки)."""
    shipment = get_object_or_404(Shipment, pk=pk)
    doc = CustomerReturn.objects.create(
        organization=shipment.organization, warehouse=shipment.warehouse,
        customer=shipment.customer, shipment=shipment,
    )
    for line in shipment.lines.all():
        CustomerReturnLine.objects.create(
            document=doc, product=line.product, quantity=line.quantity,
            price=line.price, vat_rate=line.vat_rate,
        )
    messages.success(request, f"Создан возврат покупателя № {doc.number} — проверьте и проведите")
    return redirect("customer_return_edit", pk=doc.pk)
