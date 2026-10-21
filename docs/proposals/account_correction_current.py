"""Корректировка остатка кассы: как считает программа сейчас — настоящими документами.

Только временная база; рабочую скрипт не трогает (проверяет имя и что база
пустая):

    DATABASE_URL=sqlite:///<временный файл> python -X utf8 docs/proposals/account_correction_current.py

Скрипт применяет миграции, проигрывает сценарии из account_correction_model.py
на настоящих документах (Payment, AccountCorrection) и повторяет действия
программы:

* «Сохранить» в форме корректировки — save(), как
  AccountCorrectionEditBase.form_valid (статус при этом не меняется; до
  17.10.2026 форма вызывала ещё recalc() и save(update_fields=[...]));
* кнопка «Провести» — post(); «Снять с проведения» — unpost();
* платёж — Payment (проведённый), его изменение — save(), отмена — unpost().

Рядом печатаются результаты правил А и Б из независимой модели.
Ожидаемые результаты программы записаны константами (CURRENT): если
поведение программы изменится, скрипт остановится.

С 17.10.2026 программа реализует правило А (решение владельца), поэтому
CURRENT совпадает с правилом А. Вывод прежней версии программы сохранён в
account_correction_before_output.txt.

Только чтение: сравнение существующих проведённых корректировок с правилом А
(для копии рабочих данных):

    DATABASE_URL=... python -X utf8 docs/proposals/account_correction_current.py --existing
"""
import importlib.util
import os
import sys
from datetime import date, time
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402

D = Decimal
spec = importlib.util.spec_from_file_location("account_correction_model",
                                              os.path.join(os.path.dirname(__file__), "account_correction_model.py"))
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)

name = str(settings.DATABASES["default"]["NAME"])
assert os.path.abspath(name) != os.path.abspath(os.path.join(ROOT, "db.sqlite3")) and name != "sklad", \
    "только временная база: рабочую db.sqlite3 и базу sklad скрипт не использует"

from apps.core.models import Organization  # noqa: E402
from apps.finance.models import Account, AccountCorrection, Payment  # noqa: E402
from apps.partners.models import Counterparty  # noqa: E402

# Результат программы сейчас: (разницы проведённых корректировок, остаток кассы)
CURRENT = {
    "1": ({"К": "450"}, "500"),
    "2": ({"К": "450"}, "530"),
    "2б": ({"К": "450"}, "530"),
    "3": ({"К": "420"}, "500"),
    "4": ({"К": "420"}, "500"),
    "5 повторное проведение": ({"К": "450"}, "530"),
    "5 только комментарий": ({"К": "450"}, "530"),
    "5 снятие с проведения": ({}, "80"),
    "5 комментарий, снять, провести": ({"К": "450"}, "530"),
    "6": ({"К18": "450", "К21": "70"}, "600"),
    "6б": ({"К18": "450", "К21": "70"}, "600"),
    "6в": ({"К18": "450", "К21": "70"}, "600"),
    "7 вставка 17:00": ({"К": "420"}, "500"),
    "7 изменение до +40": ({"К": "410"}, "500"),
    "7 отмена": ({"К": "450"}, "500"),
    "8 начальный 50 → 80": ({"К": "420"}, "500"),
    "9 платёж введён раньше": ({"К": "420"}, "500"),
    "9 платёж введён позже": ({"К": "450"}, "530"),
    "10 вечер": ({"К": "420"}, "530"),
    "10 вечер, затем комментарий": ({"К": "420"}, "530"),
}
DAY = date(2026, 10, 14)


def _time(when):
    hours, minutes = (int(part) for part in when.split(":"))
    return time(hours, minutes)


def _money(value):
    value = D(value).quantize(D("0.01"))
    return f"{'+' if value > 0 else ''}{value:f}"


