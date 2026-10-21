"""Продажа раньше поступления: дефицит строки и его покрытие (первый этап).

Сценарии независимой модели docs/proposals/negative_stock_model.py,
воспроизведённые на настоящих документах: приёмках, отгрузках, перемещениях
и возвратах. Ожидаемые суммы посчитаны вручную по согласованным правилам
(docs/negative-stock-proposal.md) и записаны константами — тесты не берут
их ни из модели, ни из расчёта.

Обозначения: склад W — self.wh, W1/W2/W3 — self.wh, self.wh2, self.wh3;
день N — N января 2026 года, 12:00.
"""
import datetime
import importlib
from collections import defaultdict
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from apps.catalog.models import Product
from apps.core import roles
from apps.purchases.models import Receipt, ReceiptLine
from apps.reports import services as report_services
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment, ShipmentLine

from . import costing
from .models import DeficitCover, StockBalance, StockMovement, Transfer
from .services import StockError
from .tests_costing import JAN, NOON, legacy, migration_apps
from .tests_costing_rules import RulesBase

D = Decimal
ZERO = D("0")
MONEY_ZERO = D("0.00")
JAN30 = datetime.date(2026, 1, 30)
JAN31 = datetime.date(2026, 1, 31)
FEB2 = datetime.date(2026, 2, 2)


def _by_warehouse(state):
    """{(организация, склад): значения} → {склад: значения}; организация в этих тестах одна."""
    assert len({key[0] for key in state}) <= 1, state
    return {warehouse_id: values for (_, warehouse_id), values in state.items()}


def day(value):
    return value if isinstance(value, datetime.date) else JAN[value]


class DeficitBase(RulesBase):
    """Минус разрешён организацией — продажу можно внести раньше поступления."""

    def setUp(self):
        super().setUp()
        self.org.allow_negative_stock = True
        self.org.save()

    # ---------- документы ----------

    def receive(self, when, qty, price, warehouse=None, discount=0, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                     supplier=self.supplier, date=day(when), time=NOON)
        doc.lines.create(product=self.product, quantity=D(str(qty)), price=D(str(price)), discount=discount)
        if post:
            doc.post()
        return doc

    def sell(self, when, *quantities, warehouse=None, post=True):
        """Отгрузка; несколько количеств — несколько строк одного товара."""
        doc = Shipment.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                      customer=self.customer, date=day(when), time=NOON)
        for qty in quantities:
            doc.lines.create(product=self.product, quantity=D(str(qty)), price=500)
        if post:
            doc.post()
        return doc

    def move(self, when, qty, source, target, post=True):
        doc = Transfer.objects.create(organization=self.org, warehouse_from=source, warehouse_to=target,
                                      date=day(when), time=NOON)
        doc.lines.create(product=self.product, quantity=D(str(qty)))
        if post:
            doc.post()
        return doc

    def give_back(self, when, qty, shipment=None, line=None, warehouse=None, post=True):
        """Возврат покупателя; line — строка отгрузки, из которой возвращается товар."""
        doc = CustomerReturn.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                            customer=self.customer, shipment=shipment,
                                            date=day(when), time=NOON)
        doc.lines.create(product=self.product, quantity=D(str(qty)), price=500, shipment_line=line)
        if post:
            doc.post()
        return doc

    # ---------- состояние ----------

    def sold(self, doc, index=0):
        """(себестоимость строки, сколько единиц ещё без себестоимости)."""
        line = ShipmentLine.objects.filter(shipment=doc).order_by("pk")[index]
        return line.cost_amount, line.cost_open_qty

    def amount(self, doc, warehouse=None):
        moves = StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
        if warehouse:
            moves = moves.filter(warehouse=warehouse)
        return moves.get().amount

    def stock(self, warehouse=None):
        """(количество, стоимость, не покрыто) склада — из записанных данных."""
        warehouse = warehouse or self.wh
        bal = StockBalance.objects.filter(product=self.product, warehouse=warehouse).first()
        open_qty = sum((m.open_qty for m in StockMovement.objects.filter(product=self.product,
                                                                          warehouse=warehouse)), ZERO)
        return (bal.quantity, bal.value, open_qty) if bal else (ZERO, MONEY_ZERO, open_qty)

    def on(self, when):
        """Остатки на дату: {склад: (количество, стоимость, не покрыто)}.
        Ключ учёта теперь «организация + склад» (учёт по организациям, этап
        18.10.2026); организация в этих проверках одна — сводим к складу,
        ожидаемые значения прежние."""
        return _by_warehouse(costing.state_on(self.product.pk, day(when)))

    def values_state(self):
        """Значения учёта без id движений (перепроведение пересоздаёт движения)."""
        return (
            sorted(StockMovement.objects.values_list("doc_type", "doc_id", "line_id", "warehouse_id", "quantity",
                                                     "cost", "amount", "open_qty", "date")),
            list(ShipmentLine.objects.order_by("pk").values_list("pk", "cost_price", "cost_amount", "cost_open_qty")),
            list(StockBalance.objects.order_by("warehouse_id").values_list("warehouse_id", "quantity", "value")),
            sorted(DeficitCover.objects.values_list("issue__doc_type", "issue__doc_id", "cover__doc_type",
                                                    "cover__doc_id", "quantity", "amount")),
        )

    def full_state(self):
        return super().full_state() + (
            list(ReceiptLine.objects.order_by("pk").values_list("pk", "quantity", "price")),
            list(Receipt.objects.order_by("pk").values_list("pk", "date")),
        )

    # ---------- сохранение стоимости ----------

    def assertBalanced(self, until=None):
        """По каждому складу: поступления − расходы = стоимость остатка.
        В целом по документам: закуплено + возвращено покупателями =
        себестоимость отгрузок + стоимость остатков (без списаний)."""
        if until is None:
            moves = [(m.warehouse_id, m.quantity, m.amount)
                     for m in StockMovement.objects.filter(product=self.product)]
            values = {b.warehouse_id: b.value for b in StockBalance.objects.filter(product=self.product)}
        else:
            plan = costing.build_plan(self.product.pk, until=day(until))
            moves = [(plan.stored_movements[pk].warehouse_id, plan.stored_movements[pk].quantity, amount)
                     for pk, (_, amount, _) in plan.movements.items()]
            values = {wh: value for (_, wh), (_, value, _) in plan.balances.items()}   # одна организация
        flows = defaultdict(lambda: MONEY_ZERO)
        for warehouse_id, quantity, amount in moves:
            flows[warehouse_id] += amount if quantity > 0 else -amount
        for warehouse_id in set(flows) | set(values):
            self.assertEqual(flows[warehouse_id], values.get(warehouse_id, MONEY_ZERO),
                             f"склад {warehouse_id}, на дату {until}")
        if until is None:
            purchased = sum((line.total for line in ReceiptLine.objects.filter(receipt__status="posted")), ZERO)
            returned = sum((m.amount for m in StockMovement.objects.filter(doc_type="customer_return")), ZERO)
            sold = sum((line.cost_amount for line in ShipmentLine.objects.filter(shipment__status="posted")), ZERO)
            self.assertEqual(purchased + returned, sold + sum(values.values(), MONEY_ZERO))

    def assertBalancedOnEveryDate(self):
        for when in sorted(set(StockMovement.objects.filter(product=self.product).values_list("date", flat=True))):
            self.assertBalanced(until=when)
        self.assertBalanced()

    def assertCovers(self, expected):
        """Записи покрытия: [(расход, покрывающий документ, количество, сумма)]."""
        rows = [(c.issue.doc_type, c.issue.doc_id, c.cover.doc_type, c.cover.doc_id, c.quantity, c.amount)
                for c in DeficitCover.objects.select_related("issue", "cover").order_by("pk")]
        self.assertEqual(rows, [(i.DOC_TYPE, i.pk, c.DOC_TYPE, c.pk, D(str(q)), D(a)) for i, c, q, a in expected])


# ---------------------------------------------------------------------------
# Покрытие дефицита поступлениями
# ---------------------------------------------------------------------------

