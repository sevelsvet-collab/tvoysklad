"""Переход складского учёта на ключ «организация + склад + товар».

Статус перехода — явный (StockLedgerState, одна строка): пустой склад —
нормальное состояние, поэтому отсутствие строк остатков готовностью не
считается. Пока статус не «готово»:

* складские операции (проведение, отмена, удаление документов, ввод номеров)
  отклоняются — require_ready();
* отчёты и подбор товара показывают предупреждение вместо остатков —
  is_ready().

«Готово» ставит только полный успешный пересчёт (apply): каждый товар — в
своей транзакции под блокировкой товара; прогресс пишется по ходу. Прерванный
пересчёт оставляет «выполняется» (не готово); повторный запуск проходит все
товары заново — результат тот же, потому что расчёт идёт с начала истории.

Перенос старых данных (миграции inventory 0007–0009):

* организация движения — из документа-источника; движения без источника
  остаются без организации — пересчёт не начнётся, пока человек не решит
  (assign_organization);
* остатки не делятся: их строит пересчёт истории по новым ключам;
* нарушения, которые создаёт разделение (организация с запретом минуса
  расходовала чужой товар), записываются (StockTransitionIssue) и
  блокируют операции с товаром до исправления (costing:
  transition_negative); перемещения чужого товара блокирует обычное правило
  transfer_deficit. Старыми допустимыми данными они не считаются.
"""
import bisect
import datetime
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SerialMovement, SerialNumber, StockLedgerState, StockMovement, StockTransitionIssue

READY = StockLedgerState.STATUS_READY
NOT_READY_MESSAGE = (
    "Складской учёт ещё не пересчитан по организациям после обновления программы — операции с товарами "
    "временно недоступны. Администратору: manage.py stock_transition (проверка), затем "
    "manage.py stock_transition --apply --i-have-a-backup"
)
EARLIEST = datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc)


def state():
    """Строка статуса без записи. Если её нет (таблица очищена), готовность
    определяется по пустой истории: без движений пересчитывать нечего."""
    row = StockLedgerState.objects.filter(pk=1).first()
    if row is None:
        row = StockLedgerState(pk=1, status=READY if not StockMovement.objects.exists()
                               else StockLedgerState.STATUS_PENDING)
    return row


def is_ready():
    return state().status == READY


def require_ready():
    """Перед складской операцией: статус «готово» обязателен. Если строки
    статуса нет, а история пуста (новая база), пересчитывать нечего — статус
    «готово» записывается сразу; при непустой истории без статуса — отказ."""
    from .services import StockError

    if StockLedgerState.objects.filter(pk=1).exists():
        if StockLedgerState.objects.filter(pk=1, status=READY).exists():
            return
        raise StockError(NOT_READY_MESSAGE)
    if StockMovement.objects.exists():
        raise StockError(NOT_READY_MESSAGE)
    StockLedgerState.objects.bulk_create(
        [StockLedgerState(pk=1, status=READY, finished_at=timezone.now(), message="Новая база: история пуста")],
        ignore_conflicts=True)


# ---------------------------------------------------------------------------
# Минус по организации против склада в целом
# ---------------------------------------------------------------------------

def strict_organizations(organization_ids):
    """{id организации: запрещён ли ей минус} — по текущим настройкам."""
    from apps.core.models import Organization

    allowed_everywhere = getattr(settings, "ALLOW_NEGATIVE_STOCK", False)
    flags = dict(Organization.objects.filter(pk__in=[pk for pk in organization_ids if pk]).values_list(
        "pk", "allow_negative_stock"))
    return {pk: not (allowed_everywhere or flags.get(pk, False)) for pk in organization_ids}


def point_key(date, time, created, doc_type, doc_id):
    return (date, time, created or EARLIEST, doc_type, int(doc_id))


