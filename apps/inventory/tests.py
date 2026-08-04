from decimal import Decimal

from django.test import TestCase

from apps.catalog.models import Product, Unit
from apps.core.models import Organization, Warehouse
from apps.inventory import services
from apps.inventory.models import StockAdjustment, StockBalance, Transfer
from apps.inventory.services import StockError
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt


class WeightedAverageCostTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.product = Product.objects.create(name="Гвозди", unit=self.unit)

    def _receipt(self, qty, price):
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=qty, price=price)
        return receipt

    def test_single_receipt_sets_balance_and_cost(self):
        self._receipt(10, 100).post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("10.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))

    def test_weighted_average_across_two_receipts(self):
        self._receipt(10, 100).post()   # 10 шт по 100
        self._receipt(10, 200).post()   # +10 шт по 200 → средняя 150
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("20.000"))
        self.assertEqual(bal.avg_cost, Decimal("150.00"))

    def test_unpost_reverses_balance(self):
        r1 = self._receipt(10, 100)
        r1.post()
        r2 = self._receipt(10, 200)
        r2.post()
        r2.unpost()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("10.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))

    def test_current_avg_cost_helper(self):
        self._receipt(5, 80).post()
        self.assertEqual(services.current_avg_cost(self.product.pk, self.wh.pk), Decimal("80.00"))
        self.assertEqual(services.current_qty(self.product.pk, self.wh.pk), Decimal("5.000"))


class AdjustmentTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.product = Product.objects.create(name="Товар", unit=self.unit)

    def test_income_adds_stock(self):
        adj = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org, warehouse=self.wh)
        adj.lines.create(product=self.product, quantity=7, price=50)
        adj.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("7.000"))
        self.assertEqual(bal.avg_cost, Decimal("50.00"))

    def test_expense_removes_stock_at_avg(self):
        income = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org, warehouse=self.wh)
        income.lines.create(product=self.product, quantity=10, price=100)
        income.post()
        expense = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org, warehouse=self.wh)
        expense.lines.create(product=self.product, quantity=4)
        expense.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("6.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))

    def test_expense_blocked_when_insufficient(self):
        expense = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org, warehouse=self.wh)
        expense.lines.create(product=self.product, quantity=5)
        with self.assertRaises(StockError):
            expense.post()
        # остаток не появился, статус не изменился
        self.assertFalse(StockBalance.objects.filter(product=self.product).exists())
        expense.refresh_from_db()
        self.assertFalse(expense.is_posted)


class TransferTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh1 = Warehouse.objects.create(name="Склад 1", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Склад 2")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh1, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=10, price=100)
        receipt.post()

    def test_transfer_moves_stock_keeping_cost(self):
        transfer = Transfer.objects.create(
            organization=self.org, warehouse_from=self.wh1, warehouse_to=self.wh2,
        )
        transfer.lines.create(product=self.product, quantity=4)
        transfer.post()

        b1 = StockBalance.objects.get(product=self.product, warehouse=self.wh1)
        b2 = StockBalance.objects.get(product=self.product, warehouse=self.wh2)
        self.assertEqual(b1.quantity, Decimal("6.000"))
        self.assertEqual(b2.quantity, Decimal("4.000"))
        self.assertEqual(b2.avg_cost, Decimal("100.00"))  # себестоимость перенесена

    def test_transfer_blocked_when_insufficient(self):
        transfer = Transfer.objects.create(
            organization=self.org, warehouse_from=self.wh1, warehouse_to=self.wh2,
        )
        transfer.lines.create(product=self.product, quantity=50)
        with self.assertRaises(StockError):
            transfer.post()


class DocumentNumberingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)

    def test_receipt_numbers_sequential(self):
        r1 = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        r2 = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        self.assertEqual(r1.number, "00001")
        self.assertEqual(r2.number, "00002")
