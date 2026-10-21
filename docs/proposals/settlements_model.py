"""Взаиморасчёты: независимая модель с явным назначением платежей (без Django).

Пара — «наша организация + контрагент». Задолженности считаются по
проведённым документам:

    по продажам  (он должен нам)  = отгрузки − возвраты покупателя
                                    − «оплата от покупателя» + «возврат покупателю»
                                    ± корректировки стороны «продажи»
    по закупкам  (мы должны ему)  = приёмки − возвраты поставщику
                                    − «оплата поставщику» + «возврат от поставщика»
                                    ± корректировки стороны «закупки»

Деньги без назначения («не распределено») и корректировки без стороны
(все старые) в стороны не попадают: показываются отдельно, а пока они есть,
задолженности сторон — предварительные. Справочная разница по документам:

    разница = по продажам − по закупкам − не распределено получено
              + не распределено уплачено + корректировки без стороны

Для сравнения — снятое правило прошлой версии предложения (OLD_RULE):
деньги относились к стороне, по которой есть документы, а при обеих сторонах —
входящие к продажам, исходящие к закупкам.

Запуск: python -X utf8 docs/proposals/settlements_model.py (код 0 — все
ожидаемые числа сошлись). Ожидаемые числа посчитаны вручную.
"""
from decimal import Decimal

D = Decimal
ZERO = D("0")

IN, OUT = "in", "out"
CUSTOMER_PAYMENT = "оплата от покупателя"
CUSTOMER_REFUND = "возврат покупателю"
SUPPLIER_PAYMENT = "оплата поставщику"
SUPPLIER_REFUND = "возврат от поставщика"
UNALLOCATED = "не распределено"
ALLOWED = {
    IN: {CUSTOMER_PAYMENT, SUPPLIER_REFUND, UNALLOCATED},
    OUT: {SUPPLIER_PAYMENT, CUSTOMER_REFUND, UNALLOCATED},
}
THEY_OWE, WE_OWE = "он должен", "мы должны"
SALES, PURCHASES = "продажи", "закупки"


class Pair:
    """Документы одной пары «организация + контрагент»."""

    def __init__(self):
        self.docs = []

    def _add(self, **doc):
        doc["posted"] = True
        self.docs.append(doc)
        return doc

    def ship(self, amount):
        return self._add(kind="shipment", amount=D(amount))

    def customer_return(self, amount):
        return self._add(kind="customer_return", amount=D(amount))

    def receive(self, amount):
        return self._add(kind="receipt", amount=D(amount))

    def supplier_return(self, amount):
        return self._add(kind="supplier_return", amount=D(amount))

    def pay(self, direction, amount, purpose=UNALLOCATED):
        assert purpose in ALLOWED[direction], f"назначение «{purpose}» не подходит к направлению {direction}"
        return self._add(kind="payment", direction=direction, amount=D(amount), purpose=purpose)

    def correct(self, direction, amount, side=None):
        return self._add(kind="correction", direction=direction, amount=D(amount), side=side)

    @staticmethod
    def cancel(doc):
        doc["posted"] = False

    def _posted(self, kind):
        return [doc for doc in self.docs if doc["posted"] and doc["kind"] == kind]

    def _sum(self, kind, **match):
        return sum((d["amount"] for d in self._posted(kind) if all(d.get(k) == v for k, v in match.items())), ZERO)

    # ---------- предлагаемое правило ----------

    def settle(self):
        sales = (self._sum("shipment") - self._sum("customer_return")
                 - self._sum("payment", purpose=CUSTOMER_PAYMENT) + self._sum("payment", purpose=CUSTOMER_REFUND)
                 + self._sum("correction", side=SALES, direction=THEY_OWE)
                 - self._sum("correction", side=SALES, direction=WE_OWE))
        purchases = (self._sum("receipt") - self._sum("supplier_return")
                     - self._sum("payment", purpose=SUPPLIER_PAYMENT) + self._sum("payment", purpose=SUPPLIER_REFUND)
                     + self._sum("correction", side=PURCHASES, direction=WE_OWE)
                     - self._sum("correction", side=PURCHASES, direction=THEY_OWE))
        unallocated_in = self._sum("payment", direction=IN, purpose=UNALLOCATED)
        unallocated_out = self._sum("payment", direction=OUT, purpose=UNALLOCATED)
        unsided = self._sum("correction", side=None, direction=THEY_OWE) - self._sum("correction", side=None,
                                                                                    direction=WE_OWE)
        difference = sales - purchases - unallocated_in + unallocated_out + unsided
        # Нераспределённые документы — списком, даже если их суммы взаимно
        # гасятся: «он должен 100» и «мы должны 100» без стороны дают
        # unsided = 0, но обе остаются неразобранными. Предварительность
        # определяется наличием таких документов, а не итоговой суммой.
        unallocated_docs = [d for d in self._posted("payment") if d["purpose"] == UNALLOCATED]
        unallocated_docs += [d for d in self._posted("correction") if d["side"] is None]
        return {"sales": sales, "purchases": purchases, "unallocated_in": unallocated_in,
                "unallocated_out": unallocated_out, "unsided": unsided, "difference": difference,
                "unallocated_docs": unallocated_docs, "final": not unallocated_docs}

    # ---------- снятое правило (для сравнения) ----------

    def old_rule(self):
        has_sales = bool(self._posted("shipment") or self._posted("customer_return"))
        has_purchases = bool(self._posted("receipt") or self._posted("supplier_return"))
        goods_sales = self._sum("shipment") - self._sum("customer_return")
        goods_purchases = self._sum("receipt") - self._sum("supplier_return")
        money_in, money_out = self._sum("payment", direction=IN), self._sum("payment", direction=OUT)
        if has_sales and not has_purchases:
            return {"sales": goods_sales - money_in + money_out, "purchases": ZERO}
        if has_purchases and not has_sales:
            return {"sales": ZERO, "purchases": goods_purchases - money_out + money_in}
        return {"sales": goods_sales - money_in, "purchases": goods_purchases - money_out}


