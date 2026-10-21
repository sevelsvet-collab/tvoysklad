"""Складской учёт: движения, остатки, проверки наличия.

StockMovement — источник истины. StockBalance — кэш, пересчитываемый из движений.

Ключ учёта — «организация + склад + товар»: склад — место хранения, товар на
нём принадлежит организации. Остатки, история остатка и проверки наличия —
по этому ключу (тройки (товар, склад, организация)); организация движения —
организация документа. Запрет минуса — настройка организации ключа.

Средневзвешенная себестоимость: при каждом приходе
    новая_средняя = (кол-во × средняя + приход_кол × приход_цена) / (кол-во + приход_кол).
Расход списывается по средней, которая была на складе перед ним в хронологии
документов. Сам расчёт — в costing.py: он проигрывает историю товара и
согласованно обновляет движения, строки отгрузок и остатки.
"""
import bisect
import datetime
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from . import costing
from .models import StockBalance, StockMovement

CENTS = Decimal("0.01")

# «Себестоимость определит пересчёт» (costing): расход — по средней перед ним,
# приход — по строке документа, перемещение и возврат — по своим правилам.
# В движение пишется 0 до пересчёта, который идёт в той же транзакции.
COST_BY_HISTORY = Decimal("0")


class StockError(Exception):
    """Операция нарушила бы складской учёт: не хватает товара, остаток уходит
    в минус на какую-то дату или ломается история серийного номера."""


class PostedChangeError(StockError):
    """Учётные реквизиты проведённого документа (склад, дата, время,
    организация) меняют в обход перепроведения: документ разошёлся бы со
    своими движениями (posting.PostableMixin.editing, правило 9)."""


class OrganizationChangeError(PostedChangeError):
    """То же, когда среди изменённых реквизитов — организация."""


def lock_products(product_ids):
    """Блокирует товары на время транзакции — в порядке возрастания id.

    Все операции, которые читают и меняют остаток, берут эту блокировку до
    чтения. Строка товара есть всегда, поэтому блокировка работает и тогда,
    когда записи остатка ещё нет. Единый порядок исключает взаимные блокировки
    при нескольких товарах и складах. FOR NO KEY UPDATE не мешает другим
    транзакциям ссылаться на товар (строки черновиков), но не пускает вторую
    складскую операцию по тому же товару.

    В SQLite блокировок строк нет: запись и так идёт по одной транзакции.
    """
    ids = sorted({int(pk) for pk in product_ids if pk})
    if not ids or not connection.features.has_select_for_update:
        return
    list(product_lock_query(ids, connection.features))


def product_lock_query(ids, features):
    """Запрос блокировки товаров: всегда по возрастанию id."""
    from apps.catalog.models import Product

    queryset = Product.objects.filter(pk__in=ids).order_by("pk")
    queryset = queryset.select_for_update(no_key=features.has_select_for_no_key_update)
    return queryset.values_list("pk", flat=True)


def document_pairs(doc_type, doc_id):
    """Ключи (товар, склад, организация), по которым у документа есть
    движения. Берутся из записанных движений: при смене организации
    проведённого документа это ключи прежней организации."""
    return set(
        StockMovement.objects.filter(doc_type=doc_type, doc_id=doc_id)
        .values_list("product_id", "warehouse_id", "organization_id")
    )


_EARLIEST = datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc)
ANY = object()   # история склада в целом — по всем организациям (только для диагностики перехода)


def _series(product_id, warehouse_id, organization_id=ANY, exclude_doc=None):
    """Остаток после каждой операции в хронологии: [(ключ документа, остаток)]
    — по ключу (товар, склад, организация); organization_id=ANY — по складу в
    целом.

    Порядок тот же, что у расчёта себестоимости (costing.ORDER): дата, время,
    момент создания документа, тип и id документа. Все движения одного
    документа по ключу — одна операция: остаток проверяется после неё целиком
    (несколько строк одного товара складываются).
    """
    movements = StockMovement.objects.filter(product_id=product_id, warehouse_id=warehouse_id)
    if organization_id is not ANY:
        movements = movements.filter(organization_id=organization_id)
    if exclude_doc:
        movements = movements.exclude(doc_type=exclude_doc[0], doc_id=exclude_doc[1])
    rows = [
        ((date, time, created or _EARLIEST, doc_type, doc_id), quantity)
        for date, time, created, doc_type, doc_id, quantity in
        movements.order_by(*costing.ORDER).values_list(*costing.DOC_ORDER, "quantity")
    ]
    rows.sort(key=lambda row: row[0])
    series, running = [], Decimal("0")
    for key, quantity in rows:
        running += quantity
        if series and series[-1][0] == key:
            series[-1] = (key, running)
        else:
            series.append((key, running))
    return series


