"""Агрегации для отчётов и дашборда.

Суммы считаются на уровне строк документов через БД (Sum(F*F)),
т.к. Shipment.total / Invoice.total — это Python-свойства, не поля.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.core.constants import DOC_POSTED
from apps.finance.models import Payment
from apps.sales.models import Invoice, Shipment, ShipmentLine

MONEY = DecimalField(max_digits=18, decimal_places=2)
_ZERO = Decimal("0")


def _revenue_expr():
    # Выручка с учётом скидки: quantity * price * (1 - discount/100)
    return ExpressionWrapper(
        F("quantity") * F("price") * (Decimal("1") - F("discount") / Decimal("100")),
        output_field=MONEY,
    )


def _cost_expr():
    return ExpressionWrapper(F("quantity") * F("cost_price"), output_field=MONEY)


def _posted_lines(date_from=None, date_to=None):
    qs = ShipmentLine.objects.filter(shipment__status=DOC_POSTED)
    if date_from:
        qs = qs.filter(shipment__date__gte=date_from)
    if date_to:
        qs = qs.filter(shipment__date__lte=date_to)
    return qs


def sales_summary(date_from=None, date_to=None):
    agg = _posted_lines(date_from, date_to).aggregate(
        revenue=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY),
        cost=Coalesce(Sum(_cost_expr()), _ZERO, output_field=MONEY),
    )
    revenue = agg["revenue"]
    cost = agg["cost"]
    profit = revenue - cost
    margin = (profit / revenue * 100) if revenue else _ZERO
    shipments = Shipment.objects.filter(status=DOC_POSTED)
    if date_from:
        shipments = shipments.filter(date__gte=date_from)
    if date_to:
        shipments = shipments.filter(date__lte=date_to)
    return {
        "revenue": revenue, "cost": cost, "profit": profit,
        "margin": margin, "count": shipments.count(),
    }


def top_products(date_from=None, date_to=None, limit=10):
    rows = (
        _posted_lines(date_from, date_to)
        .values("product__name")
        .annotate(
            qty=Coalesce(Sum("quantity"), _ZERO, output_field=MONEY),
            revenue=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY),
            profit=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY) - Coalesce(Sum(_cost_expr()), _ZERO, output_field=MONEY),
        )
        .order_by("-revenue")[:limit]
    )
    return list(rows)


def top_customers(date_from=None, date_to=None, limit=10):
    rows = (
        _posted_lines(date_from, date_to)
        .values("shipment__customer__name")
        .annotate(
            revenue=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY),
            profit=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY) - Coalesce(Sum(_cost_expr()), _ZERO, output_field=MONEY),
        )
        .order_by("-revenue")[:limit]
    )
    return list(rows)


def sales_by_month(months=6):
    start = (timezone.localdate().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    rows = (
        _posted_lines(date_from=start)
        .annotate(m=TruncMonth("shipment__date"))
        .values("m")
        .annotate(revenue=Coalesce(Sum(_revenue_expr()), _ZERO, output_field=MONEY))
        .order_by("m")
    )
    return _fill_months({r["m"]: r["revenue"] for r in rows}, months)


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


def _fill_months(value_by_month, months):
    """Возвращает [{'label': 'июл', 'value': Decimal}] за последние N месяцев без пропусков."""
    normalized = {_as_date(k): v for k, v in value_by_month.items()}
    result = []
    for d in _month_sequence(months):
        value = normalized.get(d, _ZERO)
        result.append({"label": f"{RU_MONTHS[d.month]} {str(d.year)[2:]}", "value": value})
    return result
