"""Расчётная модель ПРЕДЛАГАЕМОГО правила себестоимости при отрицательных
остатках — проверка предложения docs/negative-stock-proposal.md.

Это не движок программы: модель написана отдельно от apps/inventory и ничего
из него не импортирует. Ожидаемые числа посчитаны вручную и записаны
константами; модель обязана их получить. Запуск (без Django и без базы):

    python -X utf8 docs/proposals/negative_stock_model.py

Код 0 — все проверки сходятся; иначе AssertionError с описанием.

Правило:
* положительный остаток склада — средневзвешенный (количество, стоимость);
* расход берёт из положительного остатка долю стоимости (весь остаток — всю
  стоимость); недостающие единицы — ДЕФИЦИТ этой строки расхода, их
  себестоимость не определена;
* любое поступление на склад сначала покрывает дефициты в порядке расходов,
  доля суммы — нарастающим округлением до копейки по единицам поступления;
  остаток поступления — в положительный остаток;
* возврат по строке-основанию сначала гасит непокрытые единицы ЭТОЙ строки
  (сумма 0), затем восстанавливает долю её определённой себестоимости:
      база = отгружено − погашено возвратами непокрытых единиц,
      восстановить k-й раз = r(С × R_k / база) − r(С × R_(k−1) / база),
      С — определённая себестоимость строки, R — возвращено определённых единиц;
* ограничения итоговой истории — модель их ОБНАРУЖИВАЕТ (список violations),
  но нарушающую историю не оценивает:
      V1 — перемещение больше положительного остатка отправителя;
      V2 — возврат на другой склад, затрагивающий непокрытые единицы.

Два разных состояния:
* конечное — после обработки всей известной истории;
* на дату — state_on(дата): ОТДЕЛЬНОЕ проигрывание только операций с датой не
  позже границы (поздние операции на остатки на дату не влияют).
Проверка сохранения стоимости (check) сравнивает две независимо посчитанные
величины по каждому складу и в целом: потоки стоимости до даты (поступления,
части расходов из остатка, покрытия с датой не позже границы) и стоимость
остатка из state_on. Ожидаемые остатки на дату в сценариях — ручные константы.
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

D = Decimal
ZERO = D("0")
NO_MONEY = D("0.00")
CENT = D("0.01")


def money(value):
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def recorded(method):
    """Операция верхнего уровня записывается в журнал (для state_on);
    вложенные вызовы (перемещение = расход + поступление) — нет."""
    @wraps(method)
    def wrapper(self, date, *args, **kwargs):
        if self._depth == 0:
            assert not self.log or date >= self.log[-1][0], "операции подаются в хронологическом порядке"
            self.log.append((date, method.__name__, args, kwargs))
        self._depth += 1
        try:
            return method(self, date, *args, **kwargs)
        finally:
            self._depth -= 1
    return wrapper


class Line:
    """Строка расхода (отгрузки, перемещения): её себестоимость складывается
    из части, взятой из остатка, и покрытий дефицита поступлениями."""

    def __init__(self, key, warehouse, qty, date, kind):
        self.key, self.warehouse, self.qty, self.date, self.kind = key, warehouse, D(qty), date, kind
        self.from_stock = NO_MONEY
        self.covers = []               # (дата поступления, кол-во, сумма)
        self.open = ZERO               # непокрытые единицы
        self.cancelled = ZERO          # непокрытые, погашенные возвратами
        self.returned = ZERO           # возвращено определённых единиц

    @property
    def cost(self):
        return self.from_stock + sum((amount for _, _, amount in self.covers), NO_MONEY)


class Model:
    def __init__(self):
        self.stock = defaultdict(lambda: [ZERO, NO_MONEY])      # склад → [кол-во, стоимость] (положительный)
        self.deficits = defaultdict(list)                        # склад → [[строка, непокрыто]]
        self.lines = {}
        self.incoming = []      # (дата, склад, вид, сумма) — все поступления стоимости
        self.violations = []
        self.log = []
        self._depth = 0

    # ---------- операции ----------

    @recorded
    def receive(self, date, warehouse, qty, amount, kind="приход"):
        qty, amount = D(qty), D(amount)
        self.incoming.append((date, warehouse, kind, amount))
        queue = self.deficits[warehouse]
        cover = min(qty, sum((d[1] for d in queue), ZERO))
        done = ZERO
        while done < cover:
            entry = queue[0]
            take = min(entry[1], cover - done)
            share = money(amount * (done + take) / qty) - money(amount * done / qty)
            line = entry[0]
            line.covers.append((date, take, share))
            line.open -= take
            entry[1] -= take
            done += take
            if entry[1] == 0:
                queue.pop(0)
        stock = self.stock[warehouse]
        stock[0] += qty - cover
        stock[1] += amount - money(amount * cover / qty)

    @recorded
    def issue(self, date, warehouse, key, qty, kind="отгрузка"):
        line = Line(key, warehouse, qty, date, kind)
        self.lines[key] = line
        stock = self.stock[warehouse]
        take = min(line.qty, stock[0])
        if take > 0:
            line.from_stock = stock[1] if take == stock[0] else money(stock[1] * take / stock[0])
            stock[0] -= take
            stock[1] -= line.from_stock
        if line.qty > take:
            line.open = line.qty - take
            self.deficits[warehouse].append([line, line.open])
        return line

    @recorded
    def transfer(self, date, source, target, key, qty):
        available = self.stock[source][0]
        if D(qty) > available:
            self.violations.append(("V1", key, f"перемещение {qty} при остатке {available}"))
        line = self.issue(date, source, key, qty, kind="перемещение")
        self.receive(date, target, qty, line.cost, kind="перемещение")
        return line

    @recorded
    def return_line(self, date, warehouse, key, qty):
        """Возврат по конкретной строке-основанию. Возвращает восстановленную сумму."""
        line, qty = self.lines[key], D(qty)
        cancel = min(qty, line.open)
        if cancel and warehouse != line.warehouse:
            # V2: единицы физически пришли на другой склад, дыра осталась на складе
            # отгрузки. Модель такую историю не оценивает: только фиксирует нарушение
            # и учитывает количество (без стоимости), чтобы не искажать остатки.
            self.violations.append(("V2", key, f"возврат {cancel} непокрытых единиц на склад {warehouse}"))
            self.stock[warehouse][0] += qty
            return NO_MONEY
        if cancel:
            line.open -= cancel
            line.cancelled += cancel
            for entry in self.deficits[line.warehouse]:
                if entry[0] is line:
                    entry[1] -= cancel
            self.deficits[line.warehouse] = [e for e in self.deficits[line.warehouse] if e[1] > 0]
        rest = qty - cancel
        restored = NO_MONEY
        if rest:
            assert line.open == 0, "определённая часть восстанавливается только у полностью покрытой строки"
            base = line.qty - line.cancelled
            before = line.returned
            restored = money(line.cost * (before + rest) / base) - money(line.cost * before / base)
            line.returned += rest
            self.receive(date, warehouse, rest, restored, kind="возврат")
        return restored

    @recorded
    def return_document_level(self, date, warehouse, keys, qty):
        """Как в нынешней модели данных: возврат знает только документ и товар,
        не строку. Непокрытые единицы гасятся у первой подходящей строки."""
        qty, restored = D(qty), NO_MONEY
        for key in keys:
            take = min(qty, self.lines[key].open)
            if take:
                restored += self.return_line(date, warehouse, key, take)
                qty -= take
        for key in keys:
            line = self.lines[key]
            take = min(qty, line.qty - line.cancelled - line.returned)
            if take:
                restored += self.return_line(date, warehouse, key, take)
                qty -= take
        return restored

    # ---------- состояние ----------

    def warehouses(self):
        return set(self.stock) | set(self.deficits)

    def snapshot(self):
        """Конечное состояние: склад → (количество, стоимость, непокрыто)."""
        result = {}
        for warehouse in self.warehouses():
            uncovered = sum((d[1] for d in self.deficits[warehouse]), ZERO)
            qty, value = self.stock[warehouse]
            result[warehouse] = (qty - uncovered, value, uncovered)
        return result

    def state_on(self, date):
        """Состояние на дату: отдельное проигрывание только операций с датой
        не позже границы, в новой модели."""
        past = Model()
        for when, name, args, kwargs in self.log:
            if when <= date:
                getattr(past, name)(when, *args, **kwargs)
        return past.snapshot()

    def flows(self, until=None):
        """Потоки стоимости по складам до даты включительно: [поступления, расходы].
        Расход — часть строки из остатка (по дате строки) и покрытия (по дате
        покрывающего поступления)."""
        def in_time(date):
            return until is None or date <= until

        result = defaultdict(lambda: [NO_MONEY, NO_MONEY])
        for date, warehouse, _, amount in self.incoming:
            if in_time(date):
                result[warehouse][0] += amount
        for line in self.lines.values():
            if in_time(line.date):
                result[line.warehouse][1] += line.from_stock
            for date, _, amount in line.covers:
                if in_time(date):
                    result[line.warehouse][1] += amount
        return result

    def check(self, until=None):
        """Сохранение стоимости по каждому складу и в целом.

        Возвращает {склад: (поступления, расходы, стоимость остатка)} и строку
        «всего». Стоимость остатка берётся из конечного состояния или из
        state_on(until) — независимо от потоков."""
        state = self.snapshot() if until is None else self.state_on(until)
        flows = self.flows(until)
        result = {}
        for warehouse in sorted(set(state) | set(flows)):
            qty, value, uncovered = state.get(warehouse, (ZERO, NO_MONEY, ZERO))
            assert value >= 0, (warehouse, value)
            assert qty > 0 or value == 0, f"склад {warehouse}: стоимость {value} при количестве {qty}"
            assert uncovered == 0 or qty <= 0, f"склад {warehouse}: одновременно остаток и дефицит"
            assert qty >= 0 or uncovered == -qty, f"склад {warehouse}: −количество ≠ непокрыто"
            incoming, outgoing = flows.get(warehouse, (NO_MONEY, NO_MONEY))
            result[warehouse] = (incoming, outgoing, value)
        result["всего"] = tuple(sum((row[i] for row in result.values()), NO_MONEY) for i in range(3))
        return result


# --------------------------------------------------------------------------
# Сценарии. Ожидаемые числа посчитаны вручную (см. документ).
# --------------------------------------------------------------------------

RESULTS = []


def expect(name, actual, expected):
    assert actual == expected, f"{name}: получено {actual}, ожидалось {expected}"
    RESULTS.append(name)


def balanced(model, name, until=None):
    """Поступления − расходы = стоимость остатка: по каждому складу и в целом."""
    when = "конец" if until is None else f"на дату {until}"
    for warehouse, (incoming, outgoing, value) in model.check(until).items():
        expect(f"{name} {when}, {warehouse}: {incoming} − {outgoing} = {value}", incoming - outgoing, value)


def balanced_on_every_date(model, name):
    for date in sorted({entry[0] for entry in model.log}):
        balanced(model, name, until=date)
    balanced(model, name)


def scenario_a():
    m = Model()
    s = m.issue(1, "W", "S", 1)
    expect("А шаг 1: кол-во", m.snapshot()["W"], (D(-1), NO_MONEY, D(1)))
    m.receive(2, "W", 1, "250.00")                     # 1 шт. × 250,00 = 250,00
    expect("А: себестоимость отгрузки", s.cost, D("250.00"))
    expect("А: остаток", m.snapshot()["W"], (D(0), NO_MONEY, D(0)))
    balanced_on_every_date(m, "А")


def scenario_b():
    m = Model()
    s = m.issue(1, "W", "S", 2)
    m.receive(2, "W", 1, "250.00")                     # 1 × 250,00
    expect("Б шаг 2: себестоимость", (s.cost, s.open), (D("250.00"), D(1)))
    m.receive(3, "W", 1, "300.00")                     # 1 × 300,00
    expect("Б: себестоимость", (s.cost, s.open), (D("550.00"), D(0)))
    expect("Б: остаток", m.snapshot()["W"], (D(0), NO_MONEY, D(0)))
    balanced_on_every_date(m, "Б")


def scenario_c():
    """Исходный: приход 1 на 100,00 → расход 2 → приход 2 на 300,00 (по 150,00)."""
    m = Model()
    m.receive(1, "W", 1, "100.00")
    s = m.issue(2, "W", "S", 2)
    expect("В шаг 2", (s.cost, s.open, m.snapshot()["W"]), (D("100.00"), D(1), (D(-1), NO_MONEY, D(1))))
    m.receive(3, "W", 2, "300.00")
    expect("В: себестоимость отгрузки", s.cost, D("250.00"))
    expect("В: остаток", m.snapshot()["W"], (D(1), D("150.00"), D(0)))
    balanced_on_every_date(m, "В")


def scenario_c2():
    """Другой сценарий: приход 1 на 100,00 → расход 2 → приход 2 по 300,00 (на 600,00)."""
    m = Model()
    m.receive(1, "W", 1, "100.00")
    s = m.issue(2, "W", "S", 2)
    m.receive(3, "W", 2, "600.00")
    expect("В′: себестоимость отгрузки", s.cost, D("400.00"))
    expect("В′: остаток", m.snapshot()["W"], (D(1), D("300.00"), D(0)))
    balanced_on_every_date(m, "В′")


def scenario_g():
    m = Model()
    s1 = m.issue(1, "W", "S1", 1)
    s2 = m.issue(2, "W", "S2", 2)
    m.receive(3, "W", 2, "200.00")                     # 2 × 100,00
    expect("Г шаг 3", (s1.cost, s2.cost, s2.open, m.snapshot()["W"][0]), (D("100.00"), D("100.00"), D(1), D(-1)))
    m.receive(4, "W", 3, "1200.00")                    # 3 × 400,00
    expect("Г шаг 4", (s2.cost, m.snapshot()["W"]), (D("500.00"), (D(2), D("800.00"), D(0))))
    s3 = m.issue(5, "W", "S3", 1)
    expect("Г: отгрузки", (s1.cost, s2.cost, s3.cost), (D("100.00"), D("500.00"), D("400.00")))
    expect("Г: остаток", m.snapshot()["W"], (D(1), D("400.00"), D(0)))
    expect("Г: сумма себестоимости", s1.cost + s2.cost + s3.cost, D("1000.00"))
    balanced_on_every_date(m, "Г")


def scenario_returns_sequence():
    """Отгрузка 2 с пустого → возврат 1 → приход 1 на 250,00 → возврат 1."""
    m = Model()
    s = m.issue(1, "W", "S", 2)
    first = m.return_line(2, "W", "S", 1)
    expect("Р1 возврат 1: сумма, дефицит, склад", (first, s.open, m.snapshot()["W"]),
           (NO_MONEY, D(1), (D(-1), NO_MONEY, D(1))))
    m.receive(3, "W", 1, "250.00")
    expect("Р1 после прихода", (s.cost, s.open, m.snapshot()["W"][0]), (D("250.00"), D(0), D(0)))
    second = m.return_line(4, "W", "S", 1)
    # база = 2 − 1 = 1; деление на исходные 2 дало бы 125,00 — неверно
    expect("Р1 возврат 2: восстановлено", second, D("250.00"))
    expect("Р1: остаток", m.snapshot()["W"], (D(1), D("250.00"), D(0)))
    balanced_on_every_date(m, "Р1")


def scenario_returns_fractional():
    """Дробные количества, несколько частичных возвратов, округление."""
    m = Model()
    m.receive(1, "W", "1.000", "100.00")
    s = m.issue(2, "W", "S", "2.500")
    expect("Р2 отгрузка", (s.from_stock, s.open), (D("100.00"), D("1.500")))
    expect("Р2 возврат 0,4", m.return_line(3, "W", "S", "0.400"), NO_MONEY)
    expect("Р2 после возврата", (s.open, m.snapshot()["W"][0]), (D("1.100"), D("-1.100")))
    m.receive(4, "W", "3.000", "1000.00")              # 3 × 333,333… = 1000,00
    # покрыто 1,1: r(1000 × 1,1 / 3) = r(366,666…) = 366,67
    expect("Р2 себестоимость строки", s.cost, D("466.67"))
    expect("Р2 остаток после прихода", m.snapshot()["W"], (D("1.900"), D("633.33"), D(0)))
    # база = 2,5 − 0,4 = 2,1
    returns = [m.return_line(5 + i, "W", "S", q) for i, q in enumerate(("0.700", "0.800", "0.600"))]
    expect("Р2 возвраты по частям", returns, [D("155.56"), D("177.78"), D("133.33")])
    expect("Р2 сумма возвратов", sum(returns), D("466.67"))
    expect("Р2 остаток", m.snapshot()["W"], (D("4.000"), D("1100.00"), D(0)))
    expect("Р2 остатки на даты 5 и 6", (m.state_on(5)["W"], m.state_on(6)["W"]),
           ((D("2.600"), D("788.89"), D(0)), (D("3.400"), D("966.67"), D(0))))
    balanced_on_every_date(m, "Р2")


def scenario_two_lines():
    """Строка А обеспечена остатком, строка Б создаёт дефицит; возвраты по строкам."""
    m = Model()
    m.receive(1, "W", 2, "200.00")
    a = m.issue(2, "W", "А", 2)
    b = m.issue(2, "W", "Б", 3)
    expect("С1 строки", (a.cost, a.open, b.cost, b.open), (D("200.00"), D(0), NO_MONEY, D(3)))
    expect("С1 возврат 1 по строке Б", m.return_line(3, "W", "Б", 1), NO_MONEY)
    expect("С1 строка А не затронута", (a.cost, a.returned), (D("200.00"), D(0)))
    m.receive(4, "W", 2, "500.00")                     # 2 × 250,00
    expect("С1 Б покрыта", (b.cost, b.open), (D("500.00"), D(0)))
    expect("С1 возврат 1 по строке А", m.return_line(5, "W", "А", 1), D("100.00"))
    expect("С1 остаток", m.snapshot()["W"], (D(1), D("100.00"), D(0)))
    balanced_on_every_date(m, "С1")


def scenario_two_lines_return_of_covered_line():
    """Вернули единицу строки А, пока у строки Б дефицит: по строкам и без строк."""
    by_line = Model()
    by_line.receive(1, "W", 2, "200.00")
    a = by_line.issue(2, "W", "А", 2)
    b = by_line.issue(2, "W", "Б", 3)
    expect("С2 возврат А (по строке)", by_line.return_line(3, "W", "А", 1), D("100.00"))
    expect("С2 вернувшаяся единица покрыла Б", (b.cost, b.open), (D("100.00"), D(2)))
    by_line.receive(4, "W", 2, "500.00")
    expect("С2 по строкам: А, Б", (a.cost - D("100.00"), b.cost), (D("100.00"), D("600.00")))
    balanced_on_every_date(by_line, "С2 по строкам")

    by_doc = Model()
    by_doc.receive(1, "W", 2, "200.00")
    a2 = by_doc.issue(2, "W", "А", 2)
    b2 = by_doc.issue(2, "W", "Б", 3)
    expect("С2 возврат без строки", by_doc.return_document_level(3, "W", ["А", "Б"], 1), NO_MONEY)
    by_doc.receive(4, "W", 2, "500.00")
    # без связи со строкой погашен дефицит строки Б, хотя вернулась единица строки А
    expect("С2 без строк: А, Б", (a2.cost, b2.cost), (D("200.00"), D("500.00")))
    balanced_on_every_date(by_doc, "С2 без строк")


def scenario_history_return_cross_warehouse():
    """Возврат на другой склад был допустим; затем отменили покрывавшее поступление.
    Модель только ОБНАРУЖИВАЕТ нарушение итоговой истории."""
    def history(with_receipt):
        m = Model()
        m.issue(1, "W1", "S", 1)
        if with_receipt:
            m.receive(2, "W1", 1, "250.00")
        restored = m.return_line(3, "W2", "S", 1)
        return m, restored

    valid, restored = history(True)
    expect("И1 до отмены: нарушений нет, восстановлено", (valid.violations, restored), ([], D("250.00")))
    balanced_on_every_date(valid, "И1 до отмены")
    after, _ = history(False)
    expect("И1 после отмены прихода: модель обнаружила нарушение", [v[0] for v in after.violations], ["V2"])


def scenario_history_transfer():
    """Перемещение было обеспечено приходом; приход отменили, уменьшили, перенесли позже."""
    def history(receipt_qty, receipt_date):
        m = Model()
        if receipt_qty and receipt_date < 2:
            m.receive(receipt_date, "W1", receipt_qty, money(D("250.00") * D(receipt_qty)))
        m.transfer(2, "W1", "W2", "T", 1)
        if receipt_qty and receipt_date > 2:
            m.receive(receipt_date, "W1", receipt_qty, money(D("250.00") * D(receipt_qty)))
        return m

    valid = history(1, 1)
    expect("И2 исходно: нарушений нет", valid.violations, [])
    balanced_on_every_date(valid, "И2 исходно")
    for name, args in (("отмена прихода", (0, 1)), ("приход уменьшен до 0,5", ("0.5", 1)),
                       ("приход перенесён позже", (1, 3))):
        expect(f"И2 {name}: модель обнаружила нарушение", [v[0] for v in history(*args).violations], ["V1"])


def scenario_periods():
    """Отгрузка 30 января с пустого склада, поступление 2 февраля."""
    m = Model()
    s = m.issue(130, "W", "S", 1)                      # 130 — 30 января, 202 — 2 февраля
    m.receive(202, "W", 1, "250.00")
    expect("П: себестоимость отгрузки сейчас (по дате продажи — январь)", s.cost, D("250.00"))
    expect("П: остаток на 31 января", m.state_on(131)["W"], (D(-1), NO_MONEY, D(1)))
    expect("П: остаток сейчас", m.snapshot()["W"], (D(0), NO_MONEY, D(0)))
    balanced_on_every_date(m, "П")
    balanced(m, "П", until=131)


# ---------- остатки на дату: проверки, которые падали на прежней версии ----------

def scenario_state_on_date_1():
    """День 1: приход 1 на 100. День 2: расход 1."""
    m = Model()
    m.receive(1, "W", 1, "100.00")
    m.issue(2, "W", "S", 1)
    expect("Н1: на день 1 — 1 шт. на 100,00", m.state_on(1)["W"], (D(1), D("100.00"), D(0)))
    expect("Н1: конечный — 0", m.snapshot()["W"], (D(0), NO_MONEY, D(0)))
    balanced(m, "Н1", until=1)
    balanced(m, "Н1")


def scenario_state_on_date_2():
    """День 1: расход 1 с пустого. День 2: приход 2 на 500 (2 × 250,00)."""
    m = Model()
    s = m.issue(1, "W", "S", 1)
    m.receive(2, "W", 2, "500.00")
    expect("Н2: на день 1 — −1, стоимость 0, непокрыто 1", m.state_on(1)["W"], (D(-1), NO_MONEY, D(1)))
    expect("Н2: конечный — 1 шт. на 250,00", m.snapshot()["W"], (D(1), D("250.00"), D(0)))
    expect("Н2: себестоимость отгрузки", s.cost, D("250.00"))
    balanced(m, "Н2", until=1)
    balanced(m, "Н2")


def scenario_state_on_date_3():
    """Два склада. День 1: приход 3 × 100 = 300 на склад 1. День 2: перемещение 1
    на склад 2. День 3: отгрузка 2 со склада 2 (1 из остатка, 1 в дефицит).
    После даты 3: день 5 — перемещение 1 (покрывает дефицит склада 2),
    день 6 — возврат 1 по отгрузке на склад 1."""
    m = Model()
    m.receive(1, "W1", 3, "300.00")
    m.transfer(2, "W1", "W2", "T1", 1)
    s = m.issue(3, "W2", "S", 2)
    m.transfer(5, "W1", "W2", "T2", 1)
    restored = m.return_line(6, "W1", "S", 1)
    expect("Н3: нарушений нет", m.violations, [])
    expect("Н3: на день 3", m.state_on(3),
           {"W1": (D(2), D("200.00"), D(0)), "W2": (D(-1), NO_MONEY, D(1))})
    expect("Н3: на день 5", m.state_on(5),
           {"W1": (D(1), D("100.00"), D(0)), "W2": (D(0), NO_MONEY, D(0))})
    expect("Н3: конечный", m.snapshot(),
           {"W1": (D(2), D("200.00"), D(0)), "W2": (D(0), NO_MONEY, D(0))})
    expect("Н3: себестоимость отгрузки и возврат", (s.cost, restored), (D("200.00"), D("100.00")))
    for until in (3, 5, None):
        balanced(m, "Н3", until=until)


def control_per_warehouse_check():
    """Контроль самой проверки: намеренно неверная модель кладёт стоимость
    перемещения на склад-отправитель, а поступление записывает получателю.
    Итог «всего» при этом сходится — ошибку видит только проверка по складам."""
    class MisplacedTransfer(Model):
        def receive(self, date, warehouse, qty, amount, kind="приход"):
            super().receive(date, warehouse, qty, amount, kind)
            if kind == "перемещение":
                wrong = "W1" if warehouse == "W2" else "W2"
                self.stock[warehouse][1] -= D(amount)
                self.stock[wrong][1] += D(amount)

    m = MisplacedTransfer()
    m.receive(1, "W1", 3, "300.00")
    m.transfer(2, "W1", "W2", "T1", 1)
    rows = m.flows()
    incoming = sum((row[0] for row in rows.values()), NO_MONEY)
    outgoing = sum((row[1] for row in rows.values()), NO_MONEY)
    total_value = sum((v for _, v in m.stock.values()), NO_MONEY)
    expect("Контроль: итог «всего» у неверной модели сходится", incoming - outgoing, total_value)
    try:
        balanced(m, "Контроль")
    except AssertionError as exc:
        RESULTS.append(f"Контроль: проверка по складам поймала неверную модель ({str(exc)[:60]}…)")
    else:
        raise AssertionError("проверка по складам не заметила стоимость не на том складе")


SCENARIOS = (
    scenario_a, scenario_b, scenario_c, scenario_c2, scenario_g, scenario_returns_sequence,
    scenario_returns_fractional, scenario_two_lines, scenario_two_lines_return_of_covered_line,
    scenario_history_return_cross_warehouse, scenario_history_transfer, scenario_periods,
    scenario_state_on_date_1, scenario_state_on_date_2, scenario_state_on_date_3,
    control_per_warehouse_check,
)

if __name__ == "__main__":
    for scenario in SCENARIOS:
        scenario()
    print(f"Модель: проверок пройдено {len(RESULTS)}")
    for name in RESULTS:
        print("  ok", name)
