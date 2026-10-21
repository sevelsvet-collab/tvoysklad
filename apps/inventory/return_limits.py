"""Контроль возвратов по документу-основанию.

Возврат покупателя ссылается на отгрузку, возврат поставщику — на приёмку.
Правила (проверяются при проведении, отмене и удалении — в PostableMixin,
поэтому формы, кнопки, массовые действия и прямой вызов модели их не обходят):

1. По каждому товару сумма проведённых возвратов не превышает количество
   этого товара в проведённом основании. Строки одного товара складываются.
   Черновики возвратов доступное количество не расходуют.
2. При перепроведении возврата его прежнее количество в расчёт не входит.
3. Отмена и удаление возврата освобождают количество. Основание нельзя
   отменить, удалить или изменить так, чтобы товара в нём стало меньше уже
   возвращённого.
4. Организация и контрагент возврата совпадают с основанием. Склад может
   отличаться (возврат покупателя на другой склад).
5. Серийные номера возврата — только из основания и только те, что ещё не
   возвращены другим проведённым возвратом по нему.
6. Возврат стоит в хронологии после своего основания (дата, время, момент
   создания — тот же порядок, что у себестоимости и проверки наличия). Для
   возврата покупателя это проверяет расчёт себестоимости (costing), для
   возврата поставщику — этот модуль: общей проверки остатка мало, на складе
   может лежать товар из другой, более ранней приёмки.
7. Возврат без основания не проверяется и ни к чему не привязывается.

Старые нарушения (возвращено больше, чем было в основании — такие документы
могли появиться до этой проверки) не исправляются автоматически; их
показывает `manage.py check_returns`. Для них действует правило «не хуже»:

* операция, которая не увеличивает превышение, проходит — перепроведение
  возврата без изменений или с уменьшением количества, отмена и удаление
  возврата, перепроведение основания без уменьшения количества;
* операция, которая превышение создаёт или увеличивает, отклоняется.

«Прежнее» количество документа берётся из его складских движений (то, что
было проведено), а не из строк: форма меняет строки до перепроведения.
Услуги движений не имеют — для них проверка строгая, без послабления.

Конкуренция: проверка идёт под блокировкой товаров документа
(PostableMixin._lock → services.lock_products). Любая операция, меняющая
количество товара в возврате или в основании, держит блокировку этого товара,
поэтому два возврата последней единицы или возврат и одновременное уменьшение
основания выполняются по очереди, и второй видит результат первого.
"""
from collections import defaultdict
from decimal import Decimal

from django.apps import apps
from django.db.models import Q

from apps.core.constants import DOC_POSTED

RETURNS = {
    "customerreturn": {
        "basis_fk": "shipment", "partner": "customer", "partner_title": "покупатель",
        "title": "возврат покупателя", "basis_title": "отгрузка", "in_basis": "в отгрузке",
        "not_posted": "не проведена", "advice": "Укажите в возврате подходящую отгрузку или уберите основание",
        "all_returned": "По отгрузке {doc} уже возвращено всё количество — новый возврат не создан",
    },
    "supplierreturn": {
        "basis_fk": "receipt", "partner": "supplier", "partner_title": "поставщик",
        "title": "возврат поставщику", "basis_title": "приёмка", "in_basis": "в приёмке",
        "not_posted": "не проведена", "advice": "Укажите в возврате подходящую приёмку или уберите основание",
        "after_basis": True,   # хронологию «возврат после приёмки» проверяет этот модуль
        "all_returned": "По приёмке {doc} уже возвращено всё количество — новый возврат не создан",
    },
}
# основание → (приложение и модель возврата, её описание)
BASES = {
    "shipment": ("sales", "CustomerReturn"),
    "receipt": ("purchases", "SupplierReturn"),
}
ZERO = Decimal("0")
PREFIX = "На документ ссылается проведённый возврат"


def _error(message):
    from .services import StockError

    return StockError(message)


