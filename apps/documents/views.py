import base64
import mimetypes
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string

from apps.core.constants import VAT_NONE, VAT_RATES, vat_amount
from apps.sales.models import Invoice, Shipment

from .forms import InvoiceEmailForm
from .money import amount_to_words
from .pdf import render_pdf
from .qr import invoice_qr_data_uri


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


def _lines_payload(lines, only_products=False, only_services=False):
    """Готовит строки таблицы: сумма, НДС по строке, флаг наличия НДС.

    only_products=True — только товары (ТОРГ-12).
    only_services=True — только услуги (Акт).
    """
    rows = []
    total = Decimal("0")
    vat_total = Decimal("0")
    has_vat = False
    n = 0
    for line in lines:
        if only_products and line.product.is_service:
            continue
        if only_services and not line.product.is_service:
            continue
        n += 1
        line_total = line.total
        line_vat = vat_amount(line_total, line.vat_rate)
        if line.vat_rate != VAT_NONE and VAT_RATES.get(line.vat_rate):
            has_vat = True
        total += line_total
        vat_total += line_vat
        rows.append({
            "n": n,
            "product": line.product,
            "quantity": line.quantity,
            "unit": line.product.unit,
            "price": line.price,
            "vat_rate": line.get_vat_rate_display(),
            "vat_amount": line_vat,
            "total": line_total,
            "total_without_vat": line_total - line_vat,
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

def _invoice_payment_context(invoice, with_qr=False):
    """Контекст для печатной формы счёта на оплату (+ QR при with_qr)."""
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
    if with_qr:
        # QR по ГОСТ Р 56042-2014: банк подставит реквизиты при сканировании.
        context["payment_qr"] = invoice_qr_data_uri(invoice.organization, invoice, total)
    return context, total


def _invoice_pdf(request, invoice, with_qr=False):
    """Готовый (pdf_bytes, filename) счёта — для скачивания и для вложения в письмо."""
    context, _ = _invoice_payment_context(invoice, with_qr)
    html = render_to_string("documents/print/invoice_payment.html", context, request=request)
    pdf, _engine = render_pdf(html, base_url=request.build_absolute_uri("/"))
    filename = f"Счёт-QR_{invoice.number}.pdf" if with_qr else f"Счёт_{invoice.number}.pdf"
    return pdf, filename


@login_required
def invoice_payment(request, pk, with_qr=False):
    invoice = get_object_or_404(Invoice.objects.select_related("organization", "customer"), pk=pk)
    context, _ = _invoice_payment_context(invoice, with_qr)
    filename = f"Счёт-QR_{invoice.number}.pdf" if with_qr else f"Счёт_{invoice.number}.pdf"
    return _respond(request, "documents/print/invoice_payment.html", context, filename)


# ---------- Отправка счёта на email ----------

def _email_defaults(invoice, with_qr):
    """Тема и текст письма по умолчанию (редактируются в окне отправки)."""
    org = invoice.organization
    date = invoice.date.strftime("%d.%m.%Y")
    subject = f"Счёт на оплату № {invoice.number} от {date}"
    kind = "с QR-кодом для оплаты " if with_qr else ""
    message = (
        "Здравствуйте!\n\n"
        f"Направляем счёт на оплату № {invoice.number} от {date}. "
        f"Документ {kind}во вложении.\n\n"
        "С уважением,\n"
        f"{org.full_name or org.name}"
    )
    return {"to_email": invoice.customer.email, "subject": subject, "message": message}


@login_required
def invoice_send_email(request, pk, with_qr=False):
    invoice = get_object_or_404(Invoice.objects.select_related("organization", "customer"), pk=pk)

    if request.method == "POST":
        form = InvoiceEmailForm(request.POST)
        if form.is_valid():
            pdf, filename = _invoice_pdf(request, invoice, with_qr)
            email = EmailMessage(
                subject=form.cleaned_data["subject"],
                body=form.cleaned_data["message"],
                to=form.cleaned_data["to_email"],
            )
            email.attach(filename, pdf, "application/pdf")
            try:
                email.send()
            except Exception as exc:  # noqa: BLE001 — любая ошибка SMTP → сообщение пользователю
                messages.error(request, f"Не удалось отправить письмо: {exc}")
            else:
                recipients = ", ".join(form.cleaned_data["to_email"])
                messages.success(request, f"Письмо со счётом отправлено: {recipients}")
            return redirect("invoice_edit", pk=pk)
        # Форма невалидна — вернуть окно с ошибками.
        return render(request, "documents/_email_modal.html",
                      {"form": form, "invoice": invoice, "with_qr": with_qr})

    form = InvoiceEmailForm(initial=_email_defaults(invoice, with_qr))
    return render(request, "documents/_email_modal.html",
                  {"form": form, "invoice": invoice, "with_qr": with_qr})


# ---------- Формы из отгрузки: ТОРГ-12, Акт, УПД ----------

def _shipment_context(request, shipment, only_products=False, only_services=False):
    qs = shipment.lines.select_related("product", "product__unit")
    rows, total, vat_total, has_vat = _lines_payload(qs, only_products=only_products, only_services=only_services)
    context = _base_context(shipment, shipment.organization, shipment.customer)
    context.update({
        "doc": shipment,
        "rows": rows,
        "total": total,
        "total_without_vat": total - vat_total,
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
    context = _shipment_context(request, shipment, only_products=True)
    return _respond(request, "documents/print/torg12.html", context, f"ТОРГ-12_{shipment.number}.pdf")


@login_required
def shipment_act(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("organization", "customer"), pk=pk)
    context = _shipment_context(request, shipment, only_services=True)
    return _respond(request, "documents/print/act.html", context, f"Акт_{shipment.number}.pdf")


@login_required
def shipment_upd(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("organization", "customer"), pk=pk)
    context = _shipment_context(request, shipment)
    return _respond(request, "documents/print/upd.html", context, f"УПД_{shipment.number}.pdf")
