"""Сохранение общей стоимости: деньги не теряются и не возникают из округления.

Подтверждённая ошибка: приёмка 100 000 шт. по 0,99 со скидкой 10 % — итог
89 100,00. Средняя 0,89, остаток показывался как 89 000,00, полная отгрузка
получала себестоимость 89 000,00 — 100 рублей закупочной стоимости исчезали.

Ожидаемые числа в тестах посчитаны отдельно от кода (вручную, по правилам из
apps/inventory/costing.py) и записаны константами.
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from apps.core import roles
from apps.purchases.models import Receipt, ReceiptLine, SupplierReturn
from apps.reports import services as report_services
from apps.sales.models import Shipment, ShipmentLine

from . import costing
from .models import StockAdjustment, StockBalance, StockMovement
from .tests_costing import JAN, NOON, CostingBase, legacy

D = Decimal


class ConservationBase(CostingBase):
    def lot(self, day=1, qty=100000, price="0.99", discount=10, warehouse=None, time=NOON):
        """Приёмка со скидкой. По умолчанию — партия из воспроизведения: 89 100,00."""
        doc = Receipt.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                     supplier=self.supplier, date=JAN[day], time=time)
        doc.lines.create(product=self.product, quantity=qty, price=D(price), discount=discount)
        doc.post()
        return doc

    def three_for_hundred(self):
        """Три единицы общей стоимостью 100,00: 3 × 50 со скидкой 33,333 %."""
        doc = self.lot(qty=3, price="50", discount=D("33.333"))
        self.assertEqual(doc.lines.get().total, D("100.00"))
        return doc

    def value(self, warehouse=None):
        bal = StockBalance.objects.filter(product=self.product, warehouse=warehouse or self.wh).first()
        return (bal.quantity, bal.value) if bal else None

    def cost_amount(self, shipment):
        return shipment.lines.get().cost_amount

    def amount(self, doc, warehouse=None):
        moves = StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
        if warehouse:
            moves = moves.filter(warehouse=warehouse)
        return moves.get().amount

    def report_cost(self):
        """Себестоимость после возвратов по отчёту «Продажи и прибыль»."""
        return report_services.sales_summary(JAN[1], JAN[28])["cost"]

    def report_sales_cost(self):
        """Себестоимость продаж (без вычета возвратов) по тому же отчёту."""
        return report_services.sales_summary(JAN[1], JAN[28])["sales_cost"]

    def purchased(self):
        """Сколько заплачено поставщикам — по документам приёмки."""
        return sum((line.total for line in ReceiptLine.objects.filter(receipt__status="posted")), D("0"))

    def stock_value(self):
        return sum((b.value for b in StockBalance.objects.filter(product=self.product)), D("0"))

    def assertConserved(self):
        """Закуплено по документам = себестоимость проданного (по отчёту)
        + стоимость остатков; и по каждому складу остаток = приходы − расходы.
        Применимо, пока нет списаний, возвратов и минуса."""
        self.assertEqual(self.purchased(), self.report_cost() + self.stock_value())
        for bal in StockBalance.objects.filter(product=self.product):
            moves = StockMovement.objects.filter(product=self.product, warehouse=bal.warehouse)
            incoming = sum((m.amount for m in moves if m.quantity > 0), D("0"))
            outgoing = sum((m.amount for m in moves if m.quantity < 0), D("0"))
            self.assertEqual(bal.value, incoming - outgoing)
        for line in ShipmentLine.objects.filter(shipment__status="posted"):
            self.assertEqual(line.cost_amount, StockMovement.objects.get(doc_type="shipment", line_id=line.pk).amount)

    def run_command(self, *args):
        out = StringIO()
        call_command("check_costs", *args, stdout=out)
        return out.getvalue()


class ReportedCaseTests(ConservationBase):
    def test_full_sale_writes_off_whole_purchase_value(self):
        """Воспроизведение: поступление 89 100, остаток 89 100, продажа 89 100, остаток 0."""
        receipt = self.lot()
        self.assertEqual(receipt.total, D("89100.00"))
        self.assertEqual(self.amount(receipt), D("89100.00"))
        balance = StockBalance.objects.get()
        self.assertEqual((balance.quantity, balance.avg_cost), (D("100000"), D("0.89")))
        self.assertEqual(balance.value, D("89100.00"))
        self.assertEqual(balance.total_value, D("89100.00"))           # было 89 000,00

        shipment = self.shipment(2, 100000, price=2)
        line = shipment.lines.get()
        self.assertEqual(line.cost_amount, D("89100.00"))
        self.assertEqual(line.cost_total, D("89100.00"))               # было 89 000,00
        self.assertEqual(shipment.cost_total, D("89100.00"))
        self.assertEqual(shipment.profit_total, D("110900.00"))        # 200 000 − 89 100
        self.assertEqual(line.cost_price, D("0.89"))                   # цена единицы — только для показа
        self.assertEqual(self.amount(shipment), D("89100.00"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))

        summary = report_services.sales_summary(JAN[1], JAN[28])
        self.assertEqual(summary["cost"], D("89100.00"))
        self.assertEqual(summary["profit"], D("110900.00"))            # было 111 000,00
        self.assertConserved()

    def test_same_lot_sold_in_three_parts(self):
        self.lot()
        parts = [self.shipment(day, qty) for day, qty in ((2, 33333), (3, 33333), (4, 33334))]
        # 89100 × 33333/100000 = 29699.703; 59400.30 × 33333/66667 = 29699.7045; остаток 29700.60
        self.assertEqual([self.cost_amount(s) for s in parts],
                         [D("29699.70"), D("29699.70"), D("29700.60")])
        self.assertEqual(sum(self.cost_amount(s) for s in parts), D("89100.00"))
        self.assertEqual(self.report_cost(), D("89100.00"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertConserved()

    def test_uneven_parts_take_rounding_remainder_last(self):
        self.lot()
        parts = [self.shipment(day, qty) for day, qty in ((2, 1), (3, 49999), (4, 50000))]
        self.assertEqual([self.cost_amount(s) for s in parts], [D("0.89"), D("44549.11"), D("44550.00")])
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertConserved()

    def test_value_after_partial_sale(self):
        self.lot()
        self.shipment(2, 33333)
        self.assertEqual(self.value(), (D("66667"), D("59400.30")))    # 89100.00 − 29699.70
        self.assertEqual(StockBalance.objects.get().total_value, D("59400.30"))   # не 66667 × 0.89 = 59333.63
        self.assertConserved()

    def test_balance_list_totals_use_stored_value(self):
        self.lot()
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")
        response = self.client.get(reverse("balance_list"))
        self.assertEqual(response.context["total_value"], D("89100.00"))
        self.assertEqual(response.context["balances"][0].total_value, D("89100.00"))

    def test_supplier_return_and_write_off_take_their_share(self):
        self.lot()
        back = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh,
                                             supplier=self.supplier, date=JAN[2])
        back.lines.create(product=self.product, quantity=40000, price=1)
        back.post()
        self.assertEqual(self.amount(back), D("35640.00"))             # 89100 × 0.4
        loss = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org,
                                              warehouse=self.wh, date=JAN[3])
        loss.lines.create(product=self.product, quantity=60000)
        loss.post()
        self.assertEqual(self.amount(loss), D("53460.00"))             # всё, что осталось
        self.assertEqual(self.value(), (D("0"), D("0.00")))


class SmallLotTests(ConservationBase):
    def test_three_units_for_hundred_sold_one_by_one(self):
        self.three_for_hundred()
        self.assertEqual(self.value(), (D("3"), D("100.00")))
        sold = [self.shipment(day, 1) for day in (2, 3, 4)]
        # 100/3 = 33.333 → 33.33; 66.67/2 = 33.335 → 33.34; последний забирает остаток 33.33
        self.assertEqual([self.cost_amount(s) for s in sold], [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.report_cost(), D("100.00"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertConserved()

    def test_three_units_sold_at_once_equal_sold_one_by_one(self):
        self.three_for_hundred()
        shipment = self.shipment(2, 3)
        self.assertEqual(self.cost_amount(shipment), D("100.00"))
        self.assertEqual(shipment.lines.get().cost_price, D("33.33"))
        self.assertEqual(shipment.lines.get().cost_total, D("100.00"))  # не 3 × 33.33 = 99.99

    def test_fractional_quantities_and_discount(self):
        # 2.5 × 99.99 = 249.975, −12.5 % = 218.728125 → строка 218.73
        self.lot(qty=D("2.500"), price="99.99", discount=D("12.5"))
        sold = [self.shipment(day, qty) for day, qty in ((2, D("0.500")), (3, D("1.250")), (4, D("0.750")))]
        # 218.73 × 0.5/2.5 = 43.746 → 43.75; 174.98 × 1.25/2 = 109.3625 → 109.36; остаток 65.62
        self.assertEqual([self.cost_amount(s) for s in sold], [D("43.75"), D("109.36"), D("65.62")])
        self.assertEqual(self.report_cost(), D("218.73"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertConserved()

    def test_two_receipts_mix_by_value(self):
        self.three_for_hundred()
        self.receipt(2, 1, D("0.02"))
        self.assertEqual(self.value(), (D("4"), D("100.02")))
        first = self.shipment(3, 1)                                    # 100.02/4 = 25.005 → 25.01
        rest = self.shipment(4, 3)                                     # остаток 75.01
        self.assertEqual((self.cost_amount(first), self.cost_amount(rest)), (D("25.01"), D("75.01")))
        self.assertConserved()

    def test_two_lines_of_one_product_in_one_shipment(self):
        self.three_for_hundred()
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh,
                                      customer=self.customer, date=JAN[2], time=NOON)
        doc.lines.create(product=self.product, quantity=1, price=500)
        doc.lines.create(product=self.product, quantity=2, price=500)
        doc.post()
        self.assertEqual(list(doc.lines.order_by("pk").values_list("cost_amount", flat=True)),
                         [D("33.33"), D("66.67")])
        self.assertEqual(doc.cost_total, D("100.00"))


class TransferConservationTests(ConservationBase):
    def test_full_transfer_moves_whole_value(self):
        self.lot()
        transfer = self.transfer(2, 100000)
        self.assertEqual(self.amount(transfer, self.wh), D("89100.00"))
        self.assertEqual(self.amount(transfer, self.wh2), D("89100.00"))
        self.assertEqual(self.value(self.wh), (D("0"), D("0.00")))
        self.assertEqual(self.value(self.wh2), (D("100000"), D("89100.00")))
        shipment = self.shipment(3, 100000, warehouse=self.wh2)
        self.assertEqual(self.cost_amount(shipment), D("89100.00"))
        self.assertConserved()

    def test_partial_transfer_splits_value_without_loss(self):
        self.lot()
        self.transfer(2, 33333)
        self.assertEqual(self.value(self.wh), (D("66667"), D("59400.30")))
        self.assertEqual(self.value(self.wh2), (D("33333"), D("29699.70")))
        self.assertEqual(self.stock_value(), D("89100.00"))

    def test_chain_of_warehouses_one_unit_at_a_time(self):
        """Три единицы на 100,00 по одной уходят по цепочке складов 1 → 2 → 3
        и продаются там. Ни на одном шаге сумма по складам не меняется."""
        self.three_for_hundred()
        sold = []
        for step, day in enumerate((2, 5, 8)):
            self.transfer(day, 1, self.wh, self.wh2)
            self.assertEqual(self.stock_value() + sum(self.cost_amount(s) for s in sold), D("100.00"))
            self.transfer(day + 1, 1, self.wh2, self.wh3)
            self.assertEqual(self.stock_value() + sum(self.cost_amount(s) for s in sold), D("100.00"))
            sold.append(self.shipment(day + 2, 1, warehouse=self.wh3))
        self.assertEqual([self.cost_amount(s) for s in sold], [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.report_cost(), D("100.00"))
        self.assertEqual(self.stock_value(), D("0.00"))
        self.assertConserved()

    def test_chain_of_warehouses_large_lot(self):
        self.lot()
        self.transfer(2, 33333, self.wh, self.wh2)
        self.transfer(3, 33333, self.wh2, self.wh3)
        self.transfer(4, 66667, self.wh, self.wh3)
        self.assertEqual(self.value(self.wh3), (D("100000"), D("89100.00")))
        shipment = self.shipment(5, 100000, warehouse=self.wh3)
        self.assertEqual(self.cost_amount(shipment), D("89100.00"))
        self.assertConserved()


class ReturnConservationTests(ConservationBase):
    def test_partial_returns_restore_whole_shipment_cost(self):
        self.three_for_hundred()
        shipment = self.shipment(2, 3)                                 # себестоимость 100.00
        returns = [self.customer_return(day, 1, shipment=shipment) for day in (3, 4, 5)]
        # нарастающим итогом: 33.33, 66.67, 100.00
        self.assertEqual([self.amount(doc) for doc in returns], [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.value(), (D("3"), D("100.00")))

    def test_full_return_in_one_document(self):
        self.lot()
        shipment = self.shipment(2, 100000)
        back = self.customer_return(3, 100000, shipment=shipment)
        self.assertEqual(self.amount(back), D("89100.00"))
        self.assertEqual(self.value(), (D("100000"), D("89100.00")))

    def test_partial_return_of_partial_shipment(self):
        self.three_for_hundred()
        shipment = self.shipment(2, 2)                                 # 100 × 2/3 = 66.666 → 66.67
        self.assertEqual(self.cost_amount(shipment), D("66.67"))
        self.assertEqual(self.value(), (D("1"), D("33.33")))
        first = self.customer_return(3, 1, shipment=shipment)          # 66.67/2 = 33.335 → 33.34
        self.assertEqual(self.amount(first), D("33.34"))
        self.assertEqual(self.value(), (D("2"), D("66.67")))
        second = self.customer_return(4, 1, shipment=shipment)         # остаток 33.33
        self.assertEqual(self.amount(second), D("33.33"))
        self.assertEqual(self.value(), (D("3"), D("100.00")))          # всё вернулось — стоимость прежняя

    def test_return_of_large_lot_in_parts(self):
        self.lot()
        shipment = self.shipment(2, 100000)
        parts = [self.customer_return(day, qty, shipment=shipment)
                 for day, qty in ((3, 33333), (4, 33333), (5, 33334))]
        # итог: 29699.70, 59399.41 (89100 × 66666/100000 = 59399.406), 89100.00
        self.assertEqual([self.amount(doc) for doc in parts], [D("29699.70"), D("29699.71"), D("29700.59")])
        self.assertEqual(self.value(), (D("100000"), D("89100.00")))

    def test_return_then_resale(self):
        self.three_for_hundred()
        shipment = self.shipment(2, 3)
        self.customer_return(3, 1, shipment=shipment)                  # 33.33 вернулось
        resale = self.shipment(4, 1)
        self.assertEqual(self.cost_amount(resale), D("33.33"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        # продано на 100.00, вернули 33.33, перепродали 33.33
        self.assertEqual(self.report_sales_cost(), D("133.33"))
        # ПОВЕДЕНИЕ ИЗМЕНЕНО (docs/sales-report.md): раньше отчёт был валовым и
        # показывал 133,33; теперь вычитает возвращённое — 133,33 − 33,33
        self.assertEqual(self.report_cost(), D("100.00"))

    def test_return_without_basis_keeps_average(self):
        self.three_for_hundred()
        back = self.customer_return(2, 1)                              # по средней: 100 × 1/3 → 33.33
        self.assertEqual(self.amount(back), D("33.33"))
        self.assertEqual(self.value(), (D("4"), D("133.33")))


class RepeatabilityTests(ConservationBase):
    def build(self):
        self.lot()
        self.lot(day=2, qty=3, price="50", discount=D("33.333"), warehouse=self.wh2)
        first = self.shipment(3, 33333)
        self.transfer(4, 33333)
        self.shipment(5, 1000, warehouse=self.wh2)
        self.customer_return(6, 11111, shipment=first)
        self.transfer(7, 7, self.wh2, self.wh3)
        self.shipment(8, 5, warehouse=self.wh3)

    def test_repeated_recalculation_changes_nothing(self):
        self.build()
        before = self.snapshot()
        for _ in range(3):
            self.assertEqual(costing.recalculate([self.product.pk]), [])
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Расхождений нет", self.run_command())

    def test_reposting_every_document_changes_nothing(self):
        self.build()

        def state():
            return (
                sorted(StockMovement.objects.values_list("doc_type", "doc_id", "warehouse_id", "quantity", "amount", "cost")),
                list(ShipmentLine.objects.order_by("pk").values_list("cost_amount", "cost_price")),
                list(StockBalance.objects.order_by("warehouse_id").values_list("quantity", "value", "avg_cost")),
            )

        before = state()
        from apps.sales.models import CustomerReturn
        from .models import Transfer
        for model in (Receipt, Shipment, Transfer, CustomerReturn):
            for doc in model.objects.order_by("-pk"):
                doc.post()
        self.assertEqual(state(), before)

    def test_value_flow_is_consistent_everywhere(self):
        """Движения, остатки, строки документов и отчёт сходятся с документами
        закупки: закуплено = себестоимость продаж − возвращённое + остатки."""
        self.build()
        returned = sum((m.amount for m in StockMovement.objects.filter(doc_type="customer_return")), D("0"))
        self.assertEqual(self.purchased(), D("89200.00"))              # 89 100 + 100 по документам
        # ПОВЕДЕНИЕ ИЗМЕНЕНО: отчёт сам вычитает возвращённое (раньше здесь
        # было report_cost() − returned); себестоимость продаж — отдельно
        self.assertEqual(self.report_cost(), self.report_sales_cost() - returned)
        self.assertEqual(self.purchased(), self.report_cost() + self.stock_value())
        for bal in StockBalance.objects.all():
            moves = StockMovement.objects.filter(product=bal.product, warehouse=bal.warehouse)
            self.assertEqual(bal.value, sum((m.amount if m.quantity > 0 else -m.amount for m in moves), D("0")))
        transfers = StockMovement.objects.filter(doc_type="transfer")
        self.assertEqual(sum((m.amount if m.quantity > 0 else -m.amount for m in transfers), D("0")), D("0"))
        shipped = sum((m.amount for m in StockMovement.objects.filter(doc_type="shipment")), D("0"))
        self.assertEqual(shipped, self.report_sales_cost())
        self.assertEqual(shipped, sum((s.cost_total for s in Shipment.objects.all()), D("0")))


class NegativeStockInteractionTests(ConservationBase):
    """Разрешённый минус. ПРАВИЛО ИЗМЕНЕНО (согласовано владельцем,
    docs/negative-stock-proposal.md): раньше стоимость здесь не сохранялась —
    расход сверх остатка списывал больше, чем было, а приход на минус оставлял
    стоимость на нулевом количестве. Теперь недостающие единицы — дефицит строки,
    его покрывает следующее поступление, и стоимость сохраняется копейка в копейку."""

    def setUp(self):
        super().setUp()
        self.org.allow_negative_stock = True
        self.org.save()

    def test_issue_exceeding_stock_takes_only_what_there_was(self):
        self.receipt(1, 2, 100)                                        # стоимость 200
        over = self.shipment(2, 5)
        self.assertEqual(self.cost_amount(over), D("200.00"))          # было 500 — при стоимости 200
        self.assertEqual(over.lines.get().cost_open_qty, D("3"))
        self.assertEqual(self.value(), (D("-3"), D("0.00")))
        self.assertIn("проданы (списаны) раньше поступления", self.run_command())

    def test_issue_from_empty_stock_is_covered_by_receipt(self):
        empty = self.shipment(2, 4)
        self.assertEqual((self.cost_amount(empty), empty.lines.get().cost_open_qty), (D("0.00"), D("4")))
        self.receipt(3, 10, 100)                                       # 1000: 400 на дефицит, 600 в остаток
        self.assertEqual(self.cost_amount(empty), D("400.00"))
        self.assertEqual(self.value(), (D("6"), D("600.00")))          # было 6 шт. на 1000 (средняя 166,67)
        self.assertEqual(StockBalance.objects.get().avg_cost, D("100.00"))

    def test_receipt_that_closes_the_minus_leaves_no_value_on_zero_stock(self):
        """Найдено на копии базы: отгрузка из пустого склада, затем приход ровно на
        это количество. Раньше: количество 0, а 250,00 «висели» на остатке и
        переходили на следующую партию (500,00 за 1 шт.). Теперь приход покрывает
        отгрузку, на нулевом остатке стоимости нет."""
        early = self.shipment(2, 1)
        self.receipt(3, 1, 250)
        self.assertEqual(self.cost_amount(early), D("250.00"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.receipt(4, 1, 250)
        self.assertEqual(self.value(), (D("1"), D("250.00")))
        late = self.shipment(5, 1)
        self.assertEqual(self.cost_amount(late), D("250.00"))          # закуплено 500 — списано 250 + 250
        self.assertEqual(self.value(), (D("0"), D("0.00")))

    def test_conservation_with_sale_before_receipt(self):
        first = self.shipment(2, 4)
        self.receipt(3, 10, 100)
        rest = self.shipment(4, 6)
        self.assertEqual((self.cost_amount(first), self.cost_amount(rest)), (D("400.00"), D("600.00")))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertEqual(self.purchased(), self.report_cost() + self.stock_value())   # 1000 = 1000 + 0


class LegacyTotalsDiagnosticsTests(ConservationBase):
    """Данные до обновления: точных сумм нет, есть только округлённая цена."""

    def make_legacy(self):
        self.lot()
        shipment = self.shipment(2, 100000, price=2)
        StockMovement.objects.update(amount=None, line_id=None)
        ShipmentLine.objects.update(cost_amount=None)
        StockBalance.objects.update(value=None)
        return shipment

    def test_fallback_until_recalculated_matches_old_numbers(self):
        """До пересчёта показывается прежняя оценка — так же, как до обновления."""
        shipment = self.make_legacy()
        self.assertEqual(shipment.lines.get().cost_total, D("89000.00"))
        self.assertEqual(self.report_cost(), D("89000.00"))

    def test_dry_run_reports_lost_hundred_and_writes_nothing(self):
        self.make_legacy()
        before = self.snapshot()
        output = self.run_command()
        self.assertIn("скидка 10.000 %: (цена единицы, сумма) — в базе (0.89, 89000.00), по расчёту (0.89, 89100.00)",
                      output)
        self.assertIn("(цена единицы, себестоимость строки) — в базе (0.89, 89000.00), по расчёту (0.89, 89100.00)",
                      output)
        self.assertIn("Ничего не записано", output)
        self.assertEqual(self.snapshot(), before)

    def test_apply_restores_totals_from_documents(self):
        shipment = self.make_legacy()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertNotIn("НЕ исправлены", output)
        self.assertEqual(shipment.lines.get().cost_amount, D("89100.00"))
        self.assertEqual(self.report_cost(), D("89100.00"))
        self.assertEqual(self.value(), (D("0"), D("0.00")))
        self.assertIn("Расхождений нет", self.run_command())

    def test_matching_old_data_is_listed_as_not_yet_recorded(self):
        """Старый расчёт совпадает с точным — это не ошибка, сумму осталось записать."""
        self.receipt(1, 10, 100)
        self.shipment(2, 4)
        StockMovement.objects.update(amount=None)
        ShipmentLine.objects.update(cost_amount=None)
        StockBalance.objects.update(value=None)
        output = self.run_command()
        self.assertIn("Точная сумма ещё не записана", output)
        self.assertNotIn("Себестоимость движений", output)
        self.assertNotIn("Себестоимость строк отгрузок", output)
        self.run_command("--apply", "--i-have-a-backup")
        self.assertEqual(self.value(), (D("6"), D("600.00")))
        self.assertIn("Расхождений нет", self.run_command())

    def test_ambiguous_old_shipment_keeps_rounded_total_and_is_flagged(self):
        """Строку отгрузки меняли в обход проведения: точную сумму взять неоткуда.
        Товар не исправляется, проблема названа; из округлённой цены точность
        не «восстанавливается» молча."""
        shipment = self.make_legacy()
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(ShipmentLine).filter(shipment=shipment).update(quantity=D("99999"))
        before = self.snapshot()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — движения не сопоставляются со строками документа", output)
        self.assertIn(f"Товары НЕ исправлены и не изменены (id: {self.product.pk})", output)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(shipment.lines.get().cost_amount)
