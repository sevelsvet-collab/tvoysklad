"""Сквозная проверка: таблица «ожидалось / получено» по шагам основного сценария.

Только временная база (скрипт проверяет имя и что база пустая):

    DATABASE_URL=sqlite:///<временный файл> python -X utf8 docs/proposals/end_to_end_table.py

Сценарий и ожидаемые числа — apps/core/tests_end_to_end.py (MAIN, Scenario):
те же документы, что в тесте. В конце — проба вопроса, который не решён
правилами: возврат поставщику по цене документа, отличной от средней
себестоимости склада (числа печатаются как есть, без оценки «верно»).
"""
import datetime
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402

name = str(settings.DATABASES["default"]["NAME"])
assert os.path.abspath(name) != os.path.abspath(os.path.join(ROOT, "db.sqlite3")) and name != "sklad", \
    "только временная база: рабочую db.sqlite3 и базу sklad скрипт не использует"
call_command("migrate", "--noinput", verbosity=0)

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.auth.models import Group  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

from apps.core import roles  # noqa: E402
from apps.core.models import Organization  # noqa: E402
from apps.core.tests_end_to_end import Scenario  # noqa: E402
from apps.finance import settlements  # noqa: E402
from apps.inventory.models import StockBalance  # noqa: E402
from apps.purchases.models import SupplierReturn  # noqa: E402
from apps.reports.services import sales_summary  # noqa: E402

assert not Organization.objects.exists(), "база не пустая — нужна новая временная база"
setup_test_environment()          # контекст страниц в ответах клиента
settings.ALLOWED_HOSTS = ["testserver"]

FAILED = []


def assert_equal(actual, expected):
    if actual != expected:
        FAILED.append((actual, expected))
        raise AssertionError(f"получено {actual!r}, ожидалось {expected!r}")


def show(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, Decimal):
        return f"{value.normalize():f}" if value == value.to_integral() else f"{value:f}"
    if isinstance(value, tuple):
        return " / ".join(show(item) for item in value)
    return str(value)


user = get_user_model().objects.create_user("e2e_table", password="not-used-e2e")
user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
client = Client()
client.force_login(user)
scenario = Scenario(client, assert_equal)
scenario.run_main()

print("Основной сценарий: ожидалось (вручную, до запуска) / получено (программа)\n")
print("| Шаг | Показатель | Ожидалось | Получено | Совпало |")
print("|---|---|---|---|---|")
previous = None
for step, what, expected, actual in scenario.rows:
    print(f"| {step if step != previous else ''} | {what} | {show(expected)} | {show(actual)} | "
          f"{'да' if expected == actual else '**НЕТ**'} |")
    previous = step
print(f"\nПроверок: {len(scenario.rows)}, расхождений: {sum(1 for r in scenario.rows if r[2] != r[3])}")
print("Взаиморасчёты: по продажам / по закупкам / разница / окончательно / состояние. Итоги отчёта: покупатели "
      "должны / мы должны покупателям / мы должны поставщикам / поставщики должны нам / нераспределённых.")
print("Страницы «Взаиморасчёты», карточки контрагентов, «Продажи и прибыль» и «Счета и кассы» на каждом шаге "
      "показали те же числа, что сервисы (иначе сценарий остановился бы).")

# ---------------------------------------------------------------------------
# Вопрос, не решённый правилами: возврат поставщику по цене, отличной от средней
# ---------------------------------------------------------------------------
print("\nВопрос (числа как есть): возврат поставщику по цене документа, отличной от средней себестоимости")
other = scenario.supplier.__class__.objects.create(name="Поставщик 2", partner_type="supplier")
receipt = scenario.receipt(other, 10, 200, "15:00")             # склад: 7 по 100 + 10 по 200
before_stock = StockBalance.objects.get(product=scenario.product, warehouse=scenario.wh)
before_profit = sales_summary()["profit"]
doc = SupplierReturn.objects.create(organization=scenario.org, warehouse=scenario.wh, supplier=other,
                                    receipt=receipt, date=receipt.date, time=datetime.time(16, 0))
doc.lines.create(product=scenario.product, quantity=2, price=200, vat_rate="none")
doc.post()
after_stock = StockBalance.objects.get(product=scenario.product, warehouse=scenario.wh)
pair = [p for p in settlements.settlement_pairs(counterparty=other)][0]
print(f"  склад до: {before_stock.quantity:f} шт. на {before_stock.value}; средняя {before_stock.avg_cost}")
print(f"  возврат поставщику 2 шт. по цене приёмки 200 (документ на {doc.total})")
print(f"  склад после: {after_stock.quantity:f} шт. на {after_stock.value} "
      f"(уменьшился на {before_stock.value - after_stock.value} — по средней)")
print(f"  наш долг поставщику: приёмка 2000 − возврат {doc.total} = {pair.purchases}")
print(f"  прибыль «Продажи и прибыль»: {before_profit} → {sales_summary()['profit']} (возврат поставщику туда не входит)")
print(f"  разница {doc.total - (before_stock.value - after_stock.value)} — долг уменьшился больше, чем стоимость "
      "склада; ни в прибыль, ни в другой отчёт она не попадает")

if FAILED:
    sys.exit(1)
