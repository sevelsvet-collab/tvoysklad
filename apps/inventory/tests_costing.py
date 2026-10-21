"""Хронологическая себестоимость: движения, остатки и строки отгрузок согласованы.

Подтверждённая ошибка: 1 января приняли 10 по 100, 3 января отгрузили 5,
затем добавили приёмку от 2 января — 10 по 200. Себестоимость отгрузки должна
стать 150, а в ShipmentLine.cost_price оставалось 100.
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.catalog.models import Product, Unit
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.reports import services as report_services
from apps.sales.models import CustomerReturn, Shipment, ShipmentLine

from . import costing, services
from .models import StockAdjustment, StockBalance, StockMovement, Transfer


def legacy(model):
    """Прямая запись мимо программы — так тесты создают «старые данные», где
    строки проведённого документа меняли в обход перепроведения.
    ПРАВИЛО ДОПОЛНЕНО (19.10.2026): обычные пути (save, update, delete,
    связанный менеджер) такую запись теперь отклоняют (posting, правило 10)."""
    from django.db import models

    return models.QuerySet(model)


def migration_apps():
    """Модели так, как их видит migrate: исторические, без методов и
    менеджеров программы. ПРАВИЛО ДОПОЛНЕНО (19.10.2026): функции миграций
    раньше вызывались в тестах с текущими моделями — их защита строк (правило
    10) не пропустила бы запись в строки проведённых документов, которую
    миграция выполняет при настоящем migrate."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    return MigrationLoader(connection).project_state().apps

D = Decimal
JAN = [None] + [datetime.date(2026, 1, day) for day in range(1, 29)]   # JAN[3] — 3 января
NOON = datetime.time(12, 0)


class CostingBase(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True, allow_negative_stock=False)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Магазин")
        self.wh3 = Warehouse.objects.create(name="Филиал")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Кабель", unit=self.unit, purchase_price=D("77"))
        self.other = Product.objects.create(name="Провод", unit=self.unit)

    # ---------- документы ----------

    def receipt(self, day, qty, price, warehouse=None, time=NOON, product=None, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                     supplier=self.supplier, date=JAN[day], time=time)
        doc.lines.create(product=product or self.product, quantity=qty, price=price)
        if post:
            doc.post()
        return doc

    def shipment(self, day, qty, warehouse=None, time=NOON, product=None, price=500):
        doc = Shipment.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                      customer=self.customer, date=JAN[day], time=time)
        doc.lines.create(product=product or self.product, quantity=qty, price=price)
        doc.post()
        return doc

    def transfer(self, day, qty, source=None, target=None, time=NOON):
        doc = Transfer.objects.create(organization=self.org, warehouse_from=source or self.wh,
                                      warehouse_to=target or self.wh2, date=JAN[day], time=time)
        doc.lines.create(product=self.product, quantity=qty)
        doc.post()
        return doc

    def customer_return(self, day, qty, shipment=None, warehouse=None, time=NOON, post=True, product=None):
        doc = CustomerReturn.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                            customer=self.customer, shipment=shipment,
                                            date=JAN[day], time=time)
        doc.lines.create(product=product or self.product, quantity=qty, price=500)
        if post:
            doc.post()
        return doc

    def edit_and_repost(self, doc, **changes):
        """Как форма документа: меняем шапку или строку и перепроводим — тем же
        механизмом editing(), что и форма.

        ПРАВИЛО ДОПОЛНЕНО (19.10.2026): склад, дата и время проведённого
        документа меняются только вместе с перепроведением в одной транзакции
        (posting, правило 9). Раньше помощник сохранял шапку обычным save() и
        проводил отдельно — теперь такое сохранение отклоняется. Ожидания
        тестов прежние."""
        line_changes = {k: changes.pop(k) for k in ("quantity", "price") if k in changes}
        with doc.editing() as posted:
            for name, value in changes.items():
                setattr(doc, name, value)
            if changes:
                doc.save()
            if line_changes:
                doc.lines.update(**line_changes)
        if not posted:
            doc.post()

    # ---------- состояние ----------

    def line_cost(self, shipment):
        return shipment.lines.get().cost_price

    def move_cost(self, doc, warehouse=None):
        moves = StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
        if warehouse:
            moves = moves.filter(warehouse=warehouse)
        return moves.get().cost

    def balance(self, warehouse=None, product=None):
        bal = services.current_balance((product or self.product).pk, (warehouse or self.wh).pk)
        return (bal.quantity, bal.avg_cost) if bal else None

    def snapshot(self):
        return (
            list(StockMovement.objects.order_by("pk").values_list("pk", "cost", "quantity", "amount", "open_qty")),
            list(ShipmentLine.objects.order_by("pk").values_list("pk", "cost_price", "cost_amount", "cost_open_qty")),
            list(StockBalance.objects.order_by("product_id", "warehouse_id")
                 .values_list("product_id", "warehouse_id", "quantity", "avg_cost", "value")),
        )

    def assertConsistent(self):
        """Движения, строки отгрузок и остатки сходятся между собой и с расчётом."""
        for product_id in set(StockMovement.objects.values_list("product_id", flat=True)):
            found = costing.differences(costing.build_plan(product_id))
            found = [d for d in found if d.kind != "deficit"]     # продано раньше поступления — замечание
            self.assertEqual(found, [], [d.description for d in found])
        # независимо от расчёта: строка отгрузки = её движение; остаток = сумма движений
        for line in ShipmentLine.objects.filter(shipment__status="posted"):
            movement = StockMovement.objects.get(doc_type="shipment", line_id=line.pk)
            self.assertEqual(line.cost_price, movement.cost)
            self.assertEqual(line.quantity, -movement.quantity)
            self.assertEqual(line.cost_open_qty, movement.open_qty)
        for bal in StockBalance.objects.all():
            total = sum(
                m.quantity for m in
                StockMovement.objects.filter(product=bal.product, warehouse=bal.warehouse)
            )
            self.assertEqual(bal.quantity, total)
        # перемещение: приход по себестоимости своего расхода
        for transfer in Transfer.objects.filter(status="posted"):
            costs = set(StockMovement.objects.filter(doc_type="transfer", doc_id=transfer.pk)
                        .values_list("line_id", "cost"))
            self.assertEqual(len(costs), transfer.lines.count())


