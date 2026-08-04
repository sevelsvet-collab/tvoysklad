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
from apps.purchases.models import Receipt

from .models import Invoice, Shipment

User = get_user_model()


class SalesFlowTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        # приёмка 10 шт по 100
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=10, price=100)
        receipt.post()

    def _shipment(self, qty, price):
        shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        shipment.lines.create(product=self.product, quantity=qty, price=price)
        return shipment

    def test_end_to_end_profit_and_stock(self):
        """Сквозной сценарий из плана: приёмка 10×100 → отгрузка 6×150 → остаток 4, прибыль 300."""
        shipment = self._shipment(6, 150)
        shipment.post()

        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("4.000"))
        self.assertEqual(bal.avg_cost, Decimal("100.00"))  # себестоимость остатка не изменилась

        line = shipment.lines.get()
        self.assertEqual(line.cost_price, Decimal("100.00"))  # зафиксирована при проведении
        self.assertEqual(line.total, Decimal("900.00"))       # выручка 6×150
        self.assertEqual(line.cost_total, Decimal("600.00"))  # себестоимость 6×100
        self.assertEqual(line.profit, Decimal("300.00"))
        self.assertEqual(shipment.profit_total, Decimal("300.00"))

    def test_shipment_blocked_when_insufficient_stock(self):
        shipment = self._shipment(50, 150)
        with self.assertRaises(StockError):
            shipment.post()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("10.000"))  # остаток не тронут

    def test_unpost_returns_stock(self):
        shipment = self._shipment(6, 150)
        shipment.post()
        shipment.unpost()
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual(bal.quantity, Decimal("10.000"))
        self.assertFalse(shipment.is_posted)

    def test_invoice_totals_and_status(self):
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        invoice.lines.create(product=self.product, quantity=3, price=150, vat_rate="20")
        self.assertEqual(invoice.total, Decimal("450.00"))
        self.assertEqual(invoice.payment_status, "not_paid")
        invoice.post()
        self.assertEqual(invoice.status, Invoice.STATUS_ISSUED)
        self.assertTrue(invoice.is_posted)

    def test_create_shipment_from_invoice(self):
        manager = User.objects.create_user("m", password="pass12345")
        manager.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        invoice.lines.create(product=self.product, quantity=5, price=150)

        resp = self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        self.assertEqual(resp.status_code, 302)
        shipment = Shipment.objects.get(invoice=invoice)
        self.assertEqual(shipment.customer, self.customer)
        line = shipment.lines.get()
        self.assertEqual(line.quantity, Decimal("5.000"))
        self.assertEqual(line.price, Decimal("150.00"))

    def test_invoice_shipped_amount(self):
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        shipment = Shipment.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer, invoice=invoice,
        )
        shipment.lines.create(product=self.product, quantity=6, price=150)
        shipment.post()
        self.assertEqual(invoice.shipped_amount, Decimal("900.00"))


class EmptyLineRowTests(TestCase):
    """Пустая строка ввода (всегда висит внизу документа) не должна сохраняться."""

    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit, sale_price=490)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def test_trailing_empty_row_is_ignored_on_save(self):
        data = {
            "date": "2026-08-03", "organization": self.org.pk, "warehouse": self.wh.pk,
            "customer": self.customer.pk, "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            # строка 0 — заполнена
            "lines-0-product": self.product.pk, "lines-0-quantity": "1",
            "lines-0-price": "490", "lines-0-vat_rate": "none",
            # строка 1 — пустая (товар не выбран), но с дефолтными кол-вом/ставкой
            "lines-1-product": "", "lines-1-quantity": "1",
            "lines-1-price": "0", "lines-1-vat_rate": "20",
        }
        resp = self.client.post(reverse("invoice_create"), data)
        self.assertEqual(resp.status_code, 302)  # без ошибок валидации
        invoice = Invoice.objects.latest("id")
        self.assertEqual(invoice.lines.count(), 1)  # сохранилась только заполненная строка
        self.assertEqual(invoice.total, Decimal("490.00"))

    def test_line_total_with_percent_discount(self):
        from .models import InvoiceLine

        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        line = InvoiceLine.objects.create(
            invoice=invoice, product=self.product, quantity=2, price=1000, discount=10, vat_rate="none",
        )
        self.assertEqual(line.total, Decimal("1800.00"))  # 2×1000 − 10%
        self.assertEqual(invoice.total, Decimal("1800.00"))

    def test_empty_discount_saves_as_zero(self):
        data = {
            "date": "2026-08-03", "organization": self.org.pk, "warehouse": self.wh.pk,
            "customer": self.customer.pk, "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk, "lines-0-quantity": "1",
            "lines-0-price": "490", "lines-0-discount": "", "lines-0-vat_rate": "none",
        }
        resp = self.client.post(reverse("invoice_create"), data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Invoice.objects.latest("id").lines.get().discount, Decimal("0"))

    def test_document_without_any_line_saves(self):
        data = {
            "date": "2026-08-03", "organization": self.org.pk, "warehouse": self.wh.pk,
            "customer": self.customer.pk, "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": "", "lines-0-quantity": "1", "lines-0-price": "0", "lines-0-vat_rate": "20",
        }
        resp = self.client.post(reverse("invoice_create"), data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Invoice.objects.latest("id").lines.count(), 0)


class CustomerFilterTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        Counterparty.objects.create(name="Снабженец", partner_type=Counterparty.TYPE_SUPPLIER)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def test_invoice_form_uses_live_search(self):
        """В форме — поле живого поиска, а не список всех контрагентов."""
        resp = self.client.get(reverse("invoice_create"))
        self.assertContains(resp, "ac-input")
        self.assertNotContains(resp, "Снабженец")

    def test_customer_search_api_excludes_suppliers(self):
        resp = self.client.get(reverse("api_counterparty_search"), {"type": "customer"})
        names = [r["name"] for r in resp.json()["results"]]
        self.assertIn("Клиент", names)
        self.assertNotIn("Снабженец", names)
