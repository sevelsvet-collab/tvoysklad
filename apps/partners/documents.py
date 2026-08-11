"""Сбор всех документов, связанных с контрагентом, для его карточки."""
from datetime import date

from django.urls import reverse


def _doc_status(doc):
    return "Проведён" if getattr(doc, "is_posted", False) else "Черновик"


def counterparty_documents(cp):
    """Единый список документов контрагента (счета, отгрузки, приёмки,
    возвраты, платежи), отсортированный по дате — новые сверху."""
    docs = []

    def add(items, type_label, icon, url_name, status_fn=_doc_status, amount_attr="total"):
        for d in items:
            docs.append({
                "type_label": type_label,
                "icon": icon,
                "number": d.number,
                "date": d.date,
                "amount": getattr(d, amount_attr),
                "status": status_fn(d),
                "is_posted": getattr(d, "is_posted", False),
                "url": reverse(url_name, args=[d.pk]),
            })

    add(cp.invoices.prefetch_related("lines"), "Счёт покупателю", "bi-file-earmark-text",
        "invoice_edit", status_fn=lambda d: "Выставлен" if d.is_posted else "Черновик")
    add(cp.shipments.prefetch_related("lines"), "Отгрузка", "bi-truck", "shipment_edit")
    add(cp.customer_returns.prefetch_related("lines"), "Возврат покупателя",
        "bi-arrow-return-left", "customer_return_edit")
    add(cp.receipts.prefetch_related("lines"), "Приёмка", "bi-box-arrow-in-down", "receipt_edit")
    add(cp.supplier_returns.prefetch_related("lines"), "Возврат поставщику",
        "bi-arrow-return-right", "supplier_return_edit")

    for p in cp.payments.select_related("account"):
        docs.append({
            "type_label": "Входящий платёж" if p.kind == "incoming" else "Исходящий платёж",
            "icon": "bi-cash-coin",
            "number": p.number,
            "date": p.date,
            "amount": p.amount,
            "status": "Проведён" if p.is_posted else "Черновик",
            "is_posted": p.is_posted,
            "url": reverse("payment_edit", args=[p.pk]),
        })

    docs.sort(key=lambda x: (x["date"] or date.min, x["number"]), reverse=True)
    return docs