class ConfirmedScenarioTests(CostingBase):
    def test_backdated_receipt_changes_shipment_cost(self):
        """Приведённый пример: после приёмки задним числом себестоимость отгрузки — 150."""
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        self.assertEqual(self.line_cost(shipment), D("100.00"))

        self.receipt(2, 10, 200)

        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.move_cost(shipment), D("150.00"))
        self.assertEqual(self.balance(), (D("15"), D("150.00")))
        self.assertConsistent()

    def test_report_uses_recalculated_cost(self):
        """Отчёт «Продажи и прибыль» видит новую себестоимость, а не устаревшую."""
        self.receipt(1, 10, 100)
        self.shipment(3, 5, price=500)
        self.receipt(2, 10, 200)
        summary = report_services.sales_summary(JAN[1], JAN[28])
        self.assertEqual(summary["revenue"], D("2500.00"))
        self.assertEqual(summary["cost"], D("750.00"))
        self.assertEqual(summary["profit"], D("1750.00"))


class ChronologyTests(CostingBase):
    def test_several_receipts_and_partial_shipments(self):
        self.receipt(1, 10, 100)
        first = self.shipment(2, 4)            # 100; остаток 6 на 600
        self.receipt(3, 6, 160)                # 12 на 1560 → 130
        second = self.shipment(4, 3)           # 130; остаток 9 на 1170
        self.receipt(5, 1, 40)                 # 10 на 1210 → 121
        third = self.shipment(6, 10)           # 121; остаток 0
        self.assertEqual(
            [self.line_cost(s) for s in (first, second, third)],
            [D("100.00"), D("130.00"), D("121.00")],
        )
        self.assertEqual(self.balance(), (D("0"), D("0")))
        self.assertConsistent()

    def test_documents_entered_in_reverse_order(self):
        """Порядок ввода не важен: результат определяют даты документов."""
        self.receipt(1, 10, 100)
        self.receipt(5, 1, 40)
        self.receipt(3, 6, 160)
        first, second = self.shipment(2, 4), self.shipment(4, 3)
        self.assertEqual((self.line_cost(first), self.line_cost(second)), (D("100.00"), D("130.00")))
        self.assertEqual(self.balance(), (D("10"), D("121.00")))
        self.assertConsistent()

    def test_price_of_early_receipt_changed(self):
        early = self.receipt(1, 10, 100)
        self.receipt(2, 10, 200)
        shipment = self.shipment(3, 5)
        self.assertEqual(self.line_cost(shipment), D("150.00"))

        self.edit_and_repost(early, price=D("300"))

        self.assertEqual(self.line_cost(shipment), D("250.00"))
        self.assertEqual(self.balance(), (D("15"), D("250.00")))
        self.assertConsistent()

    def test_date_of_early_receipt_changed(self):
        self.receipt(1, 10, 100)
        second = self.receipt(2, 10, 200)
        shipment = self.shipment(3, 5)
        self.assertEqual(self.line_cost(shipment), D("150.00"))

        self.edit_and_repost(second, date=JAN[4])      # приёмка уехала за отгрузку
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(self.balance(), (D("15"), D("166.67")))   # (500 + 2000) / 15

        self.edit_and_repost(second, date=JAN[2])      # и вернулась обратно
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.balance(), (D("15"), D("150.00")))
        self.assertConsistent()

    def test_unposting_early_receipt(self):
        self.receipt(1, 10, 100)
        second = self.receipt(2, 10, 200)
        shipment = self.shipment(3, 5)
        second.unpost()
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(self.balance(), (D("5"), D("100.00")))
        self.assertConsistent()

    def test_deleting_early_receipt(self):
        first = self.receipt(1, 10, 100)
        self.receipt(2, 10, 200)
        shipment = self.shipment(3, 5)
        first.delete()
        self.assertEqual(self.line_cost(shipment), D("200.00"))
        self.assertEqual(self.balance(), (D("5"), D("200.00")))
        self.assertConsistent()

    def test_same_date_different_time(self):
        self.receipt(1, 10, 100, time=datetime.time(9, 0))
        shipment = self.shipment(1, 5, time=datetime.time(12, 0))
        late = self.receipt(1, 10, 200, time=datetime.time(15, 0))
        self.assertEqual(self.line_cost(shipment), D("100.00"))    # приёмка позже в тот же день

        self.edit_and_repost(late, time=datetime.time(10, 0))      # теперь раньше отгрузки
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertConsistent()

    def test_same_date_and_time_keeps_creation_order_after_repost(self):
        """Дата и время совпали — порядок по моменту создания документа.
        Перепроведение раннего документа не переставляет его в конец."""
        early = self.receipt(1, 10, 100)
        shipment = self.shipment(1, 5)
        self.receipt(1, 10, 200)           # создана последней → после отгрузки
        self.assertEqual(self.line_cost(shipment), D("100.00"))

        early.post()                       # движения приёмки получили новые id
        self.assertGreater(
            StockMovement.objects.get(doc_type="receipt", doc_id=early.pk).pk,
            StockMovement.objects.get(doc_type="shipment").pk,
        )
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(self.balance(), (D("15"), D("166.67")))

        shipment.post()                    # и перепроведение самой отгрузки ничего не меняет
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertConsistent()

    def test_changing_shipment_quantity_recalculates_later_shipment(self):
        self.receipt(1, 10, 100)
        first = self.shipment(2, 10)       # склад опустел
        self.receipt(3, 10, 200)
        second = self.shipment(4, 5)
        self.assertEqual(self.line_cost(second), D("200.00"))

        self.edit_and_repost(first, quantity=D("5"))   # 5 по 100 остались → (500 + 2000) / 15
        self.assertEqual(self.line_cost(second), D("166.67"))
        self.assertConsistent()

    def test_supplier_return_and_write_off_use_cost_before_them(self):
        self.receipt(1, 10, 100)
        back = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh,
                                             supplier=self.supplier, date=JAN[4])
        back.lines.create(product=self.product, quantity=2, price=100)
        back.post()
        loss = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org,
                                              warehouse=self.wh, date=JAN[5])
        loss.lines.create(product=self.product, quantity=1)
        loss.post()
        self.assertEqual((self.move_cost(back), self.move_cost(loss)), (D("100.00"), D("100.00")))

        self.receipt(2, 10, 200)
        self.assertEqual((self.move_cost(back), self.move_cost(loss)), (D("150.00"), D("150.00")))
        self.assertEqual(self.balance(), (D("17"), D("150.00")))
        self.assertConsistent()

    def test_other_product_is_not_touched(self):
        self.receipt(1, 10, 100)
        self.receipt(1, 4, 50, product=self.other)
        other_shipment = self.shipment(3, 1, product=self.other)
        before = (self.line_cost(other_shipment), self.balance(product=self.other))
        self.receipt(2, 10, 200)
        self.assertEqual((self.line_cost(other_shipment), self.balance(product=self.other)), before)

    def test_two_lines_of_same_product_in_one_shipment(self):
        self.receipt(1, 10, 100)
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh,
                                      customer=self.customer, date=JAN[3], time=NOON)
        doc.lines.create(product=self.product, quantity=2, price=500)
        doc.lines.create(product=self.product, quantity=3, price=400)
        doc.post()
        self.receipt(2, 10, 200)
        self.assertEqual(sorted(doc.lines.values_list("cost_price", flat=True)), [D("150.00"), D("150.00")])
        self.assertConsistent()