def _plain(value):
    from .services import _plain as plain

    return plain(value)


def _label(doc):
    return f"№ {doc.number} от {doc.date:%d.%m.%Y}"


def _product_name(product_id):
    product = apps.get_model("catalog", "Product").objects.filter(pk=product_id).first()
    return f"«{product.name}»" if product else f"#{product_id}"


def _services(product_ids):
    """Какие из товаров — услуги (у них нет складских движений)."""
    products = apps.get_model("catalog", "Product").objects.filter(pk__in=list(product_ids))
    return {p.pk for p in products if p.is_service}


def basis_id(doc):
    """id основания возврата; None — документ не возврат или основания нет."""
    config = RETURNS.get(doc._meta.model_name)
    return doc.__dict__.get(f"{config['basis_fk']}_id") if config else None


def line_quantities(doc):
    """Количество по товарам в строках документа (строки одного товара складываются)."""
    totals = defaultdict(lambda: ZERO)
    for product_id, quantity in doc.lines.values_list("product_id", "quantity"):
        totals[product_id] += quantity
    return totals


def posted_quantities(doc):
    """Количество по товарам, с которым документ сейчас проведён, — по его движениям."""
    from .models import StockMovement

    totals = defaultdict(lambda: ZERO)
    movements = StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
    for product_id, quantity in movements.values_list("product_id", "quantity"):
        totals[product_id] += abs(quantity)
    return totals


def _posted_line_quantities(doc):
    """Количество по строкам, с которым документ сейчас проведён, — по его движениям."""
    from .models import StockMovement

    totals = defaultdict(lambda: ZERO)
    movements = StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk, line_id__isnull=False)
    for line_id, quantity in movements.values_list("line_id", "quantity"):
        totals[line_id] += abs(quantity)
    return totals


def chronology_key(doc):
    """Место документа в общей хронологии — как costing.ORDER для движений."""
    return (doc.date, doc.time, doc.created_at, doc.DOC_TYPE, doc.pk)


def _moment(doc):
    return f"{doc.date:%d.%m.%Y} {doc.time:%H:%M}"


def posted_moment(doc):
    """Дата и время, с которыми документ сейчас проведён (по его движениям)."""
    from .models import StockMovement

    return (
        StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
        .values_list("date", "time").first()
    )


def _moved(doc):
    """Документ проводится впервые или с другой датой/временем, чем был проведён."""
    return posted_moment(doc) != (doc.date, doc.time)


def posted_returns(basis, exclude_pk=None):
    """Проведённые возвраты по основанию."""
    app_label, model_name = BASES[basis._meta.model_name]
    model = apps.get_model(app_label, model_name)
    fk = RETURNS[model._meta.model_name]["basis_fk"]
    returns = model.objects.filter(**{fk: basis}, status=DOC_POSTED)
    return returns.exclude(pk=exclude_pk) if exclude_pk else returns


def returned_quantities(basis, exclude_pk=None):
    """Сколько каждого товара возвращено по основанию проведёнными возвратами."""
    totals = defaultdict(lambda: ZERO)
    for doc in posted_returns(basis, exclude_pk):
        for product_id, quantity in line_quantities(doc).items():
            totals[product_id] += quantity
    return totals


def _serials_by_product(doc):
    from . import serials

    numbers = defaultdict(list)
    for line in doc.lines.select_related("product"):
        if line.product.track_serials:
            numbers[line.product_id].extend(serials.parse(line.serial_numbers))
    return numbers


def returned_serials(basis, exclude_pk=None):
    """Серийные номера, уже возвращённые по основанию: товар → {номер: документ}."""
    taken = defaultdict(dict)
    for doc in posted_returns(basis, exclude_pk):
        for product_id, numbers in _serials_by_product(doc).items():
            for number in numbers:
                taken[product_id][number] = doc
    return taken


# ---------------------------------------------------------------------------
# Проверки при проведении
# ---------------------------------------------------------------------------

