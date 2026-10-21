"""Корректировка остатка кассы: две модели правила — без Django и без базы.

Касса: начальный остаток, платежи (+ приход, − расход) и корректировки
«фактически в кассе было X». У каждого документа два времени:

* время документа (дата и время, которые указал человек) — по нему
  документы упорядочиваются;
* порядок ввода (когда документ занесли в программу) — нужен только для
  документов с одинаковыми датой и временем и для правила Б.

Порядок документов: дата и время → порядок ввода. Он однозначный.

Правило А — «остаток на момент». Корректировка хранит фактический остаток.
Её разница = факт − расчётный остаток на её момент (все проведённые
документы раньше неё). Разница пересчитывается при любом изменении
предшествующей истории; остаток кассы сразу после корректировки всегда
равен факту.

Правило Б — «разовая сумма». Разница считается при проведении корректировки
по тому же порядку документов и дальше не меняется, пока корректировку не
снимут с проведения и не проведут снова (или не изменят её факт, дату,
время). Изменения истории до неё сдвигают остаток после неё.

Запуск: python -X utf8 docs/proposals/account_correction_model.py
(код 0 — все ожидаемые числа сошлись). Ожидаемые числа посчитаны вручную.
"""
from datetime import datetime
from decimal import Decimal

D = Decimal
DAY = datetime(2026, 10, 14)


def at(hhmm):
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return DAY.replace(hour=hours, minute=minutes)


class Ledger:
    """Касса и её документы; шаги вызываются в порядке ввода в программу."""

    def __init__(self, opening):
        self.opening = D(opening)
        self.entered = 0
        self.payments = {}       # имя → {"at", "seq", "amount", "posted"}
        self.corrections = {}    # имя → {"at", "seq", "actual", "posted", "frozen"}

    def _next(self):
        self.entered += 1
        return self.entered

    # ---------- шаги ----------

    def pay(self, name, when, amount):
        self.payments[name] = {"at": at(when), "seq": self._next(), "amount": D(amount), "posted": True}

    def edit_payment(self, name, amount):
        self.payments[name]["amount"] = D(amount)

    def unpost_payment(self, name):
        self.payments[name]["posted"] = False

    def save_correction(self, name, when, actual):
        """Черновик: правилам А и Б он не важен, пока не проведён."""
        self.corrections[name] = {"at": at(when), "seq": self._next(), "actual": D(actual),
                                  "posted": False, "frozen": None}

    def post_correction(self, name):
        doc = self.corrections[name]
        doc["posted"] = True
        doc["frozen"] = doc["actual"] - self._balance_before_b(name)     # правило Б: сумма на момент проведения

    def resave_correction(self, name):
        """Сохранение без изменения факта, даты и времени (например, комментарий):
        ни в одном правиле ничего не меняет."""

    def unpost_correction(self, name):
        self.corrections[name].update(posted=False, frozen=None)

    def set_opening(self, value):
        self.opening = D(value)

    # ---------- расчёт ----------

    def _posted(self):
        docs = [("pay", n, d) for n, d in self.payments.items() if d["posted"]]
        docs += [("corr", n, d) for n, d in self.corrections.items() if d["posted"]]
        return sorted(docs, key=lambda item: (item[2]["at"], item[2]["seq"]))

    def rule_a(self):
        """{"amounts": {корректировка: разница}, "before": {корректировка: расчётный
        остаток на её момент}, "final": остаток кассы}."""
        balance, amounts, before = self.opening, {}, {}
        for kind, name, doc in self._posted():
            if kind == "pay":
                balance += doc["amount"]
            else:
                before[name] = balance
                amounts[name] = doc["actual"] - balance
                balance = doc["actual"]
        return {"amounts": amounts, "before": before, "final": balance}

    def _walk_b(self, stop=None):
        balance = self.opening
        for kind, name, doc in self._posted():
            if name == stop and kind == "corr":
                return balance
            balance += doc["amount"] if kind == "pay" else doc["frozen"]
        return balance

    def _balance_before_b(self, name):
        doc = self.corrections[name]
        balance = self.opening
        for kind, other, item in self._posted():
            if (item["at"], item["seq"]) >= (doc["at"], doc["seq"]):
                break
            balance += item["amount"] if kind == "pay" else item["frozen"]
        return balance

    def rule_b(self):
        amounts = {n: d["frozen"] for n, d in self.corrections.items() if d["posted"]}
        return {"amounts": amounts, "final": self._walk_b()}