def dump_series(series):
    return [[p[0].isoformat(), p[1].isoformat(), p[2].astimezone(datetime.timezone.utc).isoformat(), p[3], p[4],
             str(qty)] for p, qty in series]


def load_series(rows):
    return [((datetime.date.fromisoformat(d), datetime.time.fromisoformat(t), datetime.datetime.fromisoformat(c),
              doc_type, int(doc_id)), Decimal(qty)) for d, t, c, doc_type, doc_id, qty in rows]


def _at(series, point):
    index = bisect.bisect_right(series, point, key=lambda item: item[0])
    return series[index - 1][1] if index else Decimal("0")


def first_violation(own, whole):
    """Первая точка, где остаток организации отрицателен и меньше остатка
    склада в целом в той же точке (то есть организация ушла в минус за счёт
    чужого товара). own, whole — [(точка, остаток)] по возрастанию."""
    for point in sorted({p for p, _ in own} | {p for p, _ in whole}):
        mine = _at(own, point)
        if mine < 0 and mine < _at(whole, point):
            return point, mine, _at(whole, point)
    return None


def split_violations(product_id):
    """Нарушения, которые создаёт разделение истории товара по организациям:
    [(организация, склад, база — история склада в целом, точка, остаток
    организации, остаток склада)] — только для организаций с запретом минуса."""
    from .services import ANY, _series

    found = []
    pairs = defaultdict(set)
    for warehouse_id, organization_id in StockMovement.objects.filter(product_id=product_id).values_list(
            "warehouse_id", "organization_id").distinct():
        pairs[warehouse_id].add(organization_id)
    strict = strict_organizations({org for orgs in pairs.values() for org in orgs if org})
    for warehouse_id in sorted(pairs):
        whole = _series(product_id, warehouse_id, ANY)
        for organization_id in sorted(org for org in pairs[warehouse_id] if org):
            if not strict.get(organization_id):
                continue
            hit = first_violation(_series(product_id, warehouse_id, organization_id), whole)
            if hit:
                found.append((organization_id, warehouse_id, whole, *hit))
    return found


def violation_text(product_id, organization_id, warehouse_id, point, mine, whole):
    from apps.catalog.models import Product
    from apps.core.models import Organization, Warehouse

    from .costing import DOC_TITLES

    product = Product.objects.filter(pk=product_id).values_list("name", flat=True).first()
    organization = Organization.objects.filter(pk=organization_id).values_list("name", flat=True).first()
    warehouse = Warehouse.objects.filter(pk=warehouse_id).values_list("name", flat=True).first()
    doc = f"{DOC_TITLES.get(point[3], point[3])} id {point[4]} ({point[0]:%d.%m.%Y} {point[1]:%H:%M})"
    return (f"«{organization}», товар «{product}» на складе «{warehouse}»: после документа {doc} остаток "
            f"организации {_plain(mine)} при запрете минуса, а по складу в целом {_plain(whole)} — организация "
            "расходовала чужой товар. Исправьте документы (снимите с проведения или смените организацию — "
            "решение человека) или разрешите организации минус")


def _plain(value):
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# Источники и пересчёт
# ---------------------------------------------------------------------------

def unresolved_movements():
    """Складские движения без организации: {(вид, id документа): количество движений}."""
    found = defaultdict(int)
    for doc_type, doc_id in StockMovement.objects.filter(organization__isnull=True).values_list("doc_type", "doc_id"):
        found[(doc_type, doc_id)] += 1
    return dict(sorted(found.items()))