class ZeroAndNegativeTests(CostingBase):
    def test_zero_balance_resets_cost(self):
        """После нуля старая цена не подмешивается к новой партии."""
        self.receipt(1, 5, 100)
        first = self.shipment(2, 5)
        self.receipt(3, 5, 300)
        second = self.shipment(4, 2)
        self.assertEqual((self.line_cost(first), self.line_cost(second)), (D("100.00"), D("300.00")))
        self.assertEqual(self.balance(), (D("3"), D("300.00")))
        self.assertConsistent()

    def test_zero_balance_between_shipments_has_no_cost(self):
        self.receipt(1, 5, 100)
        self.shipment(2, 5)
        self.assertEqual(self.balance(), (D("0"), D("0")))

    def test_negative_stock_allowed(self):
        """Минус разрешён организацией. ПРАВИЛО ИЗМЕНЕНО (согласовано владельцем,
        docs/negative-stock-proposal.md): раньше расход из пустого склада получал 0,
        расход сверх остатка — среднюю на всё количество, а приход, закрывший
        минус, завышал среднюю (было 6 шт. по 166,67). Теперь недостающие единицы —
        дефицит строки, его покрывает следующее поступление по своей цене."""
        self.org.allow_negative_stock = True
        self.org.save()
        self.receipt(1, 2, 100)
        partly = self.shipment(2, 5)       # 2 из остатка (200,00), 3 — дефицит
        empty = self.shipment(3, 1)        # 1 — дефицит
        self.assertEqual((partly.lines.get().cost_amount, partly.lines.get().cost_open_qty), (D("200.00"), D("3")))
        self.assertEqual((empty.lines.get().cost_amount, empty.lines.get().cost_open_qty), (D("0.00"), D("1")))
        self.assertEqual(self.balance(), (D("-4"), D("0")))

        self.receipt(4, 10, 100)           # 10 × 100 = 1000: покрыто 4 шт. на 400, в остаток 6 на 600
        self.assertEqual((partly.lines.get().cost_amount, empty.lines.get().cost_amount), (D("500.00"), D("100.00")))
        self.assertEqual((partly.lines.get().cost_open_qty, empty.lines.get().cost_open_qty), (D("0"), D("0")))
        self.assertEqual(self.balance(), (D("6"), D("100.00")))
        self.assertConsistent()

    def test_backdated_receipt_gives_cost_to_shipment_from_empty_stock(self):
        self.org.allow_negative_stock = True
        self.org.save()
        shipment = self.shipment(3, 4)
        self.assertEqual(self.line_cost(shipment), D("0.00"))
        self.receipt(1, 10, 120)
        self.assertEqual(self.line_cost(shipment), D("120.00"))
        self.assertEqual(self.balance(), (D("6"), D("120.00")))
        self.assertConsistent()