# --------------------------------------------------------------------------
# Сценарии (имена — как в docs/account-correction-proposal.md)
# --------------------------------------------------------------------------

def scenario_1(k):
    """До корректировки 50; на 18:00 факт 500 → +450."""
    k.save_correction("К", "18:00", 500)
    k.post_correction("К")
    return k


def scenario_2(k):
    """Платёж +30 в 19:00 (введён после корректировки) — после неё 530."""
    scenario_1(k)
    k.pay("П19", "19:00", 30)
    return k


def scenario_2b(k):
    """Тот же платёж в 19:00 введён раньше, чем корректировка на 18:00."""
    k.pay("П19", "19:00", 30)
    return scenario_1(k)


def scenario_3(k):
    """Платёж +30 в 17:00, корректировка на 18:00 — учитывает его: +420, 500."""
    k.pay("П17", "17:00", 30)
    return scenario_1(k)


def scenario_4(k):
    """Черновик на 18:00 сохранён до ввода платежа 17:00, проведён после."""
    k.save_correction("К", "18:00", 500)
    k.pay("П17", "17:00", 30)
    k.post_correction("К")
    return k


def scenario_5(k, step):
    """Проведена на 18:00 (+450); потом введён платёж 19:00; затем шаг."""
    scenario_2(k)
    if step == "repost":
        k.post_correction("К")
    elif step == "comment":
        k.resave_correction("К")
    elif step == "unpost":
        k.unpost_correction("К")
    elif step == "comment_unpost_post":
        k.resave_correction("К")
        k.unpost_correction("К")
        k.post_correction("К")
    return k


def scenario_6(k):
    """Две корректировки: 18:00 факт 500, платёж 19:00 +30, 21:00 факт 600."""
    k.save_correction("К18", "18:00", 500)
    k.post_correction("К18")
    k.pay("П19", "19:00", 30)
    k.save_correction("К21", "21:00", 600)
    k.post_correction("К21")
    return k


def scenario_6b(k):
    """Те же документы, но пересчёт на 18:00 занесли последним."""
    k.pay("П19", "19:00", 30)
    k.save_correction("К21", "21:00", 600)
    k.post_correction("К21")
    k.save_correction("К18", "18:00", 500)
    k.post_correction("К18")
    return k


def scenario_6c(k):
    """Сценарий 6, затем в корректировке на 18:00 поправили только комментарий."""
    scenario_6(k)
    k.resave_correction("К18")
    return k


def scenario_7(k, step):
    """К на 18:00 проведена (+450); задним числом платёж 17:00 +30, затем шаг."""
    scenario_1(k)
    k.pay("П17", "17:00", 30)
    if step == "edit":
        k.edit_payment("П17", 40)
    elif step == "cancel":
        k.unpost_payment("П17")
    return k


def scenario_8(k):
    """К на 18:00 проведена (+450); начальный остаток исправили 50 → 80."""
    scenario_1(k)
    k.set_opening(80)
    return k


def scenario_9(k, payment_first):
    """Платёж +30 и корректировка с одинаковым временем 18:00."""
    if payment_first:
        k.pay("П18", "18:00", 30)
        k.save_correction("К", "18:00", 500)
    else:
        k.save_correction("К", "18:00", 500)
        k.pay("П18", "18:00", 30)
    k.post_correction("К")
    return k


def scenario_10(k, resave):
    """Вечер: в 18:00 кассу пересчитали — 500 (по программе было 50). Потом
    занесли забытый дневной платёж 17:00 +30 и вечерний 19:00 +30; затем
    (если resave) в пересчёте поправили только комментарий."""
    scenario_1(k)
    k.pay("П17", "17:00", 30)
    k.pay("П19", "19:00", 30)
    if resave:
        k.resave_correction("К")
    return k


