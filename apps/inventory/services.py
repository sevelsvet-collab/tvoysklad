"""Складской учёт: пересчёт остатков и средневзвешенной себестоимости.

StockMovement — источник истины. StockBalance — кэш, пересчитываемый из движений.

Средневзвешенная себестоимость: при каждом приходе
    новая_средняя = (кол-во × средняя + приход_кол × приход_цена) / (кол-во + приход_кол).
Расход списывается по текущей средней (avg), стоимость остатка уменьшается на кол×avg.
"""
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from .models import StockBalance, StockMovement

CENTS = Decimal("0.01")


class StockError(Exception):
    """Недостаточно товара для проведения расходного документа."""


def _q_cost(value):
    return Decimal(value).quantize(CENTS)


def current_balance(product_id, warehouse_id):
    return StockBalance.objects.filter(product_id=product_id, warehouse_id=warehouse_id).first()


def current_avg_cost(product_id, warehouse_id):
    bal = current_balance(product_id, warehouse_id)
    return bal.avg_cost if bal else Decimal("0")


def current_qty(product_id, warehouse_id):
    bal = current_balance(product_id, warehouse_id)
    return bal.quantity if bal else Decimal("0")


def recompute_balance(product_id, warehouse_id):
    """Пересчитывает остаток и среднюю себестоимость из всех движений в хронологии."""
    movements = list(
        StockMovement.objects
        .filter(product_id=product_id, warehouse_id=warehouse_id)
        .order_by("date", "id")
    )
    if not movements:
        StockBalance.objects.filter(product_id=product_id, warehouse_id=warehouse_id).delete()
        return

    qty = Decimal("0")
    value = Decimal("0")
    for m in movements:
        if m.quantity > 0:
            value += m.quantity * m.cost
            qty += m.quantity
        else:
            avg = (value / qty) if qty > 0 else Decimal("0")
            out = -m.quantity
            value -= out * avg
            qty -= out
            if qty <= 0:  # ушли в ноль/минус — обнуляем стоимость, чтобы не копить погрешность
                qty = qty if qty < 0 else Decimal("0")
                value = Decimal("0")

    avg_cost = _q_cost(value / qty) if qty > 0 else Decimal("0")
    StockBalance.objects.update_or_create(
        product_id=product_id, warehouse_id=warehouse_id,
        defaults={"quantity": qty, "avg_cost": avg_cost},
    )


def validate_stock(specs, allow_negative=None):
    """Проверяет, что расходных остатков достаточно. Бросает StockError."""
    if allow_negative is None:
        allow_negative = getattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    if allow_negative:
        return

    from apps.catalog.models import Product

    need = defaultdict(Decimal)
    for spec in specs:
        if spec["quantity"] < 0:
            need[(spec["product_id"], spec["warehouse_id"])] += -spec["quantity"]

    for (product_id, warehouse_id), qty in need.items():
        available = current_qty(product_id, warehouse_id)
        if available < qty:
            product = Product.objects.filter(pk=product_id).first()
            name = product.name if product else f"#{product_id}"
            raise StockError(
                f"Недостаточно товара «{name}» на складе: доступно {available:g}, требуется {qty:g}"
            )


@transaction.atomic
def create_movements(doc_type, doc_id, doc_number, date, specs):
    """Создаёт движения документа (предполагается, что старых нет) и пересчитывает остатки.

    specs: список dict(product_id, warehouse_id, quantity: Decimal со знаком, cost: Decimal).
    """
    affected = set()
    for spec in specs:
        StockMovement.objects.create(
            doc_type=doc_type, doc_id=doc_id, doc_number=doc_number, date=date,
            product_id=spec["product_id"], warehouse_id=spec["warehouse_id"],
            quantity=spec["quantity"], cost=_q_cost(spec["cost"]),
        )
        affected.add((spec["product_id"], spec["warehouse_id"]))
    for product_id, warehouse_id in affected:
        recompute_balance(product_id, warehouse_id)


@transaction.atomic
def clear_movements(doc_type, doc_id):
    """Удаляет движения документа и пересчитывает затронутые остатки."""
    pairs = set(
        StockMovement.objects
        .filter(doc_type=doc_type, doc_id=doc_id)
        .values_list("product_id", "warehouse_id")
    )
    StockMovement.objects.filter(doc_type=doc_type, doc_id=doc_id).delete()
    for product_id, warehouse_id in pairs:
        recompute_balance(product_id, warehouse_id)
