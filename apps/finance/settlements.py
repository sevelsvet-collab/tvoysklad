"""Взаиморасчёты с контрагентами — единый расчёт для отчёта «Взаиморасчёты»
и сводки в карточке контрагента. Только чтение: ничего не записывает.

Строка расчёта — пара «наша организация + контрагент». Документы разных
организаций друг друга не гасят. Задолженности — по проведённым документам:

    по продажам (покупатель должен нам) = отгрузки − возвраты покупателя
        − «оплата от покупателя» + «возврат покупателю»
        ± корректировки взаиморасчётов стороны «продажи»
    по закупкам (мы должны поставщику) = приёмки − возвраты поставщику
        − «оплата поставщику» + «возврат от поставщика»
        ± корректировки взаиморасчётов стороны «закупки»

Выставленный счёт задолженность не создаёт: «выставлено», «оплачено по
счёту» и «отгружено по счёту» — справочные показатели счетов. Платежи «не
распределено» и корректировки без стороны в стороны не входят и
показываются отдельно — списком документов, даже если их суммы взаимно
гасятся; пока такие документы есть, задолженности сторон предварительные.
Справочная разница по документам (от назначения платежей не зависит):

    разница = по продажам − по закупкам − не распределено получено
              + не распределено уплачено + корректировки без стороны

Взаимозачёт не выполняется: стороны показываются отдельно, итоги
складываются без сведения противоположных задолженностей (ни между
сторонами, ни между организациями и контрагентами). Корректировки остатков
касс в расчёты с контрагентами не входят.

Независимая модель этих правил — docs/proposals/settlements_model.py.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.urls import reverse
from django.utils.formats import number_format

from apps.core.constants import DOC_POSTED

ZERO = Decimal("0")


def money(value):
    """1 234,50 — для пояснений."""
    return number_format(value, 2, use_l10n=True, force_grouping=True)


@dataclass
class Pair:
    """Расчёты одной пары «наша организация + контрагент»."""

    organization: object
    counterparty: object
    # продажи
    shipped: Decimal = ZERO
    customer_returned: Decimal = ZERO
    customer_paid: Decimal = ZERO
    customer_refunded: Decimal = ZERO
    sales_corrections: Decimal = ZERO          # «+» — покупатель должен больше
    # закупки
    received: Decimal = ZERO
    supplier_returned: Decimal = ZERO
    supplier_paid: Decimal = ZERO
    supplier_refunded: Decimal = ZERO
    purchase_corrections: Decimal = ZERO       # «+» — мы должны больше
    # не распределено
    unallocated_in: Decimal = ZERO
    unallocated_out: Decimal = ZERO
    unsided_they_owe: Decimal = ZERO
    unsided_we_owe: Decimal = ZERO
    unallocated_docs: list = field(default_factory=list)
    # справочно: счета покупателю (задолженность не создают)
    invoiced: Decimal = ZERO
    invoice_paid: Decimal = ZERO
    invoice_shipped: Decimal = ZERO
    # есть ли товарные документы сторон (для подсказок диагностики)
    sales_goods: int = 0
    purchase_goods: int = 0

    @property
    def sales(self):
        """По продажам: > 0 — покупатель должен нам; < 0 — мы должны ему (аванс, переплата)."""
        return (self.shipped - self.customer_returned - self.customer_paid + self.customer_refunded
                + self.sales_corrections)

    @property
    def purchases(self):
        """По закупкам: > 0 — мы должны поставщику; < 0 — он должен нам (аванс, переплата)."""
        return (self.received - self.supplier_returned - self.supplier_paid + self.supplier_refunded
                + self.purchase_corrections)

    @property
    def unsided(self):
        return self.unsided_they_owe - self.unsided_we_owe

    @property
    def difference(self):
        """Справочная разница по документам: > 0 — в нашу пользу."""
        return self.sales - self.purchases - self.unallocated_in + self.unallocated_out + self.unsided

    @property
    def final(self):
        """Окончательно, только если нет ни одного нераспределённого документа
        (а не «их сумма нулевая»)."""
        return not self.unallocated_docs

    @property
    def unallocated_payments(self):
        return [doc for doc in self.unallocated_docs if doc["kind"] == "payment"]

    @property
    def unsided_corrections(self):
        return [doc for doc in self.unallocated_docs if doc["kind"] == "correction"]

    @property
    def closed(self):
        """Расчёты закрыты: окончательно и обе стороны по нулям."""
        return self.final and not self.sales and not self.purchases

    @property
    def status(self):
        if not self.final:
            return "Предварительно"
        return "Задолженностей нет" if self.closed else "Окончательно"

    @property
    def sales_amount(self):
        """Сумма без знака — смысл знака показывает sales_label."""
        return abs(self.sales)

    @property
    def purchases_amount(self):
        return abs(self.purchases)

    @property
    def sales_label(self):
        if self.sales > 0:
            return "долг покупателя"
        return "наш долг покупателю: аванс, переплата" if self.sales < 0 else ""

    @property
    def purchases_label(self):
        if self.purchases > 0:
            return "наш долг поставщику"
        return "долг поставщика нам: аванс, переплата" if self.purchases < 0 else ""

    @property
    def explanations(self):
        notes = []
        if self.sales > 0:
            notes.append(f"По продажам покупатель должен нам {money(self.sales)}.")
        elif self.sales < 0:
            notes.append(f"По продажам мы должны покупателю {money(-self.sales)}: оплачено или возвращено "
                         "больше, чем отгружено (аванс, переплата или не возвращённые за товар деньги), — "
                         "за нами отгрузка или возврат денег.")
        if self.purchases > 0:
            notes.append(f"По закупкам мы должны поставщику {money(self.purchases)}.")
        elif self.purchases < 0:
            notes.append(f"По закупкам поставщик должен нам {money(-self.purchases)}: уплачено или возвращено "
                         "больше, чем принято (аванс, переплата или не возвращённые за товар деньги), — "
                         "за ним поставка или возврат денег.")
        if self.sales and self.purchases:
            notes.append("Требования по продажам и закупкам встречные и показаны отдельно: "
                         "взаимозачёт автоматически не выполняется.")
        payments, corrections = self.unallocated_payments, self.unsided_corrections
        if payments:
            notes.append(f"Не распределено платежей: {len(payments)} (получено {money(self.unallocated_in)}, "
                         f"уплачено {money(self.unallocated_out)}) — уточните «Назначение для взаиморасчётов».")
        if corrections:
            notes.append(f"Корректировок без стороны: {len(corrections)} («контрагент должен» "
                         f"{money(self.unsided_they_owe)}, «мы должны» {money(self.unsided_we_owe)}) — "
                         "укажите сторону.")
        if not self.final:
            notes.append("Пока есть нераспределённые документы, задолженности по продажам и закупкам "
                         "предварительные; разница по документам — справочно.")
        if self.difference == 0 and not self.closed:
            notes.append("Справочная разница равна нулю, но расчёты не закрыты: это не означает, что всё оплачено.")
        if self.invoiced:
            notes.append(f"Справочно, счета покупателю: выставлено {money(self.invoiced)}, оплачено по счетам "
                         f"{money(self.invoice_paid)}, отгружено по счетам {money(self.invoice_shipped)}. "
                         "Счёт сам по себе задолженность не создаёт.")
        if self.closed:
            notes.append("Задолженностей нет.")
        return notes


def _doc(kind, type_label, document, effect, amount, url_name):
    return {
        "kind": kind, "type_label": type_label, "number": document.number, "date": document.date,
        "amount": amount, "effect": effect, "url": reverse(url_name, args=[document.pk]),
        "pk": document.pk,
    }


def settlement_pairs(counterparty=None):
    """Пары «организация + контрагент» по проведённым документам (у
    контрагента counterparty — только его). Порядок: организация, контрагент."""
    from apps.purchases.models import Receipt, SupplierReturn
    from apps.sales.models import CustomerReturn, Invoice, Shipment

    from .models import Payment, SettlementCorrection

    pairs = {}

    def pair(document, party):
        key = (document.organization_id, party.pk)
        if key not in pairs:
            pairs[key] = Pair(organization=document.organization, counterparty=party)
        return pairs[key]

    def posted(model, party_field):
        rows = model.objects.filter(status=DOC_POSTED)
        if counterparty is not None:
            rows = rows.filter(**{party_field: counterparty})
        return rows.select_related("organization", party_field)

    shipped_by_invoice = {}
    for doc in posted(Shipment, "customer").prefetch_related("lines"):
        target, total = pair(doc, doc.customer), doc.total
        target.shipped += total
        target.sales_goods += 1
        if doc.invoice_id:
            shipped_by_invoice[doc.invoice_id] = shipped_by_invoice.get(doc.invoice_id, ZERO) + total
    for doc in posted(CustomerReturn, "customer").prefetch_related("lines"):
        target = pair(doc, doc.customer)
        target.customer_returned += doc.total
        target.sales_goods += 1
    for doc in posted(Receipt, "supplier").prefetch_related("lines"):
        target = pair(doc, doc.supplier)
        target.received += doc.total
        target.purchase_goods += 1
    for doc in posted(SupplierReturn, "supplier").prefetch_related("lines"):
        target = pair(doc, doc.supplier)
        target.supplier_returned += doc.total
        target.purchase_goods += 1

    invoices = Invoice.objects.filter(status=Invoice.STATUS_ISSUED)
    if counterparty is not None:
        invoices = invoices.filter(customer=counterparty)
    for doc in invoices.select_related("organization", "customer").prefetch_related("lines"):
        target = pair(doc, doc.customer)
        target.invoiced += doc.total
        target.invoice_paid += doc.paid_amount
        target.invoice_shipped += shipped_by_invoice.get(doc.pk, ZERO)
        target.sales_goods += 1

    labels = dict(Payment.KIND_CHOICES)
    for payment in posted(Payment, "counterparty"):
        target = pair(payment, payment.counterparty)
        purpose = payment.settlement_purpose
        if purpose not in Payment.PURPOSES_BY_KIND.get(payment.kind, ()):
            purpose = Payment.PURPOSE_UNALLOCATED            # в базе не бывает (ограничение); на всякий случай
        if purpose == Payment.PURPOSE_CUSTOMER_PAYMENT:
            target.customer_paid += payment.amount
        elif purpose == Payment.PURPOSE_CUSTOMER_REFUND:
            target.customer_refunded += payment.amount
        elif purpose == Payment.PURPOSE_SUPPLIER_PAYMENT:
            target.supplier_paid += payment.amount
        elif purpose == Payment.PURPOSE_SUPPLIER_REFUND:
            target.supplier_refunded += payment.amount
        else:
            incoming = payment.kind == Payment.KIND_IN
            if incoming:
                target.unallocated_in += payment.amount
            else:
                target.unallocated_out += payment.amount
            target.unallocated_docs.append(_doc(
                "payment", labels.get(payment.kind, "Платёж"), payment,
                "получено" if incoming else "уплачено", payment.amount, "payment_edit"))

    for correction in posted(SettlementCorrection, "counterparty"):
        target = pair(correction, correction.counterparty)
        side, change = correction.settlement_effect
        if side == SettlementCorrection.SIDE_SALES:
            target.sales_corrections += change
        elif side == SettlementCorrection.SIDE_PURCHASES:
            target.purchase_corrections += change
        else:
            they_owe = correction.direction == SettlementCorrection.DIR_THEY_OWE
            if they_owe:
                target.unsided_they_owe += correction.amount
            else:
                target.unsided_we_owe += correction.amount
            target.unallocated_docs.append(_doc(
                "correction", "Корректировка взаиморасчётов", correction,
                "контрагент должен нам" if they_owe else "мы должны контрагенту", correction.amount,
                "settlement_correction_edit"))

    for target in pairs.values():
        target.unallocated_docs.sort(key=lambda doc: (doc["date"], doc["kind"], doc["pk"]))
    return sorted(pairs.values(), key=lambda p: (p.organization.name, p.counterparty.name, p.organization.pk,
                                                 p.counterparty.pk))


def totals(pairs):
    """Итоги без взаимозачёта: долги и авансы каждой стороны складываются
    отдельно, противоположные суммы разных пар друг друга не гасят."""
    result = {
        "customers_owe": ZERO, "customer_advances": ZERO, "we_owe_suppliers": ZERO, "supplier_advances": ZERO,
        "unallocated_in": ZERO, "unallocated_out": ZERO, "unsided_they_owe": ZERO, "unsided_we_owe": ZERO,
        "unallocated_count": 0, "preliminary_pairs": 0, "pairs": len(pairs),
    }
    for item in pairs:
        result["customers_owe"] += max(item.sales, ZERO)
        result["customer_advances"] += max(-item.sales, ZERO)
        result["we_owe_suppliers"] += max(item.purchases, ZERO)
        result["supplier_advances"] += max(-item.purchases, ZERO)
        result["unallocated_in"] += item.unallocated_in
        result["unallocated_out"] += item.unallocated_out
        result["unsided_they_owe"] += item.unsided_they_owe
        result["unsided_we_owe"] += item.unsided_we_owe
        result["unallocated_count"] += len(item.unallocated_docs)
        result["preliminary_pairs"] += 0 if item.final else 1
    return result


def purpose_hint(item, payment_kind):
    """Подсказка для ручного уточнения — только подсказка, назначение не
    ставится. По товарным документам пары (и счетам покупателю)."""
    from .models import Payment

    incoming = payment_kind == Payment.KIND_IN
    if item.sales_goods and not item.purchase_goods:
        return "у пары только продажи — вероятно, «Оплата от покупателя»" if incoming \
            else "у пары только продажи — вероятно, «Возврат покупателю»"
    if item.purchase_goods and not item.sales_goods:
        return "у пары только закупки — вероятно, «Возврат от поставщика»" if incoming \
            else "у пары только закупки — вероятно, «Оплата поставщику»"
    if item.sales_goods and item.purchase_goods:
        return "у пары есть и продажи, и закупки — уточните по выписке или договору"
    return "у пары нет отгрузок, приёмок и счетов — уточните по выписке (аванс?)"


# ---------------------------------------------------------------------------
# Прежний отчёт (до 18.10.2026) — только для диагностики перехода
# ---------------------------------------------------------------------------

def previous_report():
    """Строки прежнего отчёта «Взаиморасчёты» по контрагентам (все
    организации вместе): выставленные счета − возвраты покупателя − все
    входящие платежи; приёмки − возвраты поставщику − все исходящие;
    сальдо = разность ± корректировки. Сохранён без изменений, чтобы
    показать «было / стало»; в программе больше не используется."""
    from django.db.models import Q, Sum

    from apps.partners.models import Counterparty
    from apps.purchases.models import Receipt, SupplierReturn
    from apps.sales.models import CustomerReturn, Invoice

    from .models import Payment, SettlementCorrection

    def total(rows):
        return sum((doc.total for doc in rows.prefetch_related("lines")), ZERO)

    result = []
    for cp in Counterparty.objects.order_by("name", "pk"):
        sales = total(Invoice.objects.filter(customer=cp, status=Invoice.STATUS_ISSUED))
        purch = total(Receipt.objects.filter(supplier=cp, status=DOC_POSTED))
        paid = Payment.objects.filter(counterparty=cp, status=DOC_POSTED).aggregate(
            paid_in=Sum("amount", filter=Q(kind=Payment.KIND_IN)),
            paid_out=Sum("amount", filter=Q(kind=Payment.KIND_OUT)))
        paid_in, paid_out = paid["paid_in"] or ZERO, paid["paid_out"] or ZERO
        cust_return = total(CustomerReturn.objects.filter(customer=cp, status=DOC_POSTED))
        supp_return = total(SupplierReturn.objects.filter(supplier=cp, status=DOC_POSTED))
        correction = sum((c.signed_amount for c in SettlementCorrection.objects.filter(
            counterparty=cp, status=DOC_POSTED)), ZERO)
        they_owe = sales - cust_return - paid_in
        we_owe = purch - supp_return - paid_out
        if sales or paid_in or purch or paid_out or cust_return or supp_return or correction:
            result.append({
                "cp": cp, "sales": sales, "paid_in": paid_in, "purch": purch, "paid_out": paid_out,
                "cust_return": cust_return, "supp_return": supp_return, "correction": correction,
                "they_owe": they_owe, "we_owe": we_owe, "balance": they_owe - we_owe + correction,
            })
    return result
