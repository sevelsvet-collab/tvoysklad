from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.inventory.models import StockBalance
from apps.inventory.services import StockError
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Shipment

User = get_user_model()


class CustomerReturnTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=10, price=100)
        receipt.post()
        self.shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.shipment.lines.create(product=self.product, quantity=6, price=150, vat_rate="none")
        self.shipment.post()  # остаток 4

    def test_customer_return_adds_stock_back(self):
        ret = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ret.lines.create(product=self.product, quantity=2, price=150, vat_rate="none")
        ret.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("6.000"))  # было 4, вернули 2
        self.assertEqual(bal.avg_cost, Decimal("100.00"))  # по текущей средней, не изменилась

    def test_customer_return_unpost_removes_stock(self):
        ret = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ret.lines.create(product=self.product, quantity=2, price=150, vat_rate="none")
        ret.post()
        ret.unpost()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("4.000"))

    def test_return_at_purchase_price_when_stock_zero(self):
        # распродали весь остаток
        ship2 = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ship2.lines.create(product=self.product, quantity=4, price=150, vat_rate="none")
        ship2.post()
        self.assertEqual(StockBalance.objects.get(product=self.product, warehouse=self.wh).quantity, Decimal("0.000"))
        self.product.purchase_price = Decimal("100")
        self.product.save()

        ret = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ret.lines.create(product=self.product, quantity=1, price=150, vat_rate="none")
        ret.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("1.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))  # взяли закупочную, не 0

    def test_create_return_from_shipment(self):
        manager = User.objects.create_user("m", password="pass12345")
        manager.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")
        resp = self.client.post(reverse("shipment_to_return", args=[self.shipment.pk]))
        self.assertEqual(resp.status_code, 302)
        ret = CustomerReturn.objects.get(shipment=self.shipment)
        self.assertEqual(ret.customer, self.customer)
        line = ret.lines.get()
        self.assertEqual(line.quantity, Decimal("6.000"))
        self.assertEqual(line.price, Decimal("150.00"))


class SupplierReturnTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        self.receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        self.receipt.lines.create(product=self.product, quantity=10, price=100)
        self.receipt.post()

    def test_supplier_return_removes_stock(self):
        ret = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        ret.lines.create(product=self.product, quantity=3, price=100)
        ret.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("7.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))

    def test_supplier_return_blocked_when_insufficient(self):
        ret = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        ret.lines.create(product=self.product, quantity=50, price=100)
        with self.assertRaises(StockError):
            ret.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("10.000"))

    def test_create_return_from_receipt(self):
        kl = User.objects.create_user("kl", password="pass12345")
        kl.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        self.client.login(username="kl", password="pass12345")
        resp = self.client.post(reverse("receipt_to_return", args=[self.receipt.pk]))
        self.assertEqual(resp.status_code, 302)
        ret = SupplierReturn.objects.get(receipt=self.receipt)
        self.assertEqual(ret.supplier, self.supplier)
        self.assertEqual(ret.lines.get().quantity, Decimal("10.000"))


class SettlementsWithReturnsTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_customer_return_reduces_debt(self):
        from apps.sales.models import Invoice

        inv = Invoice.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer, status=Invoice.STATUS_ISSUED,
        )
        inv.lines.create(product=self.product, quantity=1, price=1000, vat_rate="none")
        ret = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ret.lines.create(product=self.product, quantity=1, price=300, vat_rate="none")
        ret.post()

        resp = self.client.get(reverse("settlements"))
        row = next(r for r in resp.context["rows"] if r["cp"] == self.customer)
        self.assertEqual(row["sales"], Decimal("1000"))
        self.assertEqual(row["cust_return"], Decimal("300"))
        self.assertEqual(row["balance"], Decimal("700"))  # 1000 продали − 300 вернули