RESULTS = []


def expect(name, actual, expected):
    assert actual == expected, f"{name}: получено {actual}, ожидалось {expected}"
    RESULTS.append(name)


def sides(result):
    return tuple(f"{result[key]:f}" for key in ("sales", "purchases", "difference"))


# --------------------------------------------------------------------------
# 1. Одинаковые направление и сумма — разные задолженности
# --------------------------------------------------------------------------

def same_incoming_different_purpose():
    """Отгрузили 1000; приняли товар на 500, уплатили поставщику 800 (аванс 300
    сверх поставки). Пришло 300 — как оплата продажи или как возврат аванса."""
    def base():
        p = Pair()
        p.ship(1000)
        p.receive(500)
        p.pay(OUT, 800, SUPPLIER_PAYMENT)
        return p

    as_payment, as_refund = base(), base()
    as_payment.pay(IN, 300, CUSTOMER_PAYMENT)
    as_refund.pay(IN, 300, SUPPLIER_REFUND)
    # оплата продажи: он должен 700, поставщик должен нам аванс 300 (−300), разница 1000
    expect("Вх. 300 как оплата продажи", sides(as_payment.settle()), ("700", "-300", "1000"))
    # возврат аванса: он должен 1000, по закупкам 0, разница 1000
    expect("Вх. 300 как возврат аванса поставщика", sides(as_refund.settle()), ("1000", "0", "1000"))


def same_outgoing_different_purpose():
    """Отгрузили 1000, покупатель заплатил 1200 (переплата 200); приняли товар
    на 500, не оплачен. Ушло 200 — как оплата поставщику или как возврат
    переплаты покупателю."""
    def base():
        p = Pair()
        p.ship(1000)
        p.pay(IN, 1200, CUSTOMER_PAYMENT)
        p.receive(500)
        return p

    to_supplier, to_customer = base(), base()
    to_supplier.pay(OUT, 200, SUPPLIER_PAYMENT)
    to_customer.pay(OUT, 200, CUSTOMER_REFUND)
    # оплата поставщику: переплата покупателя 200 (−200), мы должны поставщику 300; разница −500
    expect("Исх. 200 как оплата поставщику", sides(to_supplier.settle()), ("-200", "300", "-500"))
    # возврат покупателю: по продажам 0, мы должны поставщику 500; разница −500
    expect("Исх. 200 как возврат покупателю", sides(to_customer.settle()), ("0", "500", "-500"))


# --------------------------------------------------------------------------
# 2. Первая закупка и её отмена не переклассифицируют старые платежи
# --------------------------------------------------------------------------

