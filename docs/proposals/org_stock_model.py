"""Складской учёт по организациям: независимая модель (без Django).

Два режима ключа учёта:

* CURRENT — как программа сейчас: количество, стоимость и дефицит
  по «склад + товар»; организация документа на остаток не влияет;
* TARGET  — предложение: по «организация + склад + товар». Склад — место
  хранения, на нём лежат товары разных организаций.

Правила расчёта в обоих режимах одни и те же (docs/cost-recalculation.md,
costing.py): средневзвешенная по ключу; расход забирает долю стоимости
(весь остаток — всю стоимость); расход сверх остатка — дефицит, его
покрывают следующие поступления того же ключа по порядку расходов
(нарастающее округление); перемещение — расход и приход той же суммой,
организация перемещения у обеих сторон одна; возврат покупателя по
основанию сначала гасит непокрытые единицы строки, потом восстанавливает
долю её определённой себестоимости; без основания — по средней ключа.

Проверки при операции (как PostableMixin): если организации документа
минус запрещён, ни в одной точке хронологии остаток затронутого ключа не
может стать отрицательным и хуже прежнего; перемещение берёт только
положительный остаток своего ключа; серийный номер расходует и перемещает
только тот, у кого он лежит.

Запуск: python -X utf8 docs/proposals/org_stock_model.py — код 0, если все
ожидаемые числа (посчитаны вручную) сошлись.
"""
from collections import deque
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
ZERO = D("0")
CURRENT, TARGET = "склад + товар", "организация + склад + товар"


def money(value):
    return D(value).quantize(D("0.01"), rounding=ROUND_HALF_UP)


class Refused(Exception):
    """Операция отклонена; состояние книги не меняется."""


class Doc:
    def __init__(self, kind, org, wh, qty, amount=ZERO, wh_to=None, basis=None, serial=None, fallback=None):
        self.kind, self.org, self.wh, self.qty = kind, org, wh, D(qty)
        self.amount, self.wh_to, self.basis, self.serial = D(amount), wh_to, basis, serial
        self.fallback = fallback
        self.posted = False
        self.cost = ZERO          # расход: определённая себестоимость
        self.open = ZERO          # расход: не покрыто поступлениями
        self.cancelled = ZERO     # расход: непокрытые единицы, погашенные возвратом
        self.returned = ZERO      # расход: возвращено единиц с определённой себестоимостью
        self.in_amount = ZERO     # приход: сумма

    def __repr__(self):
        return f"{self.kind}({self.org}, {self.wh}, {self.qty})"


class _Stock:
    def __init__(self):
        self.qty, self.value, self.last, self.queue = ZERO, money(0), None, deque()

    def share(self, qty):
        if self.qty > 0:
            return money(self.value * qty / self.qty)
        if self.last:
            return money(self.last[0] * qty / self.last[1])
        return None

    def issue(self, doc, qty):
        available = self.qty if self.qty > 0 else ZERO
        take = min(qty, available)
        if take > 0:
            self.last = (self.value, self.qty)
            part = self.value if take == available else money(self.value * take / available)
            doc.cost += part
            self.value -= part
        self.qty -= qty
        if self.qty <= 0:
            self.value = money(0)
        doc.open += qty - take
        if qty - take:
            self.queue.append(doc)

    def receive(self, qty, amount):
        covered = ZERO
        cover = min(qty, -self.qty if self.qty < 0 else ZERO)
        while covered < cover:
            issue = self.queue[0]
            take = min(issue.open, cover - covered)
            issue.cost += money(amount * (covered + take) / qty) - money(amount * covered / qty)
            issue.open -= take
            covered += take
            if issue.open == 0:
                self.queue.popleft()
        self.qty += qty
        self.value += amount - money(amount * cover / qty)
        if self.qty > 0:
            self.last = (self.value, self.qty)


