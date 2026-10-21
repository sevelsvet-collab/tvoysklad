"""Учёт по серийным номерам.

Номера строки документа хранятся в поле serial_numbers — по одному в строке
текста (запятая и точка с запятой тоже разделяют). При проведении документа
номера товаров с включённым учётом проверяются строго и двигаются вместе
с товаром:

- приход (приёмка, возврат покупателя, оприходование) — номер не должен уже
  числиться на складе ни у какой организации: это одна физическая единица;
- расход (отгрузка, возврат поставщику, списание) — номер должен лежать
  на складе документа у организации документа;
- перемещение — номер должен лежать на складе-отправителе у организации
  перемещения; организация номера сохраняется.

Номер принадлежит организации, у которой лежит (SerialNumber.organization —
по последнему движению). Порядок истории номера — как у складских движений:
дата, время и момент создания документа. Номер, чья принадлежность не
определена при переносе (ввод старого остатка без документа), расходовать и
перемещать нельзя, пока человек не решит, чей он (stock_transition).

Количество номеров равно количеству в строке. Черновик можно сохранить
без номеров — проверка срабатывает при проведении.
"""
import datetime
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone


from .models import SerialMovement, SerialNumber
from .services import StockError, current_qty, lock_products

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


def by_lines(text, quantity, candidates):
    """Из каких строк основания возвращаются номера: {ключ строки: количество}.

    candidates — [(ключ, текст номеров строки основания)]. Каждый номер
    возврата должен быть ровно в одной строке, а номеров — столько же, сколько
    единиц; иначе None: по номерам распределить нельзя.
    """
    numbers = parse(text)
    if not numbers or Decimal(len(numbers)) != Decimal(quantity):
        return None
    owners = {}
    for key, line_text in candidates:
        for number in parse(line_text):
            owners.setdefault(number, []).append(key)
    parts = {}
    for number in numbers:
        keys = owners.get(number, [])
        if len(keys) != 1:
            return None
        parts[keys[0]] = parts.get(keys[0], 0) + 1
    return parts


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


def ordered(serial):
    """Движения номера в хронологии документов (как складские движения)."""
    return serial.movements.order_by(*SerialMovement.ORDER)


def recompute(serial):
    """Состояние номера по последнему движению; без движений номер удаляется."""
    last = ordered(serial).last()
    if last is None:
        serial.delete()
        return
    if last.quantity > 0:
        serial.warehouse_id, serial.organization_id = last.warehouse_id, last.organization_id
        serial.status = SerialNumber.STATUS_IN_STOCK
    else:
        serial.warehouse_id, serial.organization_id = None, None
        serial.status = OUT_STATUS.get(last.doc_type, SerialNumber.STATUS_SOLD)
    serial.save(update_fields=["warehouse", "organization", "status"])


def _warehouse_names(ids):
    from apps.core.models import Warehouse

    return {w.pk: w.name for w in Warehouse.objects.filter(pk__in=[i for i in ids if i])}


def _owner(serial):
    if serial.organization_id is None:
        return "организация не определена"
    from apps.core.models import Organization

    return f"«{Organization.objects.filter(pk=serial.organization_id).values_list('name', flat=True).first()}»"


@transaction.atomic
def apply(doc):
    """Проверяет номера проводимого документа и записывает их движения.

    Бросает StockError с понятным текстом — документ тогда не проводится.
    """
    kind = doc._meta.model_name
    rows = [r for r in plan(doc) if r[0].product.track_serials]
    if not rows:
        return set()
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
                                  f"«{serial.warehouse}» ({_owner(serial)}) — это та же единица товара")
            elif serial is None or serial.warehouse_id != wh_from:
                errors.append(f"«{product.name}»: номера {number} нет на складе «{names.get(wh_from, '')}»")
            elif serial.organization_id != doc.organization_id:
                errors.append(f"«{product.name}»: номер {number} на складе «{names.get(wh_from, '')}» принадлежит "
                              f"не организации документа ({_owner(serial)})")
            moves.append((product, number, serial, direction, wh_from, wh_to))

    if errors:
        more = f" …и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        raise StockError("Серийные номера: " + "; ".join(errors[:5]) + more)

    touched = set()
    for product, number, serial, direction, wh_from, wh_to in moves:
        if serial is None:
            serial = SerialNumber.objects.create(product=product, number=number, warehouse_id=wh_to,
                                                 organization_id=doc.organization_id)
        if direction in (OUT, MOVE):
            _record(serial, kind, doc, wh_from, -1)
        if direction in (IN, MOVE):
            _record(serial, kind, doc, wh_to, +1)
        recompute(serial)
        touched.add(serial.pk)
    return touched


def _record(serial, doc_type, doc, warehouse_id, quantity):
    SerialMovement.objects.create(
        serial=serial, doc_type=doc_type, doc_id=doc.pk, doc_number=doc.number or "",
        date=doc.date, time=getattr(doc, "time", None) or datetime.time(0, 0),
        doc_created_at=getattr(doc, "created_at", None), organization_id=doc.organization_id,
        warehouse_id=warehouse_id, quantity=quantity,
    )