def organization_mismatches():
    """Документы, чьи движения — складские или серийных номеров — записаны с
    другой организацией, чем у самого документа: [(вид, id, организация
    документа, организации движений)]. Программа такого не допускает
    (posting, правило 9); расхождение — старые данные или прямая правка
    базы. Только чтение: организация движений при чтении не переписывается.
    Исправление — решение человека, fix_organization_mismatch()."""
    from django.apps import apps

    from .migrations_sources import STOCK_SOURCES

    sources = {doc_type: tuple(source) for doc_type, source in STOCK_SOURCES.items()}
    by_model_name = {name.lower(): (app, name) for app, name in sources.values()}
    keys = defaultdict(set)          # (приложение, модель, id) → организации движений
    for doc_type, doc_id, organization_id in StockMovement.objects.exclude(organization__isnull=True).values_list(
            "doc_type", "doc_id", "organization_id").distinct():
        if doc_type in sources:
            keys[(*sources[doc_type], doc_id)].add(organization_id)
    for doc_type, doc_id, organization_id in SerialMovement.objects.exclude(organization__isnull=True).values_list(
            "doc_type", "doc_id", "organization_id").distinct():
        if doc_type in by_model_name:
            keys[(*by_model_name[doc_type], doc_id)].add(organization_id)
    by_model = defaultdict(set)
    for app, name, doc_id in keys:
        by_model[(app, name)].add(doc_id)
    found = []
    for (app, name), ids in sorted(by_model.items()):
        for doc in apps.get_model(app, name)._base_manager.filter(pk__in=ids).order_by("pk"):
            movement_orgs = keys[(app, name, doc.pk)]
            if movement_orgs != {doc.organization_id}:
                found.append((doc.DOC_TYPE, doc.pk, doc.organization_id, sorted(movement_orgs)))
    return sorted(found)


def requisite_mismatches():
    """Документы, чьи движения — складские или серийных номеров — стоят не на
    том складе или не в тот момент, что сам документ: [(вид, id, [описания])].

    Склад движения сверяется с полем документа для его направления
    (movement_warehouse_field): у перемещения расход — склад-отправитель,
    приход — склад-получатель, у остальных документов — склад документа.
    Дата и время движения — дата и время документа. Программа расхождений не
    допускает (posting, правило 9): это старые данные или прямая правка базы.
    Только чтение, ничего не исправляет. Решение — человека: перепровести
    документ (движения примут реквизиты документа) или вернуть документу
    реквизиты его движений — в форме документа, с проверкой истории."""
    from django.apps import apps

    from .migrations_sources import STOCK_SOURCES
    from .posting import TITLES, show_value

    sources = {doc_type: tuple(source) for doc_type, source in STOCK_SOURCES.items()}
    by_model_name = {name.lower(): (app, name) for app, name in sources.values()}
    seen = defaultdict(set)          # (приложение, модель, id) → {(приход?, склад, дата, время)}
    for doc_type, doc_id, quantity, warehouse_id, date, time in StockMovement.objects.values_list(
            "doc_type", "doc_id", "quantity", "warehouse_id", "date", "time").distinct():
        if doc_type in sources:
            seen[(*sources[doc_type], doc_id)].add((quantity > 0, warehouse_id, date, time))
    for doc_type, doc_id, quantity, warehouse_id, date, time in SerialMovement.objects.values_list(
            "doc_type", "doc_id", "quantity", "warehouse_id", "date", "time").distinct():
        if doc_type in by_model_name:
            seen[(*by_model_name[doc_type], doc_id)].add((quantity > 0, warehouse_id, date, time))
    by_model = defaultdict(set)
    for app, name, doc_id in seen:
        by_model[(app, name)].add(doc_id)
    found = []
    for (app, name), ids in sorted(by_model.items()):
        model = apps.get_model(app, name)
        for doc in model._base_manager.filter(pk__in=ids).order_by("pk"):
            differ = defaultdict(set)    # поле документа → значения в движениях
            for incoming, warehouse_id, date, time in seen[(app, name, doc.pk)]:
                field = doc.movement_warehouse_field(incoming)
                if warehouse_id != getattr(doc, f"{field}_id"):
                    differ[field].add(warehouse_id)
                if date != doc.date:
                    differ["date"].add(date)
                if time != doc.time:
                    differ["time"].add(time)
            texts = []
            for field_name in doc.STOCK_FIELDS:
                if field_name in differ:
                    field = model._meta.get_field(field_name)
                    theirs = ", ".join(show_value(field, value) for value in sorted(differ[field_name]))
                    texts.append(f"{TITLES[field_name]}: в документе {show_value(field, getattr(doc, field.attname))}, "
                                 f"в движениях {theirs}")
            if texts:
                found.append((doc.DOC_TYPE, doc.pk, texts))
    return sorted(found)


