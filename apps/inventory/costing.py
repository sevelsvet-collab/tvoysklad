"""Хронологический расчёт средневзвешенной себестоимости.

Единственное место, где считается себестоимость. Расчёт идёт по товару целиком
(по всем складам и организациям сразу), в одном хронологическом порядке, и
записывает согласованный результат в три места:

* StockMovement.amount / cost            — сумма движения и цена единицы;
* ShipmentLine.cost_amount / cost_price  — себестоимость строки отгрузки
                                           (её читает отчёт «Продажи и прибыль»);
* StockBalance.value / avg_cost          — стоимость остатка и средняя.

ДЕНЬГИ ХРАНЯТСЯ СУММАМИ. Учётная величина — сумма в копейках: стоимость
остатка (value) и сумма каждого движения (amount). Цена единицы (cost,
cost_price, avg_cost) — производная «сумма ÷ количество», округлённая для
показа; итоги из неё не вычисляются. Поэтому стоимость не теряется и не
возникает из округления цены:

    стоимость остатка = сумма приходов − сумма расходов   (копейка в копейку).

Порядок операций (ORDER) берётся из документа и не зависит от того, когда
документ проводили: дата, время, момент создания документа, затем тип и номер
записи документа и порядок его строк. Перепроведение создаёт движения заново,
но с тем же ключом, поэтому место документа в хронологии не меняется. Тот же
порядок использует проверка наличия (services.check_history).

Ключ учёта — «организация + склад» (по товару): склад — место хранения, на
нём лежат товары разных организаций, и у каждой свои количество, стоимость,
средняя и дефицит. Организация движения — организация документа. Всё ниже
«остаток», «склад», «дефицит» означает остаток этого ключа: поступление одной
организации никогда не покрывает дефицит другой, возврат без основания
оценивается только по истории своей организации, перемещение — документ одной
организации, расход и приход в её ключах.

Правила (метод прежний — средневзвешенная по ключу):

1. Приёмка и оприходование — сумма строки документа. Для приёмки это сумма
   со скидкой поставщика (ReceiptLine.total). НДС учитывается как раньше:
   цена берётся из документа как есть.
2. Расход (отгрузка, возврат поставщику, списание, расход перемещения) —
   доля стоимости остатка перед операцией:
       сумма = стоимость остатка × количество ÷ остаток, до копейки (половина — вверх).
   Расход, который забирает весь остаток, списывает всю оставшуюся стоимость:
   последний расход забирает остаток округления, и несколько частичных
   расходов в сумме списывают ровно столько же, сколько один полный.
3. Приход перемещения — та же сумма, что у парного расхода этого перемещения:
   между складами переносится стоимость целиком, цепочка складов её не теряет.
4. Возврат покупателя с отгрузкой-основанием относится к строке отгрузки
   (CustomerReturnLine.shipment_line; если строка товара в основании одна —
   она). Сначала он гасит НЕПОКРЫТЫЕ единицы этой строки (сумма 0: у них
   ещё нет стоимости, дефицит просто уменьшается), затем восстанавливает долю
   её определённой себестоимости нарастающим итогом:
       база = отгружено по строке − погашено возвратами непокрытых единиц,
       восстановить k-й раз = r(С × R_k ÷ база) − r(С × R_(k−1) ÷ база),
   где С — определённая себестоимость строки, R — возвращено определённых
   единиц с начала. Полный возврат (в один или несколько документов)
   восстанавливает ровно С. Основание должно быть проведено, стоять в
   хронологии раньше возврата и содержать этот товар.
   Серийный товар без указанной строки распределяется по номерам: каждый
   номер — в свою строку отгрузки (одна строка возврата может прийтись на
   несколько строк отгрузки).
   Если в основании несколько строк товара, а строка в возврате не указана
   (и не определяется однозначно), возврат считается «по отгрузке в целом»:
   С — определённая себестоимость всех этих строк, база — их количество.
   В одной группе «отгрузка + товар» все проведённые возвраты считаются
   одним способом: либо все по строкам, либо все по отгрузке в целом.
   Смешение (return_mixed) — проблема, требующая решения: иначе одни и те
   же единицы восстанавливали бы стоимость дважды.
5. Возврат покупателя без основания — по средней на складе возврата перед
   операцией; если остатка там нет — по последней известной средней этого
   склада; если её не было — по цене, зафиксированной в строке возврата при
   первом проведении (CustomerReturnLine.fallback_cost). Текущая цена
   карточки в пересчёте не участвует. Как любое поступление, он сначала
   покрывает дефицит склада.
6. Отрицательный остаток (когда организации он разрешён). Расход сверх
   положительного остатка берёт из остатка всю его стоимость, а недостающие
   единицы становятся ДЕФИЦИТОМ этой строки расхода: их себестоимость не
   определена (StockMovement.open_qty, ShipmentLine.cost_open_qty), в суммы
   не входит, и прибыль по такой отгрузке предварительная. Любое следующее
   поступление на этот склад сначала покрывает дефициты в порядке расходов;
   доля суммы поступления — нарастающим округлением по его единицам
   (r(S × покрыто ÷ поступило)), остаток поступления образует положительный
   остаток. Каждое покрытие записывается (DeficitCover). При количестве ≤ 0
   стоимость остатка ровно 0 — стоимости без количества не бывает.
7. Ограничения итоговой истории (нарушение — операция отклоняется целиком,
   PostableMixin; в старых данных — «ТРЕБУЕТ РЕШЕНИЯ» в диагностике):
   - перемещение не может забрать больше положительного остатка отправителя
     на момент перемещения, независимо от разрешения минуса (transfer_deficit);
   - возврат на другой склад не может затрагивать непокрытые единицы строки
     отгрузки (return_elsewhere);
   - возврат без указанной строки, когда в основании несколько строк товара
     и у какой-то из них есть непокрытые единицы, не допускается
     (return_ambiguous): строку нужно выбрать;
   - по одной отгрузке и товару с несколькими строками не может быть
     одновременно возвратов по строкам и возвратов без строки (return_mixed):
     в возвратах без строки её нужно указать.

Источник суммы — строка документа. Движение помнит свою строку (line_id).
Старые движения без этой ссылки сопоставляются со строкой, только если это
однозначно: в документе ровно одна свободная строка этого товара с таким
количеством и ровно одно такое движение. Иначе движение не угадывается:
его сумма остаётся прежней («заморожена»; если суммы не было — количество ×
записанная цена), строка документа не трогается, а диагностика показывает
документ, который нужно перепровести.

Расчёт всегда идёт с начала истории товара: результат зависит только от
документов, повторный запуск ничего не меняет. Вызывающий код держит
блокировку товара (services.lock_products) — другой операции по тому же
товару в это время нет.
"""
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps
from django.db import transaction