def first_purchase_keeps_old_refund():
    """Покупатель: отгрузили 1000, он заплатил 1000, вернул товар на 300, мы
    вернули ему деньги 300. Затем — первая закупка у него на 500, потом её отмена."""
    p = Pair()
    p.ship(1000)
    p.pay(IN, 1000, CUSTOMER_PAYMENT)
    p.customer_return(300)
    p.pay(OUT, 300, CUSTOMER_REFUND)
    expect("До закупки: новое правило", sides(p.settle()), ("0", "0", "0"))
    expect("До закупки: снятое правило", (f"{p.old_rule()['sales']:f}", f"{p.old_rule()['purchases']:f}"),
           ("0", "0"))
    receipt = p.receive(500)
    # новое правило: возврат денег покупателю остаётся возвратом; мы должны поставщику 500
    expect("После первой закупки: новое правило", sides(p.settle()), ("0", "500", "-500"))
    # снятое правило: те же 300 стали «оплатой поставщику» — покупатель «переплатил» 300
    expect("После первой закупки: снятое правило", (f"{p.old_rule()['sales']:f}", f"{p.old_rule()['purchases']:f}"),
           ("-300", "200"))
    p.cancel(receipt)
    expect("После отмены закупки: новое правило", sides(p.settle()), ("0", "0", "0"))
    expect("После отмены закупки: назначение не изменилось",
           [d["purpose"] for d in p.docs if d["kind"] == "payment"], [CUSTOMER_PAYMENT, CUSTOMER_REFUND])


def first_sale_keeps_old_supplier_refund():
    """Поставщик: приняли 1000, оплатили 1000, вернули ему товар на 200, он
    вернул деньги 200. Затем — первая продажа ему на 300."""
    p = Pair()
    p.receive(1000)
    p.pay(OUT, 1000, SUPPLIER_PAYMENT)
    p.supplier_return(200)
    p.pay(IN, 200, SUPPLIER_REFUND)
    p.ship(300)
    expect("После первой продажи: новое правило", sides(p.settle()), ("300", "0", "300"))
    # снятое правило: возврат денег поставщиком стал «оплатой продажи»
    expect("После первой продажи: снятое правило", (f"{p.old_rule()['sales']:f}", f"{p.old_rule()['purchases']:f}"),
           ("100", "-200"))


# --------------------------------------------------------------------------
# 3. Разные организации, одинаковая разница, нераспределённые деньги
# --------------------------------------------------------------------------

def organizations_do_not_offset():
    """Организация А отгрузила 1000; покупатель заплатил 1000 организации Б."""
    a, b = Pair(), Pair()
    a.ship(1000)
    b.pay(IN, 1000, CUSTOMER_PAYMENT)
    expect("А: он должен 1000", sides(a.settle()), ("1000", "0", "1000"))
    expect("Б: получено без отгрузок — мы должны 1000", sides(b.settle()), ("-1000", "0", "-1000"))
    owed_to_us = sum((max(pair.settle()["difference"], ZERO) for pair in (a, b)), ZERO)
    we_owe = sum((max(-pair.settle()["difference"], ZERO) for pair in (a, b)), ZERO)
    expect("Сводка: нам должны / мы должны — отдельно", (f"{owed_to_us:f}", f"{we_owe:f}"), ("1000", "1000"))


def same_difference_different_debts():
    """Разница 300 у двух пар: в одной он должен 300 и мы ничего, в другой
    он должен 1000, а мы ему 700."""
    simple, crossed = Pair(), Pair()
    simple.ship(300)
    crossed.ship(1000)
    crossed.receive(700)
    expect("Пара 1", sides(simple.settle()), ("300", "0", "300"))
    expect("Пара 2", sides(crossed.settle()), ("1000", "700", "300"))


def unallocated_money_is_visible():
    """Старый входящий платёж 400 без назначения: стороны предварительные,
    сумма видна отдельно, разница — справочно."""
    p = Pair()
    p.ship(1000)
    p.receive(700)
    p.pay(IN, 400)                                   # старый платёж: назначение неизвестно
    result = p.settle()
    expect("Не распределено: стороны и разница", sides(result), ("1000", "700", "-100"))
    expect("Не распределено: сумма видна, итог не окончательный",
           (f"{result['unallocated_in']:f}", result["final"]), ("400", False))
    payment = [d for d in p.docs if d["kind"] == "payment"][0]
    payment["purpose"] = CUSTOMER_PAYMENT              # человек уточнил назначение
    result = p.settle()
    expect("После уточнения", (sides(result), result["final"]), (("600", "700", "-100"), True))