def kind_mismatches():
    """Документы, вид которых не совпадает с видом их складских движений:
    списание с движениями оприходования и наоборот — [(вид документа, id,
    [виды движений])]. Программа такого не допускает (posting, правило 11):
    это старые данные или прямая правка базы. Только чтение. Перепроводить
    такой документ нельзя (движения прежнего вида не убрались бы — программа
    это отклоняет); решение — человека: вернуть документу вид его движений
    (единственная смена вида, допустимая у документа с движениями), затем при
    необходимости штатно отменить проведение и исправить черновик."""
    from django.apps import apps

    from .migrations_sources import STOCK_SOURCES

    types_of = defaultdict(list)
    for doc_type, source in STOCK_SOURCES.items():
        types_of[tuple(source)].append(doc_type)
    found = []
    for (app, name), types in sorted(types_of.items()):
        if len(types) < 2:
            continue
        kinds = defaultdict(set)
        for doc_type, doc_id in StockMovement.objects.filter(doc_type__in=types).values_list(
                "doc_type", "doc_id").distinct():
            kinds[doc_id].add(doc_type)
        for doc in apps.get_model(app, name)._base_manager.filter(pk__in=list(kinds)).order_by("pk"):
            if kinds[doc.pk] != {doc.DOC_TYPE}:
                found.append((doc.DOC_TYPE, doc.pk, sorted(kinds[doc.pk])))
    return found


# Направление движений по строкам документа (как в build_specs): приход +1,
# расход −1; у перемещения — оба. Строки услуг у отгрузки и возврата покупателя
# движений не дают.
LINE_SIGNS = {
    "receipt": (1,), "customer_return": (1,), "adjustment_income": (1,),
    "shipment": (-1,), "supplier_return": (-1,), "adjustment_expense": (-1,), "transfer": (-1, 1),
}
SERVICES_WITHOUT_MOVEMENTS = {"shipment", "customer_return"}