from apps.core.constants import DOC_POSTED, line_total

from .models import StockBalance, StockMovement

CENTS = Decimal("0.01")
ZERO = Decimal("0")
NO_MONEY = Decimal("0.00")

# Хронология: только поля, которые берутся из документа и переживают перепроведение.
ORDER = ("date", "time", "doc_created_at", "doc_type", "doc_id", "id")
DOC_ORDER = ORDER[:-1]   # место документа в хронологии (без порядка строк)

DOC_SHIPMENT = "shipment"
DOC_CUSTOMER_RETURN = "customer_return"
DOC_TRANSFER = "transfer"
DOC_RECEIPT = "receipt"
DOC_INCOME = "adjustment_income"

# Документы, чьи строки нужны расчёту: тип документа → (приложение, модель строки, ссылка на документ)
SOURCE_LINES = {
    DOC_SHIPMENT: ("sales", "ShipmentLine", "shipment_id"),
    DOC_CUSTOMER_RETURN: ("sales", "CustomerReturnLine", "document_id"),
    DOC_RECEIPT: ("purchases", "ReceiptLine", "receipt_id"),
    DOC_INCOME: ("inventory", "AdjustmentLine", "adjustment_id"),
}
DOC_TITLES = {
    DOC_SHIPMENT: "отгрузка", DOC_CUSTOMER_RETURN: "возврат покупателя", DOC_TRANSFER: "перемещение",
    DOC_RECEIPT: "приёмка", DOC_INCOME: "оприходование", "adjustment_expense": "списание",
    "supplier_return": "возврат поставщику",
}

FALLBACK_CARD = "card"       # цена карточки, зафиксированная при первом проведении
FALLBACK_LEGACY = "legacy"   # перенесена из старого движения: фактическая стоимость до фиксации

# Виды расхождений
FIXABLE = {"movement", "receipt_cost", "link", "shipment_line", "balance", "balance_extra", "amount_missing",
           "cover"}
# нужен человек: пересчёт товара не считается исправлением, а операции с товаром
# отклоняются, пока проблема не устранена (PostableMixin, правило «итоговая
# история товара корректна»)
BLOCKING = {"unmatched", "return_basis", "transfer_deficit", "return_elsewhere", "return_ambiguous",
            "return_mixed", "organization_missing", "transition_negative"}
NOTES = {"return_uncertain", "deficit", "return_unlinked"}   # замечания: значения верны, но стоит посмотреть