class Book:
    """Документы одного товара в порядке хронологии (порядок добавления)."""

    def __init__(self, mode, negative_allowed=None):
        self.mode = mode
        self.negative_allowed = dict(negative_allowed or {})
        self.docs = []

    def key(self, org, wh):
        return (org, wh) if self.mode == TARGET else wh

    # ---------- расчёт ----------

    def replay(self):
        """Проигрывает проведённые документы. Возвращает (остатки {ключ: (кол-во,
        стоимость)}, ряды {ключ: [кол-во после каждого документа]}, проблемы)."""
        stocks, series, problems = {}, {}, []
        for doc in self.docs:
            doc.cost = doc.open = doc.cancelled = doc.returned = ZERO
            doc.in_amount = ZERO
        for index, doc in enumerate(self.docs):
            if not doc.posted:
                continue
            key = self.key(doc.org, doc.wh)
            stock = stocks.setdefault(key, _Stock())
            if doc.kind in ("receipt", "income"):
                doc.in_amount = doc.amount
                stock.receive(doc.qty, doc.amount)
            elif doc.kind in ("ship", "supplier_return", "write_off"):
                stock.issue(doc, doc.qty)
            elif doc.kind == "transfer":
                if doc.qty > max(stock.qty, ZERO):
                    problems.append(f"перемещение {doc.org}: {doc.wh} → {doc.wh_to}: на ключе {key} "
                                    f"{max(stock.qty, ZERO):f}, а перемещается {doc.qty:f}")
                stock.issue(doc, doc.qty)
                to_key = self.key(doc.org, doc.wh_to)
                stocks.setdefault(to_key, _Stock()).receive(doc.qty, doc.cost)
                series.setdefault(to_key, {})[index] = stocks[to_key].qty
            elif doc.kind == "customer_return":
                doc.in_amount = self._customer_return(doc, stock)
            series.setdefault(key, {})[index] = stock.qty
        balances = {key: (stock.qty, stock.value) for key, stock in stocks.items()}
        return balances, series, problems

    def _customer_return(self, doc, stock):
        basis = doc.basis
        if basis is not None and basis.posted:
            cancel = min(doc.qty, basis.open)
            if cancel:
                basis.open -= cancel
                basis.cancelled += cancel
                stock.qty += cancel
                if basis.open == 0 and basis in stock.queue:
                    stock.queue.remove(basis)
            rest = doc.qty - cancel
            if not rest:
                return money(0)
            base = basis.qty - basis.cancelled
            before = basis.returned
            basis.returned = before + rest
            amount = money(basis.cost * basis.returned / base) - money(basis.cost * before / base)
            stock.receive(rest, amount)
            return amount
        amount = stock.share(doc.qty)
        if amount is None:
            amount = money(doc.qty * D(doc.fallback or 0))
        stock.receive(doc.qty, amount)
        return amount

    # ---------- операции с проверками ----------

    def _snapshot(self):
        _, series, _ = self.replay()
        return series

    def _check(self, before, keys, strict_for):
        balances, after, problems = self.replay()
        if problems:
            raise Refused(problems[0])
        for key in keys:
            if not strict_for(key):
                continue
            old, new = before.get(key, {}), after.get(key, {})
            points = sorted(set(old) | set(new))

            def at(series, point):
                known = [p for p in series if p <= point]
                return series[max(known)] if known else ZERO

            for point in points:
                if at(new, point) < 0 and at(new, point) < at(old, point):
                    raise Refused(f"на ключе {key} остаток стал бы {at(new, point):f}")
        return balances

    def _keys(self, doc, org=None):
        org = org or doc.org
        keys = {self.key(org, doc.wh)}
        if doc.wh_to:
            keys.add(self.key(org, doc.wh_to))
        return keys

    def _strict(self, doc_org):
        """Кому запрещён минус: сейчас — по организации документа операции
        (ключ общий); по предложению — по организации ключа."""
        if self.mode == TARGET:
            return lambda key: not self.negative_allowed.get(key[0], False)
        return lambda key: not self.negative_allowed.get(doc_org, False)

    def post(self, doc):
        if doc not in self.docs:
            self.docs.append(doc)
        before = self._snapshot()
        doc.posted = True
        try:
            if doc.basis is not None and doc.basis.org != doc.org:
                raise Refused("организация возврата не совпадает с организацией основания")
            self._check(before, self._keys(doc), self._strict(doc.org))
        except Refused:
            doc.posted = False
            self.replay()
            raise
        return doc

    def unpost(self, doc):
        before = self._snapshot()
        doc.posted = False
        try:
            self._check(before, self._keys(doc), self._strict(doc.org))
        except Refused:
            doc.posted = True
            self.replay()
            raise

    def change_org(self, doc, org):
        """Изменение организации проведённого документа (сохранение формы и
        перепроведение)."""
        old_org = doc.org
        before = self._snapshot()
        doc.org = org
        try:
            keys = self._keys(doc, old_org) | self._keys(doc, org)
            self._check(before, keys, self._strict(org))
        except Refused:
            doc.org = old_org
            self.replay()
            raise

    def balances(self):
        return self.replay()[0]