def check_return(doc, previous, previous_basis_id):
    """Проведение возврата: основание годится, количество и номера доступны.

    previous — posted_quantities(doc) до изменения движений; previous_basis_id —
    основание, с которым возврат был загружен из базы. Послабление «не больше
    прежнего» действует, только если основание то же.
    """
    config = RETURNS.get(doc._meta.model_name)
    if config is None:
        return
    field = doc._meta.get_field(config["basis_fk"])
    basis_pk = getattr(doc, field.attname)
    if basis_pk is None:
        _check_no_links(doc, config)
        return
    # Основание читаем заново, уже под блокировкой товаров: объект, загруженный
    # вместе с возвратом раньше, мог устареть (основание успели отменить).
    basis = field.related_model.objects.filter(pk=basis_pk).first()
    if basis is None:
        return
    title = f"{config['title']} {_label(doc)}"
    basis_name = f"{config['basis_title']}-основание {_label(basis)}"

    if basis.status != DOC_POSTED:
        raise _error(f"{title}: {basis_name} {config['not_posted']}. {config['advice']}")
    if basis.organization_id != doc.organization_id:
        raise _error(
            f"{title}: организация возврата («{doc.organization}») не совпадает с организацией "
            f"документа-основания («{basis.organization}»). {config['advice']}"
        )
    partner = config["partner"]
    if getattr(basis, f"{partner}_id") != getattr(doc, f"{partner}_id"):
        raise _error(
            f"{title}: {config['partner_title']} возврата («{getattr(doc, partner)}») не совпадает "
            f"с документом-основанием («{getattr(basis, partner)}»). {config['advice']}"
        )

    same_basis = previous_basis_id == basis.pk
    if config.get("after_basis") and chronology_key(doc) <= chronology_key(basis):
        # старое нарушение не мешает перепровести возврат, который никуда не двигали
        if _moved(doc) or not same_basis:
            raise _error(
                f"{title}: {basis_name} стоит в хронологии не раньше возврата — "
                f"{config['basis_title']} от {_moment(basis)}, возврат от {_moment(doc)} "
                f"(сравниваются дата и время документов). {config['advice']}"
            )
    need = line_quantities(doc)
    in_basis = line_quantities(basis)
    others = returned_quantities(basis, exclude_pk=doc.pk)
    for product_id in sorted(need):
        quantity = need[product_id]
        total, already = in_basis.get(product_id, ZERO), others.get(product_id, ZERO)
        if quantity <= total - already:
            continue
        if same_basis and quantity <= previous.get(product_id, ZERO):
            continue        # старое превышение: возврат его не увеличивает
        name = _product_name(product_id)
        if total == 0:
            raise _error(f"{title}: {basis_name} не содержит товар {name}. {config['advice']}")
        raise _error(
            f"{title}, товар {name}: {config['in_basis']} {_label(basis)} — {_plain(total)}, "
            f"уже возвращено другими документами — {_plain(already)}, "
            f"доступно к возврату — {_plain(max(total - already, ZERO))}, "
            f"в этом возврате — {_plain(quantity)}"
        )
    _check_serials(doc, basis, config, title)
    if doc._meta.model_name == "customerreturn":
        _check_shipment_lines(doc, basis, title, same_basis, previous)


def linked_returned(shipment_line_ids, exclude_pk=None):
    """Сколько возвращено проведёнными возвратами с указанной строкой отгрузки."""
    model = apps.get_model("sales", "CustomerReturnLine")
    rows = model.objects.filter(shipment_line_id__in=list(shipment_line_ids), document__status=DOC_POSTED)
    if exclude_pk:
        rows = rows.exclude(document_id=exclude_pk)
    totals = defaultdict(lambda: ZERO)
    for line_id, quantity in rows.values_list("shipment_line_id", "quantity"):
        totals[line_id] += quantity
    return totals