def history_snapshot(pairs):
    """История остатков по ключам (товар, склад, организация) до изменения —
    для check_history."""
    return {pair: _series(*pair) for pair in pairs}


def _balance_at(series, key):
    """Остаток после последней операции с ключом не позже key."""
    index = bisect.bisect_right(series, key, key=lambda item: item[0])
    return series[index - 1][1] if index else Decimal("0")


def negative_allowed_organizations(organization_ids):
    """Организации из списка, которым разрешён минус (или он разрешён всем)."""
    from apps.core.models import Organization

    ids = {pk for pk in organization_ids if pk}
    if getattr(settings, "ALLOW_NEGATIVE_STOCK", False):
        return ids
    return set(Organization.objects.filter(pk__in=ids, allow_negative_stock=True).values_list("pk", flat=True))


def check_history(pairs, before, exclude_doc=None, allowed=()):
    """Запрещает операцию, если она создаёт или углубляет минус в любой точке
    хронологии — после любой операции, а не только на конец дня. Проверяются
    ключи (товар, склад, организация) организаций, которым минус запрещён;
    allowed — id организаций, которым он разрешён (их ключи не проверяются).

    Сравнивается вся история, а не только сегодняшний остаток: снять приёмку,
    товар из которой уже отгружен, нельзя, даже если позже был новый приход;
    отгрузить в 11:00 товар, принятый в 12:00 того же дня, тоже нельзя.

    Минус, который существовал и до операции, сам по себе её не блокирует:
    в каждой точке новый остаток сравнивается с прежним в той же точке, и
    ошибка возникает, только если он отрицательный и стал меньше прежнего.

    before — снимок history_snapshot до изменения. Для пар, которых в снимке
    нет (документ начал двигать новый товар или склад), прежняя история — это
    текущая без движений документа exclude_doc.
    """
    for pair in sorted(pairs, key=lambda key: (key[0], key[1], key[2] or 0)):
        if pair[2] in allowed:
            continue
        old = before[pair] if pair in before else _series(*pair, exclude_doc=exclude_doc)
        new = _series(*pair)
        for key in sorted({key for key, _ in old} | {key for key, _ in new}):
            balance = _balance_at(new, key)
            if balance < 0 and balance < _balance_at(old, key):
                raise StockError(_shortage_message(pair, key, balance))


def _shortage_message(pair, key, balance):
    from apps.catalog.models import Product
    from apps.core.models import Organization, Warehouse

    product = Product.objects.filter(pk=pair[0]).first()
    warehouse = Warehouse.objects.filter(pk=pair[1]).first()
    organization = Organization.objects.filter(pk=pair[2]).first() if len(pair) > 2 else None
    owner = (f" Остаток — организации «{organization}»: товар других организаций на этом складе в него "
             "не входит" if organization else "")
    return (
        f"Недостаточно товара «{product.name if product else pair[0]}» на складе "
        f"«{warehouse.name if warehouse else pair[1]}»: на {key[0]:%d.%m.%Y} {key[1]:%H:%M} "
        f"остаток стал бы {_plain(balance)}. Проверьте дату и время документа: расход не может "
        "стоять раньше прихода. Иначе сначала измените или отмените документы, которые расходуют этот товар."
        + owner
    )