class Serials:
    """Где лежит серийный номер. Сейчас — склад; по предложению — организация + склад."""

    def __init__(self, mode):
        self.mode, self.where = mode, {}

    def _place(self, org, wh):
        return (org, wh) if self.mode == TARGET else wh

    def receive(self, number, org, wh):
        if self.where.get(number) is not None:
            raise Refused(f"номер {number} уже лежит: {self.where[number]}")
        self.where[number] = self._place(org, wh)

    def move(self, number, org, wh_from, wh_to):
        if self.where.get(number) != self._place(org, wh_from):
            raise Refused(f"номера {number} нет у {self._place(org, wh_from)} (лежит: {self.where.get(number)})")
        self.where[number] = self._place(org, wh_to)

    def ship(self, number, org, wh):
        if self.where.get(number) != self._place(org, wh):
            raise Refused(f"номера {number} нет у {self._place(org, wh)} (лежит: {self.where.get(number)})")
        self.where[number] = None


# ---------------------------------------------------------------------------
# Сценарии: ожидаемые числа посчитаны вручную для обоих режимов
# ---------------------------------------------------------------------------

RESULTS = []


def expect(name, actual, expected):
    assert actual == expected, f"{name}: получено {actual!r}, ожидалось {expected!r}"
    RESULTS.append(name)


def refused(action):
    try:
        action()
    except Refused as exc:
        return str(exc)
    return None


def q(value):
    return D(str(value))


def s1_strict_b_uses_a_stock():
    """А приняла 1 × 100 на W; у Б поступлений нет, минус запрещён; Б отгружает 1."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": False})
        book.post(Doc("receipt", "А", "W", 1, 100))
        ship_b = Doc("ship", "Б", "W", 1)
        reason = refused(lambda: book.post(ship_b))
        ship_a = Doc("ship", "А", "W", 1)
        reason_a = refused(lambda: book.post(ship_a))
        out[mode] = (reason is None, ship_b.cost if ship_b.posted else None, reason_a is None,
                     book.balances())
    # сейчас: Б отгружает товар А по 100, А свою единицу продать уже не может
    expect("1. сейчас: Б проводит отгрузку по 100, у А отгрузка отклонена",
           out[CURRENT][:3], (True, q("100.00"), False))
    expect("1. сейчас: остаток W 0 / 0", out[CURRENT][3], {"W": (q(0), q("0.00"))})
    # по предложению: Б отклонена, А продаёт свою единицу по 100
    expect("1. цель: Б отклонена, А отгружает", out[TARGET][:3], (False, None, True))
    expect("1. цель: остатки по ключам", out[TARGET][3], {("А", "W"): (q(0), q("0.00"))})


def s2_negative_b_deficit_not_covered_by_a():
    """А приняла 1 × 100; Б (минус разрешён) отгружает 1; затем Б принимает 1 × 80."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": True})
        book.post(Doc("receipt", "А", "W", 1, 100))
        ship_b = book.post(Doc("ship", "Б", "W", 1))
        after_ship = (ship_b.cost, ship_b.open, book.balances())
        book.post(Doc("receipt", "Б", "W", 1, 80))
        out[mode] = (after_ship, (ship_b.cost, ship_b.open, book.balances()))
    expect("2. сейчас: Б отгружает из остатка А по 100, дефицита нет",
           out[CURRENT][0], (q("100.00"), q(0), {"W": (q(0), q("0.00"))}))
    expect("2. сейчас: приход Б становится остатком W 1 / 80 (у А товара больше нет)",
           out[CURRENT][1], (q("100.00"), q(0), {"W": (q(1), q("80.00"))}))
    expect("2. цель: у Б дефицит 1, себестоимость предварительная 0; у А 1 / 100",
           out[TARGET][0], (q("0.00"), q(1), {("А", "W"): (q(1), q("100.00")), ("Б", "W"): (q(-1), q("0.00"))}))
    expect("2. цель: приход Б покрывает её дефицит по 80; у А по-прежнему 1 / 100",
           out[TARGET][1], (q("80.00"), q(0), {("А", "W"): (q(1), q("100.00")), ("Б", "W"): (q(0), q("0.00"))}))