def group_returns(shipment_id, product_id, document_id=None, line_pk=None):
    """Возвраты группы «отгрузка + товар»: (есть ли возврат без строки
    отгрузки, возвращено по каждой строке отгрузки).

    Учитываются проведённые возвраты других документов и строки самого
    документа document_id: указанные строки — все, кроме line_pk; строки без
    строки отгрузки — только раньше line_pk (их уже обработали по порядку).
    Серийный товар без указанной строки, распределяемый по номерам, — это
    возврат по строкам (costing), а не «по отгрузке в целом».
    """
    from . import serials

    model = apps.get_model("sales", "CustomerReturnLine")
    rows = model.objects.filter(document__shipment_id=shipment_id, product_id=product_id).filter(
        Q(document__status=DOC_POSTED) | Q(document_id=document_id)
    )
    tracked = apps.get_model("catalog", "Product").objects.filter(pk=product_id, track_serials=True).exists()
    candidates = list(apps.get_model("sales", "ShipmentLine").objects.filter(
        shipment_id=shipment_id, product_id=product_id).values_list("pk", "serial_numbers")) if tracked else []
    unlinked, taken = False, defaultdict(lambda: ZERO)
    for pk, doc_id, source_id, quantity, numbers in rows.values_list(
            "pk", "document_id", "shipment_line_id", "quantity", "serial_numbers"):
        own = document_id is not None and doc_id == document_id
        if own and pk == line_pk:
            continue
        if source_id is not None:
            taken[source_id] += quantity
            continue
        parts = serials.by_lines(numbers, quantity, candidates) if tracked else None
        if parts is not None:
            for key, count in parts.items():
                taken[key] += count
        elif not own or (line_pk is not None and pk < line_pk):
            unlinked = True
    return unlinked, taken


def only_shipment_line(shipment, line, document_id=None):
    """Строка отгрузки, из которой однозначно возвращается строка возврата,
    или None. Единое правило для проведения и миграции старых данных.

    - строка этого товара в отгрузке одна — она;
    - если по этой отгрузке и товару уже есть возврат без строки, группа
      считается «по отгрузке в целом» и строка не назначается: иначе в ней
      смешались бы два способа восстановления стоимости (costing:
      return_mixed);
    - серийный товар — номера возврата есть только в одной строке;
    - иначе — единственная строка, по которой ещё не всё возвращено, если в
      ней хватает количества.
    Ничего не угадывается: если подходит больше одной строки — None.
    """
    from . import serials

    candidates = list(shipment.lines.filter(product_id=line.product_id).order_by("pk"))
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    unlinked, taken = group_returns(shipment.pk, line.product_id, document_id, line.pk)
    if unlinked:
        return None
    numbers = set(serials.parse(line.serial_numbers)) if line.product.track_serials else set()
    if numbers:
        matching = [c for c in candidates if numbers <= set(serials.parse(c.serial_numbers))]
        return matching[0] if len(matching) == 1 else None
    free = [c for c in candidates if c.quantity > taken[c.pk]]
    if len(free) == 1 and free[0].quantity - taken[free[0].pk] >= line.quantity:
        return free[0]
    return None


def _check_no_links(doc, config):
    """Возврат без основания не ссылается на строки отгрузки: связь строки
    возврата покупателя с исходной строкой бывает только вместе с основанием в
    шапке (иначе себестоимость восстанавливалась бы по отгрузке, которой у
    возврата нет). save() снимает такие связи при смене основания; здесь —
    старые данные или прямая правка базы."""
    if doc._meta.model_name != "customerreturn":
        return
    linked = doc.lines.filter(shipment_line__isnull=False).select_related("product").first()
    if linked is not None:
        raise _error(
            f"{config['title']} {_label(doc)} без основания, а строка «{linked.product.name}» ссылается на строку "
            "отгрузки. Укажите отгрузку-основание или уберите связь строки"
        )