class CoverTests(DeficitBase):
    def test_a_sale_from_empty_stock_then_receipt(self):
        """А: отгрузка 1 с пустого склада, затем приход 1 на 250,00."""
        sale = self.sell(1, 1)
        self.assertEqual(self.sold(sale), (MONEY_ZERO, D("1")))
        self.assertEqual(self.stock(), (D("-1"), MONEY_ZERO, D("1")))
        self.assertTrue(Shipment.objects.get(pk=sale.pk).cost_is_preliminary)
        receipt = self.receive(2, 1, "250.00")
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))
        self.assertEqual(self.stock(), (ZERO, MONEY_ZERO, ZERO))
        self.assertFalse(Shipment.objects.get(pk=sale.pk).cost_is_preliminary)
        self.assertCovers([(sale, receipt, 1, "250.00")])
        self.assertBalancedOnEveryDate()

    def test_b_deficit_covered_by_two_receipts(self):
        """Б: отгрузка 2 с пустого, приход 1 × 250,00, приход 1 × 300,00."""
        sale = self.sell(1, 2)
        self.receive(2, 1, "250.00")
        self.assertEqual(self.sold(sale), (D("250.00"), D("1")))
        self.receive(3, 1, "300.00")
        self.assertEqual(self.sold(sale), (D("550.00"), ZERO))
        self.assertEqual(self.stock(), (ZERO, MONEY_ZERO, ZERO))
        self.assertBalancedOnEveryDate()

    def test_c_original_case(self):
        """В (исходный случай): приход 1 на 100,00 → отгрузка 2 → приход 2 по 150,00."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        self.assertEqual(self.sold(sale), (D("100.00"), D("1")))
        self.assertEqual(self.stock(), (D("-1"), MONEY_ZERO, D("1")))
        self.receive(3, 2, "150.00")                                   # 300,00: 150 на дефицит, 150 в остаток
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))         # прежний расчёт давал 100,00
        self.assertEqual(self.stock(), (D("1"), D("150.00"), ZERO))
        self.assertEqual(StockBalance.objects.get().avg_cost, D("150.00"))
        self.assertBalancedOnEveryDate()

    def test_c2_receipt_at_higher_price(self):
        """В′: приход 1 на 100,00 → отгрузка 2 → приход 2 по 300,00."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        self.receive(3, 2, "300.00")
        self.assertEqual(self.sold(sale), (D("400.00"), ZERO))
        self.assertEqual(self.stock(), (D("1"), D("300.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_g_deficits_are_covered_in_order_of_sales(self):
        """Г: две отгрузки в минус, два прихода, третья отгрузка из остатка."""
        s1 = self.sell(1, 1)
        s2 = self.sell(2, 2)
        r3 = self.receive(3, 2, "100.00")                              # 200,00
        self.assertEqual((self.sold(s1), self.sold(s2)), ((D("100.00"), ZERO), (D("100.00"), D("1"))))
        self.assertEqual(self.stock()[0], D("-1"))
        r4 = self.receive(4, 3, "400.00")                              # 1200,00
        self.assertEqual(self.sold(s2), (D("500.00"), ZERO))
        self.assertEqual(self.stock(), (D("2"), D("800.00"), ZERO))
        s3 = self.sell(5, 1)
        self.assertEqual(self.sold(s3), (D("400.00"), ZERO))
        self.assertEqual(self.stock(), (D("1"), D("400.00"), ZERO))
        self.assertEqual(self.sold(s1)[0] + self.sold(s2)[0] + self.sold(s3)[0], D("1000.00"))
        self.assertCovers([(s1, r3, 1, "100.00"), (s2, r3, 1, "100.00"), (s2, r4, 1, "400.00")])
        self.assertBalancedOnEveryDate()

    def test_cover_by_transfer_to_the_deficit_warehouse(self):
        """Перемещение на склад с дефицитом покрывает его по стоимости перемещения."""
        self.receive(1, 2, "100.00", warehouse=self.wh)
        sale = self.sell(2, 1, warehouse=self.wh2)
        transfer = self.move(3, 1, self.wh, self.wh2)
        self.assertEqual(self.sold(sale), (D("100.00"), ZERO))
        self.assertEqual((self.stock(self.wh), self.stock(self.wh2)),
                         ((D("1"), D("100.00"), ZERO), (ZERO, MONEY_ZERO, ZERO)))
        self.assertCovers([(sale, transfer, 1, "100.00")])
        self.assertBalancedOnEveryDate()


# ---------------------------------------------------------------------------
# Возвраты по строке отгрузки
# ---------------------------------------------------------------------------

class ReturnTests(DeficitBase):
    def test_r1_sequential_returns_restore_all_determined_cost(self):
        """Р1: отгрузка 2 с пустого → возврат 1 → приход 1 на 250,00 → возврат 1."""
        sale = self.sell(1, 2)
        first = self.give_back(2, 1, shipment=sale)
        self.assertEqual(self.amount(first), MONEY_ZERO)               # погашена непокрытая единица
        self.assertEqual(self.sold(sale), (MONEY_ZERO, D("1")))
        self.assertEqual(self.stock(), (D("-1"), MONEY_ZERO, D("1")))
        self.receive(3, 1, "250.00")
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))
        self.assertEqual(self.stock()[0], ZERO)
        second = self.give_back(4, 1, shipment=sale)
        self.assertEqual(self.amount(second), D("250.00"))             # база 2 − 1 = 1, а не 2 (было бы 125,00)
        self.assertEqual(self.stock(), (D("1"), D("250.00"), ZERO))
        self.assertEqual(CustomerReturnLine.objects.get(document=second).shipment_line_id,
                         ShipmentLine.objects.get(shipment=sale).pk)     # строка одна — связь однозначна
        self.assertBalancedOnEveryDate()

    def test_r2_fractional_quantities_and_rounding(self):
        """Р2: дробные количества, частичные возвраты, нарастающее округление."""
        self.receive(1, "1.000", "100.00")
        sale = self.sell(2, "2.500")
        self.assertEqual(self.sold(sale), (D("100.00"), D("1.5")))
        self.assertEqual(self.amount(self.give_back(3, "0.400", shipment=sale)), MONEY_ZERO)
        self.assertEqual((self.sold(sale)[1], self.stock()[0]), (D("1.1"), D("-1.1")))
        receipt = self.receive(4, "3.000", "333.34", discount=D("0.002"))
        self.assertEqual(receipt.lines.get().total, D("1000.00"))      # 3 × 333,34 × 0,99998 = 999,99999… → 1000,00
        # покрыто 1,1: r(1000 × 1,1 / 3) = r(366,666…) = 366,67
        self.assertEqual(self.sold(sale), (D("466.67"), ZERO))
        self.assertEqual(self.stock(), (D("1.9"), D("633.33"), ZERO))
        # база = 2,5 − 0,4 = 2,1; r(466,67 × 0,7/2,1) = 155,56; r(466,67 × 1,5/2,1) = 333,34; 466,67
        backs = [self.give_back(5 + i, qty, shipment=sale) for i, qty in enumerate(("0.700", "0.800", "0.600"))]
        self.assertEqual([self.amount(b) for b in backs], [D("155.56"), D("177.78"), D("133.33")])
        self.assertEqual(sum(self.amount(b) for b in backs), D("466.67"))
        self.assertEqual(self.stock(), (D("4"), D("1100.00"), ZERO))
        self.assertEqual((self.on(5)[self.wh.pk], self.on(6)[self.wh.pk]),
                         ((D("2.6"), D("788.89"), ZERO), (D("3.4"), D("966.67"), ZERO)))
        self.assertBalancedOnEveryDate()

    def test_s1_two_lines_of_one_product(self):
        """С1: строка А обеспечена остатком, строка Б создаёт дефицит; возвраты по строкам."""
        self.receive(1, 2, "100.00")
        sale = self.sell(2, 2, 3)
        line_a, line_b = ShipmentLine.objects.filter(shipment=sale).order_by("pk")
        self.assertEqual((self.sold(sale, 0), self.sold(sale, 1)), ((D("200.00"), ZERO), (MONEY_ZERO, D("3"))))
        self.assertEqual(self.amount(self.give_back(3, 1, shipment=sale, line=line_b)), MONEY_ZERO)
        self.assertEqual(self.sold(sale, 0), (D("200.00"), ZERO))      # строка А не затронута
        self.receive(4, 2, "250.00")
        self.assertEqual(self.sold(sale, 1), (D("500.00"), ZERO))
        self.assertEqual(self.amount(self.give_back(5, 1, shipment=sale, line=line_a)), D("100.00"))
        self.assertEqual(self.stock(), (D("1"), D("100.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_s2_return_of_covered_line_while_other_line_waits(self):
        """С2: вернули единицу строки А, пока у строки Б дефицит. Вернувшаяся
        единица (100,00) покрывает дефицит Б; без указания строки — отказ."""
        self.receive(1, 2, "100.00")
        sale = self.sell(2, 2, 3)
        line_a, line_b = ShipmentLine.objects.filter(shipment=sale).order_by("pk")

        unlinked = self.give_back(3, 1, shipment=sale, post=False)
        self.assert_rejected(unlinked.post, "Укажите в возврате, из какой строки отгрузки")
        self.assertEqual(CustomerReturn.objects.get(pk=unlinked.pk).status, "draft")

        CustomerReturnLine.objects.filter(document=unlinked).update(shipment_line=line_a)
        CustomerReturn.objects.get(pk=unlinked.pk).post()
        self.assertEqual(self.amount(unlinked), D("100.00"))
        self.assertEqual(self.sold(sale, 1), (D("100.00"), D("2")))
        receipt = self.receive(4, 2, "250.00")
        self.assertEqual((self.sold(sale, 0), self.sold(sale, 1)), ((D("200.00"), ZERO), (D("600.00"), ZERO)))
        self.assertCovers([(sale, unlinked, 1, "100.00"), (sale, receipt, 2, "500.00")])
        self.assertBalancedOnEveryDate()

    def test_line_of_other_product_or_shipment_is_rejected(self):
        self.receive(1, 5, "100.00")
        sale = self.sell(2, 1, 1)
        other = self.sell(3, 1)
        back = self.give_back(4, 1, shipment=sale, line=ShipmentLine.objects.get(shipment=other), post=False)
        self.assert_rejected(back.post, "указывает на строку другой отгрузки")

    def test_more_than_the_line_had_is_rejected(self):
        """По строке нельзя вернуть больше, чем в ней было, даже если в
        отгрузке всего этого товара больше."""
        self.receive(1, 5, "100.00")
        sale = self.sell(2, 1, 3)
        line_a = ShipmentLine.objects.filter(shipment=sale).order_by("pk")[0]
        back = self.give_back(3, 2, shipment=sale, line=line_a, post=False)
        self.assert_rejected(back.post, "в выбранной строке отгрузки 1")

    def test_line_cannot_be_reduced_below_returned(self):
        self.receive(1, 5, "100.00")
        sale = self.sell(2, 1, 3)
        line_b = ShipmentLine.objects.filter(shipment=sale).order_by("pk")[1]
        self.give_back(3, 2, shipment=sale, line=line_b)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        def reduce():
            with Shipment.objects.get(pk=sale.pk).editing():
                ShipmentLine.objects.filter(pk=line_b.pk).update(quantity=1)

        self.assert_rejected(reduce, "по строке отгрузки уже возвращено 2")

    def test_changing_basis_or_product_drops_stale_line_link(self):
        """Ссылка на строку прежней отгрузки или строку другого товара не
        остаётся: при проведении строка определяется заново (однозначно)."""
        self.receive(1, 5, "100.00")
        sale, other = self.sell(2, 1), self.sell(3, 1)
        back = self.give_back(4, 1, shipment=sale, line=ShipmentLine.objects.get(shipment=sale), post=False)
        back.shipment = other
        back.save()
        line = CustomerReturnLine.objects.get(document=back)
        self.assertIsNone(line.shipment_line_id)
        CustomerReturn.objects.get(pk=back.pk).post()
        line = CustomerReturnLine.objects.get(document=back)
        self.assertEqual(line.shipment_line_id, ShipmentLine.objects.get(shipment=other).pk)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строку проведённого возврата отдельно не меняют
        # (posting, правило 10); сброс ссылки при смене товара проверяем у снятого с проведения
        # возврата — отмена ссылку не трогает. Ожидание прежнее.
        CustomerReturn.objects.get(pk=back.pk).unpost()
        line = CustomerReturnLine.objects.get(document=back)
        line.product = self.other
        line.save()
        self.assertIsNone(CustomerReturnLine.objects.get(pk=line.pk).shipment_line_id)

    def test_return_without_basis_covers_deficit_by_fixed_estimate(self):
        """Возврат без основания при пустой истории — по цене карточки,
        зафиксированной при проведении (77); он покрывает дефицит. Позже цену
        карточки поменяли — пересчёт не меняет ни возврат, ни отгрузку."""
        sale = self.sell(1, 1)
        back = self.give_back(2, 1)
        self.assertEqual(self.amount(back), D("77.00"))
        self.assertEqual(self.sold(sale), (D("77.00"), ZERO))
        Product.objects.filter(pk=self.product.pk).update(purchase_price=D("999"))
        self.receive(3, 1, "100.00")                                   # пересчёт истории товара
        Shipment.objects.get(pk=sale.pk).post()
        self.assertEqual((self.amount(back), self.sold(sale)), (D("77.00"), (D("77.00"), ZERO)))
        self.assertEqual(self.stock(), (D("1"), D("100.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_return_without_basis_uses_last_known_average(self):
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        back = self.give_back(3, 1)                                    # остатка нет, последняя средняя 100,00
        self.assertEqual(self.amount(back), D("100.00"))
        self.assertEqual(self.sold(sale), (D("200.00"), ZERO))
        Product.objects.filter(pk=self.product.pk).update(purchase_price=D("999"))
        Shipment.objects.get(pk=sale.pk).post()
        self.assertEqual(self.amount(back), D("100.00"))
        self.assertEqual(self.stock(), (ZERO, MONEY_ZERO, ZERO))
        self.assertBalancedOnEveryDate()

    def test_based_on_prefill_links_lines_and_respects_linked_returns(self):
        """«На основании»: строки возврата заполняются со ссылкой на строки
        отгрузки; уже возвращённое по строке вычитается из неё, а не из первой
        строки товара — черновик проводится без правки."""
        self.receive(1, 2, "100.00")
        sale = self.sell(2, 2, 3)
        line_a, line_b = ShipmentLine.objects.filter(shipment=sale).order_by("pk")
        self.give_back(3, 1, shipment=sale, line=line_b)
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")
        self.client.post(reverse("shipment_to_return", args=[sale.pk]))
        draft = CustomerReturn.objects.exclude(status="posted").get()
        self.assertEqual(sorted(draft.lines.values_list("shipment_line_id", "quantity")),
                         sorted([(line_a.pk, D("2")), (line_b.pk, D("2"))]))
        draft.date, draft.time = JAN[5], NOON
        draft.save()
        draft.post()                                                   # проводится без правки
        # возврат строки А (2 шт., 200,00) покрывает дефицит Б; затем возврат Б
        # восстанавливает её определённую себестоимость: база 3 − 1 = 2, 200 × 2/2
        self.assertEqual(self.sold(sale, 1), (D("200.00"), ZERO))
        self.assertEqual(self.stock(), (D("2"), D("200.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_return_form_offers_basis_lines(self):
        self.receive(1, 5, "100.00")
        sale = self.sell(2, 1, 3)
        back = self.give_back(3, 1, shipment=sale, post=False)
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")
        page = self.client.get(reverse("customer_return_edit", args=[back.pk])).content.decode()
        self.assertIn("Строка отгрузки:", page)
        self.assertIn("— определить автоматически —", page)


# ---------------------------------------------------------------------------
# Изменение истории: нарушающие операции отклоняются целиком
# ---------------------------------------------------------------------------

class HistoryChangeTests(DeficitBase):
    def test_i1_return_to_other_warehouse_after_cover(self):
        """И1: возврат на другой склад допустим, пока проданное покрыто; отмена,
        удаление или перенос покрывавшего прихода отклоняются."""
        self.sell(1, 1, warehouse=self.wh)
        receipt = self.receive(2, 1, "250.00", warehouse=self.wh)
        sale = Shipment.objects.get()
        back = self.give_back(3, 1, shipment=sale, warehouse=self.wh2)
        self.assertEqual(self.amount(back), D("250.00"))
        self.assertBalancedOnEveryDate()

        self.assert_rejected(Receipt.objects.get(pk=receipt.pk).unpost, "на другой склад")
        self.assert_rejected(Receipt.objects.get(pk=receipt.pk).delete, "на другой склад")
        self.assert_rejected(lambda: self.edit_and_repost(Receipt.objects.get(pk=receipt.pk), date=JAN[4]),
                             "на другой склад")
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).status, "posted")

    def test_return_of_uncovered_units_to_other_warehouse_is_rejected(self):
        sale = self.sell(1, 1, warehouse=self.wh)
        back = self.give_back(2, 1, shipment=sale, warehouse=self.wh2, post=False)
        self.assert_rejected(back.post, "на другой склад")
        same = self.give_back(2, 1, shipment=sale, warehouse=self.wh)     # на склад отгрузки — можно
        self.assertEqual(self.amount(same), MONEY_ZERO)

    def test_i2_transfer_must_be_covered_regardless_of_negative_permission(self):
        """И2: перемещение обеспечено приходом; отменить, удалить, уменьшить до
        0,5 или перенести приход позже перемещения нельзя — даже при
        разрешённом минусе."""
        receipt = self.receive(1, 1, "250.00", warehouse=self.wh)
        self.move(2, 1, self.wh, self.wh2)
        self.assertBalancedOnEveryDate()
        fresh = lambda: Receipt.objects.get(pk=receipt.pk)      # noqa: E731
        self.assert_rejected(fresh().unpost, "Перемещать можно только то, что есть на складе")
        self.assert_rejected(fresh().delete, "Перемещать можно только то, что есть на складе")
        self.assert_rejected(lambda: self.edit_and_repost(fresh(), quantity=D("0.5")),
                             "перемещается 1, а на складе «Основной» на этот момент 0.5")
        self.assert_rejected(lambda: self.edit_and_repost(fresh(), date=JAN[3]),
                             "Перемещать можно только то, что есть на складе")

    def test_transfer_beyond_stock_and_backdated_transfer_are_rejected(self):
        self.receive(3, 1, "250.00", warehouse=self.wh)
        early = self.move(2, 1, self.wh, self.wh2, post=False)               # раньше прихода
        self.assert_rejected(early.post, "Перемещать можно только то, что есть на складе")
        too_much = self.move(4, 2, self.wh, self.wh2, post=False)      # больше текущего остатка
        self.assert_rejected(too_much.post, "Недостаточно товара «Кабель» на складе: доступно 1, требуется 2")

    def test_unpost_of_shipment_and_repeated_recalculation_keep_values(self):
        """Снятие и повторное проведение возвращают те же значения; повторный
        пересчёт ничего не меняет."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        self.give_back(3, 1, shipment=sale)
        self.receive(4, 2, "150.00")
        self.assertBalancedOnEveryDate()
        before = self.values_state()
        self.assertEqual(costing.fixable(costing.recalculate([self.product.pk])), [])
        self.assertEqual(self.values_state(), before)
        for model in (Receipt, Shipment, CustomerReturn):                # перепроведение пересоздаёт движения
            for doc in model.objects.order_by("pk"):
                doc.post()
        self.assertEqual(self.values_state(), before)
        self.assertIn("Расхождений нет", self.run_command())

    def test_unpost_of_shipment_with_deficit_releases_cover(self):
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        self.receive(3, 2, "150.00")
        self.assertEqual(DeficitCover.objects.count(), 1)
        Shipment.objects.get(pk=sale.pk).unpost()
        self.assertEqual(DeficitCover.objects.count(), 0)
        self.assertEqual(self.stock(), (D("3"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_serial_numbers_cannot_go_negative(self):
        """Контроль серийных номеров не ослаблен: номер, которого нет на
        складе, отгрузить нельзя и при разрешённом минусе."""
        phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                      date=JAN[2], time=NOON)
        doc.lines.create(product=phone, quantity=1, price=900, serial_numbers="SN-1")
        with self.assertRaises(StockError):
            doc.post()
        self.assertEqual(Shipment.objects.get(pk=doc.pk).status, "draft")
        self.assertFalse(StockMovement.objects.filter(product=phone).exists())


# ---------------------------------------------------------------------------
# Остатки на дату и отчёт о прибыли
# ---------------------------------------------------------------------------

class ReportTests(DeficitBase):
    def login(self):
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")

    def test_n1_stock_on_date_before_later_issue(self):
        """Н1: день 1 приход 1 на 100,00, день 2 отгрузка 1."""
        self.receive(1, 1, "100.00")
        self.sell(2, 1)
        self.assertEqual(self.on(1), {self.wh.pk: (D("1"), D("100.00"), ZERO)})
        self.assertEqual(self.stock(), (ZERO, MONEY_ZERO, ZERO))
        self.assertBalanced(until=1)
        self.assertBalanced()

    def test_n2_stock_on_date_before_cover(self):
        """Н2: день 1 отгрузка 1 с пустого, день 2 приход 2 на 500,00."""
        sale = self.sell(1, 1)
        self.receive(2, 2, "250.00")
        self.assertEqual(self.on(1), {self.wh.pk: (D("-1"), MONEY_ZERO, D("1"))})
        self.assertEqual(self.stock(), (D("1"), D("250.00"), ZERO))
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))
        self.assertBalanced(until=1)
        self.assertBalanced()

    def test_n3_two_warehouses_on_dates(self):
        """Н3: приход на W1, перемещение на W2, отгрузка с W2 в минус, позже
        перемещение покрывает дефицит, возврат по отгрузке — на W1."""
        self.receive(1, 3, "100.00", warehouse=self.wh)
        self.move(2, 1, self.wh, self.wh2)
        sale = self.sell(3, 2, warehouse=self.wh2)
        self.move(5, 1, self.wh, self.wh2)
        back = self.give_back(6, 1, shipment=sale, warehouse=self.wh)
        self.assertEqual(self.on(3), {self.wh.pk: (D("2"), D("200.00"), ZERO),
                                      self.wh2.pk: (D("-1"), MONEY_ZERO, D("1"))})
        self.assertEqual(self.on(5), {self.wh.pk: (D("1"), D("100.00"), ZERO),
                                      self.wh2.pk: (ZERO, MONEY_ZERO, ZERO)})
        self.assertEqual((self.stock(self.wh), self.stock(self.wh2)),
                         ((D("2"), D("200.00"), ZERO), (ZERO, MONEY_ZERO, ZERO)))
        self.assertEqual((self.sold(sale), self.amount(back)), ((D("200.00"), ZERO), D("100.00")))
        for until in (3, 5, None):
            self.assertBalanced(until=until)

    def test_p_profit_by_sale_date_is_preliminary_until_receipt(self):
        """П: отгрузка 30 января с пустого склада, поступление 2 февраля.
        Прибыль января сначала предварительная (себестоимость 0 и пометка),
        после поступления уточняется в январе — периоде продажи."""
        sale = self.sell(JAN30, 1)
        january = lambda: report_services.sales_summary(JAN[1], JAN31)      # noqa: E731
        self.assertEqual((january()["cost"], january()["open_qty"], january()["preliminary_count"]),
                         (MONEY_ZERO, D("1"), 1))
        self.receive(FEB2, 1, "250.00")
        self.assertEqual((january()["cost"], january()["profit"], january()["open_qty"],
                          january()["preliminary_count"]), (D("250.00"), D("250.00"), ZERO, 0))
        self.assertEqual(report_services.sales_summary(FEB2, FEB2)["cost"], ZERO)
        self.assertEqual(self.on(JAN31), {self.wh.pk: (D("-1"), MONEY_ZERO, D("1"))})
        self.assertEqual(self.stock(), (ZERO, MONEY_ZERO, ZERO))
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))
        self.assertBalancedOnEveryDate()
        self.assertBalanced(until=JAN31)

    def test_warnings_in_shipment_card_list_and_report(self):
        self.login()
        sale = self.sell(1, 3)
        card = self.client.get(reverse("shipment_edit", args=[sale.pk])).content.decode()
        self.assertIn("data-cost-preliminary", card)
        self.assertIn("3 шт. продано раньше, чем товар поступил на склад", card)
        self.assertIn("предв.", self.client.get(reverse("shipment_list")).content.decode())
        report = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
        self.assertIn("data-profit-preliminary", report.content.decode())

        receipt = self.receive(2, 3, "100.00")
        card = self.client.get(reverse("shipment_edit", args=[sale.pk])).content.decode()
        self.assertNotIn("data-cost-preliminary", card)
        self.assertIn("data-deficit-covers", card)                     # объяснение: чем покрыто
        self.assertIn(f"приёмка № {receipt.number} от 02.01.2026 12:00 — 3 шт. на 300,00", card)
        self.assertNotIn("предв.", self.client.get(reverse("shipment_list")).content.decode())
        report = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
        self.assertNotIn("data-profit-preliminary", report.content.decode())

    def test_balance_list_on_date(self):
        self.login()
        self.sell(1, 1)
        self.receive(2, 2, "250.00")
        now = self.client.get(reverse("balance_list"))
        self.assertEqual([(b.quantity, b.total_value) for b in now.context["balances"]], [(D("1"), D("250.00"))])
        then = self.client.get(reverse("balance_list"), {"on": "2026-01-01"})
        self.assertEqual([(b.quantity, b.total_value) for b in then.context["balances"]], [(D("-1"), MONEY_ZERO)])
        self.assertEqual(then.context["total_value"], MONEY_ZERO)
        self.assertIn("ждёт поступления", then.content.decode())
        before = self.client.get(reverse("balance_list"), {"on": "2025-12-31"})
        self.assertEqual(list(before.context["balances"]), [])


# ---------------------------------------------------------------------------
# Старые данные: диагностика, исправляющая операция, пакетное исправление
# ---------------------------------------------------------------------------

class LegacyViolationTests(DeficitBase):
    def legacy_transfers(self):
        """В старой базе приход перенесли позже двух перемещений (до появления
        проверки) — оба перемещения забирали товар с пустого склада."""
        receipt = self.receive(1, 2, "250.00", warehouse=self.wh)
        t1 = self.move(2, 1, self.wh, self.wh2)
        t2 = self.move(3, 1, self.wh, self.wh3)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): дату проведённого документа массово не меняют
        # (posting, правило 9) — старые данные имитируются прямой записью мимо программы
        Receipt._base_manager.filter(pk=receipt.pk).update(date=JAN[5])
        StockMovement.objects.filter(doc_type="receipt", doc_id=receipt.pk).update(date=JAN[5])
        return receipt, t1, t2

    def fix(self, *args):
        out = StringIO()
        call_command("fix_stock_history", *args, stdout=out)
        return out.getvalue()

    def test_diagnostics_names_the_documents(self):
        _, t1, t2 = self.legacy_transfers()
        output = self.run_command()
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — перемещения забирали больше, чем было на складе: 2", output)
        self.assertIn(f"перемещение № {t1.number}", output)
        self.assertIn(f"перемещение № {t2.number}", output)
        self.assertIn("fix_stock_history", output)

    def test_repair_does_not_touch_problem_product(self):
        self.legacy_transfers()
        before = self.full_state()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("НЕ исправлены и не изменены", output)
        self.assertEqual(self.full_state(), before)

    def test_unrelated_operation_is_rejected_with_blocking_documents(self):
        _, t1, _ = self.legacy_transfers()
        late = self.receive(10, 1, "100.00", post=False)
        self.assert_rejected(late.post, f"перемещение № {t1.number}")
        self.assertEqual(Receipt.objects.get(pk=late.pk).status, "draft")

    def test_corrective_operation_one_by_one_is_rejected_while_other_problem_remains(self):
        _, t1, t2 = self.legacy_transfers()
        self.assert_rejected(Transfer.objects.get(pk=t1.pk).unpost, f"перемещение № {t2.number}")
        self.assert_rejected(Transfer.objects.get(pk=t2.pk).unpost, f"перемещение № {t1.number}")

    def test_corrective_operation_after_which_history_is_correct(self):
        """Одна проблема — одна исправляющая операция: перенос прихода на его
        настоящую дату (перепроведение) проходит, история становится корректной."""
        receipt = self.receive(1, 1, "250.00", warehouse=self.wh)
        transfer = self.move(2, 1, self.wh, self.wh2)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): дату проведённого документа массово не меняют
        # (posting, правило 9) — старые данные имитируются прямой записью мимо программы
        Receipt._base_manager.filter(pk=receipt.pk).update(date=JAN[5])
        StockMovement.objects.filter(doc_type="receipt", doc_id=receipt.pk).update(date=JAN[5])
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())
        Transfer.objects.get(pk=transfer.pk).unpost()                 # исправление пользователя
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())
        self.receive(10, 1, "100.00")                                   # теперь операции проходят
        self.assertBalancedOnEveryDate()

    def test_batch_fix_dry_run_partial_and_apply(self):
        receipt, t1, t2 = self.legacy_transfers()
        before = self.full_state()

        with self.assertRaisesMessage(CommandError, "--i-have-a-backup"):
            self.fix("--unpost", f"transfer:{t1.pk}", "--apply")
        with self.assertRaisesMessage(CommandError, "Исправление отменено целиком"):
            self.fix("--unpost", f"transfer:{t1.pk}", "--apply", "--i-have-a-backup")
        self.assertEqual(self.full_state(), before)

        dry = self.fix("--unpost", f"transfer:{t1.pk}", "--unpost", f"transfer:{t2.pk}")
        self.assertIn("Проблем после этих операций не останется. Ничего не записано", dry)
        self.assertEqual(self.full_state(), before)

        done = self.fix("--unpost", f"transfer:{t1.pk}", "--unpost", f"transfer:{t2.pk}",
                        "--apply", "--i-have-a-backup")
        self.assertIn("Готово: операций 2", done)
        self.assertEqual(list(Transfer.objects.values_list("status", flat=True)), ["draft", "draft"])
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).date, JAN[5])   # дата не «исправлена»
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())
        self.assertEqual(self.stock(self.wh), (D("2"), D("500.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_batch_fix_rejects_unknown_or_draft_documents(self):
        _, t1, _ = self.legacy_transfers()
        with self.assertRaisesMessage(CommandError, "Неверный документ"):
            self.fix("--unpost", "invoice:1")
        with self.assertRaisesMessage(CommandError, "не найден"):
            self.fix("--unpost", "transfer:99999")
        draft = self.move(4, 1, self.wh, self.wh2, post=False)
        with self.assertRaisesMessage(CommandError, "не проведён"):
            self.fix("--unpost", f"transfer:{draft.pk}")

    def test_old_values_of_sale_before_receipt_are_recalculated(self):
        """Отгрузка, проведённая по прежнему правилу (В: 2 × средняя 100 = 200,00,
        дефицита нет), после обновления пересчитывается: 250,00. Диагностика
        показывает, себестоимость какого месяца продаж изменится."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 2)
        receipt = self.receive(3, 2, "150.00")
        ShipmentLine.objects.filter(shipment=sale).update(cost_price=D("100.00"), cost_amount=D("200.00"),
                                                          cost_open_qty=0)
        StockMovement.objects.filter(doc_type="shipment").update(cost=D("100.00"), amount=D("200.00"), open_qty=0)
        DeficitCover.objects.all().delete()
        output = self.run_command()
        self.assertIn("01.2026: в базе 200.00, по расчёту 250.00, разница 50.00", output)
        self.assertIn("в базе 0, по расчёту 1", output)                 # запись покрытия появится
        self.run_command("--apply", "--i-have-a-backup")
        self.assertEqual(self.sold(sale), (D("250.00"), ZERO))
        self.assertCovers([(sale, receipt, 1, "150.00")])
        self.assertIn("Расхождений нет", self.run_command())
        self.assertBalancedOnEveryDate()

    def test_old_ambiguous_return_is_shown_and_blocks(self):
        """Старый возврат без строки, когда в отгрузке две строки товара и у
        одной дефицит, — «ТРЕБУЕТ РЕШЕНИЯ»; указание строки исправляет."""
        self.receive(1, 2, "100.00")
        sale = self.sell(2, 2, 3)
        back = self.give_back(3, 1, shipment=sale, line=ShipmentLine.objects.filter(shipment=sale)[1])
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).filter(document=back).update(shipment_line=None)   # как до обновления
        output = self.run_command()
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — возврат без строки отгрузки", output)
        self.assertIn("не будет записано, пока у товара есть проблема", output)
        late = self.receive(4, 1, "100.00", post=False)
        self.assert_rejected(late.post, f"возврат покупателя № {back.number}")
        line_b = ShipmentLine.objects.filter(shipment=sale).order_by("pk")[1]
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with CustomerReturn.objects.get(pk=back.pk).editing():         # исправляющая операция
            CustomerReturnLine.objects.filter(document=back).update(shipment_line=line_b)
        late.post()
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())


# ---------------------------------------------------------------------------
# Миграция: связи строк старых возвратов — только однозначные
# ---------------------------------------------------------------------------

class ReturnLineLinkMigrationTests(DeficitBase):
    def link(self):
        migration = importlib.import_module("apps.sales.migrations.0008_return_line_link")
        migration.link_return_lines(migration_apps(), None)

    def test_unambiguous_links_are_restored_and_ambiguous_left_for_diagnostics(self):
        self.receive(1, 10, "100.00")
        single = self.sell(2, 2)
        double = self.sell(3, 2, 2)
        one = self.give_back(4, 1, shipment=single)
        two = self.give_back(5, 1, shipment=double, line=ShipmentLine.objects.filter(shipment=double)[0])
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).update(shipment_line=None)              # как до обновления
        self.link()
        self.assertEqual(CustomerReturnLine.objects.get(document=one).shipment_line_id,
                         ShipmentLine.objects.get(shipment=single).pk)
        self.assertIsNone(CustomerReturnLine.objects.get(document=two).shipment_line_id)
        output = self.run_command()
        self.assertIn("ЗАМЕЧАНИЕ — возврат без строки отгрузки", output)    # без дефицита — не блокирует
        self.assertIn(f"возврат покупателя № {two.number}", output)

    def test_serial_return_is_linked_by_its_numbers(self):
        phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                         date=JAN[1], time=NOON)
        receipt.lines.create(product=phone, quantity=3, price=100, serial_numbers="A\nB\nC")
        receipt.post()
        sale = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                       date=JAN[2], time=NOON)
        first = sale.lines.create(product=phone, quantity=1, price=900, serial_numbers="A")
        second = sale.lines.create(product=phone, quantity=2, price=900, serial_numbers="B\nC")
        sale.post()
        back = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                             shipment=sale, date=JAN[3], time=NOON)
        back.lines.create(product=phone, quantity=1, price=900, serial_numbers="C", shipment_line=second)
        back.post()
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).update(shipment_line=None)
        self.link()
        self.assertEqual(CustomerReturnLine.objects.get(document=back).shipment_line_id, second.pk)
        self.assertNotEqual(first.pk, second.pk)


# ---------------------------------------------------------------------------
# Смешанные возвраты: без строки и по строке в одной группе «отгрузка + товар»
# ---------------------------------------------------------------------------

class MixedReturnTests(DeficitBase):
    """Единое правило: в группе «отгрузка + товар» с несколькими строками все
    возвраты считаются одним способом — либо по строкам, либо по отгрузке в
    целом. Смешение отклоняется с указанием возврата без строки."""

    def two_lines_100_and_300(self):
        """День 1 — приход 1 по 100; день 2 — отгрузка двумя строками по 1 шт.;
        день 3 — приход 1 по 300. Себестоимость строк: 100 и 300."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1, 1)
        self.receive(3, 1, "300.00")
        first, second = ShipmentLine.objects.filter(shipment=sale).order_by("pk")
        self.assertEqual((self.sold(sale, 0), self.sold(sale, 1)), ((D("100.00"), ZERO), (D("300.00"), ZERO)))
        return sale, first, second

    def one_and_two(self):
        """День 1 — приход 1 по 100; день 2 — отгрузка строками 1 и 2 шт.
        (вторая — в дефицит); день 3 — приход 2 по 150. Строки: 100 и 300."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1, 2)
        self.receive(3, 2, "150.00")
        first, second = ShipmentLine.objects.filter(shipment=sale).order_by("pk")
        self.assertEqual((self.sold(sale, 0), self.sold(sale, 1)), ((D("100.00"), ZERO), (D("300.00"), ZERO)))
        return sale, first, second

    def link_of(self, back):
        return CustomerReturnLine.objects.get(document=back).shipment_line_id

    def login(self):
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")

    def form_post(self, back, source=None, action="save"):
        """Сохранение возврата через форму с выбранной строкой отгрузки."""
        back = CustomerReturn.objects.get(pk=back.pk)
        line = back.lines.get()
        return self.client.post(reverse("customer_return_edit", args=[back.pk]), {
            "date": back.date.isoformat(), "number": back.number, "time": "12:00",
            "organization": self.org.pk, "warehouse": back.warehouse_id, "customer": self.customer.pk,
            "comment": "", "action": action,
            "lines-TOTAL_FORMS": 1, "lines-INITIAL_FORMS": 1, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
            "lines-0-id": line.pk, "lines-0-product": self.product.pk, "lines-0-quantity": f"{line.quantity:f}",
            "lines-0-price": "500", "lines-0-discount": "0", "lines-0-vat_rate": "none",
            "lines-0-serial_numbers": "", "lines-0-shipment_line": source.pk if source else "",
        }, follow=True)

    def post_as_before(self, doc):
        """Проведение так, как его пропускала прежняя версия: без проверки
        итоговой истории — так в базе появляются старые смешанные возвраты."""
        with costing.defer_history_check():
            CustomerReturn.objects.get(pk=doc.pk).post()

    # ---------- исходный сценарий и порядок возвратов ----------

    def test_reported_case_unlinked_then_linked_return(self):
        """Воспроизведение: возврат без строки (200,00 — по отгрузке в целом),
        затем возврат по второй строке (300,00) — на складе 2 шт. на 500,00 при
        закупке на 400,00. Второй возврат должен быть отклонён с указанием
        возврата, у которого нужно уточнить строку."""
        sale, first, second = self.two_lines_100_and_300()
        unlinked = self.give_back(4, 1, shipment=sale)
        self.assertEqual((self.amount(unlinked), self.link_of(unlinked)), (D("200.00"), None))
        linked = self.give_back(5, 1, shipment=sale, line=second, post=False)
        self.assert_rejected(linked.post, f"— возврат покупателя № {unlinked.number} от")
        self.assertEqual(CustomerReturn.objects.get(pk=linked.pk).status, "draft")
        self.assertEqual(self.stock(), (D("1"), D("200.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_clarified_through_form_then_linked_return_posts(self):
        """Уточнение через форму: в проведённом возврате выбрали первую строку —
        он перепроводится (100,00); затем возврат по второй строке — 300,00.
        Полный возврат восстанавливает ровно 400,00."""
        self.login()
        sale, first, second = self.two_lines_100_and_300()
        unlinked = self.give_back(4, 1, shipment=sale)
        page = self.form_post(unlinked, source=first).content.decode()
        self.assertIn("сохранён и перепроведён", page)
        self.assertEqual((self.amount(unlinked), self.link_of(unlinked)), (D("100.00"), first.pk))
        linked = self.give_back(5, 1, shipment=sale, line=second)
        self.assertEqual(self.amount(linked), D("300.00"))
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_linked_then_unlinked_return_gets_the_only_free_line(self):
        """Обратный порядок: сначала по второй строке (300,00), затем без
        строки — по второй строке возвращать уже нечего, единственная строка с
        остатком к возврату — первая; она и записывается (100,00)."""
        sale, first, second = self.two_lines_100_and_300()
        linked = self.give_back(4, 1, shipment=sale, line=second)
        unlinked = self.give_back(5, 1, shipment=sale)
        self.assertEqual((self.amount(linked), self.amount(unlinked)), (D("300.00"), D("100.00")))
        self.assertEqual(self.link_of(unlinked), first.pk)
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    # ---------- частичные и дробные возвраты, полный возврат ----------

    def test_partial_fractional_returns_by_lines(self):
        """По строкам: 0,4 из первой (40,00), 0,7 из второй (r(300 × 0,7/2) =
        105,00). Возврат 0,5 без строки — подходят обе строки, он отклоняется.
        Затем 1,3 из второй (195,00) и 0,6 из первой (60,00): всего 400,00."""
        sale, first, second = self.one_and_two()
        a1 = self.give_back(4, "0.4", shipment=sale, line=first)
        b1 = self.give_back(5, "0.7", shipment=sale, line=second)
        self.assertEqual((self.amount(a1), self.amount(b1)), (D("40.00"), D("105.00")))
        ambiguous = self.give_back(6, "0.5", shipment=sale, post=False)
        self.assert_rejected(ambiguous.post, f"— возврат покупателя № {ambiguous.number} от")
        self.assertIsNone(self.link_of(ambiguous))
        b2 = self.give_back(7, "1.3", shipment=sale, line=second)
        a2 = self.give_back(8, "0.6", shipment=sale, line=first)
        self.assertEqual((self.amount(b2), self.amount(a2)), (D("195.00"), D("60.00")))
        self.assertEqual(sum(self.amount(d) for d in (a1, b1, b2, a2)), D("400.00"))
        self.assertEqual(self.stock(), (D("3"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_partial_fractional_returns_of_whole_shipment(self):
        """По отгрузке в целом: С = 100 + 300 = 400, база 3. Нарастающим итогом:
        0,5 → r(66,666…) = 66,67; 1,7 → r(226,666…) − 66,67 = 160,00;
        3 → 400 − 226,67 = 173,33. Всего ровно 400,00. Возврат по строке в
        такой группе отклоняется с указанием первого возврата без строки."""
        sale, first, second = self.one_and_two()
        r1 = self.give_back(4, "0.5", shipment=sale)
        linked = self.give_back(5, 1, shipment=sale, line=second, post=False)
        self.assert_rejected(linked.post, f"— возврат покупателя № {r1.number} от")
        r2 = self.give_back(6, "1.2", shipment=sale)
        r3 = self.give_back(7, "1.3", shipment=sale)
        self.assertEqual([self.amount(d) for d in (r1, r2, r3)], [D("66.67"), D("160.00"), D("173.33")])
        self.assertEqual([self.link_of(d) for d in (r1, r2, r3)], [None, None, None])
        self.assertEqual(self.stock(), (D("3"), D("400.00"), ZERO))
        output = self.run_command()
        self.assertIn("ЗАМЕЧАНИЕ — возврат без строки отгрузки", output)
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", output)
        self.assertBalancedOnEveryDate()

    # ---------- отмена возврата и повторный пересчёт ----------

    def test_unpost_and_repeated_recalculation_of_whole_shipment_returns(self):
        sale, first, second = self.one_and_two()
        r1 = self.give_back(4, "0.5", shipment=sale)
        r2 = self.give_back(5, "1.2", shipment=sale)
        r3 = self.give_back(6, "1.3", shipment=sale)
        before = self.values_state()
        CustomerReturn.objects.get(pk=r2.pk).unpost()
        # без среднего: 0,5 → 66,67; 1,8 → r(240) − 66,67 = 173,33
        self.assertEqual((self.amount(r1), self.amount(r3)), (D("66.67"), D("173.33")))
        self.assertBalancedOnEveryDate()
        CustomerReturn.objects.get(pk=r2.pk).post()
        self.assertEqual(self.values_state(), before)
        self.assertEqual(costing.fixable(costing.recalculate([self.product.pk])), [])
        for doc in (r3, r1, r2):
            CustomerReturn.objects.get(pk=doc.pk).post()
        self.assertEqual(self.values_state(), before)
        self.assertIn("Расхождений нет", self.run_command())

    def test_unpost_and_repost_of_linked_returns(self):
        sale, first, second = self.two_lines_100_and_300()
        a = self.give_back(4, 1, shipment=sale, line=first)
        b = self.give_back(5, 1, shipment=sale, line=second)
        before = self.values_state()
        CustomerReturn.objects.get(pk=a.pk).unpost()
        self.assertEqual(self.amount(b), D("300.00"))
        CustomerReturn.objects.get(pk=a.pk).post()
        self.assertEqual((self.link_of(a), self.values_state()), (first.pk, before))

    # ---------- изменение истории ----------

    def test_history_change_creating_cost_difference_keeps_totals(self):
        """Строки сначала по 100 и 100; возвраты по отгрузке в целом. Затем
        приход, покрывший вторую строку, исправили на 300 — возвраты
        пересчитываются тем же способом, вместе ровно 400,00. Возврат по
        строке в такой группе по-прежнему отклоняется."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1, 1)
        receipt = self.receive(3, 1, "100.00")
        second = ShipmentLine.objects.filter(shipment=sale).order_by("pk")[1]
        r1 = self.give_back(4, 1, shipment=sale)
        self.assertEqual(self.amount(r1), D("100.00"))
        linked = self.give_back(5, 1, shipment=sale, line=second, post=False)
        self.assert_rejected(linked.post, f"— возврат покупателя № {r1.number} от")   # и при равной себестоимости
        r2 = self.give_back(6, 1, shipment=sale)
        self.edit_and_repost(Receipt.objects.get(pk=receipt.pk), price=D("300"))
        self.assertEqual((self.amount(r1), self.amount(r2)), (D("200.00"), D("200.00")))
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_history_change_with_linked_returns_keeps_totals(self):
        sale, first, second = self.two_lines_100_and_300()
        receipt = Receipt.objects.order_by("pk").last()
        a = self.give_back(4, 1, shipment=sale, line=first)
        b = self.give_back(5, 1, shipment=sale, line=second)
        self.edit_and_repost(receipt, price=D("500"))
        self.assertEqual((self.amount(a), self.amount(b)), (D("100.00"), D("500.00")))
        self.assertEqual(self.stock(), (D("2"), D("600.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_deleting_shipment_line_that_would_mix_returns_is_rejected(self):
        """Строку отгрузки удалили: её возврат теряет ссылку и становится
        возвратом без строки рядом с возвратом по строке — изменение
        отклоняется целиком, ссылка остаётся."""
        self.receive(1, 3, "100.00")
        sale = self.sell(2, 1, 1, 1)
        a, b, _ = ShipmentLine.objects.filter(shipment=sale).order_by("pk")
        back_a = self.give_back(3, 1, shipment=sale, line=a)
        self.give_back(4, 1, shipment=sale, line=b)

        def drop_first_line():
            # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
            # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
            # отдельно. Учётные ожидания прежние.
            with Shipment.objects.get(pk=sale.pk).editing():
                ShipmentLine.objects.filter(pk=a.pk).delete()

        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строку, на которую ссылается проведённый возврат, нельзя
        # удалить — отказ теперь сразу, до проверки истории (было: «— возврат покупателя № … от» из
        # перечня смешанных возвратов). Отказ целиком и ссылка — как прежде.
        self.assert_rejected(drop_first_line, f"На документ ссылается проведённый возврат покупателя № {back_a.number} от")
        self.assertEqual(self.link_of(back_a), a.pk)

    # ---------- старые данные: диагностика и исправление ----------

    def mixed_as_before(self):
        sale, first, second = self.two_lines_100_and_300()
        unlinked = self.give_back(4, 1, shipment=sale)
        linked = self.give_back(5, 1, shipment=sale, line=second, post=False)
        self.post_as_before(linked)
        self.assertEqual(self.stock(), (D("2"), D("500.00"), ZERO))      # ошибка прежней версии
        return sale, first, second, unlinked, linked

    def test_diagnostics_detects_old_mixed_returns(self):
        sale, first, second, unlinked, linked = self.mixed_as_before()
        output = self.run_command()
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — по отгрузке есть возвраты и по строкам, и без строки: 1", output)
        line = CustomerReturnLine.objects.get(document=unlinked)
        self.assertIn(f"возврат покупателя № {unlinked.number}", output)
        self.assertIn(f"(строка возврата id {line.pk})", output)
        self.assertIn(f"строки этой отгрузки с товаром: id {first.pk}, id {second.pk}", output)
        before = self.full_state()
        self.assertIn("НЕ исправлены и не изменены", self.run_command("--apply", "--i-have-a-backup"))
        self.assertEqual(self.full_state(), before)

    def test_old_mixed_returns_block_unrelated_operations(self):
        _, _, _, unlinked, _ = self.mixed_as_before()
        late = self.receive(10, 1, "100.00", post=False)
        self.assert_rejected(late.post, f"— возврат покупателя № {unlinked.number} от")

    def test_unposting_the_unlinked_return_is_a_corrective_operation(self):
        _, _, _, unlinked, linked = self.mixed_as_before()
        CustomerReturn.objects.get(pk=unlinked.pk).unpost()
        self.assertEqual(self.amount(linked), D("300.00"))
        self.assertEqual(self.stock(), (D("1"), D("300.00"), ZERO))
        self.receive(10, 1, "100.00")
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())
        self.assertBalancedOnEveryDate()

    def test_old_mixed_returns_fixed_by_explicit_link(self):
        """Уточнение старого возврата пакетной командой: строку указывает
        человек (первая), команда только проверяет, что она из той же отгрузки."""
        sale, first, second, unlinked, linked = self.mixed_as_before()
        line = CustomerReturnLine.objects.get(document=unlinked)
        out = StringIO()
        call_command("fix_stock_history", "--link", f"{line.pk}:{first.pk}", stdout=out)
        self.assertIn("Проблем после этих операций не останется", out.getvalue())
        self.assertIsNone(self.link_of(unlinked))                        # пробный прогон ничего не записал
        call_command("fix_stock_history", "--link", f"{line.pk}:{first.pk}", "--apply", "--i-have-a-backup",
                     stdout=StringIO())
        self.assertEqual((self.amount(unlinked), self.amount(linked)), (D("100.00"), D("300.00")))
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))
        self.assertIn("Расхождений нет", self.run_command())
        other = self.sell(6, 1)
        with self.assertRaisesMessage(CommandError, "из отгрузки-основания возврата и с тем же товаром"):
            call_command("fix_stock_history", "--link", f"{line.pk}:{other.lines.get().pk}", stdout=StringIO())

    def test_clarifying_one_of_two_old_returns_through_form_is_rejected_atomically(self):
        """Два старых возврата без строки; в одном через форму указали строку —
        второй остаётся без строки, смешение: сохранение отклоняется целиком,
        сообщение называет второй возврат."""
        self.login()
        sale, first, second = self.one_and_two()
        r1 = self.give_back(4, 1, shipment=sale)
        r2 = self.give_back(5, 1, shipment=sale)
        before = self.full_state()
        page = self.form_post(r1, source=first).content.decode()
        self.assertIn("Изменения не сохранены", page)
        self.assertIn(f"— возврат покупателя № {r2.number} от", page)
        self.assertEqual(self.full_state(), before)
        self.assertIsNone(self.link_of(r1))
        lines = [CustomerReturnLine.objects.get(document=d).pk for d in (r1, r2)]
        call_command("fix_stock_history", "--link", f"{lines[0]}:{first.pk}", "--link", f"{lines[1]}:{second.pk}",
                     "--apply", "--i-have-a-backup", stdout=StringIO())
        self.assertEqual((self.amount(r1), self.amount(r2)), (D("100.00"), D("150.00")))
        self.assertBalancedOnEveryDate()

    def test_explicit_line_in_form_for_draft_in_whole_shipment_group(self):
        """Черновик возврата: в форме выбрали строку, «Сохранить и провести» —
        черновик сохранён, проведение отклонено с указанием возврата без строки;
        учёт не изменился."""
        self.login()
        sale, first, second = self.two_lines_100_and_300()
        unlinked = self.give_back(4, 1, shipment=sale)
        draft = self.give_back(5, 1, shipment=sale, post=False)
        before = self.full_state()
        page = self.form_post(draft, source=second, action="save_post").content.decode()
        self.assertIn("Сохранено, но не проведено", page)
        self.assertIn(f"— возврат покупателя № {unlinked.number} от", page)
        self.assertEqual(self.full_state(), before)
        self.assertEqual(CustomerReturn.objects.get(pk=draft.pk).status, "draft")

    # ---------- «на основании» ----------

    def based_on(self, sale):
        self.client.post(reverse("shipment_to_return", args=[sale.pk]))
        draft = CustomerReturn.objects.exclude(status="posted").order_by("pk").last()
        draft.date, draft.time = JAN[9], NOON
        draft.save()
        return draft

    def test_based_on_in_whole_shipment_group_stays_whole_shipment(self):
        """Был возврат без строки — черновик «на основании» строк не указывает
        (иначе смешение); проводится без правки, вместе ровно 400,00."""
        self.login()
        sale, first, second = self.two_lines_100_and_300()
        unlinked = self.give_back(4, 1, shipment=sale)
        draft = self.based_on(sale)
        self.assertEqual(list(draft.lines.values_list("shipment_line_id", "quantity")), [(None, D("1"))])
        draft.post()
        self.assertEqual(self.amount(unlinked) + self.amount(draft), D("400.00"))
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))
        self.assertBalancedOnEveryDate()

    def test_based_on_without_previous_returns_links_every_line(self):
        self.login()
        sale, first, second = self.two_lines_100_and_300()
        draft = self.based_on(sale)
        self.assertEqual(sorted(draft.lines.values_list("shipment_line_id", "quantity")),
                         [(first.pk, D("1")), (second.pk, D("1"))])
        draft.post()
        self.assertEqual(self.stock(), (D("2"), D("400.00"), ZERO))

    # ---------- миграция: старые возвраты связываются только целой группой ----------

    def test_migration_and_serial_returns_spanning_lines(self):
        """Серийный товар: строки отгрузки «A» и «B, C»; всего 3 шт. на 99,99.
        Старые возвраты «C» и «A, B». Миграция связывает «C» со второй строкой;
        «A, B» из двух строк — без связи, пересчёт распределяет его по номерам.
        Это возвраты по строкам, смешения нет; вместе ровно 99,99."""
        phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                         date=JAN[1], time=NOON)
        receipt.lines.create(product=phone, quantity=3, price=D("33.33"), serial_numbers="A\nB\nC")
        receipt.post()
        sale = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                       date=JAN[2], time=NOON)
        first = sale.lines.create(product=phone, quantity=1, price=900, serial_numbers="A")
        second = sale.lines.create(product=phone, quantity=2, price=900, serial_numbers="B\nC")
        sale.post()
        self.assertEqual([ShipmentLine.objects.get(pk=x.pk).cost_amount for x in (first, second)],
                         [D("33.33"), D("66.66")])
        backs = []
        for day_, numbers in ((3, "C"), (4, "A\nB")):
            back = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                                 shipment=sale, date=JAN[day_], time=NOON)
            back.lines.create(product=phone, quantity=len(numbers.split()), price=900, serial_numbers=numbers)
            back.post()
            backs.append(back)
        self.assertEqual([self.link_of(b) for b in backs], [second.pk, None])
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).update(shipment_line=None)             # как до обновления
        importlib.import_module("apps.sales.migrations.0008_return_line_link").link_return_lines(migration_apps(), None)
        self.assertEqual([self.link_of(b) for b in backs], [second.pk, None])
        amounts = [StockMovement.objects.get(doc_type="customer_return", doc_id=b.pk).amount for b in backs]
        # «C» — r(66,66 × 1/2) = 33,33; «A, B» — 33,33 из первой и 66,66 − 33,33 из второй
        self.assertEqual(amounts, [D("33.33"), D("66.66")])
        self.assertEqual(sum(amounts), D("99.99"))
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())
        CustomerReturn.objects.get(pk=backs[1].pk).unpost()            # «A» снова можно вернуть
        late = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                             shipment=sale, date=JAN[5], time=NOON)
        late.lines.create(product=phone, quantity=1, price=900, serial_numbers="A", shipment_line=second)
        with self.assertRaisesMessage(StockError, "номера A («Телефон») не из выбранной строки отгрузки"):
            late.post()