class TransferTests(CostingBase):
    def test_transfer_then_sale_on_other_warehouse(self):
        self.receipt(1, 10, 100)
        transfer = self.transfer(3, 4)
        shipment = self.shipment(4, 2, warehouse=self.wh2)
        self.assertEqual(self.line_cost(shipment), D("100.00"))

        self.receipt(2, 10, 200)           # задним числом на складе-отправителе

        self.assertEqual(self.move_cost(transfer, self.wh), D("150.00"))
        self.assertEqual(self.move_cost(transfer, self.wh2), D("150.00"))
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.balance(self.wh), (D("16"), D("150.00")))
        self.assertEqual(self.balance(self.wh2), (D("2"), D("150.00")))
        self.assertConsistent()

    def test_chain_of_transfers(self):
        """Склад 1 → склад 2 → склад 3 → продажа: изменение доходит до конца цепочки."""
        self.receipt(1, 10, 100)
        self.transfer(3, 6, self.wh, self.wh2)
        self.transfer(4, 4, self.wh2, self.wh3)
        shipment = self.shipment(5, 3, warehouse=self.wh3)
        self.receipt(2, 10, 200)
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.balance(self.wh3), (D("1"), D("150.00")))

    def test_transfer_mixes_with_stock_of_target_warehouse(self):
        self.receipt(1, 10, 100)
        self.receipt(1, 10, 300, warehouse=self.wh2)
        self.transfer(3, 10)                             # 10 по 100 → на складе 2: 20 по 200
        shipment = self.shipment(4, 5, warehouse=self.wh2)
        self.assertEqual(self.line_cost(shipment), D("200.00"))
        self.receipt(2, 10, 200)                         # отправитель: 150 → получатель: 225
        self.assertEqual(self.line_cost(shipment), D("225.00"))
        self.assertConsistent()

    def test_transfer_back_and_forth_on_same_day(self):
        self.receipt(1, 10, 100)
        self.transfer(2, 10, self.wh, self.wh2, time=datetime.time(10, 0))
        self.transfer(2, 10, self.wh2, self.wh, time=datetime.time(11, 0))
        shipment = self.shipment(2, 5, time=datetime.time(12, 0))
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(self.balance(self.wh2), (D("0"), D("0")))
        self.assertConsistent()

    def test_unposting_transfer_recalculates_target_warehouse(self):
        self.receipt(1, 10, 100)
        self.receipt(1, 10, 300, warehouse=self.wh2)
        transfer = self.transfer(3, 10)
        self.assertEqual(self.balance(self.wh2), (D("20"), D("200.00")))
        transfer.unpost()
        self.assertEqual(self.balance(self.wh2), (D("10"), D("300.00")))
        self.assertEqual(self.balance(self.wh), (D("10"), D("100.00")))