def money(value):
    """Сумма: до копейки, половина — вверх."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


round_cost = money   # цена единицы для показа округляется так же


def unit_cost(amount, quantity):
    """Цена единицы для показа. Итоги из неё не считаются — только из сумм."""
    return money(amount / abs(quantity)) if quantity else NO_MONEY


def implied_amount(quantity, cost):
    """Сумма, которая получалась раньше: количество × округлённая цена единицы."""
    return money(abs(quantity) * cost)


@dataclass
class Difference:
    """Одно расхождение между базой и расчётом либо проблема исходных данных."""

    kind: str
    product_id: int
    description: str
    stored: object = None
    expected: object = None
    docs: tuple = ()   # затронутые документы: ((тип, id), ...)


@dataclass
class Plan:
    """Каким должно быть состояние товара после пересчёта (без записи в базу)."""

    product_id: int
    movements: dict = field(default_factory=dict)         # id движения → (цена единицы, сумма, не покрыто)
    links: dict = field(default_factory=dict)             # id старого движения → id найденной строки
    lines: dict = field(default_factory=dict)             # id строки отгрузки → (строка, цена, сумма, не покрыто)
    balances: dict = field(default_factory=dict)          # (организация, склад) → (количество, стоимость, средняя)
    covers: list = field(default_factory=list)            # (расход, поступление, кол-во, сумма, дата)
    stored_movements: dict = field(default_factory=dict)  # id движения → само движение
    source_lines: dict = field(default_factory=dict)      # id движения → строка документа
    problems: list = field(default_factory=list)          # Difference видов BLOCKING и NOTES
    open_by_key: dict = field(default_factory=dict)       # (организация, склад) → непокрытое количество
    series: dict = field(default_factory=dict)            # (организация, склад) → [(точка документа, остаток)]
    tracked: bool = False                                  # серийный товар: возвраты распределяются по номерам
    # возврат погасил непокрытые единицы строки: (расход, возврат, количество).
    # Только для чтения (отчёт «Продажи и прибыль»): у этих единиц себестоимости
    # не было и не будет — в базу не пишется
    cancels: list = field(default_factory=list)


class _Issue:
    """Расход (строка отгрузки, перемещения, списания…): часть из остатка
    плюс покрытия дефицита поступлениями; непокрытое — open."""

    __slots__ = ("movement", "warehouse_id", "key", "qty", "from_stock", "cover_amount", "open", "cancelled",
                 "returned")

    def __init__(self, movement, qty):
        self.movement = movement
        self.warehouse_id = movement.warehouse_id
        self.key = _key(movement)
        self.qty = qty
        self.from_stock = NO_MONEY
        self.cover_amount = NO_MONEY
        self.open = ZERO          # не покрыто поступлениями
        self.cancelled = ZERO     # непокрытые единицы, погашенные возвратами
        self.returned = ZERO      # возвращено единиц с определённой себестоимостью

    @property
    def cost(self):
        """Определённая себестоимость."""
        return self.from_stock + self.cover_amount


def _key(movement):
    """Ключ учёта движения: (организация, склад)."""
    return (movement.organization_id, movement.warehouse_id)


def _point(movement):
    """Место документа в хронологии (как services._series)."""
    from .transition import point_key

    return point_key(movement.date, movement.time, movement.doc_created_at, movement.doc_type, movement.doc_id)


class _Stock:
    """Состояние одного ключа «организация + склад» по ходу пересчёта.

    qty — количество (меньше нуля — есть дефицит), value — стоимость
    положительного остатка в копейках, queue — расходы с непокрытыми единицами
    в порядке хронологии. Инвариант: qty < 0 ⇔ есть непокрытые единицы,
    и −qty = их сумме; при qty ≤ 0 стоимость 0.
    """

    __slots__ = ("qty", "value", "last", "queue")

    def __init__(self):
        self.qty = ZERO
        self.value = NO_MONEY
        self.last = None       # (стоимость, количество) последнего положительного остатка
        self.queue = deque()

    def average(self):
        return money(self.value / self.qty) if self.qty > 0 else None

    def share(self, quantity):
        """Стоимость такого количества по текущей (или последней известной)
        средней; None — средней никогда не было."""
        if self.qty > 0:
            return money(self.value * quantity / self.qty)
        if self.last:
            return money(self.last[0] * quantity / self.last[1])
        return None

    def issue(self, issue):
        """Расход: из положительного остатка — доля стоимости (весь остаток —
        вся стоимость); недостающее — дефицит этого расхода."""
        available = self.qty if self.qty > 0 else ZERO
        take = min(issue.qty, available)
        if take > 0:
            self.last = (self.value, self.qty)
            issue.from_stock = self.value if take == available else money(self.value * take / available)
            self.value -= issue.from_stock
        self.qty -= issue.qty
        if self.qty <= 0:
            self.value = NO_MONEY
        issue.open = issue.qty - take
        if issue.open:
            self.queue.append(issue)

    def receive(self, quantity, amount, cover_movement, plan):
        """Поступление: сначала покрывает дефицит по порядку расходов (доля
        суммы — нарастающим округлением по единицам поступления), остаток
        поступления — в положительный остаток."""
        covered = ZERO
        uncovered = -self.qty if self.qty < 0 else ZERO
        cover = min(quantity, uncovered)
        while covered < cover:
            issue = self.queue[0]
            take = min(issue.open, cover - covered)
            share = money(amount * (covered + take) / quantity) - money(amount * covered / quantity)
            issue.cover_amount += share
            issue.open -= take
            covered += take
            plan.covers.append((issue.movement.pk, cover_movement.pk, take, share, cover_movement.date))
            if issue.open == 0:
                self.queue.popleft()
        self.qty += quantity
        self.value += amount - money(amount * cover / quantity)
        if self.qty > 0:
            self.last = (self.value, self.qty)

    def cancel(self, issue, quantity):
        """Возврат непокрытых единиц на этот же склад: дыра закрывается самим
        вернувшимся товаром, стоимости у этих единиц нет."""
        issue.open -= quantity
        issue.cancelled += quantity
        self.qty += quantity
        if issue.open == 0:
            self.queue.remove(issue)


def build_plan(product_id, until=None):
    """Проигрывает историю товара и возвращает ожидаемое состояние.

    until — проиграть только документы с датой не позже этой (остатки на дату).
    """
    movements = StockMovement.objects.filter(product_id=product_id)
    if until is not None:
        movements = movements.filter(date__lte=until)
    movements = list(movements.order_by(*ORDER))
    plan = Plan(product_id=product_id)
    plan.stored_movements = {m.pk: m for m in movements}
    unmatched = _link_lines(plan, movements)

    stocks = defaultdict(_Stock)                  # (организация, склад) → состояние
    issues = {}                                   # id движения → расход
    transfer_out = defaultdict(deque)             # перемещение → расходы по порядку
    shipment_issues = defaultdict(list)           # отгрузка → её расходы (строки) по порядку
    by_shipment_line = {}                         # id строки отгрузки → расход
    aggregate_returned = defaultdict(lambda: ZERO)   # (отгрузка) → возвращено без указания строки
    return_basis = _return_basis(m.doc_id for m in movements if m.doc_type == DOC_CUSTOMER_RETURN)
    plan.tracked = _tracked(product_id)
    _check_return_groups(plan, movements, return_basis)

    missing = [m for m in movements if m.organization_id is None]
    if missing:
        docs = sorted({(m.doc_type, m.doc_id) for m in missing})
        plan.problems.append(Difference(
            "organization_missing", plan.product_id,
            f"движения без организации (документ-источник не найден при переносе): "
            f"{', '.join(f'{DOC_TITLES.get(t, t)} id {i}' for t, i in docs)}. Чья это операция, решает человек "
            "(manage.py stock_transition --assign)",
            docs=tuple(docs),
        ))

    def remember(key, point, qty):
        series = plan.series.setdefault(key, [])
        if series and series[-1][0] == point:
            series[-1] = (point, qty)
        else:
            series.append((point, qty))

    for m in movements:
        key = _key(m)
        stock = stocks[key]
        if m.quantity < 0:
            issue = _Issue(m, -m.quantity)
            if m.doc_type == DOC_TRANSFER and issue.qty > max(stock.qty, ZERO):
                owner = f" (остаток организации «{m.organization}»)" if m.organization_id else ""
                plan.problems.append(Difference(
                    "transfer_deficit", plan.product_id,
                    f"{_doc_label(m)}: перемещается {_qty(issue.qty)}, а на складе «{m.warehouse.name}» на этот "
                    f"момент {_qty(max(stock.qty, ZERO))}{owner}. Перемещать можно только то, что есть на складе у "
                    "своей организации",
                    docs=((m.doc_type, m.doc_id),),
                ))
            stock.issue(issue)
            issues[m.pk] = issue
            if m.doc_type == DOC_TRANSFER:
                transfer_out[m.doc_id].append(issue)
            elif m.doc_type == DOC_SHIPMENT:
                shipment_issues[m.doc_id].append(issue)
                line = plan.source_lines.get(m.pk)
                if line is not None and m.pk not in unmatched:
                    by_shipment_line[line.pk] = issue
        elif m.quantity == 0:
            pass
        elif m.doc_type == DOC_TRANSFER and transfer_out[m.doc_id]:
            source = transfer_out[m.doc_id].popleft()
            # та же сумма, что ушла со склада-отправителя (при V1 — только часть из остатка)
            stock.receive(m.quantity, source.cost, m, plan)
            plan.movements[m.pk] = (unit_cost(source.cost, m.quantity), source.cost, ZERO)
        elif m.doc_type == DOC_CUSTOMER_RETURN:
            amount = _customer_return(plan, m, stock, stocks, return_basis.get(m.doc_id), shipment_issues,
                                      by_shipment_line, aggregate_returned)
            plan.movements[m.pk] = (unit_cost(amount, m.quantity), amount, ZERO)
        else:
            if m.doc_type in (DOC_RECEIPT, DOC_INCOME) and m.pk in plan.source_lines:
                amount = _line_amount(m.doc_type, plan.source_lines[m.pk])
            else:
                amount = _stored_amount(m)          # нет строки-источника: оставляем как записано
            stock.receive(m.quantity, amount, m, plan)
            plan.movements[m.pk] = (unit_cost(amount, m.quantity), amount, ZERO)
        if m.quantity == 0:
            plan.movements[m.pk] = (NO_MONEY, NO_MONEY, ZERO)
        remember(key, _point(m), stock.qty)      # возврат и перемещение меняют только ключи своей организации

    # расходы: сумма — только определённая часть, непокрытое — отдельно
    for pk, issue in issues.items():
        determined = issue.qty - issue.open - issue.cancelled
        plan.movements[pk] = (unit_cost(issue.cost, determined) if determined > 0 else NO_MONEY,
                              issue.cost, issue.open)
        m = issue.movement
        if m.doc_type == DOC_SHIPMENT and pk not in unmatched and pk in plan.source_lines:
            line = plan.source_lines[pk]
            plan.lines[line.pk] = (line, plan.movements[pk][0], issue.cost, issue.open)
        if issue.open:
            plan.problems.append(Difference(
                "deficit", plan.product_id,
                f"{_doc_label(m)}: {_qty(issue.open)} шт. проданы (списаны) раньше поступления — себестоимость "
                "предварительная, уточнится после поступления",
                docs=((m.doc_type, m.doc_id),),
            ))

    for key, stock in stocks.items():
        plan.balances[key] = (stock.qty, stock.value, stock.average() or ZERO)
        plan.open_by_key[key] = sum((issue.open for issue in stock.queue), ZERO)
    _check_transition_issues(plan)
    return plan


def _check_transition_issues(plan):
    """Нарушения, созданные переходом на учёт по организациям
    (StockTransitionIssue): организация с запретом минуса ушла в минус за
    счёт чужого товара. Действуют, пока в истории её ключа есть точка, где
    остаток отрицателен и меньше остатка склада в целом на момент перехода."""
    from .models import StockTransitionIssue
    from .transition import first_violation, load_series, strict_organizations

    issues = list(StockTransitionIssue.objects.filter(product_id=plan.product_id))
    if not issues:
        return
    strict = strict_organizations({issue.organization_id for issue in issues})
    for issue in issues:
        if not strict.get(issue.organization_id):
            continue                                   # минус ей разрешили — это обычный дефицит
        hit = first_violation(plan.series.get((issue.organization_id, issue.warehouse_id), []),
                              load_series(issue.baseline))
        if hit:
            point = hit[0]
            plan.problems.append(Difference(
                "transition_negative", plan.product_id, issue.description,
                docs=((point[3], point[4]),),
            ))


def _check_return_groups(plan, movements, return_basis):
    """Один способ распределения на группу «отгрузка + товар».

    Если в отгрузке несколько строк товара, возврат без указанной строки
    восстанавливает долю себестоимости всех строк сразу, а возврат по строке —
    долю своей строки. Вместе они считали бы одни и те же единицы дважды
    (или ни разу): полный возврат восстановил бы больше или меньше
    себестоимости отгрузки. Поэтому смешение — проблема, требующая решения;
    уточнять нужно возвраты без строки. Правило структурное, от сумм не
    зависит: равная себестоимость строк сегодня не делает смешение безопасным
    после изменения истории.
    """
    lines_in_shipment = defaultdict(list)          # отгрузка → id её строк с этим товаром
    shipment_lines = defaultdict(list)             # отгрузка → её строки (для номеров)
    for m in movements:
        if m.doc_type == DOC_SHIPMENT and m.quantity < 0:
            lines_in_shipment[m.doc_id].append(m.line_id)
            if m.pk in plan.source_lines:
                shipment_lines[m.doc_id].append(plan.source_lines[m.pk])
    linked, unlinked = defaultdict(list), defaultdict(list)
    for m in movements:
        if m.doc_type != DOC_CUSTOMER_RETURN or m.quantity <= 0:
            continue
        basis_id = return_basis.get(m.doc_id)
        if not basis_id or len(lines_in_shipment[basis_id]) < 2:
            continue
        line = plan.source_lines.get(m.pk)
        by_line = line is not None and (
            line.shipment_line_id
            or _serial_parts(plan, line, m.quantity, [(s.pk, s) for s in shipment_lines[basis_id]]) is not None
        )
        (linked if by_line else unlinked)[basis_id].append((m, line))
    for basis_id in sorted(set(linked) & set(unlinked)):
        to_fix = []
        for m, line in unlinked[basis_id]:
            label = _doc_label(m) + (f" (строка возврата id {line.pk})" if line is not None else "")
            if label not in to_fix:
                to_fix.append(label)
        others = sorted({_doc_label(m) for m, _ in linked[basis_id]})
        choices = ", ".join(f"id {pk}" for pk in lines_in_shipment[basis_id] if pk)
        plan.problems.append(Difference(
            "return_mixed", plan.product_id,
            f"{'; '.join(to_fix)}: строка отгрузки не указана, а по этой же отгрузке (id {basis_id}) "
            f"есть возвраты по конкретным строкам: {'; '.join(others)}. Себестоимость одних и тех же "
            f"единиц восстановилась бы дважды. Укажите строку отгрузки в возврате без строки "
            f"(строки этой отгрузки с товаром: {choices})",
            docs=tuple((DOC_CUSTOMER_RETURN, m.doc_id) for m, _ in unlinked[basis_id])
            + tuple((DOC_CUSTOMER_RETURN, m.doc_id) for m, _ in linked[basis_id]) + ((DOC_SHIPMENT, basis_id),),
        ))


def _customer_return(plan, m, stock, stocks, basis_id, shipment_issues, by_shipment_line, aggregate_returned):
    """Возврат покупателя: по строке основания — сначала гасит её непокрытые
    единицы, затем восстанавливает долю определённой себестоимости; без
    основания — по оценке. Возвращает сумму движения."""
    line = plan.source_lines.get(m.pk)
    quantity = m.quantity
    if basis_id:
        candidates = [issue for issue in shipment_issues.get(basis_id, ())]
        target = None
        if line is not None and line.shipment_line_id:
            target = by_shipment_line.get(line.shipment_line_id)
            if target is not None and target not in candidates:
                target = None
            if target is None and candidates:
                plan.problems.append(Difference(
                    "return_basis", plan.product_id,
                    f"{_doc_label(m)}: строка отгрузки, указанная в возврате, не относится к отгрузке-основанию "
                    "с этим товаром. Укажите подходящую строку",
                    docs=((DOC_CUSTOMER_RETURN, m.doc_id), (DOC_SHIPMENT, basis_id)),
                ))
                candidates = []
            # нет строк-кандидатов — негодно само основание: о нём сообщение ниже
        elif len(candidates) == 1:
            target = candidates[0]
        elif line is not None and len(candidates) > 1:
            parts = _serial_parts(plan, line, quantity, [
                (index, plan.source_lines[issue.movement.pk]) for index, issue in enumerate(candidates)
                if issue.movement.pk in plan.source_lines
            ])
            if parts is not None:              # серийный товар: каждый номер — из своей строки
                return _return_to_issues(plan, m, stock, stocks,
                                         [(candidates[index], Decimal(count)) for index, count in parts.items()])
        if target is not None:
            return _return_to_issues(plan, m, stock, stocks, [(target, quantity)])
        if len(candidates) > 1:
            if any(issue.open for issue in candidates):
                plan.problems.append(Difference(
                    "return_ambiguous", plan.product_id,
                    f"{_doc_label(m)}: в отгрузке-основании несколько строк этого товара, у части из них "
                    "товар ещё не поступил. Укажите в возврате, из какой строки отгрузки возвращается товар",
                    docs=((DOC_CUSTOMER_RETURN, m.doc_id), (DOC_SHIPMENT, basis_id)),
                ))
            else:
                plan.problems.append(Difference(
                    "return_unlinked", plan.product_id,
                    f"{_doc_label(m)}: строка отгрузки не указана, в основании несколько строк этого товара — "
                    "себестоимость восстановлена по ним в целом. Укажите строку, если она известна",
                    docs=((DOC_CUSTOMER_RETURN, m.doc_id),),
                ))
            cost = sum((issue.cost for issue in candidates), NO_MONEY)
            base = sum((issue.qty - issue.cancelled for issue in candidates), ZERO)
            before = aggregate_returned[basis_id]
            aggregate_returned[basis_id] = before + quantity
            amount = money(cost * (before + quantity) / base) - money(cost * before / base) if base else NO_MONEY
            stock.receive(quantity, amount, m, plan)
            return amount
        plan.problems.append(Difference(
            "return_basis", plan.product_id, _basis_problem(m, basis_id),
            docs=((DOC_CUSTOMER_RETURN, m.doc_id), (DOC_SHIPMENT, basis_id)),
        ))
        # дальше — оценка «как без основания», чтобы расчёт не останавливался
    amount = _estimate(plan, m, stock, line)
    stock.receive(quantity, amount, m, plan)
    return amount


def _return_to_issues(plan, m, stock, stocks, parts):
    """Возврат по строкам отгрузки: parts — [(расход строки, количество)].
    По каждой строке сначала гасятся её непокрытые единицы, затем
    восстанавливается доля её определённой себестоимости нарастающим итогом
    (база — отгружено по строке минус погашенное)."""
    cancels = [min(quantity, issue.open) for issue, quantity in parts]
    for (issue, _), cancel in zip(parts, cancels):
        if cancel and _key(m) != issue.key:
            plan.problems.append(Difference(
                "return_elsewhere", plan.product_id,
                f"{_doc_label(m)}: возвращается {_qty(cancel)} шт., под которые товар на склад отгрузки ещё не "
                "поступал, — на другой склад. Такой возврат можно оформить на склад отгрузки или после "
                "поступления товара",
                docs=((DOC_CUSTOMER_RETURN, m.doc_id), (issue.movement.doc_type, issue.movement.doc_id)),
            ))
            stock.receive(m.quantity, NO_MONEY, m, plan)     # оценка нарушающей истории не записывается
            return NO_MONEY
    received, amount = ZERO, NO_MONEY
    for (issue, quantity), cancel in zip(parts, cancels):
        if cancel:
            stocks[issue.key].cancel(issue, cancel)
            plan.cancels.append((issue.movement.pk, m.pk, cancel))
        rest = quantity - cancel
        if not rest:
            continue
        base = issue.qty - issue.cancelled
        before = issue.returned
        issue.returned = before + rest
        amount += money(issue.cost * issue.returned / base) - money(issue.cost * before / base)
        received += rest
    if received:
        stock.receive(received, amount, m, plan)
    return amount


def _tracked(product_id):
    from apps.catalog.models import Product

    return Product.objects.filter(pk=product_id, track_serials=True).exists()


def _serial_parts(plan, line, quantity, candidates):
    """Серийный товар: {ключ строки отгрузки: количество} по номерам строки
    возврата (candidates — [(ключ, строка отгрузки)]) или None."""
    if not plan.tracked or not line.serial_numbers:
        return None
    from . import serials

    return serials.by_lines(line.serial_numbers, quantity, [(key, s.serial_numbers) for key, s in candidates])


def _estimate(plan, m, stock, line):
    """Оценка возврата без основания: средняя склада перед возвратом →
    последняя известная средняя → цена, зафиксированная в строке возврата."""
    amount = stock.share(m.quantity)
    if amount is not None:
        return amount
    if line is not None and line.fallback_cost is not None:
        if line.fallback_source != FALLBACK_CARD:
            plan.problems.append(Difference(
                "return_uncertain", plan.product_id,
                f"{_doc_label(m)}: возврат без истории остатков оценён по стоимости {line.fallback_cost:f}, "
                "перенесённой из старого движения; закупочная цена карточки на дату возврата не сохранилась",
                docs=((DOC_CUSTOMER_RETURN, m.doc_id),),
            ))
        return money(m.quantity * line.fallback_cost)
    plan.problems.append(Difference(
        "return_uncertain", plan.product_id,
        f"{_doc_label(m)}: исходная стоимость возврата без истории остатков не зафиксирована; "
        f"оставлена сумма движения {_stored_amount(m):f}",
        docs=((DOC_CUSTOMER_RETURN, m.doc_id),),
    ))
    return _stored_amount(m)


def state_on(product_id, date):
    """Остатки товара на дату — отдельным проигрыванием только документов с
    датой не позже этой: {(организация, склад): (количество, стоимость, не
    покрыто)}. Поздние документы на остаток на дату не влияют."""
    plan = build_plan(product_id, until=date)
    return {
        key: (qty, value, plan.open_by_key.get(key, ZERO))
        for key, (qty, value, _) in plan.balances.items()
    }


def _stored_amount(m):
    """Сумма движения, как она записана. У старых движений суммы нет — тогда
    это количество × записанная цена единицы (точнее восстановить нечем)."""
    return m.amount if m.amount is not None else implied_amount(m.quantity, m.cost)


def _line_amount(doc_type, line):
    """Сумма прихода по строке документа."""
    if doc_type == DOC_RECEIPT:
        return line_total(line.quantity, line.price, line.discount)   # со скидкой поставщика
    return line_total(line.quantity, line.price)


def _return_basis(return_ids):
    """Возврат покупателя → отгрузка-основание."""
    ids = set(return_ids)
    if not ids:
        return {}
    model = apps.get_model("sales", "CustomerReturn")
    return dict(
        model.objects.filter(pk__in=ids, shipment__isnull=False).values_list("pk", "shipment_id")
    )


def _basis_problem(m, shipment_id):
    """Почему отгрузка не годится основанием возврата — текст для пользователя."""
    shipment = apps.get_model("sales", "Shipment").objects.filter(pk=shipment_id).first()
    product = apps.get_model("catalog", "Product").objects.filter(pk=m.product_id).first()
    name = f"«{product.name}»" if product else f"#{m.product_id}"
    if shipment is None:
        reason, title = "не найдена", f"id {shipment_id}"
    else:
        title = f"№ {shipment.number} от {shipment.date:%d.%m.%Y} {shipment.time:%H:%M}"
        if shipment.status != DOC_POSTED:
            reason = "не проведена"
        elif not shipment.lines.filter(product_id=m.product_id).exists():
            reason = f"не содержит товар {name}"
        else:
            reason = "стоит в хронологии не раньше возврата (сравниваются дата и время документов)"
    return (
        f"{_doc_label(m)}, товар {name}: отгрузка-основание {title} {reason}. "
        "Укажите в возврате подходящую отгрузку или уберите основание"
    )


def _qty(value):
    """4.000 → «4», 2.500 → «2.5»."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _doc_label(m):
    title = DOC_TITLES.get(m.doc_type, m.doc_type)
    return f"{title} № {m.doc_number or m.doc_id} от {m.date:%d.%m.%Y} {m.time:%H:%M}"


