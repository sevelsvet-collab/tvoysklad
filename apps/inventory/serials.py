"""Учёт по серийным номерам.

Номера строки документа хранятся в поле serial_numbers — по одному в строке
текста (запятая и точка с запятой тоже разделяют). При проведении документа
номера товаров с включённым учётом проверяются строго и двигаются вместе
с товаром:

- приход (приёмка, возврат покупателя, оприходование) — номер не должен уже
  числиться на складе;
- расход (отгрузка, возврат поставщику, списание) — номер должен лежать
  на складе документа;
- перемещение — номер должен лежать на складе-отправителе.

Количество номеров равно количеству в строке. Черновик можно сохранить
без номеров — проверка срабатывает при проведении.
"""
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import SerialMovement, SerialNumber
from .services import StockError, current_qty

IN, OUT, MOVE = "in", "out", "move"
STOCK_ENTRY = "serial_stock"   # ввод номеров для уже лежащего на складе товара
_SPLIT = re.compile(r"[\n\r,;]+")

# Каким становится номер после расхода по виду документа
OUT_STATUS = {
    "shipment": SerialNumber.STATUS_SOLD,
    "supplierreturn": SerialNumber.STATUS_RETURNED,
    "stockadjustment": SerialNumber.STATUS_WRITTEN_OFF,
}


def parse(text):
    """Текст поля → список номеров без пустых и повторов, порядок сохраняется."""
    seen, result = set(), []
    for part in _SPLIT.split(text or ""):
        number = part.strip()[:100]
        if number and number not in seen:
            seen.add(number)
            result.append(number)
    return result


def plan(doc):
    """Строки документа с направлением: (строка, направление, откуда, куда)."""
    kind = doc._meta.model_name
    lines = list(doc.lines.select_related("product"))
    if kind in ("receipt", "customerreturn"):
        return [(line, IN, None, doc.warehouse_id) for line in lines]
    if kind in ("shipment", "supplierreturn"):
        return [(line, OUT, doc.warehouse_id, None) for line in lines]
    if kind == "transfer":
        return [(line, MOVE, doc.warehouse_from_id, doc.warehouse_to_id) for line in lines]
    if kind == "stockadjustment":
        if doc.kind == doc.KIND_INCOME:
            return [(line, IN, None, doc.warehouse_id) for line in lines]
        return [(line, OUT, doc.warehouse_id, None) for line in lines]
    return []


def recompute(serial):
    """Состояние номера по последнему движению; без движений номер удаляется."""
    last = serial.movements.order_by("date", "id").last()
    if last is None:
        serial.delete()
        return
    if last.quantity > 0:
        serial.warehouse_id, serial.status = last.warehouse_id, SerialNumber.STATUS_IN_STOCK
    else:
        serial.warehouse_id = None
        serial.status = OUT_STATUS.get(last.doc_type, SerialNumber.STATUS_SOLD)
    serial.save(update_fields=["warehouse", "status"])


def _warehouse_names(ids):
    from apps.core.models import Warehouse

    return {w.pk: w.name for w in Warehouse.objects.filter(pk__in=[i for i in ids if i])}