# --------------------------------------------------------------------------
# 4. Взаимозачёт и корректировки
# --------------------------------------------------------------------------

def offset_is_not_a_balance_correction():
    """Он должен 1000 (продажи), мы ему 700 (закупки), разница 300.
    Зачёт на 700 должен дать 300 и 0, разница 300. Существующая корректировка
    «мы должны 700» (без стороны) меняет разницу и зачётом не является."""
    p = Pair()
    p.ship(1000)
    p.receive(700)
    expect("До зачёта", sides(p.settle()), ("1000", "700", "300"))
    wrong = Pair()
    wrong.docs = [dict(d) for d in p.docs]
    wrong.correct(WE_OWE, 700)
    expect("Корректировка «мы должны 700» — не зачёт", sides(wrong.settle()), ("1000", "700", "-400"))
    offset = Pair()
    offset.docs = [dict(d) for d in p.docs]
    offset.correct(WE_OWE, 700, side=SALES)          # долг покупателя по продажам −700
    offset.correct(THEY_OWE, 700, side=PURCHASES)    # наш долг по закупкам −700
    expect("Зачёт — две записи по сторонам (на будущее)", sides(offset.settle()), ("300", "0", "300"))


def corrections_need_a_side():
    """«Он должен 100»: недовыставили покупателю (продажи) или поставщик
    переплачен (закупки) — знак один, стороны разные. Старая корректировка без
    стороны в стороны не распределяется."""
    def base():
        p = Pair()
        p.ship(1000)
        p.receive(700)
        return p

    on_sales, on_purchases, legacy = base(), base(), base()
    on_sales.correct(THEY_OWE, 100, side=SALES)
    on_purchases.correct(THEY_OWE, 100, side=PURCHASES)
    legacy.correct(THEY_OWE, 100)
    expect("«Он должен 100» по продажам", sides(on_sales.settle()), ("1100", "700", "400"))
    expect("«Он должен 100» по закупкам", sides(on_purchases.settle()), ("1000", "600", "400"))
    result = legacy.settle()
    expect("Старая без стороны: стороны не трогает", sides(result), ("1000", "700", "400"))
    expect("Старая без стороны: видна отдельно", (f"{result['unsided']:f}", result["final"]), ("100", False))


def zero_sum_unallocated_stays_preliminary():
    """Регрессия (дефект прежней версии модели): две корректировки без
    стороны — «он должен 100» и «мы должны 100» — в сумме дают 0, но остаются
    двумя неразобранными документами. Прежняя проверка «final = нет ненулевых
    сумм» давала final=True."""
    p = Pair()
    p.ship(1000)
    p.correct(THEY_OWE, 100)
    p.correct(WE_OWE, 100)
    result = p.settle()
    expect("Две корректировки без стороны: стороны и разница", sides(result), ("1000", "0", "1000"))
    expect("Две корректировки без стороны: сумма 0, но итог предварительный",
           (f"{result['unsided']:f}", result["final"]), ("0", False))
    expect("Две корректировки без стороны: обе показаны отдельно",
           [(d["direction"], f"{d['amount']:f}") for d in result["unallocated_docs"]],
           [(THEY_OWE, "100"), (WE_OWE, "100")])
    # то же для денег: пришло 50 и ушло 50 без назначения
    money = Pair()
    money.pay(IN, 50)
    money.pay(OUT, 50)
    result = money.settle()
    expect("Деньги 50 туда и обратно без назначения: итог предварительный",
           (sides(result), len(result["unallocated_docs"]), result["final"]), (("0", "0", "0"), 2, False))
    # после уточнения стороны у обеих — окончательно
    for doc, side in zip(p.docs[1:], (SALES, SALES)):
        doc["side"] = side
    result = p.settle()
    expect("После уточнения сторон — окончательно", (sides(result), result["final"]),
           (("1000", "0", "1000"), True))


SCENARIOS = (
    same_incoming_different_purpose, same_outgoing_different_purpose, first_purchase_keeps_old_refund,
    first_sale_keeps_old_supplier_refund, organizations_do_not_offset, same_difference_different_debts,
    unallocated_money_is_visible, offset_is_not_a_balance_correction, corrections_need_a_side,
    zero_sum_unallocated_stays_preliminary,
)

if __name__ == "__main__":
    for scenario in SCENARIOS:
        scenario()
    print(f"Модель взаиморасчётов: проверок пройдено {len(RESULTS)}")
    for name in RESULTS:
        print("  ok", name)