# (сценарий, корректировки и ожидаемые разницы А, остаток А, разницы Б, остаток Б)
EXPECTED = [
    ("1", scenario_1, {"К": "450"}, "500", {"К": "450"}, "500"),
    ("2", scenario_2, {"К": "450"}, "530", {"К": "450"}, "530"),
    ("2б", scenario_2b, {"К": "450"}, "530", {"К": "450"}, "530"),
    ("3", scenario_3, {"К": "420"}, "500", {"К": "420"}, "500"),
    ("4", scenario_4, {"К": "420"}, "500", {"К": "420"}, "500"),
    ("5 повторное проведение", lambda k: scenario_5(k, "repost"), {"К": "450"}, "530", {"К": "450"}, "530"),
    ("5 только комментарий", lambda k: scenario_5(k, "comment"), {"К": "450"}, "530", {"К": "450"}, "530"),
    ("5 снятие с проведения", lambda k: scenario_5(k, "unpost"), {}, "80", {}, "80"),
    ("5 комментарий, снять, провести", lambda k: scenario_5(k, "comment_unpost_post"),
     {"К": "450"}, "530", {"К": "450"}, "530"),
    ("6", scenario_6, {"К18": "450", "К21": "70"}, "600", {"К18": "450", "К21": "70"}, "600"),
    # Б: К21 проведена, когда К18 ещё не было: 600 − (50 + 30) = 520 и дальше не меняется;
    # К18 при проведении: 500 − 50 = 450. Итог Б: 50 + 30 + 520 + 450 = 1050.
    ("6б", scenario_6b, {"К18": "450", "К21": "70"}, "600", {"К18": "450", "К21": "520"}, "1050"),
    ("6в", scenario_6c, {"К18": "450", "К21": "70"}, "600", {"К18": "450", "К21": "70"}, "600"),
    ("7 вставка 17:00", lambda k: scenario_7(k, "insert"), {"К": "420"}, "500", {"К": "450"}, "530"),
    ("7 изменение до +40", lambda k: scenario_7(k, "edit"), {"К": "410"}, "500", {"К": "450"}, "540"),
    ("7 отмена", lambda k: scenario_7(k, "cancel"), {"К": "450"}, "500", {"К": "450"}, "500"),
    ("8 начальный 50 → 80", scenario_8, {"К": "420"}, "500", {"К": "450"}, "530"),
    ("9 платёж введён раньше", lambda k: scenario_9(k, True), {"К": "420"}, "500", {"К": "420"}, "500"),
    ("9 платёж введён позже", lambda k: scenario_9(k, False), {"К": "450"}, "530", {"К": "450"}, "530"),
    ("10 вечер", lambda k: scenario_10(k, False), {"К": "420"}, "530", {"К": "450"}, "560"),
    ("10 вечер, затем комментарий", lambda k: scenario_10(k, True), {"К": "420"}, "530", {"К": "450"}, "560"),
]


def run():
    checked = 0
    for title, build, amounts_a, final_a, amounts_b, final_b in EXPECTED:
        ledger = build(Ledger(50))
        a, b = ledger.rule_a(), ledger.rule_b()
        got = ({n: f"{v:f}" for n, v in a["amounts"].items()}, f"{a['final']:f}",
               {n: f"{v:f}" for n, v in b["amounts"].items()}, f"{b['final']:f}")
        want = (amounts_a, final_a, amounts_b, final_b)
        assert got == want, f"сценарий {title}: получено {got}, ожидалось {want}"
        # правило А: сразу после каждой проведённой корректировки остаток равен факту
        for name, before in a["before"].items():
            assert before + a["amounts"][name] == ledger.corrections[name]["actual"], title
        checked += 1
        print(f"  ok {title}: А {amounts_a} → {final_a}; Б {amounts_b} → {final_b}")
    print(f"Модель: сценариев сошлось {checked}")


if __name__ == "__main__":
    run()
