"""Документы «на основании»: счёт → отгрузка, отгрузка → возврат покупателя,
приёмка → возврат поставщику.

Подтверждённая ошибка: счёт со скидкой 10% (итог 90) давал отгрузку без скидки
(итог 100). Скидка терялась во всех трёх переходах, а шапка и строки создавались
не одной транзакцией.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.constants import DOC_DRAFT
from apps.core.models import DocumentNumber, Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn, SupplierReturnLine
from apps.sales.models import CustomerReturn, CustomerReturnLine, Invoice, Shipment, ShipmentLine

User = get_user_model()

# товар, количество, цена, скидка %, НДС — в т.ч. дробные количества и скидки
LINES = [
    ("Кабель", Decimal("1"), Decimal("100"), Decimal("10"), "none"),
    ("Провод, м", Decimal("2.5"), Decimal("33.33"), Decimal("12.5"), "20"),
    ("Гайка, кг", Decimal("0.375"), Decimal("999.99"), Decimal("0"), "10"),
    ("Монтаж", Decimal("3"), Decimal("1500"), Decimal("7.125"), "none"),
]
FIELDS = ("product_id", "quantity", "price", "discount", "vat_rate", "serial_numbers")


class BasedOnBase(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.products = [Product.objects.create(name=name, unit=self.unit) for name, *_ in LINES]
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def fill(self, document):
        for product, (_name, quantity, price, discount, vat) in zip(self.products, LINES):
            document.lines.create(product=product, quantity=quantity, price=price, discount=discount, vat_rate=vat)
        return document

    def rows(self, document):
        return list(document.lines.order_by("pk").values_list(*FIELDS))

    def assert_same_lines(self, source, target):
        self.assertEqual(self.rows(target), self.rows(source))
        self.assertEqual(target.total, source.total)
        self.assertEqual(target.status, DOC_DRAFT)       # статус проведения не копируется


class InvoiceToShipmentTests(BasedOnBase):
    def invoice(self):
        return Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)

    def test_confirmed_scenario_discount_kept(self):
        invoice = self.invoice()
        invoice.lines.create(product=self.products[0], quantity=1, price=100, discount=10, vat_rate="none")
        self.assertEqual(invoice.total, Decimal("90.00"))
        self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        shipment = Shipment.objects.get(invoice=invoice)
        self.assertEqual(shipment.total, Decimal("90.00"))
        self.assertEqual(shipment.lines.get().discount, Decimal("10"))

    def test_several_lines_with_fractions(self):
        invoice = self.fill(self.invoice())
        resp = self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        shipment = Shipment.objects.get(invoice=invoice)
        self.assertRedirects(resp, reverse("shipment_edit", args=[shipment.pk]))
        self.assert_same_lines(invoice, shipment)
        self.assertEqual((shipment.organization, shipment.warehouse, shipment.customer),
                         (self.org, self.wh, self.customer))

    def test_serials_follow_and_cost_is_not_invented(self):
        invoice = self.invoice()
        invoice.lines.create(product=self.products[0], quantity=2, price=100, serial_numbers="SN-1\nSN-2")
        self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        line = Shipment.objects.get(invoice=invoice).lines.get()
        self.assertEqual(line.serial_numbers, "SN-1\nSN-2")
        self.assertEqual(line.cost_price, 0)             # себестоимость считается при проведении

    def test_issued_invoice_gives_draft_shipment(self):
        invoice = self.fill(self.invoice())
        invoice.post()
        self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        self.assertEqual(Shipment.objects.get(invoice=invoice).status, DOC_DRAFT)

    def test_atomic_no_partial_shipment_on_error(self):
        invoice = self.fill(self.invoice())
        numbers_before = list(DocumentNumber.objects.values("doc_type", "last_number"))
        real_create = ShipmentLine.objects.create
        calls = []

        def fail_on_third(**kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("сбой на третьей строке")
            return real_create(**kwargs)

        with mock.patch.object(ShipmentLine.objects, "create", side_effect=fail_on_third):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        self.assertFalse(Shipment.objects.exists())                  # ни шапки,
        self.assertFalse(ShipmentLine.objects.exists())              # ни строк,
        self.assertEqual(list(DocumentNumber.objects.values("doc_type", "last_number")), numbers_before)  # ни номера


class ShipmentToReturnTests(BasedOnBase):
    def shipment(self):
        return Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)

    def test_discount_and_fractions_kept(self):
        shipment = self.fill(self.shipment())
        resp = self.client.post(reverse("shipment_to_return", args=[shipment.pk]))
        doc = CustomerReturn.objects.get(shipment=shipment)
        self.assertRedirects(resp, reverse("customer_return_edit", args=[doc.pk]))
        self.assert_same_lines(shipment, doc)
        self.assertEqual(doc.customer, self.customer)

    def test_single_discounted_line(self):
        shipment = self.shipment()
        shipment.lines.create(product=self.products[0], quantity=1, price=100, discount=10, vat_rate="none")
        self.client.post(reverse("shipment_to_return", args=[shipment.pk]))
        self.assertEqual(CustomerReturn.objects.get(shipment=shipment).total, Decimal("90.00"))

    def test_sold_serials_come_back(self):
        """Возвращаются те же единицы, что были отгружены."""
        shipment = self.shipment()
        shipment.lines.create(product=self.products[0], quantity=1, price=100, serial_numbers="SN-7")
        self.client.post(reverse("shipment_to_return", args=[shipment.pk]))
        self.assertEqual(CustomerReturn.objects.get(shipment=shipment).lines.get().serial_numbers, "SN-7")

    def test_atomic(self):
        shipment = self.fill(self.shipment())
        with mock.patch.object(CustomerReturnLine.objects, "create", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("shipment_to_return", args=[shipment.pk]))
        self.assertFalse(CustomerReturn.objects.exists())


class ReceiptToReturnTests(BasedOnBase):
    def receipt(self):
        return Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)

    def test_discount_and_fractions_kept(self):
        receipt = self.fill(self.receipt())
        resp = self.client.post(reverse("receipt_to_return", args=[receipt.pk]))
        doc = SupplierReturn.objects.get(receipt=receipt)
        self.assertRedirects(resp, reverse("supplier_return_edit", args=[doc.pk]))
        self.assert_same_lines(receipt, doc)
        self.assertEqual(doc.supplier, self.supplier)

    def test_single_discounted_line(self):
        receipt = self.receipt()
        receipt.lines.create(product=self.products[0], quantity=1, price=100, discount=10, vat_rate="none")
        self.client.post(reverse("receipt_to_return", args=[receipt.pk]))
        self.assertEqual(SupplierReturn.objects.get(receipt=receipt).total, Decimal("90.00"))

    def test_received_serials_go_back(self):
        receipt = self.receipt()
        receipt.lines.create(product=self.products[0], quantity=1, price=100, serial_numbers="SN-3")
        self.client.post(reverse("receipt_to_return", args=[receipt.pk]))
        self.assertEqual(SupplierReturn.objects.get(receipt=receipt).lines.get().serial_numbers, "SN-3")

    def test_posted_receipt_gives_draft_return(self):
        receipt = self.fill(self.receipt())
        receipt.post()
        self.client.post(reverse("receipt_to_return", args=[receipt.pk]))
        self.assertEqual(SupplierReturn.objects.get(receipt=receipt).status, DOC_DRAFT)

    def test_atomic(self):
        receipt = self.fill(self.receipt())
        with mock.patch.object(SupplierReturnLine.objects, "create", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("receipt_to_return", args=[receipt.pk]))
        self.assertFalse(SupplierReturn.objects.exists())