def _link_violations():
    """Проведённые возвраты покупателей, у которых строка ссылается на строку
    не своей отгрузки-основания (или основания нет вовсе)."""
    from django.db.models import F, Q

    line_model = apps.get_model("sales", "CustomerReturnLine")
    rows = (line_model.objects.filter(document__status=DOC_POSTED, shipment_line__isnull=False)
            .filter(Q(document__shipment__isnull=True) | ~Q(shipment_line__shipment_id=F("document__shipment_id")))
            .select_related("document", "document__shipment", "shipment_line__shipment", "product")
            .order_by("document_id", "pk"))
    return [{"kind": "line_link", "config": RETURNS["customerreturn"], "basis": row.document.shipment,
             "returns": [row.document], "line": row} for row in rows]


def _check_shipment_lines(doc, basis, title, same_basis, previous):
    """Строка отгрузки, указанная в строке возврата, — из этого основания, с тем
    же товаром, и по ней возвращено не больше, чем в ней было."""
    lines = list(doc.lines.select_related("shipment_line", "product").filter(shipment_line__isnull=False))
    if not lines:
        return
    need = defaultdict(lambda: ZERO)
    for line in lines:
        source = line.shipment_line
        if source.shipment_id != basis.pk or source.product_id != line.product_id:
            raise _error(
                f"{title}: строка возврата «{line.product.name}» указывает на строку другой отгрузки "
                "или другого товара. Выберите строку отгрузки-основания с этим товаром"
            )
        if line.product.track_serials and line.serial_numbers:
            from . import serials

            foreign = [n for n in serials.parse(line.serial_numbers) if n not in serials.parse(source.serial_numbers)]
            if foreign:
                raise _error(
                    f"{title}: номера {', '.join(foreign)} («{line.product.name}») не из выбранной строки "
                    "отгрузки. Выберите строку с этими номерами или оставьте определение автоматическим"
                )
        need[source.pk] += line.quantity
    others = linked_returned(need, exclude_pk=doc.pk)
    for source_id, quantity in need.items():
        source = next(line.shipment_line for line in lines if line.shipment_line_id == source_id)
        available = source.quantity - others.get(source_id, ZERO)
        if quantity <= available:
            continue
        if same_basis and quantity <= previous.get(source.product_id, ZERO):
            continue        # старое превышение: возврат его не увеличивает
        raise _error(
            f"{title}, товар {_product_name(source.product_id)}: в выбранной строке отгрузки "
            f"{_plain(source.quantity)}, уже возвращено по ней — {_plain(others.get(source_id, ZERO))}, "
            f"доступно — {_plain(max(available, ZERO))}, в этом возврате — {_plain(quantity)}"
        )


def _check_serials(doc, basis, config, title):
    mine = _serials_by_product(doc)
    if not mine:
        return
    in_basis = _serials_by_product(basis)
    taken = returned_serials(basis, exclude_pk=doc.pk)
    errors = []
    for product_id, numbers in mine.items():
        name = _product_name(product_id)
        allowed = set(in_basis.get(product_id, ()))
        for number in numbers:
            if number not in allowed:
                errors.append(f"{name}: номера {number} нет {config['in_basis']} {_label(basis)}")
            elif number in taken[product_id]:
                errors.append(
                    f"{name}: номер {number} уже возвращён документом {_label(taken[product_id][number])}"
                )
    if errors:
        more = f" …и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        raise _error(f"{title}. Серийные номера: " + "; ".join(errors[:5]) + more)