def s3_different_prices_on_shared_warehouse():
    """А покупает 10 × 100, Б — 10 × 200 на W; А продаёт 5, Б продаёт 2."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": False})
        book.post(Doc("receipt", "А", "W", 10, 1000))
        book.post(Doc("receipt", "Б", "W", 10, 2000))
        ship_a = book.post(Doc("ship", "А", "W", 5))
        ship_b = book.post(Doc("ship", "Б", "W", 2))
        out[mode] = (ship_a.cost, ship_b.cost, book.balances())
    # сейчас: средняя 150 общая: 3000 × 5 / 20 = 750; 2250 × 2 / 15 = 300; остаток 13 / 1950
    expect("3. сейчас: общая средняя 150", out[CURRENT],
           (q("750.00"), q("300.00"), {"W": (q(13), q("1950.00"))}))
    # цель: у А 100, у Б 200; остатки 5 / 500 и 8 / 1600, вместе 13 / 2100
    expect("3. цель: средняя у каждой своя", out[TARGET],
           (q("500.00"), q("400.00"), {("А", "W"): (q(5), q("500.00")), ("Б", "W"): (q(8), q("1600.00"))}))


def s4_receipt_covers_only_own_deficit():
    """Минус разрешён обеим. А отгружает 2, Б отгружает 3 (нулевой остаток);
    Б принимает 3 × 100; затем А принимает 2 × 120."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": True, "Б": True})
        ship_a = book.post(Doc("ship", "А", "W", 2))
        ship_b = book.post(Doc("ship", "Б", "W", 3))
        book.post(Doc("receipt", "Б", "W", 3, 300))
        first = (ship_a.cost, ship_a.open, ship_b.cost, ship_b.open)
        book.post(Doc("receipt", "А", "W", 2, 240))
        out[mode] = (first, (ship_a.cost, ship_a.open, ship_b.cost, ship_b.open), book.balances())
    # сейчас: приход Б сначала покрывает более ранний дефицит А (2 × 100), потом 1 шт. своей
    expect("4. сейчас: приход Б покрыл дефицит А", out[CURRENT][0], (q("200.00"), q(0), q("100.00"), q(2)))
    expect("4. сейчас: приход А покрыл остаток дефицита Б", out[CURRENT][1],
           (q("200.00"), q(0), q("340.00"), q(0)))
    expect("4. цель: приход Б покрыл только её дефицит", out[TARGET][0], (q("0.00"), q(2), q("300.00"), q(0)))
    expect("4. цель: приход А покрыл её дефицит", out[TARGET][1], (q("240.00"), q(0), q("300.00"), q(0)))
    expect("4. цель: остатки нулевые по обоим ключам", out[TARGET][2],
           {("А", "W"): (q(0), q("0.00")), ("Б", "W"): (q(0), q("0.00"))})


def s5_returns():
    """А 10 × 100, Б 10 × 200 на W. А отгружает 5; покупатель возвращает А 2 по
    этой отгрузке; Б принимает возврат 1 без основания; Б возвращает
    поставщику 2. Возврат Б по отгрузке А — отклоняется в обоих режимах."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": False})
        book.post(Doc("receipt", "А", "W", 10, 1000))
        book.post(Doc("receipt", "Б", "W", 10, 2000))
        ship_a = book.post(Doc("ship", "А", "W", 5))
        back_a = book.post(Doc("customer_return", "А", "W", 2, basis=ship_a))
        back_b = book.post(Doc("customer_return", "Б", "W", 1))
        to_supplier = book.post(Doc("supplier_return", "Б", "W", 2))
        cross = refused(lambda: book.post(Doc("customer_return", "Б", "W", 1, basis=ship_a)))
        out[mode] = (ship_a.cost, back_a.in_amount, back_b.in_amount, to_supplier.cost, cross is not None,
                     book.balances())
    # сейчас: 750; 750 × 2 / 5 = 300; средняя W 2550 / 17 = 150; затем 2700 / 18 × 2 = 300
    expect("5. сейчас", out[CURRENT],
           (q("750.00"), q("300.00"), q("150.00"), q("300.00"), True, {"W": (q(16), q("2400.00"))}))
    # цель: 500; 500 × 2 / 5 = 200; средняя Б 200; возврат поставщику 2 × 200 = 400
    expect("5. цель", out[TARGET],
           (q("500.00"), q("200.00"), q("200.00"), q("400.00"), True,
            {("А", "W"): (q(7), q("700.00")), ("Б", "W"): (q(9), q("1800.00"))}))


def s6_transfer_keeps_organization():
    """А принимает 5 × 100 на W1. Б перемещает 5 с W1 на W2 и продаёт их там.
    А перемещает 3 с W1 на W2."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": False})
        book.post(Doc("receipt", "А", "W1", 5, 500))
        move_b = Doc("transfer", "Б", "W1", 5, wh_to="W2")
        reason = refused(lambda: book.post(move_b))
        ship_b = Doc("ship", "Б", "W2", 5)
        reason_ship = refused(lambda: book.post(ship_b))
        move_a = Doc("transfer", "А", "W1", 3, wh_to="W2")
        reason_a = refused(lambda: book.post(move_a))
        out[mode] = (reason is None, reason_ship is None, ship_b.cost if ship_b.posted else None,
                     reason_a is None, book.balances())
    # сейчас: Б перемещает и продаёт товар А (500); у А на W1 ничего, её перемещение отклонено
    expect("6. сейчас: товар А незаметно ушёл к Б", out[CURRENT],
           (True, True, q("500.00"), False, {"W1": (q(0), q("0.00")), "W2": (q(0), q("0.00"))}))
    # цель: перемещение Б и её отгрузка отклонены; А перемещает своё, организация сохраняется
    expect("6. цель", out[TARGET],
           (False, False, None, True, {("А", "W1"): (q(2), q("200.00")), ("А", "W2"): (q(3), q("300.00"))}))