def _link_lines(plan, movements):
    """Находит строку документа для каждого движения, которому она нужна.

    Заполняет plan.source_lines и plan.links (ссылки для старых движений).
    Возвращает id движений, для которых строка не найдена однозначно; о каждом
    таком документе в plan.problems появляется запись «unmatched».
    """
    groups = defaultdict(list)          # (тип, id документа) → движения
    for m in movements:
        if m.doc_type in SOURCE_LINES and m.quantity != 0:
            groups[(m.doc_type, m.doc_id)].append(m)

    lines = defaultdict(dict)           # (тип, id документа) → {id строки: строка}
    for doc_type in {key[0] for key in groups}:
        app_label, model_name, fk = SOURCE_LINES[doc_type]
        doc_ids = [key[1] for key in groups if key[0] == doc_type]
        queryset = apps.get_model(app_label, model_name).objects.filter(
            **{f"{fk}__in": doc_ids, "product_id": plan.product_id}
        ).order_by("pk")
        for line in queryset:
            lines[(doc_type, getattr(line, fk))][line.pk] = line

    unmatched = set()
    for key, items in groups.items():
        free = dict(lines.get(key, {}))
        failed = []
        for m in items:                 # сначала движения, которые помнят строку
            if not m.line_id:
                continue
            line = free.get(m.line_id)
            if line is not None and line.quantity == abs(m.quantity):
                plan.source_lines[m.pk] = free.pop(line.pk)
            else:
                failed.append(m)
        legacy = [m for m in items if not m.line_id]
        same_quantity = Counter(abs(m.quantity) for m in legacy)
        for m in legacy:                # старые — только однозначное совпадение
            candidates = [line for line in free.values() if line.quantity == abs(m.quantity)]
            if len(candidates) == 1 and same_quantity[abs(m.quantity)] == 1:
                line = free.pop(candidates[0].pk)
                plan.source_lines[m.pk] = line
                plan.links[m.pk] = line.pk
            else:
                failed.append(m)
        if failed and key[0] != DOC_CUSTOMER_RETURN:
            # (возврату строка нужна только ради зафиксированной стоимости —
            # если она понадобится и не найдётся, появится «return_uncertain»)
            unmatched.update(m.pk for m in failed)
            first = failed[0]
            ids = ", ".join(f"#{m.pk}" for m in failed)
            plan.problems.append(Difference(
                "unmatched", plan.product_id,
                f"{_doc_label(first)} (id {first.doc_id}): движения {ids} не сопоставляются однозначно "
                "со строками документа — их суммы оставлены как записаны, строки не изменены. "
                "Перепроведите документ в программе",
                docs=(key,),
            ))
    return unmatched