class CustomerReturnTests(CostingBase):
    def test_return_uses_cost_of_source_shipment(self):
        """Возврат после изменения средней: по себестоимости исходной отгрузки."""
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5)                   # 100
        self.receipt(3, 10, 200)                         # средняя стала 166.67
        self.assertEqual(self.balance(), (D("15"), D("166.67")))

        back = self.customer_return(4, 2, shipment=shipment)

        self.assertEqual(self.move_cost(back), D("100.00"))      # не 166.67 и не цена карточки 77
        self.assertEqual(self.balance(), (D("17"), D("158.82")))  # (2500 + 200) / 17
        self.assertConsistent()

    def test_partial_and_repeated_returns(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5)
        self.receipt(3, 10, 200)
        first = self.customer_return(4, 2, shipment=shipment)
        second = self.customer_return(5, 1, shipment=shipment)
        third = self.customer_return(5, 2, shipment=shipment, time=datetime.time(18, 0))
        self.assertEqual(
            [self.move_cost(doc) for doc in (first, second, third)],
            [D("100.00")] * 3,
        )
        self.assertEqual(self.balance(), (D("20"), D("150.00")))  # всё вернулось: 1000 + 2000
        self.assertConsistent()

    def test_return_follows_recalculated_shipment_cost(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        back = self.customer_return(4, 2, shipment=shipment)
        later = self.shipment(5, 1)
        self.assertEqual(self.move_cost(back), D("100.00"))

        self.receipt(2, 10, 200)           # отгрузка стала стоить 150 — возврат тоже

        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.move_cost(back), D("150.00"))
        self.assertEqual(self.line_cost(later), D("150.00"))
        self.assertConsistent()

    def test_return_to_another_warehouse(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5)
        self.receipt(3, 10, 400, warehouse=self.wh2)
        back = self.customer_return(4, 2, shipment=shipment, warehouse=self.wh2)
        self.assertEqual(self.move_cost(back), D("100.00"))
        self.assertEqual(self.balance(self.wh2), (D("12"), D("350.00")))   # (4000 + 200) / 12

    def test_return_of_product_shipped_in_two_lines(self):
        """Один товар двумя строками: первая обеспечена остатком, у второй дефицит.
        ПРАВИЛО ИЗМЕНЕНО: раньше возврат без строки оценивался средним по строкам
        (50,00 — с «бесплатной» второй строкой). Теперь возврат относится к строке:
        без её указания при дефиците он отклоняется, по строке — считается точно."""
        self.org.allow_negative_stock = True
        self.org.save()
        self.receipt(1, 2, 100)
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh,
                                      customer=self.customer, date=JAN[2], time=NOON)
        covered = doc.lines.create(product=self.product, quantity=2, price=500)    # из остатка, 200,00
        deficit = doc.lines.create(product=self.product, quantity=2, price=500)    # склад пуст — дефицит 2
        doc.post()
        unlinked = self.customer_return(3, 1, shipment=doc, post=False)
        with self.assertRaisesMessage(services.StockError, "Укажите в возврате, из какой строки отгрузки"):
            unlinked.post()
        unlinked.lines.update(shipment_line=deficit)          # вернули единицу второй строки
        unlinked.post()
        self.assertEqual(self.move_cost(unlinked), D("0.00"))                     # её стоимость ещё не определена
        self.assertEqual(ShipmentLine.objects.get(pk=deficit.pk).cost_open_qty, D("1"))
        by_first = self.customer_return(4, 1, shipment=doc, post=False)
        by_first.lines.update(shipment_line=covered)
        by_first.post()
        self.assertEqual(self.move_cost(by_first), D("100.00"))                   # r(200 × 1 / 2)

    def test_return_without_basis_uses_average_before_it(self):
        """Правило без основания: средняя на складе возврата перед операцией."""
        self.receipt(1, 10, 100)
        self.shipment(2, 5)
        self.receipt(3, 10, 200)
        back = self.customer_return(4, 2)
        self.assertEqual(self.move_cost(back), D("166.67"))
        self.assertEqual(self.balance(), (D("17"), D("166.67")))         # средняя не меняется

        # история изменилась — возврат пересчитан по хронологии: после отгрузки
        # 5 по 100, затем +5 по 400 (тот же день, создана позже) и +10 по 200 → 4500 на 20
        self.receipt(2, 5, 400)
        self.assertEqual(self.move_cost(back), D("225.00"))
        self.assertConsistent()

    def test_return_without_basis_on_empty_stock_uses_last_known_average(self):
        self.receipt(1, 5, 120)
        self.shipment(2, 5)                # склад пуст, последняя средняя — 120
        back = self.customer_return(3, 1)
        self.assertEqual(self.move_cost(back), D("120.00"))
        self.assertEqual(self.balance(), (D("1"), D("120.00")))

    def test_return_without_basis_and_history_uses_card_price(self):
        back = self.customer_return(3, 1)
        self.assertEqual(self.move_cost(back), D("77.00"))               # закупочная цена карточки

    # Раньше здесь были два теста, закреплявших обратное: возврат с основанием,
    # которое стоит позже или не проведено, молча считался «без основания».
    # Правило изменено: такая операция отклоняется (см. tests_costing_rules,
    # ReturnBasisTests) — основание либо годится, либо его нужно убрать явно.

    def test_basis_dated_after_return_is_rejected(self):
        self.receipt(1, 10, 100)
        self.receipt(2, 10, 200)
        shipment = self.shipment(5, 5)
        back = self.customer_return(4, 1, shipment=shipment, post=False)
        before = self.snapshot()
        with self.assertRaisesMessage(services.StockError, "не раньше возврата"):
            back.post()
        back.refresh_from_db()
        self.assertEqual(back.status, "draft")
        self.assertEqual(self.snapshot(), before)

    def test_unposting_basis_of_posted_return_is_rejected(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5)
        self.receipt(3, 10, 200)
        back = self.customer_return(4, 2, shipment=shipment)
        before = self.snapshot()
        with self.assertRaisesMessage(services.StockError, "На документ ссылается проведённый возврат"):
            shipment.unpost()
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, "posted")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.move_cost(back), D("100.00"))
        self.assertConsistent()