def line_mismatches():
    """Проведённые документы (и документы с движениями), у которых строки
    расходятся с движениями: [(вид, id, [описания])].

    * количество товара в строках ≠ количеству в движениях по каждому
      направлению (приход, расход) — строку добавили, убрали или изменили мимо
      перепроведения; check_costs сверяет каждое движение со строкой и не
      видит добавленную строку;
    * серийные номера строк (у товаров с учётом номеров) ≠ номерам в движениях.

    Программа таких расхождений не допускает (posting, правило 10): это
    старые данные или прямая правка базы. Только чтение, ничего не исправляет.
    Решение — человека: перепровести документ в форме (движения примут
    строки) или вернуть строки к проведённым — тоже в форме."""
    from django.apps import apps

    from . import serials
    from .migrations_sources import STOCK_SOURCES

    moved = defaultdict(lambda: defaultdict(Decimal))     # (вид, id) → {(товар, направление): количество}
    for doc_type, doc_id, product_id, quantity in StockMovement.objects.values_list(
            "doc_type", "doc_id", "product_id", "quantity"):
        moved[(doc_type, doc_id)][(product_id, 1 if quantity > 0 else -1)] += abs(quantity)
    numbered = defaultdict(set)                         # (модель, id) → {(товар, направление, номер)}
    for doc_type, doc_id, product_id, number, quantity in SerialMovement.objects.values_list(
            "doc_type", "doc_id", "serial__product_id", "serial__number", "quantity"):
        numbered[(doc_type, doc_id)].add((product_id, 1 if quantity > 0 else -1, number))
    names = {}

    def name(product):
        names.setdefault(product.pk, product.name)
        return f"«{product.name}»"

    found = []
    for app, model_name in sorted({tuple(source) for source in STOCK_SOURCES.values()}):
        model = apps.get_model(app, model_name)
        types = model.stock_doc_types()
        ids = set(model._base_manager.filter(status="posted").values_list("pk", flat=True))
        ids |= {doc_id for doc_type, doc_id in moved if doc_type in types}
        ids |= {doc_id for doc_type, doc_id in numbered if doc_type == model._meta.model_name}
        for doc in model._base_manager.filter(pk__in=ids).order_by("pk"):
            if any((kind, doc.pk) in moved for kind in types if kind != doc.DOC_TYPE):
                continue        # вид документа ≠ виду движений — это kind_mismatches(), не строки
            expected, expected_numbers, products = defaultdict(Decimal), set(), {}
            for line in doc.lines.select_related("product").order_by("pk"):
                products[line.product_id] = line.product
                if doc.DOC_TYPE in SERVICES_WITHOUT_MOVEMENTS and line.product.is_service:
                    continue
                for sign in LINE_SIGNS[doc.DOC_TYPE]:
                    expected[(line.product_id, sign)] += line.quantity
                    if line.product.track_serials:
                        expected_numbers |= {(line.product_id, sign, n) for n in serials.parse(line.serial_numbers)}
            actual = moved[(doc.DOC_TYPE, doc.pk)]
            texts = []
            for product_id, sign in sorted(set(expected) | set(actual)):
                have, posted = expected.get((product_id, sign), Decimal("0")), actual.get((product_id, sign),
                                                                                       Decimal("0"))
                if have != posted:
                    product = products.get(product_id) or apps.get_model("catalog", "Product").objects.get(
                        pk=product_id)
                    side = "приход" if sign > 0 else "расход"
                    texts.append(f"товар {name(product)}, {side}: в строках {_plain(have)}, в движениях {_plain(posted)}")
            actual_numbers = numbered[(doc._meta.model_name, doc.pk)]
            for product_id in sorted({key[0] for key in expected_numbers ^ actual_numbers}):
                product = products.get(product_id) or apps.get_model("catalog", "Product").objects.get(pk=product_id)
                ours = sorted({n for p, _, n in expected_numbers if p == product_id})
                theirs = sorted({n for p, _, n in actual_numbers if p == product_id})
                texts.append(f"номера {name(product)}: в строках {', '.join(ours) or 'нет'}, "
                             f"в движениях {', '.join(theirs) or 'нет'}")
            if texts:
                found.append((doc.DOC_TYPE, doc.pk, texts))
    return sorted(found)


FIX_DOCUMENT, FIX_MOVEMENTS = "document", "movements"


def fix_organization_mismatch(doc_type, doc_id, choice):
    """Решение человека по расхождению из organization_mismatches() — через
    тот же механизм, что форма документа (PostableMixin.editing): документ
    перепроводится, проверяются обе истории, возвраты и основания, номера;
    отказ — StockError, ничего не меняется.

    choice="document" — верна организация документа: движения, остатки и
    номера переходят к ней;
    choice="movements" — верна организация движений: она возвращается
    документу (change_organization).

    Возвращает организацию документа после исправления."""
    from django.apps import apps

    from .migrations_sources import STOCK_SOURCES

    match = next((row for row in organization_mismatches() if row[:2] == (doc_type, doc_id)), None)
    if match is None:
        raise TransitionError(f"У документа {doc_type}:{doc_id} нет расхождения организации с движениями")
    doc = apps.get_model(*STOCK_SOURCES[doc_type]).objects.get(pk=doc_id)
    if not doc.is_posted:
        raise TransitionError(f"{doc} не проведён, а движения есть: сначала снимите его с проведения или проведите")
    if choice == FIX_DOCUMENT:
        with doc.editing():          # перепроведение с организацией документа
            pass
    elif choice == FIX_MOVEMENTS:
        if len(match[3]) != 1:
            raise TransitionError(f"Движения {doc} записаны с несколькими организациями — выберите «{FIX_DOCUMENT}»")
        doc.change_organization(apps.get_model("core", "Organization").objects.get(pk=match[3][0]))
    else:
        raise TransitionError(f"Решение — «{FIX_DOCUMENT}» или «{FIX_MOVEMENTS}», а не «{choice}»")
    return doc.organization_id


