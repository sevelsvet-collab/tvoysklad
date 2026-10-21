"""Одновременные операции разных организаций на общем складе — как сейчас (только PostgreSQL).

Только временная база (скрипт создаёт данные и проверяет, что база пустая):

    DATABASE_URL=postgres://.../<временная база> python -X utf8 docs/proposals/org_stock_concurrency.py

А приняла 1 шт. на склад W; Б поступлений не имеет; минус запрещён обеим.
А и Б одновременно отгружают эту единицу. Первая операция останавливается,
уже держа блокировку товара (после проверки истории), вторая должна встать в
ожидание — это доказывается по pg_stat_activity (run_overlapping). Затем
порядок меняется. Сейчас исход зависит только от порядка: кто первый, тот и
продал; при порядке «Б, затем А» Б продаёт товар А, а А получает отказ.
По предложению при любом порядке: А — проведено, Б — отказ.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402

assert connection.vendor == "postgresql", "нужен PostgreSQL: блокировки строк проверяются только там"
assert settings.DATABASES["default"]["NAME"] not in ("sklad",), "только временная база"
call_command("migrate", "--noinput", verbosity=0)

from apps.catalog.models import Product, Unit  # noqa: E402
from apps.core.concurrency_testing import ConcurrencyMixin  # noqa: E402
from apps.core.models import Organization, Warehouse  # noqa: E402
from apps.inventory import services  # noqa: E402
from apps.inventory.models import StockBalance  # noqa: E402
from apps.partners.models import Counterparty  # noqa: E402
from apps.purchases.models import Receipt  # noqa: E402
from apps.sales.models import Shipment, ShipmentLine  # noqa: E402

assert not Organization.objects.exists(), "база не пустая — нужна новая временная база"


class Probe(ConcurrencyMixin, unittest.TestCase):
    def runTest(self):
        pass


org_a = Organization.objects.create(name="А", is_default=True)
org_b = Organization.objects.create(name="Б")
wh = Warehouse.objects.create(name="W", is_default=True)
unit = Unit.objects.create(name="шт")
supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
probe = Probe()


def draft_shipment(org, product):
    doc = Shipment.objects.create(organization=org, warehouse=wh, customer=customer)
    doc.lines.create(product=product, quantity=1, price=300, vat_rate="none")
    return doc


for order in (("А", "Б"), ("Б", "А")):
    product = Product.objects.create(name=f"Товар, порядок {' → '.join(order)}", unit=unit)
    receipt = Receipt.objects.create(organization=org_a, warehouse=wh, supplier=supplier)
    receipt.lines.create(product=product, quantity=1, price=100, vat_rate="none")
    receipt.post()
    docs = {"А": draft_shipment(org_a, product), "Б": draft_shipment(org_b, product)}
    first, second = (docs[name] for name in order)
    results = probe.run_overlapping(
        lambda: Shipment.objects.get(pk=first.pk).post(),
        lambda: Shipment.objects.get(pk=second.pk).post(),
        pause_on=(services, "check_history"),
    )
    outcome = {}
    for name, result in zip(order, results):
        doc = Shipment.objects.get(pk=docs[name].pk)
        line = ShipmentLine.objects.get(shipment=doc)
        outcome[name] = ("проведено, себестоимость " + f"{line.cost_amount}") if doc.is_posted else \
            "отказ: " + str(result).split(":")[0]
    balance = StockBalance.objects.get(product=product, warehouse=wh)
    print(f"Порядок {' → '.join(order)} (вторая операция ждала блокировку товара — проверено по pg_stat_activity):")
    print(f"  А: {outcome['А']}")
    print(f"  Б: {outcome['Б']}")
    print(f"  остаток W: {balance.quantity:f} / {balance.value}")
print("\nСейчас победитель определяется порядком; по предложению при любом порядке: А — проведено, Б — отказ.")