def differences(plan):
    """Сравнивает ожидаемое состояние с тем, что сейчас записано в базе.

    Сравниваются суммы, а не только цена единицы. Если точной суммы в базе
    ещё нет (данные до её появления), сравнение идёт с тем, что получалось
    раньше — количество × округлённая цена: совпало — «amount_missing»
    (сумму осталось записать), не совпало — настоящее расхождение.
    """
    found = []
    for pk, (cost, amount, open_qty) in plan.movements.items():
        m = plan.stored_movements[pk]
        if m.cost == cost and m.amount == amount and m.open_qty == open_qty:
            continue
        kind = _kind(m.amount, m.cost == cost and implied_amount(m.quantity, m.cost) == amount
                     and m.open_qty == open_qty, "movement")
        line = plan.source_lines.get(pk)
        description = f"{_doc_label(m)}, движение #{pk} (склад {m.warehouse_id}, кол-во {m.quantity:+f})"
        if m.doc_type == DOC_RECEIPT and line is not None and kind == "movement":
            kind = "receipt_cost"
            description = (
                f"{_doc_label(m)}, движение #{pk}: строка документа — кол-во {line.quantity:f}, "
                f"цена {line.price:f}, скидка {line.discount:f} %"
            )
        if m.open_qty or open_qty:
            found.append(Difference(
                kind, plan.product_id, description + ": (цена единицы, сумма, не покрыто)",
                (m.cost, _stored_amount(m), m.open_qty), (cost, amount, open_qty),
            ))
        else:
            found.append(Difference(
                kind, plan.product_id, description + ": (цена единицы, сумма)",
                (m.cost, _stored_amount(m)), (cost, amount),
            ))
    for pk, line_id in plan.links.items():
        found.append(Difference(
            "link", plan.product_id,
            f"{_doc_label(plan.stored_movements[pk])}, движение #{pk}: ссылка на строку документа",
            None, line_id,
        ))
    for pk, (line, cost, amount, open_qty) in plan.lines.items():
        if line.cost_price == cost and line.cost_amount == amount and line.cost_open_qty == open_qty:
            continue
        implied = implied_amount(line.quantity, line.cost_price)
        stored_amount = line.cost_amount if line.cost_amount is not None else implied
        with_open = bool(line.cost_open_qty or open_qty)
        found.append(Difference(
            _kind(line.cost_amount, line.cost_price == cost and implied == amount
                  and line.cost_open_qty == open_qty, "shipment_line"),
            plan.product_id,
            f"строка отгрузки #{pk} (отгрузка id {line.shipment_id}, кол-во {line.quantity:f}): "
            + ("(цена единицы, себестоимость строки, не определено)" if with_open
               else "(цена единицы, себестоимость строки)"),
            (line.cost_price, stored_amount, line.cost_open_qty) if with_open else (line.cost_price, stored_amount),
            (cost, amount, open_qty) if with_open else (cost, amount),
            docs=((DOC_SHIPMENT, line.shipment_id),),
        ))
    stored_covers = _stored_covers(plan.product_id)
    if stored_covers != _plan_covers(plan):
        found.append(Difference(
            "cover", plan.product_id, "записи покрытия дефицита поступлениями: (записано, по расчёту)",
            len(stored_covers), len(plan.covers),
        ))
    stored = {(b.organization_id, b.warehouse_id): b for b in StockBalance.objects.filter(product_id=plan.product_id)}
    for key, (qty, value, avg) in plan.balances.items():
        organization_id, warehouse_id = key
        if organization_id is None:
            continue                     # движения без организации — проблема organization_missing
        balance = stored.pop(key, None)
        if balance is None:
            found.append(Difference(
                "balance", plan.product_id,
                f"остаток на складе {warehouse_id}, организация {organization_id}: (количество, стоимость, средняя)",
                None, (qty, value, avg),
            ))
            continue
        if (balance.quantity, balance.value, balance.avg_cost) == (qty, value, avg):
            continue
        implied = implied_amount(balance.quantity, balance.avg_cost)
        same = (balance.quantity, balance.avg_cost) == (qty, avg) and implied == value
        found.append(Difference(
            _kind(balance.value, same, "balance"), plan.product_id,
            f"остаток на складе {warehouse_id}, организация {organization_id}: (количество, стоимость, средняя)",
            (balance.quantity, balance.value if balance.value is not None else implied, balance.avg_cost),
            (qty, value, avg),
        ))
    for (organization_id, warehouse_id), balance in stored.items():
        found.append(Difference(
            "balance_extra", plan.product_id,
            f"остаток на складе {warehouse_id}, организация {organization_id} без движений: (количество, средняя)",
            (balance.quantity, balance.avg_cost), None,
        ))
    return found + plan.problems