def unresolved_serials():
    """Движения номеров без организации (ввод старого остатка, источник не найден)."""
    return list(SerialMovement.objects.filter(organization__isnull=True).select_related(
        "serial", "serial__product", "warehouse").order_by("serial__product_id", "serial__number", "id"))


@transaction.atomic
def assign_organization(doc_type, doc_id, organization_id):
    """Решение человека: движения документа без источника относятся к
    организации. Только для движений, у которых организации нет."""
    return StockMovement.objects.filter(doc_type=doc_type, doc_id=doc_id, organization__isnull=True).update(
        organization_id=organization_id)


@transaction.atomic
def assign_serial(product_id, number, organization_id):
    """Решение человека: чей номер, введённый остатком без документа."""
    from . import serials

    serial = SerialNumber.objects.get(product_id=product_id, number=number)
    count = serial.movements.filter(organization__isnull=True).update(organization_id=organization_id)
    serials.recompute(serial)
    return count


def products_to_recalculate():
    return sorted(set(StockMovement.objects.values_list("product_id", flat=True))
                  | set(SerialNumber.objects.values_list("product_id", flat=True)))


class TransitionError(Exception):
    pass


def apply(progress=None):
    """Пересчёт по новым ключам. Возвращает {товар: найденные проблемы}.

    progress — функция (номер, всего, товар) для вывода хода."""
    from . import costing, serials, services

    if is_ready() and StockLedgerState.objects.filter(pk=1).exists():
        raise TransitionError("Переход уже завершён: статус «готово». Повторный пересчёт не нужен")
    unresolved = unresolved_movements()
    if unresolved:
        raise TransitionError(
            f"Есть складские движения без организации ({len(unresolved)} документов) — источник не найден. "
            "Решите, к какой организации они относятся (--assign ВИД:ID=ОРГАНИЗАЦИЯ), и запустите снова")
    products = products_to_recalculate()
    StockLedgerState.objects.update_or_create(pk=1, defaults={
        "status": StockLedgerState.STATUS_RUNNING, "products_total": len(products), "products_done": 0,
        "started_at": timezone.now(), "finished_at": None,
        "message": "Пересчёт выполняется или прерван — запустите stock_transition --apply снова",
    })
    found = {}
    for index, product_id in enumerate(products, 1):
        with transaction.atomic():
            services.lock_products([product_id])
            StockTransitionIssue.objects.filter(product_id=product_id).delete()
            for organization_id, warehouse_id, whole, point, mine, total in split_violations(product_id):
                StockTransitionIssue.objects.create(
                    product_id=product_id, organization_id=organization_id, warehouse_id=warehouse_id,
                    baseline=dump_series(whole),
                    description=violation_text(product_id, organization_id, warehouse_id, point, mine, total))
            found[product_id] = costing.recalculate([product_id])
            for serial in SerialNumber.objects.filter(product_id=product_id):
                serials.recompute(serial)
            StockLedgerState.objects.filter(pk=1).update(products_done=index)
        if progress:
            progress(index, len(products), product_id)
    with transaction.atomic():
        StockLedgerState.objects.filter(pk=1).update(
            status=READY, finished_at=timezone.now(),
            message=f"Пересчитано товаров: {len(products)}. Нарушений после перехода: "
                    f"{StockTransitionIssue.objects.count()}")
    return found