def s7_cancel_receipt():
    """А принимает 1 × 100, Б (минус запрещён / разрешён) отгружает 1, А снимает
    свою приёмку."""
    out = {}
    for negative_b in (False, True):
        for mode in (CURRENT, TARGET):
            book = Book(mode, {"А": False, "Б": negative_b})
            receipt = book.post(Doc("receipt", "А", "W", 1, 100))
            ship_b = Doc("ship", "Б", "W", 1)
            shipped = refused(lambda: book.post(ship_b)) is None
            cancelled = refused(lambda: book.unpost(receipt)) is None
            out[(negative_b, mode)] = (shipped, cancelled)
    # сейчас: Б продала единицу А — А не может снять собственную приёмку
    expect("7. сейчас, минус Б запрещён", out[(False, CURRENT)], (True, False))
    expect("7. сейчас, минус Б разрешён", out[(True, CURRENT)], (True, False))
    # цель: при запрете отгрузка Б отклонена, А снимает приёмку; при разрешении у Б свой дефицит
    expect("7. цель, минус Б запрещён", out[(False, TARGET)], (False, True))
    expect("7. цель, минус Б разрешён", out[(True, TARGET)], (True, True))


def s8_change_organization_of_posted_document():
    """А принимает 1 × 100 и продаёт 1; затем у приёмки меняют организацию на Б
    (сохранение формы и перепроведение)."""
    out = {}
    for mode in (CURRENT, TARGET):
        book = Book(mode, {"А": False, "Б": False})
        receipt = book.post(Doc("receipt", "А", "W", 1, 100))
        ship_a = book.post(Doc("ship", "А", "W", 1))
        changed = refused(lambda: book.change_org(receipt, "Б")) is None
        out[mode] = (changed, ship_a.cost, book.balances())
    # сейчас: перепроведение проходит, ничего не меняется — у А продажа без поступления
    expect("8. сейчас: смена организации незаметна", out[CURRENT], (True, q("100.00"), {"W": (q(0), q("0.00"))}))
    # цель: отклонено — у А отгрузка осталась бы без товара
    expect("8. цель: отклонено", out[TARGET][:2], (False, q("100.00")))


def s9_serial_number():
    """А принимает номер SN-1 на W1; Б перемещает его на W2 и продаёт."""
    out = {}
    for mode in (CURRENT, TARGET):
        serials = Serials(mode)
        serials.receive("SN-1", "А", "W1")
        moved = refused(lambda: serials.move("SN-1", "Б", "W1", "W2")) is None
        shipped = refused(lambda: serials.ship("SN-1", "Б", "W2")) is None
        out[mode] = (moved, shipped, serials.where["SN-1"])
    expect("9. сейчас: номер А перемещён и продан Б", out[CURRENT], (True, True, None))
    expect("9. цель: перемещение и продажа Б отклонены, номер у А на W1", out[TARGET], (False, False, ("А", "W1")))


SCENARIOS = (s1_strict_b_uses_a_stock, s2_negative_b_deficit_not_covered_by_a, s3_different_prices_on_shared_warehouse,
             s4_receipt_covers_only_own_deficit, s5_returns, s6_transfer_keeps_organization, s7_cancel_receipt,
             s8_change_organization_of_posted_document, s9_serial_number)

if __name__ == "__main__":
    for scenario in SCENARIOS:
        scenario()
    print(f"Модель учёта по организациям: проверок пройдено {len(RESULTS)}")
    for name in RESULTS:
        print("  ok", name)