@transaction.atomic
def apply(doc):
    """Проверяет номера проводимого документа и записывает их движения.

    Бросает StockError с понятным текстом — документ тогда не проводится.
    """
    kind = doc._meta.model_name
    rows = [r for r in plan(doc) if r[0].product.track_serials]
    if not rows:
        return
    names = _warehouse_names({r[2] for r in rows} | {r[3] for r in rows})
    errors, moves = [], []

    for line, direction, wh_from, wh_to in rows:
        product = line.product
        numbers = parse(line.serial_numbers)
        need = Decimal(line.quantity)
        if need != need.to_integral_value():
            errors.append(f"«{product.name}»: при учёте по серийным номерам количество должно быть целым")
            continue
        if len(numbers) != int(need):
            errors.append(f"«{product.name}»: нужно серийных номеров — {int(need)}, указано — {len(numbers)}")
            continue
        existing = {s.number: s for s in SerialNumber.objects.filter(product=product, number__in=numbers)}
        for number in numbers:
            serial = existing.get(number)
            if direction == IN:
                if serial and serial.warehouse_id:
                    errors.append(f"«{product.name}»: номер {number} уже числится на складе "
                                  f"«{serial.warehouse}»")
            elif serial is None or serial.warehouse_id != wh_from:
                errors.append(f"«{product.name}»: номера {number} нет на складе «{names.get(wh_from, '')}»")
            moves.append((product, number, serial, direction, wh_from, wh_to))

    if errors:
        more = f" …и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        raise StockError("Серийные номера: " + "; ".join(errors[:5]) + more)

    for product, number, serial, direction, wh_from, wh_to in moves:
        if serial is None:
            serial = SerialNumber.objects.create(product=product, number=number, warehouse_id=wh_to)
        if direction in (OUT, MOVE):
            _record(serial, kind, doc, wh_from, -1)
        if direction in (IN, MOVE):
            _record(serial, kind, doc, wh_to, +1)
        recompute(serial)


def _record(serial, doc_type, doc, warehouse_id, quantity):
    SerialMovement.objects.create(
        serial=serial, doc_type=doc_type, doc_id=doc.pk, doc_number=doc.number or "",
        date=doc.date, warehouse_id=warehouse_id, quantity=quantity,
    )


@transaction.atomic
def clear(doc):
    """Снимает движения номеров документа (снятие с проведения, перепроведение)."""
    moves = SerialMovement.objects.filter(doc_type=doc._meta.model_name, doc_id=doc.pk)
    serial_ids = set(moves.values_list("serial_id", flat=True))
    moves.delete()
    for serial in SerialNumber.objects.filter(pk__in=serial_ids):
        recompute(serial)


def available(product_id, warehouse_id):
    """Номера товара, которые лежат на складе, — из них выбирают при расходе."""
    return list(
        SerialNumber.objects.filter(product_id=product_id, warehouse_id=warehouse_id)
        .order_by("number").values_list("number", flat=True)
    )


def _qty(value):
    """1.000 → «1», 2.500 → «2.5» — количество в сообщениях без хвостовых нулей."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def without_serials(product, warehouse):
    """Сколько единиц товара на складе ещё без серийных номеров."""
    in_stock = current_qty(product.pk, warehouse.pk)
    numbered = SerialNumber.objects.filter(product=product, warehouse=warehouse).count()
    return max(in_stock - numbered, Decimal("0"))


@transaction.atomic
def enter_stock(product, warehouse, text):
    """Ввод номеров для товара, который уже лежал на складе, когда включили учёт."""
    numbers = parse(text)
    if not numbers:
        raise StockError("Введите хотя бы один серийный номер")
    free = without_serials(product, warehouse)
    if len(numbers) > free:
        raise StockError(f"На складе «{warehouse}» без номеров {_qty(free)} шт., а введено {len(numbers)}")
    busy = list(
        SerialNumber.objects.filter(product=product, number__in=numbers)
        .exclude(warehouse=None).values_list("number", flat=True)
    )
    if busy:
        raise StockError("Эти номера уже числятся на складе: " + ", ".join(busy[:10]))
    today = timezone.localdate()
    for number in numbers:
        serial, _ = SerialNumber.objects.get_or_create(product=product, number=number)
        SerialMovement.objects.create(
            serial=serial, doc_type=STOCK_ENTRY, doc_id=0, doc_number="Ввод остатка",
            date=today, warehouse=warehouse, quantity=+1,
        )
        recompute(serial)
    return len(numbers)


@transaction.atomic
def remove_stock_entry(serial):
    """Убрать номер, ошибочно введённый как остаток (если по нему не было документов)."""
    if serial.movements.exclude(doc_type=STOCK_ENTRY).exists():
        raise StockError(f"Номер {serial.number} уже участвовал в документах — удалить нельзя")
    serial.delete()