@transaction.atomic
def clear(doc):
    """Снимает движения номеров документа (снятие с проведения, перепроведение).

    Возвращает id затронутых номеров — их историю потом проверяет validate_history.
    """
    moves = SerialMovement.objects.filter(doc_type=doc._meta.model_name, doc_id=doc.pk)
    serial_ids = set(moves.values_list("serial_id", flat=True))
    moves.delete()
    for serial in SerialNumber.objects.filter(pk__in=serial_ids):
        recompute(serial)
    return serial_ids


def validate_history(serial_ids):
    """История номера должна оставаться связной: на конец любого дня номер либо
    лежит на складе (1), либо нет (0).

    Нельзя, например, отменить приёмку номера, который уже продан: останется
    продажа того, что не поступало. Правило действует всегда — разрешение
    отрицательных остатков его не отменяет.
    """
    for serial in SerialNumber.objects.filter(pk__in=serial_ids).select_related("product"):
        count, problem_date = 0, None
        days = serial.movements.values("date").annotate(total=Sum("quantity")).order_by("date")
        for row in days:
            count += row["total"]
            if count not in (0, 1):
                problem_date = row["date"]
                break
        if problem_date is None:
            _check_owners(serial)
            continue
        documents = ", ".join(sorted({
            f"№ {m.doc_number}" for m in serial.movements.all() if m.doc_number
        })) or "других документах"
        raise StockError(
            f"Серийные номера: номер {serial.number} товара «{serial.product.name}» участвует "
            f"в проведённых документах ({documents}). Сначала отмените их — иначе история номера "
            f"на {problem_date:%d.%m.%Y} станет противоречивой"
        )


def _check_owners(serial):
    """Расход и перемещение номера — только организацией, у которой он лежит
    (по порядку документов). Номер без определённой принадлежности
    (ввод старого остатка, не решённый при переносе) расходовать нельзя."""
    holder = None                                   # (организация, склад), пока номер на складе
    for m in ordered(serial):
        if m.quantity > 0:
            holder = (m.organization_id, m.warehouse_id)
            continue
        if holder is None:
            continue                                # «ушёл, не придя» — ловит проверка по дням
        if holder[0] is None or m.organization_id is None:
            raise StockError(
                f"Серийные номера: номер {serial.number} товара «{serial.product.name}» — принадлежность не "
                "определена (номер введён остатком без документа). Решите, чей он: manage.py stock_transition"
            )
        if holder != (m.organization_id, m.warehouse_id):
            raise StockError(
                f"Серийные номера: номер {serial.number} товара «{serial.product.name}» расходуется документом "
                f"№ {m.doc_number or m.doc_id} от {m.date:%d.%m.%Y} не той организацией или не с того склада, "
                "где он лежит по истории. Сначала отмените документы, после которых история номера противоречива"
            )
        holder = None


def available(product_id, warehouse_id, organization_id=None):
    """Номера товара, которые лежат на складе у организации, — из них выбирают
    при расходе."""
    rows = SerialNumber.objects.filter(product_id=product_id, warehouse_id=warehouse_id)
    if organization_id is not None:
        rows = rows.filter(organization_id=organization_id)
    return list(rows.order_by("number").values_list("number", flat=True))


def _qty(value):
    """1.000 → «1», 2.500 → «2.5» — количество в сообщениях без хвостовых нулей."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def without_serials(product, warehouse, organization):
    """Сколько единиц товара организации на складе ещё без серийных номеров."""
    in_stock = current_qty(product.pk, warehouse.pk, organization.pk)
    numbered = SerialNumber.objects.filter(product=product, warehouse=warehouse, organization=organization).count()
    return max(in_stock - numbered, Decimal("0"))


@transaction.atomic
def enter_stock(product, warehouse, text, organization):
    """Ввод номеров для товара, который уже лежал на складе у организации,
    когда включили учёт. Организация обязательна: чей это товар, программа
    не угадывает."""
    from .transition import require_ready

    require_ready()
    numbers = parse(text)
    if not numbers:
        raise StockError("Введите хотя бы один серийный номер")
    if organization is None:
        raise StockError("Укажите организацию, которой принадлежит товар на складе")
    lock_products([product.pk])   # остаток и номера читаем под блокировкой товара
    free = without_serials(product, warehouse, organization)
    if len(numbers) > free:
        raise StockError(f"На складе «{warehouse}» у организации «{organization}» без номеров {_qty(free)} шт., "
                         f"а введено {len(numbers)}")
    busy = list(
        SerialNumber.objects.filter(product=product, number__in=numbers)
        .exclude(warehouse=None).values_list("number", flat=True)
    )
    if busy:
        raise StockError("Эти номера уже числятся на складе: " + ", ".join(busy[:10]))
    now = timezone.now()
    local = timezone.localtime(now)
    for number in numbers:
        serial, _ = SerialNumber.objects.get_or_create(product=product, number=number)
        SerialMovement.objects.create(
            serial=serial, doc_type=STOCK_ENTRY, doc_id=0, doc_number="Ввод остатка",
            date=local.date(), time=local.time().replace(microsecond=0), doc_created_at=now,
            organization=organization, warehouse=warehouse, quantity=+1,
        )
        recompute(serial)
    return len(numbers)


@transaction.atomic
def remove_stock_entry(serial):
    """Убрать номер, ошибочно введённый как остаток (если по нему не было документов)."""
    from .transition import require_ready

    require_ready()
    if serial.movements.exclude(doc_type=STOCK_ENTRY).exists():
        raise StockError(f"Номер {serial.number} уже участвовал в документах — удалить нельзя")
    serial.delete()
