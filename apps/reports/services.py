"""Агрегации для отчётов и дашборда.

Отчёт «Продажи и прибыль» (sales_summary, top_products, top_customers,
sales_by_month; подробно — docs/sales-report.md):

* продажи — проведённые отгрузки, по дате отгрузки; возвраты — проведённые
  возвраты покупателей, по дате возврата (в месяц продажи не переносятся).
  Черновики и снятые с проведения не учитываются;
* выручка продаж и возвратов — итоги строк документов по тем же правилам,
  что в самих документах (line_total: цена × количество со скидкой,
  округление строки до копейки), поэтому отчёт сходится с их итогами;
* себестоимость продажи — записанная сумма строки (ShipmentLine.cost_amount),
  себестоимость возврата — точная сумма, восстановленная складским движением
  возврата (StockMovement.amount). Если сумма ещё не записана (данные до
  обновления, пересчёт не выполнялся), берётся прежняя оценка «количество ×
  цена единицы» и отчёт помечает, что нужен пересчёт; возврат без складского
  движения помечается отдельно — его себестоимость не известна;
* результат после возвратов: выручка = продажи − возвраты, себестоимость =
  продажи − возвраты, прибыль = выручка − себестоимость. Это не движение
  денег: платежи и возврат денег покупателю сюда не входят;
* предварительная себестоимость: единицы, проданные раньше поступления и
  ещё не покрытые (ShipmentLine.cost_open_qty), а также непокрытые единицы,
  которые погасил возврат в другом периоде (их себестоимость так и не
  определилась, costing: Plan.cancels). Если и продажа, и такой возврат в
  периоде — они взаимно исключаются и пометки нет.

Все показатели строятся из одних и тех же записей (_entries), поэтому итог
равен сумме по товарам, по покупателям и по месяцам. Фильтр organization —
продажи и возвраты одной организации (по организации документа); без него —
все организации: себестоимость каждой строки посчитана по своей организации
(учёт «организация + склад + товар»), итог — их сумма, общая средняя не
используется. Чтение отчёта ничего
не записывает. Закрытия периодов нет: поступление уточняет себестоимость
продажи задним числом — в периоде продажи.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.catalog.models import Product
from apps.core.constants import DOC_POSTED, line_total
from apps.finance.models import Payment
from apps.inventory import costing
from apps.inventory.models import StockMovement
from apps.sales.models import CustomerReturn, CustomerReturnLine, Invoice, Shipment, ShipmentLine

MONEY = DecimalField(max_digits=18, decimal_places=2)
_ZERO = Decimal("0")
_CENTS = Decimal("0.01")


@dataclass
class _Entry:
    """Продажа (строка отгрузки) или возврат (товар одного возврата)."""

    day: date
    doc_id: int
    product_id: int
    product: str
    customer_id: int
    customer: str
    qty: Decimal
    revenue: Decimal
    cost: Decimal
    estimated: bool = False       # точная себестоимость ещё не записана — прежняя оценка
    missing: bool = False         # у возврата нет складского движения — себестоимость неизвестна
    open_qty: Decimal = _ZERO     # продажа: единиц без определённой себестоимости


def _period(qs, field, date_from, date_to):
    if date_from:
        qs = qs.filter(**{f"{field}__gte": date_from})
    if date_to:
        qs = qs.filter(**{f"{field}__lte": date_to})
    return qs


def _sales(date_from=None, date_to=None, organization=None):
    qs = _period(ShipmentLine.objects.filter(shipment__status=DOC_POSTED), "shipment__date", date_from, date_to)
    if organization:
        qs = qs.filter(shipment__organization_id=organization)
    entries = []
    for (doc_id, day, product_id, product, service, customer_id, customer, qty, price, discount,
         cost_amount, cost_price, open_qty) in qs.values_list(
            "shipment_id", "shipment__date", "product_id", "product__name", "product__item_type",
            "shipment__customer_id", "shipment__customer__name", "quantity", "price", "discount",
            "cost_amount", "cost_price", "cost_open_qty").order_by("shipment__date", "shipment_id", "pk"):
        if cost_amount is None:       # как ShipmentLine.cost_total
            cost, estimated = (qty * cost_price).quantize(_CENTS), service != Product.TYPE_SERVICE
        else:
            cost, estimated = cost_amount, False
        entries.append(_Entry(day, doc_id, product_id, product, customer_id, customer, qty,
                              line_total(qty, price, discount), cost, estimated=estimated, open_qty=open_qty))
    return entries


def _returns(date_from=None, date_to=None, organization=None):
    qs = _period(CustomerReturnLine.objects.filter(document__status=DOC_POSTED), "document__date",
                 date_from, date_to)
    if organization:
        qs = qs.filter(document__organization_id=organization)
    grouped = {}
    for (doc_id, day, product_id, product, service, customer_id, customer, qty, price, discount) in qs.values_list(
            "document_id", "document__date", "product_id", "product__name", "product__item_type",
            "document__customer_id", "document__customer__name", "quantity", "price", "discount").order_by(
            "document__date", "document_id", "pk"):
        entry = grouped.get((doc_id, product_id))
        if entry is None:
            entry = grouped[(doc_id, product_id)] = _Entry(day, doc_id, product_id, product, customer_id, customer,
                                                           _ZERO, _ZERO, _ZERO,
                                                           missing=service != Product.TYPE_SERVICE)
        entry.qty += qty
        entry.revenue += line_total(qty, price, discount)
    movements = StockMovement.objects.filter(doc_type=CustomerReturn.DOC_TYPE,
                                             doc_id__in={doc_id for doc_id, _ in grouped})
    for doc_id, product_id, quantity, cost, amount in movements.values_list(
            "doc_id", "product_id", "quantity", "cost", "amount"):
        entry = grouped.get((doc_id, product_id))
        if entry is None:
            continue
        entry.missing = False
        if amount is None:            # движение ещё не пересчитано — прежняя оценка
            entry.cost += costing.implied_amount(quantity, cost)
            entry.estimated = True
        else:
            entry.cost += amount
    return list(grouped.values())


def _entries(date_from=None, date_to=None, organization=None):
    return _sales(date_from, date_to, organization), _returns(date_from, date_to, organization)


def _total(entries, name):
    return sum((getattr(entry, name) for entry in entries), _ZERO)


def _cancelled(date_from, date_to, organization=None):
    """Непокрытые единицы, погашенные возвратом в другом периоде: их
    себестоимость не определилась. (по продажам периода, по возвратам
    периода, отгрузки, возвраты). Расчёт проигрывает историю только тех
    товаров, по которым есть возвраты с основанием, — и ничего не пишет."""
    def inside(day):
        return (date_from is None or day >= date_from) and (date_to is None or day <= date_to)

    lines = CustomerReturnLine.objects.filter(document__status=DOC_POSTED, document__shipment__isnull=False).exclude(
        product__item_type=Product.TYPE_SERVICE)
    if organization:
        lines = lines.filter(document__organization_id=organization)
    if date_from or date_to:
        lines = lines.filter(Q(pk__in=_period(lines, "document__date", date_from, date_to).values("pk"))
                             | Q(pk__in=_period(lines, "document__shipment__date", date_from, date_to).values("pk")))
    sold = returned = _ZERO
    shipments, returns = set(), set()
    for product_id in sorted(set(lines.values_list("product_id", flat=True))):
        plan = costing.build_plan(product_id)
        for issue_pk, return_pk, quantity in plan.cancels:
            sale, back = plan.stored_movements[issue_pk], plan.stored_movements[return_pk]
            if inside(sale.date) and not inside(back.date):
                sold += quantity
                shipments.add(sale.doc_id)
            elif inside(back.date) and not inside(sale.date):
                returned += quantity
                returns.add(back.doc_id)
    return sold, returned, shipments, returns


def _margin(profit, revenue):
    """Рентабельность продаж, %; при выручке ≤ 0 не считается (None)."""
    return profit / revenue * 100 if revenue > 0 else None


def sales_summary(date_from=None, date_to=None, organization=None):
    sales, returns = _entries(date_from, date_to, organization)
    result = {
        "sales_revenue": _total(sales, "revenue"), "sales_cost": _total(sales, "cost"),
        "returns_revenue": _total(returns, "revenue"), "returns_cost": _total(returns, "cost"),
    }
    result["sales_profit"] = result["sales_revenue"] - result["sales_cost"]
    result["returns_profit"] = result["returns_revenue"] - result["returns_cost"]
    # результат после возвратов
    result["revenue"] = result["sales_revenue"] - result["returns_revenue"]
    result["cost"] = result["sales_cost"] - result["returns_cost"]
    result["profit"] = result["revenue"] - result["cost"]
    result["margin"] = _margin(result["profit"], result["revenue"])
    # счётчики документов — отдельно, смысл «count» прежний: проведённые отгрузки
    shipments, customer_returns = Shipment.objects.filter(status=DOC_POSTED), CustomerReturn.objects.filter(
        status=DOC_POSTED)
    if organization:
        shipments = shipments.filter(organization_id=organization)
        customer_returns = customer_returns.filter(organization_id=organization)
    result["count"] = _period(shipments, "date", date_from, date_to).count()
    result["returns_count"] = _period(customer_returns, "date", date_from, date_to).count()
    # предварительная себестоимость
    sold, returned, cancelled_shipments, cancelled_returns = _cancelled(date_from, date_to, organization)
    open_shipments = {entry.doc_id for entry in sales if entry.open_qty > 0}
    result["open_qty"] = _total(sales, "open_qty") + sold
    result["preliminary_count"] = len(open_shipments | cancelled_shipments)
    result["returns_open_qty"] = returned
    result["preliminary_returns"] = len(cancelled_returns)
    # данные, которым нужен пересчёт
    result["estimated_count"] = sum(1 for entry in sales + returns if entry.estimated)
    result["missing_cost_count"] = sum(1 for entry in returns if entry.missing)
    return result


def _breakdown(date_from, date_to, key, name, organization=None):
    sales, returns = _entries(date_from, date_to, organization)
    rows = {}
    for sign, entries in ((1, sales), (-1, returns)):
        for entry in entries:
            row = rows.setdefault(getattr(entry, key), {
                name: getattr(entry, name), "sales_qty": _ZERO, "returns_qty": _ZERO,
                "sales_revenue": _ZERO, "returns_revenue": _ZERO, "sales_cost": _ZERO, "returns_cost": _ZERO,
            })
            prefix = "sales" if sign > 0 else "returns"
            row[f"{prefix}_qty"] += entry.qty
            row[f"{prefix}_revenue"] += entry.revenue
            row[f"{prefix}_cost"] += entry.cost
    for row in rows.values():
        row["qty"] = row["sales_qty"] - row["returns_qty"]
        row["revenue"] = row["sales_revenue"] - row["returns_revenue"]
        row["cost"] = row["sales_cost"] - row["returns_cost"]
        row["profit"] = row["revenue"] - row["cost"]
    return sorted(rows.values(), key=lambda row: (-row["revenue"], row[name]))


def product_rows(date_from=None, date_to=None, organization=None):
    """Все товары периода: продажи, возвраты и результат после возвратов."""
    return _breakdown(date_from, date_to, "product_id", "product", organization)


def customer_rows(date_from=None, date_to=None, organization=None):
    """Все покупатели периода: продажи, возвраты и результат после возвратов."""
    return _breakdown(date_from, date_to, "customer_id", "customer", organization)


def top_products(date_from=None, date_to=None, limit=10, organization=None):
    return product_rows(date_from, date_to, organization)[:limit]


def top_customers(date_from=None, date_to=None, limit=10, organization=None):
    return customer_rows(date_from, date_to, organization)[:limit]


def sales_by_month(months=6, organization=None):
    """По месяцам: выручка продаж, возвратов и после возвратов (value)."""
    start = (timezone.localdate().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    sales, returns = _entries(date_from=start, organization=organization)
    by_month = defaultdict(lambda: {"sales": _ZERO, "returns": _ZERO})
    for entry in sales:
        by_month[entry.day.replace(day=1)]["sales"] += entry.revenue
    for entry in returns:
        by_month[entry.day.replace(day=1)]["returns"] += entry.revenue
    rows = []
    for row in _fill_months(by_month, months, default={"sales": _ZERO, "returns": _ZERO}):
        value = row.pop("value")
        rows.append({**row, "sales": value["sales"], "returns": value["returns"],
                     "value": value["sales"] - value["returns"]})
    return rows


def cashflow_summary(date_from=None, date_to=None):
    qs = Payment.objects.filter(status=DOC_POSTED)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    agg = qs.aggregate(
        income=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_IN)), _ZERO, output_field=MONEY),
        outcome=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_OUT)), _ZERO, output_field=MONEY),
    )
    agg["net"] = agg["income"] - agg["outcome"]
    return agg


def cashflow_by_account(date_from=None, date_to=None):
    qs = Payment.objects.filter(status=DOC_POSTED)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    rows = (
        qs.values("account__name")
        .annotate(
            income=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_IN)), _ZERO, output_field=MONEY),
            outcome=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_OUT)), _ZERO, output_field=MONEY),
        )
        .order_by("account__name")
    )
    return list(rows)


def cashflow_by_month(months=6):
    start = (timezone.localdate().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    qs = Payment.objects.filter(status=DOC_POSTED, date__gte=start).annotate(m=TruncMonth("date"))
    income_rows = qs.values("m").annotate(v=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_IN)), _ZERO, output_field=MONEY))
    outcome_rows = qs.values("m").annotate(v=Coalesce(Sum("amount", filter=Q(kind=Payment.KIND_OUT)), _ZERO, output_field=MONEY))
    income = _fill_months({r["m"]: r["v"] for r in income_rows}, months)
    outcome = _fill_months({r["m"]: r["v"] for r in outcome_rows}, months)
    return income, outcome


def overdue_invoices():
    today = timezone.localdate()
    return (
        Invoice.objects.filter(status=Invoice.STATUS_ISSUED, due_date__isnull=False, due_date__lt=today)
        .annotate(calc_total=Coalesce(Sum(_revenue_expr_invoice()), _ZERO, output_field=MONEY))
        .filter(calc_total__gt=F("paid_amount"))
        .select_related("customer")
        .order_by("due_date")
    )


def _revenue_expr_invoice():
    return ExpressionWrapper(
        F("lines__quantity") * F("lines__price") * (Decimal("1") - F("lines__discount") / Decimal("100")),
        output_field=MONEY,
    )


# ---------- вспомогательное ----------

RU_MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _month_sequence(months):
    today = timezone.localdate().replace(day=1)
    seq = []
    y, m = today.year, today.month
    for _ in range(months):
        seq.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(seq))


def _as_date(value):
    return value.date() if hasattr(value, "date") else value


def _fill_months(value_by_month, months, default=_ZERO):
    """Возвращает [{'label': 'июл', 'value': …}] за последние N месяцев без пропусков."""
    normalized = {_as_date(k): v for k, v in value_by_month.items()}
    result = []
    for d in _month_sequence(months):
        value = normalized.get(d, default)
        result.append({"label": f"{RU_MONTHS[d.month]} {str(d.year)[2:]}", "value": value})
    return result