class Program:
    """Касса в базе и действия пользователя в программе."""

    def __init__(self, organization, customer, title, opening):
        self.organization, self.customer = organization, customer
        self.account = Account.objects.create(organization=organization, name=f"Касса «{title}»",
                                              kind=Account.KIND_CASH, opening_balance=D(opening))
        self.payments, self.corrections = {}, {}

    def pay(self, label, when, amount):
        amount = D(amount)
        payment = Payment.objects.create(
            kind=Payment.KIND_IN if amount > 0 else Payment.KIND_OUT, organization=self.organization,
            account=self.account, counterparty=self.customer, amount=abs(amount), date=DAY, time=_time(when),
            status="posted",
        )
        self.payments[label] = payment.pk

    def edit_payment(self, label, amount):
        payment = Payment.objects.get(pk=self.payments[label])
        payment.amount = abs(D(amount))
        payment.save()

    def unpost_payment(self, label):
        Payment.objects.get(pk=self.payments[label]).unpost()

    @staticmethod
    def _form_save(correction):
        """Как AccountCorrectionEditBase.form_valid: сохранить (расчёт — в модели)."""
        correction.save()

    def save_correction(self, label, when, actual):
        correction = AccountCorrection(organization=self.organization, account=Account.objects.get(pk=self.account.pk),
                                       date=DAY, time=_time(when), actual_balance=D(actual))
        self._form_save(correction)
        self.corrections[label] = correction.pk

    def post_correction(self, label):
        AccountCorrection.objects.get(pk=self.corrections[label]).post()

    def resave_correction(self, label):
        correction = AccountCorrection.objects.get(pk=self.corrections[label])
        correction.comment = "поправлен только комментарий"
        self._form_save(correction)

    def unpost_correction(self, label):
        AccountCorrection.objects.get(pk=self.corrections[label]).unpost()

    def set_opening(self, value):
        account = Account.objects.get(pk=self.account.pk)
        account.opening_balance = D(value)
        account.save()

    def result(self):
        amounts = {label: AccountCorrection.objects.get(pk=pk).amount for label, pk in self.corrections.items()
                   if AccountCorrection.objects.get(pk=pk).is_posted}
        return amounts, Account.objects.get(pk=self.account.pk).balance


STEP_TITLES = {
    "pay": lambda label, when, amount: f"платёж {label} {_money(amount)} на {when}",
    "edit_payment": lambda label, amount: f"платёж {label} изменён на {_money(amount)}",
    "unpost_payment": lambda label: f"платёж {label} снят с проведения",
    "save_correction": lambda label, when, actual: f"«Сохранить» корректировку {label} на {when}, факт {actual}",
    "post_correction": lambda label: f"«Провести» {label}",
    "resave_correction": lambda label: f"в {label} поправлен только комментарий, «Сохранить»",
    "unpost_correction": lambda label: f"«Снять с проведения» {label}",
    "set_opening": lambda value: f"начальный остаток кассы исправлен на {value}",
}


class Pair:
    """Каждый шаг — и в программе, и в модели; шаги записываются для печати."""

    def __init__(self, program, ledger):
        self.program, self.ledger, self.steps = program, ledger, []

    def __getattr__(self, action):
        def step(*args):
            self.steps.append(STEP_TITLES[action](*args))
            getattr(self.program, action)(*args)
            getattr(self.ledger, action)(*args)
        return step


def run_scenarios():
    call_command("migrate", "--noinput", verbosity=0)
    assert not Organization.objects.exists(), "база не пустая — нужна новая временная база"
    organization = Organization.objects.create(name="Организация", is_default=True)
    customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
    mismatches = []
    for title, build, _, _, _, _ in model.EXPECTED:
        pair = Pair(Program(organization, customer, title, 50), model.Ledger(50))
        build(pair)
        amounts, final = pair.program.result()
        current = ({label: f"{value.normalize():f}" for label, value in amounts.items()}, f"{final.normalize():f}")
        if current != CURRENT[title]:
            mismatches.append((title, current, CURRENT[title]))
        a, b = pair.ledger.rule_a(), pair.ledger.rule_b()
        print(f"\nСценарий {title}. Начальный остаток кассы 50.")
        for number, text in enumerate(pair.steps, 1):
            print(f"  ввод {number}: {text}")
        for row, (row_amounts, row_final) in (("сейчас (программа)", (amounts, final)),
                                              ("правило А (факт на момент)", (a["amounts"], a["final"])),
                                              ("правило Б (сумма при проведении)", (b["amounts"], b["final"]))):
            shown = ", ".join(f"{label} {_money(value)}" for label, value in sorted(row_amounts.items())) or "нет"
            print(f"  {row:<34} разница: {shown:<22} остаток кассы: {D(row_final).quantize(D('0.01')):f}")
    assert not mismatches, f"поведение программы отличается от записанного: {mismatches}"
    stale_account(organization, customer)
    print(f"\nСценариев: {len(model.EXPECTED) + 1}; результаты программы совпали с записанными.")