def check_basis(doc, previous, removing=False):
    """Проведение, отмена или удаление основания, по которому есть проведённые
    возвраты: товара не должно стать меньше уже возвращённого.

    previous — posted_quantities(doc) до изменения. Если количество товара
    не уменьшилось, старое превышение операцию не блокирует.
    """
    if doc._meta.model_name not in BASES:
        return
    returns = list(posted_returns(doc))
    if not returns:
        return
    config = RETURNS[returns[0]._meta.model_name]
    name = f"{config['basis_title']} {_label(doc)}"
    now = {} if removing else line_quantities(doc)
    taken = defaultdict(lambda: ZERO)
    by_product = defaultdict(list)
    for ret in returns:
        for product_id, quantity in line_quantities(ret).items():
            taken[product_id] += quantity
            by_product[product_id].append(ret)
    services = _services(taken)
    for product_id in sorted(taken):
        have = now.get(product_id, ZERO)
        if taken[product_id] <= have:
            continue
        if product_id not in services and have >= previous.get(product_id, ZERO):
            continue        # количество в основании не уменьшилось — превышение старое
        documents = ", ".join(sorted({_label(ret) for ret in by_product[product_id]}))
        action = "после операции в документе не останется" if have == 0 else f"в документе останется {_plain(have)}"
        raise _error(
            f"{PREFIX}. {name}, товар {_product_name(product_id)}: уже возвращено {_plain(taken[product_id])}, "
            f"{action}. Сначала отмените или уменьшите возвраты: {documents}"
        )
    if not removing and doc._meta.model_name == "shipment":
        quantities = dict(doc.lines.values_list("pk", "quantity"))
        posted_lines = _posted_line_quantities(doc)
        for source_id, quantity in linked_returned(quantities).items():
            have = quantities.get(source_id, ZERO)
            if quantity <= have or have >= posted_lines.get(source_id, ZERO):
                continue        # строка не уменьшилась — превышение по ней старое
            raise _error(
                f"{PREFIX}. {name}: по строке отгрузки уже возвращено {_plain(quantity)}, а в ней "
                f"останется {_plain(have)}. Сначала отмените или уменьшите возвраты по этой строке"
            )
    if removing:
        return
    if config.get("after_basis") and _moved(doc):
        for ret in returns:
            if chronology_key(ret) <= chronology_key(doc):
                raise _error(
                    f"{PREFIX}. {name}: документ нельзя поставить позже возврата по нему — "
                    f"{config['basis_title']} от {_moment(doc)}, {config['title']} {_label(ret)} от {_moment(ret)} "
                    "(сравниваются дата и время документов). Сначала отмените или перенесите возврат"
                )
    partner = config["partner"]
    for ret in returns:
        if ret.organization_id != doc.organization_id or getattr(ret, f"{partner}_id") != getattr(doc, f"{partner}_id"):
            raise _error(
                f"{PREFIX}. {name}: организация и {config['partner_title']} должны совпадать "
                f"с возвратом {_label(ret)}. Сначала отмените этот возврат"
            )


# ---------------------------------------------------------------------------
# Создание возврата «на основании»
# ---------------------------------------------------------------------------