def _stored_covers(product_id):
    from .models import DeficitCover

    return Counter(
        (issue, cover, Decimal(qty), Decimal(amount), date)
        for issue, cover, qty, amount, date in
        DeficitCover.objects.filter(product_id=product_id).values_list("issue_id", "cover_id", "quantity",
                                                                       "amount", "date")
    )


def _plan_covers(plan):
    return Counter((issue, cover, Decimal(qty), Decimal(amount), date)
                   for issue, cover, qty, amount, date in plan.covers)


def _kind(stored_amount, implied_matches, kind):
    """Суммы в базе нет, а старый расчёт «количество × цена» даёт то же самое —
    это не ошибка в деньгах, сумму просто осталось записать."""
    return "amount_missing" if stored_amount is None and implied_matches else kind


_DEFER = ContextVar("defer_history_check", default=False)


@contextmanager
def defer_history_check():
    """Внутри блока PostableMixin не проверяет итоговую историю после каждой
    операции — так команда fix_stock_history снимает несколько документов
    сразу. Вызывающий обязан проверить историю сам (remaining_problems)
    в той же транзакции."""
    token = _DEFER.set(True)
    try:
        yield
    finally:
        _DEFER.reset(token)


def history_check_deferred():
    return _DEFER.get()


def remaining_problems(product_ids):
    """Проблемы, требующие решения, в итоговой истории товаров (по расчёту, без записи)."""
    found = []
    for product_id in sorted({int(pk) for pk in product_ids if pk}):
        found.extend(blocking(differences(build_plan(product_id))))
    return found


