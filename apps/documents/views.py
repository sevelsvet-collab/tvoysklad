import base64
import mimetypes
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string

from apps.core.constants import VAT_NONE, VAT_RATES, vat_amount
from apps.sales.models import Invoice, Shipment

from .money import amount_to_words
from .pdf import render_pdf


def _image_data_uri(field):
    """Картинка ImageField → data:URI (встраивается в HTML, работает в обоих PDF-движках)."""
    if not field:
        return ""
    try:
        with field.open("rb") as fh:
            data = fh.read()
    except (FileNotFoundError, ValueError):
        return ""
    mime = mimetypes.guess_type(field.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _lines_payload(lines):
    """Готовит строки таблицы: сумма, НДС по строке, флаг наличия НДС."""
    rows = []
    total = Decimal("0")
    vat_total = Decimal("0")
    has_vat = False
    for i, line in enumerate(lines, start=1):
        line_total = line.total
        line_vat = vat_amount(line_total, line.vat_rate)
        if line.vat_rate != VAT_NONE and VAT_RATES.get(line.vat_rate):
            has_vat = True
        total += line_total
        vat_total += line_vat
        rows.append({
            "n": i,
            "product": line.product,
            "quantity": line.quantity,
            "unit": line.product.unit,
            "price": line.price,
            "vat_rate": line.get_vat_rate_display(),
            "vat_amount": line_vat,
            "total": line_total,
        })
    return rows, total, vat_total, has_vat


def _base_context(document, organization, counterparty):
    return {
        "org": organization,
        "counterparty": counterparty,
        "org_signature": _image_data_uri(organization.signature),
        "org_stamp": _image_data_uri(organization.stamp),
        "org_logo": _image_data_uri(organization.logo),
    }


def _respond(request, template, context, filename):
    """fmt=html → предпросмотр в браузере; иначе PDF-файл."""
    html = render_to_string(template, context, request=request)
    if request.GET.get("fmt") == "html":
        return HttpResponse(html)
    pdf, engine = render_pdf(html, base_url=request.build_absolute_uri("/"))
    response = HttpResponse(pdf, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


# ---------- Счёт на оплату (из счёта покупателю) ----------

@login_required
def invoice_payment(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("organization", "customer"), pk=pk)
    rows, total, vat_total, has_vat = _lines_payload(invoice.lines.select_related("product", "product__unit"))
    context = _base_context(invoice, invoice.organization, invoice.customer)
    context.update({
        "doc": invoice,
        "rows": rows,
        "total": total,
        "vat_total": vat_total,
        "has_vat": has_vat,
        "total_words": amount_to_words(total),
        "count": len(rows),
    })
    return _respond(request, "documents/print/invoice_payment.html", context, f"Счёт_{invoice.number}.pdf")


# ---------- Формы из отгрузки: ТОРГ-12, Акт, УПД ----------

def _shipment_context(request, shipment):
    rows, total, vat_total, has_vat = _lines_payload(shipment.lines.select_related("product", "product__unit"))
    context = _base_context(shipment, shipment.organization, shipment.customer)
    context.update({
        "doc": shipment,
        "rows": rows,
        "total": total,
        "vat_total": vat_total,
        "has_vat": has_vat,
        "total_words": amount_to_words(total),
        "vat_words": amount_to_words(vat_total),
        "count": len(rows),
    })
    return context


@login_required
def shipment_torg12(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("organization", "customer"), pk=pk)
    context = _shipment_context(request, shipment)
    return _respond(request, "documents/print/torg12.html", context, f"ТОРГ-12_{shipment.number}.pdf")


@login_required
def shipment_act(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("organization", "customer"), pk=pk)
    context = _shipment_context(request, shipment)
    return _respond(request, "documents/print/act.html", context, f"Акт_{shipment.number}.pdf")


@login_required
def shipment_upd(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("organization", "customer"), pk=pk)
    context = _shipment_context(request, shipment)
    return _respond(request, "documents/print/upd.html", context, f"УПД_{shipment.number}.pdf")