def _plain(value):
    """4.000 → «4», 2.500 → «2.5»: количество в сообщениях без хвостовых нулей."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _q_cost(value):
    return costing.round_cost(value)


def current_balance(product_id, warehouse_id, organization_id=None):
    """Остаток организации на складе. Без организации — только если на складе
    по товару ровно одна организация (для проверок с одной организацией);
    несколько — ошибка: общий остаток разных организаций не используется."""
    rows = StockBalance.objects.filter(product_id=product_id, warehouse_id=warehouse_id)
    if organization_id is not None:
        return rows.filter(organization_id=organization_id).first()
    found = list(rows[:2])
    if len(found) > 1:
        raise ValueError("На складе остатки нескольких организаций — укажите организацию")
    return found[0] if found else None


def current_avg_cost(product_id, warehouse_id, organization_id=None):
    bal = current_balance(product_id, warehouse_id, organization_id)
    return bal.avg_cost if bal else Decimal("0")


def current_qty(product_id, warehouse_id, organization_id=None):
    bal = current_balance(product_id, warehouse_id, organization_id)
    return bal.quantity if bal else Decimal("0")


def recompute_balance(product_id, warehouse_id=None):
    """Пересчитывает остатки и себестоимость товара из движений (по всем складам:
    перемещения связывают склады, считать один склад отдельно нельзя)."""
    costing.recalculate([product_id])


def document_outflow(doc_type, doc_id):
    """Сколько документ сейчас расходует по каждому ключу (товар, склад, организация)."""
    outflow = defaultdict(Decimal)
    for product_id, warehouse_id, organization_id, quantity in (
        StockMovement.objects.filter(doc_type=doc_type, doc_id=doc_id, quantity__lt=0)
        .values_list("product_id", "warehouse_id", "organization_id", "quantity")
    ):
        outflow[(product_id, warehouse_id, organization_id)] += -quantity
    return outflow


def validate_stock(specs, allow_negative=None, previous=None):
    """Проверяет, что расходных остатков достаточно. Бросает StockError.

    previous — прежний расход этого же документа (document_outflow до очистки
    движений). Перепроведение, которое расходует не больше прежнего, проходит
    и при старом минусе: оно его не углубляет. Точки внутри истории проверяет
    check_history. Остаток — организации спецификации (organization_id):
    товар другой организации на том же складе недоступен.
    """
    if allow_negative is None:
        allow_negative = getattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    if allow_negative:
        return
    previous = previous or {}

    from apps.catalog.models import Product

    from apps.core.models import Organization

    need = defaultdict(Decimal)
    for spec in specs:
        if spec["quantity"] < 0:
            need[(spec["product_id"], spec["warehouse_id"], spec.get("organization_id"))] += -spec["quantity"]

    for (product_id, warehouse_id, organization_id), qty in need.items():
        available = (current_qty(product_id, warehouse_id, organization_id) if organization_id is not None
                     else Decimal("0"))
        if available < qty and qty > previous.get((product_id, warehouse_id, organization_id), 0):
            product = Product.objects.filter(pk=product_id).first()
            name = product.name if product else f"#{product_id}"
            organization = Organization.objects.filter(pk=organization_id).first()
            raise StockError(
                f"Недостаточно товара «{name}» на складе: доступно {_plain(available)}, требуется {_plain(qty)}"
                + (f" — остаток организации «{organization}»; товар других организаций на этом складе "
                   "в него не входит" if organization else "")
            )


@transaction.atomic
def create_movements(doc_type, doc_id, doc_number, date, specs, time=None, created_at=None,
                     also_products=(), organization_id=None):
    """Создаёт движения документа (предполагается, что старых нет) и пересчитывает
    себестоимость и остатки затронутых товаров.

    specs: список dict(product_id, warehouse_id, quantity: Decimal со знаком,
    cost: Decimal, line_id и organization_id — необязательно). organization_id —
    организация документа: товар принадлежит ей. time и created_at — время и
    момент создания документа: вместе с датой они задают место движений в
    хронологии.
    also_products — товары, которые тоже нужно пересчитать (например, убранные
    из документа при перепроведении). Возвращает результат costing.recalculate.
    """
    products = set(also_products)
    for spec in specs:
        StockMovement.objects.create(
            doc_type=doc_type, doc_id=doc_id, doc_number=doc_number, date=date,
            time=time or datetime.time(0, 0), doc_created_at=created_at or timezone.now(),
            line_id=spec.get("line_id"), organization_id=spec.get("organization_id", organization_id),
            product_id=spec["product_id"], warehouse_id=spec["warehouse_id"],
            quantity=spec["quantity"], cost=_q_cost(spec["cost"]),
        )
        products.add(spec["product_id"])
    return costing.recalculate(products)


@transaction.atomic
def clear_movements(doc_type, doc_id):
    """Удаляет движения документа и пересчитывает себестоимость и остатки
    затронутых товаров — в том числе последующие операции других документов.
    Возвращает результат costing.recalculate."""
    movements = StockMovement.objects.filter(doc_type=doc_type, doc_id=doc_id)
    products = set(movements.values_list("product_id", flat=True))
    movements.delete()
    return costing.recalculate(products)