def stale_account(organization, customer):
    """8б: К на 18:00 проведена (+450); начальный остаток исправили 50 → 80
    через другой объект кассы; затем recalc() и сохранение разницы вызваны у
    объекта корректировки, загруженного раньше (касса в нём — с остатком 50)."""
    program = Program(organization, customer, "8б", 50)
    program.save_correction("К", "18:00", 500)
    program.post_correction("К")
    stale = AccountCorrection.objects.get(pk=program.corrections["К"])
    assert stale.account.opening_balance == 50                     # касса загружена до исправления
    program.set_opening(80)
    stale.recalc()
    stale.save(update_fields=["balance_before", "amount"])
    amounts, final = program.result()
    assert (amounts["К"], final) == (D("420.00"), D("500.00")), (amounts, final)
    fresh = AccountCorrection.objects.get(pk=program.corrections["К"])
    fresh.recalc()
    print("\nСценарий 8б устаревший объект кассы. Начальный остаток кассы 50.")
    print("  ввод 1–2: «Сохранить» и «Провести» К на 18:00, факт 500 (+450)")
    print("  ввод 3: начальный остаток исправлен на 80 через другой объект кассы")
    print("  ввод 4: recalc() у объекта К, загруженного до исправления (касса в памяти — 50)")
    print(f"  сейчас (программа)                 разница: К {_money(amounts['К'])}"
          f"              остаток кассы: {final:f}  (по свежим данным было бы {_money(fresh.amount)})")
    print("  правило А (факт на момент)         разница: К +420.00              остаток кассы: 500.00")
    print("  правило Б (сумма при проведении)   разница: К +450.00              остаток кассы: 530.00")


def existing_report():
    """Только чтение: проведённые корректировки и их разница по правилу А."""
    for account in Account.objects.filter(corrections__isnull=False).distinct().order_by("pk"):
        rows = []
        for payment in Payment.objects.filter(account=account, status="posted"):
            rows.append(((payment.date, payment.time, payment.created_at, 0, payment.pk), "pay", payment))
        for correction in AccountCorrection._base_manager.filter(account=account, status="posted"):
            rows.append(((correction.date, correction.time, correction.created_at, 1, correction.pk), "corr",
                         correction))
        balance = account.opening_balance
        print(f"\nКасса «{account}» ({account.organization}), начальный остаток {account.opening_balance:f}")
        for _, kind, doc in sorted(rows, key=lambda row: row[0]):
            if kind == "pay":
                balance += doc.amount if doc.kind == Payment.KIND_IN else -doc.amount
                continue
            amount_a = doc.actual_balance - balance
            print(f"  корректировка № {doc.number} от {doc.date:%d.%m.%Y} {doc.time:%H:%M} (введена "
                  f"{doc.created_at:%d.%m.%Y %H:%M}): факт {doc.actual_balance:f}; сейчас разница "
                  f"{_money(doc.amount)}, расчётный {doc.balance_before:f}; правило А: разница {_money(amount_a)}, "
                  f"расчётный {balance:f}")
            balance = doc.actual_balance
        print(f"  остаток кассы: сейчас {account.balance:f}; правило А {balance:f}")


if __name__ == "__main__":
    if "--existing" in sys.argv:
        existing_report()
    else:
        run_scenarios()
