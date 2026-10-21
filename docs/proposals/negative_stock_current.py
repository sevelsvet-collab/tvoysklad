"""ТЕКУЩЕЕ поведение программы на сценариях предложения
docs/negative-stock-proposal.md — настоящие документы и проведение.

Запускать только на отдельной временной базе (создаёт организации, склады,
товары и документы):

    DATABASE_URL=sqlite:///<временный файл> python manage.py migrate
    DATABASE_URL=sqlite:///<временный файл> python manage.py shell -c "exec(open('docs/proposals/negative_stock_current.py', encoding='utf-8').read())"

Печатает фактические значения; ожидаемые по предложению — в документе и в
docs/proposals/negative_stock_model.py.
"""
import datetime
from decimal import Decimal as D

from django.conf import settings
from django.db import connection, transaction

from apps.catalog.models import Product, Unit
from apps.core.models import Organization, Warehouse
from apps.inventory.models import StockBalance, StockMovement, Transfer
from apps.inventory.services import StockError
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import CustomerReturn, Shipment

assert "sqlite" in connection.vendor or settings.DATABASES["default"]["NAME"] != "sklad", "только временная база"

NOON = datetime.time(12, 0)
unit, _ = Unit.objects.get_or_create(name="шт")
org = Organization.objects.create(name="Проверка предложения", allow_negative_stock=True)
W1 = Warehouse.objects.create(name="Склад 1 (проверка)")
W2 = Warehouse.objects.create(name="Склад 2 (проверка)")
supplier = Counterparty.objects.create(name="Поставщик проверки", partner_type=Counterparty.TYPE_SUPPLIER)
customer = Counterparty.objects.create(name="Покупатель проверки", partner_type=Counterparty.TYPE_CUSTOMER)


def day(n):
    return datetime.date(2026, 3, 1) + datetime.timedelta(days=n)


def receipt(product, n, qty, amount, wh=W1):
    doc = Receipt.objects.create(organization=org, warehouse=wh, supplier=supplier, date=day(n), time=NOON)
    doc.lines.create(product=product, quantity=D(qty), price=D(amount) / D(qty))
    doc.post()
    return doc


def shipment(product, n, *quantities, wh=W1):
    doc = Shipment.objects.create(organization=org, warehouse=wh, customer=customer, date=day(n), time=NOON)
    for qty in quantities:
        doc.lines.create(product=product, quantity=D(qty), price=1000)
    doc.post()
    return doc


def give_back(product, n, basis, qty, wh=W1):
    doc = CustomerReturn.objects.create(organization=org, warehouse=wh, customer=customer, shipment=basis,
                                        date=day(n), time=NOON)
    doc.lines.create(product=product, quantity=D(qty), price=1000)
    doc.post()
    return StockMovement.objects.get(doc_type="customer_return", doc_id=doc.pk).amount


def state(product, wh=W1):
    b = StockBalance.objects.filter(product=product, warehouse=wh).first()
    return f"{b.quantity} шт. на {b.value}" if b else "нет остатка"


def costs(doc):
    return [str(line.cost_amount) for line in doc.lines.order_by("pk")]


def section(title):
    print("-" * 90)
    print(title)


section("В. Приход 1 на 100,00 → расход 2 → приход 2 на 300,00 (по 150,00)")
p = Product.objects.create(name="В", unit=unit)
receipt(p, 1, 1, "100.00")
s = shipment(p, 2, 2)
print("  после расхода:", state(p), "| отгрузка", costs(s))
receipt(p, 3, 2, "300.00")
print("  после прихода:", state(p), "| отгрузка", costs(s), "| предложение: отгрузка 250.00, остаток 1 шт. на 150.00")