class RoundingAndRepeatTests(CostingBase):
    def test_rounding_keeps_total_cost(self):
        """Копейки не теряются: сумма себестоимости отгрузок равна сумме закупок."""
        self.receipt(1, 1, D("10.00"))
        self.receipt(1, 2, D("10.01"), time=datetime.time(13, 0))        # 30.02 на 3 → 10.0067
        shipments = [self.shipment(day, 1) for day in (2, 3, 4)]
        costs = [self.line_cost(s) for s in shipments]
        self.assertEqual(costs, [D("10.01"), D("10.01"), D("10.00")])
        self.assertEqual(sum(costs), D("30.02"))
        self.assertEqual(self.balance(), (D("0"), D("0")))

    def test_half_cent_rounds_up(self):
        self.assertEqual(costing.round_cost(D("10.005")), D("10.01"))
        self.assertEqual(costing.round_cost(D("10.015")), D("10.02"))
        self.assertEqual(costing.round_cost(D("10.0049")), D("10.00"))

    def test_fractional_quantities(self):
        self.receipt(1, D("2.500"), D("99.99"))
        self.receipt(2, D("1.250"), D("120.50"))         # 249.975 + 150.625 = 400.6 на 3.75 → 106.83
        shipment = self.shipment(3, D("0.750"))
        self.assertEqual(self.line_cost(shipment), D("106.83"))
        self.assertIsInstance(self.line_cost(shipment), Decimal)
        self.assertConsistent()

    def test_repeated_recalculation_changes_nothing(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        self.transfer(4, 2)
        self.customer_return(5, 1, shipment=shipment)
        self.shipment(6, 1, warehouse=self.wh2)
        self.receipt(2, 10, 200)
        before = self.snapshot()

        for _ in range(3):
            self.assertEqual(costing.recalculate([self.product.pk, self.other.pk]), [])
            services.recompute_balance(self.product.pk, self.wh.pk)
        self.assertEqual(self.snapshot(), before)

    def test_repost_of_every_document_changes_nothing(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        self.transfer(4, 2)
        self.customer_return(5, 1, shipment=shipment)
        self.receipt(2, 10, 200)

        def costs():
            return (
                sorted(StockMovement.objects.values_list("doc_type", "doc_id", "warehouse_id", "cost")),
                list(ShipmentLine.objects.order_by("pk").values_list("cost_price", flat=True)),
                list(StockBalance.objects.order_by("warehouse_id").values_list("quantity", "avg_cost")),
            )

        before = costs()
        for model in (Receipt, Shipment, Transfer, CustomerReturn):
            for doc in model.objects.order_by("-pk"):
                doc.post()
        self.assertEqual(costs(), before)

    def test_mixed_history_is_consistent(self):
        """Согласованность стоимости движений, остатков и строк отгрузок."""
        self.receipt(1, 10, 100)
        self.receipt(1, 7, D("133.33"), warehouse=self.wh2)
        first = self.shipment(3, 4)
        self.transfer(4, 3)
        self.shipment(5, 6, warehouse=self.wh2)
        self.customer_return(6, 2, shipment=first)
        self.transfer(7, 2, self.wh2, self.wh3)
        self.shipment(8, 1, warehouse=self.wh3)
        self.receipt(2, 10, 200)
        self.receipt(2, 3, D("17.77"), warehouse=self.wh2, time=datetime.time(8, 0))
        self.assertConsistent()
        # полная стоимость сохраняется: приход по документам = расход наружу + остаток
        moves = StockMovement.objects.filter(product=self.product)
        external = moves.exclude(doc_type="transfer")
        net = sum((m.quantity * m.cost for m in external), D("0"))
        internal = sum((m.quantity * m.cost for m in moves.filter(doc_type="transfer")), D("0"))
        self.assertEqual(internal, D("0"))               # перемещения стоимость не создают
        left = sum(
            (b.quantity * b.avg_cost for b in StockBalance.objects.filter(product=self.product)), D("0"),
        )
        self.assertLess(abs(net - left), D("0.10"))      # расхождение — только округление средней


class DiagnosticCommandTests(CostingBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("check_costs", *args, stdout=out)
        return out.getvalue()

    def make_stale(self):
        """Состояние «как до исправления»: остатки новые, строка отгрузки старая."""
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        self.receipt(2, 10, 200)
        ShipmentLine.objects.filter(shipment=shipment).update(cost_price=D("100.00"), cost_amount=D("500.00"))
        StockMovement.objects.filter(doc_type="shipment").update(cost=D("100.00"), amount=D("500.00"))
        StockBalance.objects.filter(product=self.product).update(avg_cost=D("166.67"), value=D("2500.00"))
        return shipment

    def test_clean_database(self):
        self.receipt(1, 10, 100)
        self.shipment(3, 5)
        self.assertIn("Расхождений нет", self.run_command())

    def test_dry_run_reports_and_writes_nothing(self):
        shipment = self.make_stale()
        before = self.snapshot()
        output = self.run_command()
        self.assertIn("запись выключена", output)
        self.assertIn("Найдено расхождений: 3", output)
        self.assertIn("в базе (100.00, 500.00), по расчёту (150.00, 750.00)", output)
        self.assertIn("Ничего не записано", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.line_cost(shipment), D("100.00"))

    def test_apply_requires_backup_confirmation(self):
        shipment = self.make_stale()
        before = self.snapshot()
        with self.assertRaises(CommandError):
            self.run_command("--apply")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.line_cost(shipment), D("100.00"))

    def test_apply_fixes(self):
        shipment = self.make_stale()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("Исправлено расхождений: 3", output)
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertEqual(self.balance(), (D("15"), D("150.00")))
        self.assertIn("Расхождений нет", self.run_command())

    def test_single_product_filter(self):
        self.make_stale()
        self.assertIn("Расхождений нет", self.run_command("--product", str(self.other.pk)))
        self.assertIn("Найдено расхождений: 3", self.run_command("--product", str(self.product.pk)))

    def test_old_movements_are_linked_when_the_line_is_unambiguous(self):
        """Старые движения не помнят строку. Одна строка товара в документе —
        соответствие однозначно: ссылка записывается, себестоимость исправляется."""
        shipment = self.make_stale()
        StockMovement.objects.update(line_id=None, amount=None, time=datetime.time(0, 0))
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", output)
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertFalse(StockMovement.objects.filter(line_id=None).exists())
        self.assertIn("Расхождений нет", self.run_command())

    def test_line_changed_behind_posting_is_reported_and_product_left_untouched(self):
        """Строку изменили в обход проведения — пересчёт не угадывает. Товар
        не исправляется вовсе: ни движения, ни строки, ни остатки."""
        shipment = self.make_stale()
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(ShipmentLine).filter(shipment=shipment).update(quantity=D("4"))
        before = self.snapshot()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — движения не сопоставляются со строками документа: 1", output)
        self.assertIn("Исправлено расхождений: 0", output)
        self.assertIn(f"Товары НЕ исправлены и не изменены (id: {self.product.pk})", output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.line_cost(shipment), D("100.00"))


class SalesReportReturnsTests(CostingBase):
    def test_report_subtracts_returns(self):
        """ПОВЕДЕНИЕ ИЗМЕНЕНО: раньше здесь было закреплено ограничение — отчёт
        «Продажи и прибыль» считал только валовые отгрузки, и возврат не менял
        ни выручку, ни себестоимость (2500 / 500 / 2000). Теперь возврат
        уменьшает выручку на свою сумму (2 × 500 = 1000) и себестоимость на
        восстановленную складским движением (2 × 100 = 200); продажи по-прежнему
        видны отдельно (docs/sales-report.md)."""
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5, price=500)
        before = report_services.sales_summary(JAN[1], JAN[28])
        self.customer_return(3, 2, shipment=shipment)
        after = report_services.sales_summary(JAN[1], JAN[28])
        self.assertEqual((before["revenue"], before["cost"], before["profit"]),
                         (D("2500.00"), D("500.00"), D("2000.00")))
        self.assertEqual((after["sales_revenue"], after["sales_cost"], after["sales_profit"]),
                         (D("2500.00"), D("500.00"), D("2000.00")))
        self.assertEqual((after["returns_revenue"], after["returns_cost"]), (D("1000.00"), D("200.00")))
        self.assertEqual((after["revenue"], after["cost"], after["profit"]),
                         (D("1500.00"), D("300.00"), D("1200.00")))