def fixable(found):
    """Расхождения, которые устраняет пересчёт."""
    return [d for d in found if d.kind in FIXABLE]


def blocking(found):
    """Проблемы, которые пересчёт не решает: нужно исправить документ."""
    return [d for d in found if d.kind in BLOCKING]


def notes(found):
    """Замечания: пересчёт их не меняет, но человеку стоит посмотреть."""
    return [d for d in found if d.kind in NOTES]


@transaction.atomic
def recalculate(product_ids):
    """Пересчитывает себестоимость и остатки товаров; пишет только изменившееся.

    Рабочий пересчёт при проведении документов. Движения, которые нельзя
    однозначно сопоставить со строкой, не меняются вместе со своими строками,
    так что движение и строка не расходятся. Возвращает найденные расхождения
    и проблемы. Вызывать под блокировкой товаров (services.lock_products).
    """
    result = []
    for product_id in sorted({int(pk) for pk in product_ids if pk}):
        plan = build_plan(product_id)
        found = differences(plan)
        if fixable(found):
            _apply(plan)
        result.extend(found)
    return result


@transaction.atomic
def repair(product_id):
    """Исправление существующих данных одного товара — всё или ничего.

    Если у товара есть проблема, требующая решения человека (BLOCKING),
    не записывается ничего: товар не объявляется исправленным наполовину.
    Возвращает (расхождения, применено ли).
    """
    plan = build_plan(product_id)
    found = differences(plan)
    if blocking(found):
        return found, False
    if fixable(found):
        _apply(plan)
    return found, True