section("Р1. Отгрузка 2 с пустого → возврат 1 → приход 1 на 250,00 → возврат 1")
p = Product.objects.create(name="Р1", unit=unit)
s = shipment(p, 1, 2)
print("  возврат 1:", give_back(p, 2, s, 1), "|", state(p))
receipt(p, 3, 1, "250.00")
print("  после прихода:", state(p), "| отгрузка", costs(s))
print("  возврат 1:", give_back(p, 4, s, 1), "|", state(p), "| предложение: 0.00, затем 250.00; остаток 1 шт. на 250.00")

section("Р2. Дробные: приход 1,000 на 100,00 → отгрузка 2,500 → возврат 0,400 → приход 3,000 на 1000,00 → возвраты 0,7/0,8/0,6")
p = Product.objects.create(name="Р2", unit=unit)
receipt(p, 1, "1.000", "100.00")
s = shipment(p, 2, "2.500")
print("  возврат 0,400:", give_back(p, 3, s, "0.400"), "|", state(p))
receipt(p, 4, "3.000", "1000.00")
print("  после прихода:", state(p), "| отгрузка", costs(s))
print("  возвраты:", [str(give_back(p, 5 + i, s, q)) for i, q in enumerate(("0.700", "0.800", "0.600"))],
      "|", state(p), "| предложение: 155.56, 177.78, 133.33; остаток 4 шт. на 1100.00")

section("С. Две строки одного товара: приход 2 на 200,00 → отгрузка (строка А 2, строка Б 3)")
p = Product.objects.create(name="С", unit=unit)
receipt(p, 1, 2, "200.00")
s = shipment(p, 2, 2, 3)
print("  строки отгрузки (А, Б):", costs(s), "|", state(p))
print("  у строки возврата нет ссылки на строку отгрузки:",
      "shipment_line" not in [f.name for f in CustomerReturn._meta.get_field("lines").related_model._meta.fields])

section("Серийный товар не уходит в минус (даже при разрешённом минусе)")
phone = Product.objects.create(name="Серийный", unit=unit, track_serials=True)
try:
    with transaction.atomic():
        doc = Shipment.objects.create(organization=org, warehouse=W1, customer=customer, date=day(1), time=NOON)
        doc.lines.create(product=phone, quantity=1, price=1000, serial_numbers="X1")
        doc.post()
    print("  отгрузка номера, которого нет на складе: ПРОВЕДЕНА")
except StockError as exc:
    print("  отгрузка номера, которого нет на складе: отказ —", str(exc)[:90])

section("И1. Возврат на другой склад был допустим; затем отменили покрывавшее поступление")
p = Product.objects.create(name="И1", unit=unit)
s = shipment(p, 1, 1)
r = receipt(p, 2, 1, "250.00")
print("  возврат на склад 2:", give_back(p, 3, s, 1, wh=W2), "| склад 1:", state(p), "| склад 2:", state(p, W2))
try:
    r.unpost()
    print("  отмена прихода: ПРОВЕДЕНА | склад 1:", state(p), "| склад 2:", state(p, W2), "| отгрузка", costs(s))
except StockError as exc:
    print("  отмена прихода: отказ —", exc)

section("И2. Перемещение обеспечено приходом; затем приход отменяют / уменьшают / переносят позже")
for name in ("отмена", "уменьшение до 0,5", "перенос позже"):
    p = Product.objects.create(name=f"И2 {name}", unit=unit)
    r = receipt(p, 1, 1, "250.00")
    t = Transfer.objects.create(organization=org, warehouse_from=W1, warehouse_to=W2, date=day(2), time=NOON)
    t.lines.create(product=p, quantity=1)
    t.post()
    try:
        if name == "отмена":
            r.unpost()
        elif name == "уменьшение до 0,5":
            r.lines.update(quantity=D("0.5"))
            r.post()
        else:
            r.date = day(3)
            r.save()
            r.post()
        print(f"  {name}: ПРОВЕДЕНО | склад 1: {state(p)} | склад 2: {state(p, W2)}")
    except StockError as exc:
        print(f"  {name}: отказ — {exc}")