def remaining_rows(basis, fields):
    """Строки для нового возврата: по каждой строке основания — то, что ещё
    не возвращено проведёнными возвратами. Цена, скидка и НДС сохраняются.

    Серийный товар: по конкретным номерам. В строке остаются те её номера,
    которые ещё не возвращены, количество — по их числу, цена, скидка и НДС —
    этой же строки. Общий счётчик количества к таким строкам не применяется:
    иначе возвращённый номер вычитался бы дважды — из своей строки и, по
    порядку, из чужой.

    Остальные товары (и строки серийного товара без номеров): возвращённое с
    указанием строки отгрузки списывается с этой строки, остальное
    возвращённое — со строк основания по порядку.

    Пустой список — возвращать больше нечего.
    """
    from . import serials

    lines = list(basis.lines.select_related("product").order_by("pk"))
    returned = returned_quantities(basis)
    taken_serials = returned_serials(basis)

    numbered = {}      # id строки → её номера (только серийный товар с номерами)
    for line in lines:
        numbers = serials.parse(line.serial_numbers) if line.product.track_serials else []
        if numbers:
            numbered[line.pk] = numbers
            # эти возвраты учтены по номерам — из общего счётчика их убираем
            matched = sum(1 for n in numbers if n in taken_serials[line.product_id])
            returned[line.product_id] -= matched

    is_shipment = basis._meta.model_name == "shipment"
    linked = linked_returned([line.pk for line in lines]) if is_shipment else {}
    # товары, по которым уже есть возврат без строки (а строк в отгрузке
    # несколько): группа «по отгрузке в целом» — новые строки тоже без ссылки,
    # иначе способы восстановления стоимости смешались бы (costing: return_mixed)
    per_product = defaultdict(int)
    for line in lines:
        per_product[line.product_id] += 1
    whole = {product_id for product_id, count in per_product.items()
             if is_shipment and count > 1 and group_returns(basis.pk, product_id)[0]}
    for line in lines:
        if line.pk not in numbered:
            returned[line.product_id] -= linked.get(line.pk, ZERO)   # остаётся возвращённое без строки

    rows = []
    for line in lines:
        values = {name: getattr(line, name) for name in fields}
        if line.pk in numbered:
            left = [n for n in numbered[line.pk] if n not in taken_serials[line.product_id]]
            quantity = Decimal(len(left))
            values["serial_numbers"] = "\n".join(left)
        else:
            own = min(line.quantity, linked.get(line.pk, ZERO))
            used = min(line.quantity - own, max(returned[line.product_id], ZERO))
            returned[line.product_id] -= used
            quantity = line.quantity - own - used
        if quantity <= 0:
            continue
        values["quantity"] = quantity
        if is_shipment and line.product_id not in whole:
            values["shipment_line"] = line       # возврат знает, из какой строки отгрузки товар
        rows.append(values)
    return rows


def all_returned_message(basis):
    app_label, model_name = BASES[basis._meta.model_name]
    return RETURNS[model_name.lower()]["all_returned"].format(doc=_label(basis))


# ---------------------------------------------------------------------------
# Диагностика существующих данных (только чтение)
# ---------------------------------------------------------------------------

def violations():
    """Старые нарушения: превышения количества и несовпадение сторон.

    Возвращает список dict(kind, basis, product_id, in_basis, returned, excess,
    returns); kind: quantity | party | chronology | line_link (строка возврата
    покупателя ссылается на строку не своей отгрузки-основания). Ничего не
    изменяет.
    """
    found = []
    for basis_model_name, (app_label, model_name) in BASES.items():
        return_model = apps.get_model(app_label, model_name)
        config = RETURNS[return_model._meta.model_name]
        fk, partner = config["basis_fk"], config["partner"]
        posted = return_model.objects.filter(status=DOC_POSTED, **{f"{fk}__isnull": False})
        basis_ids = sorted(set(posted.values_list(f"{fk}_id", flat=True)))
        basis_model = return_model._meta.get_field(fk).related_model
        for basis in basis_model.objects.filter(pk__in=basis_ids).order_by("pk"):
            returns = list(posted_returns(basis))
            in_basis = line_quantities(basis) if basis.status == DOC_POSTED else {}
            taken, by_product = defaultdict(lambda: ZERO), defaultdict(list)
            for ret in returns:
                for product_id, quantity in line_quantities(ret).items():
                    taken[product_id] += quantity
                    by_product[product_id].append(ret)
                if basis.status == DOC_POSTED and chronology_key(ret) <= chronology_key(basis):
                    found.append({
                        "kind": "chronology", "config": config, "basis": basis, "returns": [ret],
                    })
                same_partner = getattr(ret, f"{partner}_id") == getattr(basis, f"{partner}_id")
                if ret.organization_id != basis.organization_id or not same_partner:
                    found.append({
                        "kind": "party", "config": config, "basis": basis, "returns": [ret],
                    })
            for product_id in sorted(taken):
                total = in_basis.get(product_id, ZERO)
                if taken[product_id] > total:
                    found.append({
                        "kind": "quantity", "config": config, "basis": basis, "product_id": product_id,
                        "in_basis": total, "returned": taken[product_id],
                        "excess": taken[product_id] - total, "returns": by_product[product_id],
                    })
    return found + _link_violations()