def _apply(plan):
    from .models import DeficitCover

    for pk, (cost, amount, open_qty) in plan.movements.items():
        m = plan.stored_movements[pk]
        changes = {}
        if m.cost != cost:
            changes["cost"] = cost
        if m.amount != amount:
            changes["amount"] = amount
        if m.open_qty != open_qty:
            changes["open_qty"] = open_qty
        if pk in plan.links:
            changes["line_id"] = plan.links[pk]
        if changes:
            StockMovement.objects.filter(pk=pk).update(**changes)
    line_model = apps.get_model("sales", "ShipmentLine")
    for pk, (line, cost, amount, open_qty) in plan.lines.items():
        if line.cost_price != cost or line.cost_amount != amount or line.cost_open_qty != open_qty:
            line_model.objects.filter(pk=pk).update(cost_price=cost, cost_amount=amount, cost_open_qty=open_qty)
    if _stored_covers(plan.product_id) != _plan_covers(plan):
        DeficitCover.objects.filter(product_id=plan.product_id).delete()
        DeficitCover.objects.bulk_create([
            DeficitCover(product_id=plan.product_id, issue_id=issue, cover_id=cover, quantity=qty,
                         amount=amount, date=date)
            for issue, cover, qty, amount, date in plan.covers
        ])
    keep = []
    for (organization_id, warehouse_id), (qty, value, avg) in plan.balances.items():
        if organization_id is None:
            continue                     # без организации остаток не пишется (organization_missing)
        balance, _ = StockBalance.objects.update_or_create(
            organization_id=organization_id, product_id=plan.product_id, warehouse_id=warehouse_id,
            defaults={"quantity": qty, "value": value, "avg_cost": avg},
        )
        keep.append(balance.pk)
    StockBalance.objects.filter(product_id=plan.product_id).exclude(pk__in=keep).delete()
