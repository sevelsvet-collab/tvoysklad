from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.core import roles
from apps.core.constants import DOC_POSTED
from apps.core.document_edit import LineDocumentMixin
from apps.core.permissions import RoleRequiredMixin
from apps.partners.models import Counterparty

from .forms import (
    ReceiptForm,
    ReceiptLineFormSet,
    SupplierReturnForm,
    SupplierReturnLineFormSet,
)
from .models import Receipt, SupplierReturn, SupplierReturnLine

EDIT_ROLES = [roles.ROLE_ADMIN, roles.ROLE_STOREKEEPER, roles.ROLE_MANAGER]


class ReceiptListView(RoleRequiredMixin, ListView):
    model = Receipt
    template_name = "purchases/receipt_list.html"
    context_object_name = "receipts"
    paginate_by = 50

    def get_queryset(self):
        qs = Receipt.objects.select_related("supplier", "warehouse", "organization")
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(supplier__name__icontains=q) | Q(supplier_invoice__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs


class ReceiptCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = EDIT_ROLES
    model = Receipt
    form_class = ReceiptForm
    formset_class = ReceiptLineFormSet
    template_name = "purchases/receipt_form.html"

    def get_success_url(self):
        return reverse("receipt_list")

    def get_edit_url(self):
        return reverse("receipt_edit", args=[self.object.pk])


class ReceiptUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = EDIT_ROLES
    model = Receipt
    form_class = ReceiptForm
    formset_class = ReceiptLineFormSet
    template_name = "purchases/receipt_form.html"

    def get_success_url(self):
        return reverse("receipt_list")

    def get_edit_url(self):
        return reverse("receipt_edit", args=[self.object.pk])


@require_POST
def receipt_post(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    try:
        receipt.post()
        messages.success(request, f"{receipt} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("receipt_edit", pk=pk)


@require_POST
def receipt_unpost(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    receipt.unpost()
    messages.info(request, f"{receipt} — снят с проведения")
    return redirect("receipt_edit", pk=pk)


@require_POST
def receipt_delete(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    if receipt.status == DOC_POSTED:
        receipt.unpost()
    receipt.delete()
    messages.info(request, "Приёмка удалена")
    return redirect("receipt_list")


# ---------- Возвраты поставщикам ----------

class SupplierReturnListView(RoleRequiredMixin, ListView):
    model = SupplierReturn
    template_name = "purchases/supplier_return_list.html"
    context_object_name = "returns"
    paginate_by = 50

    def get_queryset(self):
        qs = SupplierReturn.objects.select_related("supplier", "warehouse", "organization").prefetch_related("lines")
        status = self.request.GET.get("status", "")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(supplier__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        return qs


class SupplierReturnCreateView(RoleRequiredMixin, LineDocumentMixin, CreateView):
    allowed_roles = EDIT_ROLES
    model = SupplierReturn
    form_class = SupplierReturnForm
    formset_class = SupplierReturnLineFormSet
    template_name = "purchases/supplier_return_form.html"

    def get_success_url(self):
        return reverse("supplier_return_list")

    def get_edit_url(self):
        return reverse("supplier_return_edit", args=[self.object.pk])


class SupplierReturnUpdateView(RoleRequiredMixin, LineDocumentMixin, UpdateView):
    allowed_roles = EDIT_ROLES
    model = SupplierReturn
    form_class = SupplierReturnForm
    formset_class = SupplierReturnLineFormSet
    template_name = "purchases/supplier_return_form.html"

    def get_success_url(self):
        return reverse("supplier_return_list")

    def get_edit_url(self):
        return reverse("supplier_return_edit", args=[self.object.pk])


@require_POST
def supplier_return_post(request, pk):
    doc = get_object_or_404(SupplierReturn, pk=pk)
    try:
        doc.post()
        messages.success(request, f"{doc} — проведён")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Не удалось провести: {exc}")
    return redirect("supplier_return_edit", pk=pk)


@require_POST
def supplier_return_unpost(request, pk):
    doc = get_object_or_404(SupplierReturn, pk=pk)
    doc.unpost()
    messages.info(request, f"{doc} — снят с проведения")
    return redirect("supplier_return_edit", pk=pk)


@require_POST
def supplier_return_delete(request, pk):
    doc = get_object_or_404(SupplierReturn, pk=pk)
    if doc.status == DOC_POSTED:
        doc.unpost()
    doc.delete()
    messages.info(request, "Возврат поставщику удалён")
    return redirect("supplier_return_list")


@require_POST
def receipt_to_return(request, pk):
    """Создать возврат поставщику на основании приёмки (копирует шапку и строки)."""
    receipt = get_object_or_404(Receipt, pk=pk)
    doc = SupplierReturn.objects.create(
        organization=receipt.organization, warehouse=receipt.warehouse,
        supplier=receipt.supplier, receipt=receipt,
    )
    for line in receipt.lines.all():
        SupplierReturnLine.objects.create(
            document=doc, product=line.product, quantity=line.quantity,
            price=line.price, vat_rate=line.vat_rate,
        )
    messages.success(request, f"Создан возврат поставщику № {doc.number} — проверьте и проведите")
    return redirect("supplier_return_edit", pk=doc.pk)
