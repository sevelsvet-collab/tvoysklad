"""Учёт по серийным номерам: приход, расход, перемещение, возвраты, списание,
ввод номеров старого остатка, отчёт и печать."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Invoice, Shipment

from . import serials
from .models import SerialNumber, StockAdjustment, Transfer
from .services import StockError

User = get_user_model()


class SerialTrackingTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Магазин")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True, sale_price=900)
        self.cable = Product.objects.create(name="Кабель", unit=self.unit)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="m", password="pass12345")

    # ---------- помощники ----------

    def _receipt(self, numbers, qty=None, product=None, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        doc.lines.create(product=product or self.phone, quantity=qty if qty is not None else len(numbers),
                         price=500, serial_numbers="\n".join(numbers))
        if post:
            doc.post()
        return doc

    def _shipment(self, numbers, qty=None, warehouse=None):
        doc = Shipment.objects.create(organization=self.org, warehouse=warehouse or self.wh, customer=self.customer)
        doc.lines.create(product=self.phone, quantity=qty if qty is not None else len(numbers),
                         price=900, serial_numbers="\n".join(numbers))
        return doc

    def _serial(self, number):
        return SerialNumber.objects.get(product=self.phone, number=number)

    # ---------- приёмка ----------

    def test_receipt_puts_serials_on_stock(self):
        self._receipt(["SN-1", "SN-2"])
        self.assertEqual(self._serial("SN-1").warehouse, self.wh)
        self.assertEqual(self._serial("SN-2").status, SerialNumber.STATUS_IN_STOCK)

    def test_receipt_requires_serial_for_each_unit(self):
        doc = self._receipt(["SN-1"], qty=2, post=False)
        with self.assertRaisesMessage(StockError, "нужно серийных номеров — 2, указано — 1"):
            doc.post()
        doc.refresh_from_db()
        self.assertFalse(doc.is_posted)                         # документ не провёлся
        self.assertFalse(SerialNumber.objects.exists())
        self.assertFalse(self.phone.movements.exists())        # и остаток не изменился

    def test_receipt_rejects_number_already_on_stock(self):
        self._receipt(["SN-1"])
        with self.assertRaisesMessage(StockError, "номер SN-1 уже числится на складе"):
            self._receipt(["SN-1"])

    def test_fractional_quantity_rejected(self):
        with self.assertRaisesMessage(StockError, "количество должно быть целым"):
            self._receipt(["SN-1"], qty=Decimal("1.5"))

    def test_product_without_tracking_needs_no_serials(self):
        self._receipt([], qty=3, product=self.cable)
        self.assertFalse(SerialNumber.objects.exists())

    def test_serials_parsed_by_newline_comma_semicolon(self):
        self.assertEqual(serials.parse(" A1 \nB2, C3;A1\n\n"), ["A1", "B2", "C3"])

    # ---------- отгрузка ----------

    def test_shipment_sells_serial_from_stock(self):
        self._receipt(["SN-1", "SN-2"])
        self._shipment(["SN-2"]).post()
        self.assertEqual(self._serial("SN-2").status, SerialNumber.STATUS_SOLD)
        self.assertIsNone(self._serial("SN-2").warehouse)
        self.assertEqual(self._serial("SN-1").warehouse, self.wh)

    def test_shipment_requires_serials(self):
        self._receipt(["SN-1"])
        with self.assertRaisesMessage(StockError, "нужно серийных номеров — 1, указано — 0"):
            self._shipment([], qty=1).post()

    def test_shipment_only_numbers_from_stock(self):
        self._receipt(["SN-1"])
        with self.assertRaisesMessage(StockError, "номера SN-9 нет на складе «Основной»"):
            self._shipment(["SN-9"]).post()

    def test_shipment_from_other_warehouse_rejected(self):
        """Товар есть на обоих складах, но номер SN-1 лежит не в «Магазине»."""
        self._receipt(["SN-1"])
        other = Receipt.objects.create(organization=self.org, warehouse=self.wh2, supplier=self.supplier)
        other.lines.create(product=self.phone, quantity=1, price=500, serial_numbers="SN-2")
        other.post()
        with self.assertRaisesMessage(StockError, "номера SN-1 нет на складе «Магазин»"):
            self._shipment(["SN-1"], warehouse=self.wh2).post()

    def test_sold_serial_cannot_be_sold_twice(self):
        self._receipt(["SN-1", "SN-2"])
        self._shipment(["SN-1"]).post()
        with self.assertRaises(StockError):
            self._shipment(["SN-1"]).post()

    def test_unpost_shipment_returns_serial_to_stock(self):
        self._receipt(["SN-1"])
        shipment = self._shipment(["SN-1"])
        shipment.post()
        shipment.unpost()
        self.assertEqual(self._serial("SN-1").warehouse, self.wh)
        self.assertEqual(self._serial("SN-1").status, SerialNumber.STATUS_IN_STOCK)

    def test_unpost_receipt_removes_serials(self):
        receipt = self._receipt(["SN-1"])
        receipt.unpost()
        self.assertFalse(SerialNumber.objects.exists())

    def test_draft_saved_without_serials(self):
        """Черновик отгрузки без номеров сохраняется — проверка только при проведении."""
        shipment = self._shipment([], qty=1)
        self.assertEqual(shipment.lines.count(), 1)

    # ---------- перемещение, возвраты, списание ----------

    def test_transfer_moves_serial_between_warehouses(self):
        self._receipt(["SN-1"])
        transfer = Transfer.objects.create(organization=self.org, warehouse_from=self.wh, warehouse_to=self.wh2)
        transfer.lines.create(product=self.phone, quantity=1, serial_numbers="SN-1")
        transfer.post()
        self.assertEqual(self._serial("SN-1").warehouse, self.wh2)
        transfer.unpost()
        self.assertEqual(self._serial("SN-1").warehouse, self.wh)

    def test_customer_return_brings_serial_back(self):
        self._receipt(["SN-1"])
        self._shipment(["SN-1"]).post()
        ret = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        ret.lines.create(product=self.phone, quantity=1, price=900, serial_numbers="SN-1")
        ret.post()
        self.assertEqual(self._serial("SN-1").status, SerialNumber.STATUS_IN_STOCK)

    def test_supplier_return_marks_serial_returned(self):
        self._receipt(["SN-1"])
        ret = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        ret.lines.create(product=self.phone, quantity=1, price=500, serial_numbers="SN-1")
        ret.post()
        self.assertEqual(self._serial("SN-1").status, SerialNumber.STATUS_RETURNED)

    def test_expense_adjustment_writes_serial_off(self):
        self._receipt(["SN-1"])
        adj = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org,
                                             warehouse=self.wh)
        adj.lines.create(product=self.phone, quantity=1, serial_numbers="SN-1")
        adj.post()
        self.assertEqual(self._serial("SN-1").status, SerialNumber.STATUS_WRITTEN_OFF)

    def test_income_adjustment_adds_serial(self):
        adj = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org,
                                             warehouse=self.wh)
        adj.lines.create(product=self.phone, quantity=1, price=500, serial_numbers="SN-7")
        adj.post()
        self.assertEqual(self._serial("SN-7").warehouse, self.wh)

    # ---------- старый остаток без номеров ----------

    def test_enter_numbers_for_old_stock(self):
        self.phone.track_serials = False
        self.phone.save()
        self._receipt([], qty=2)                       # приняли до включения учёта
        self.phone.track_serials = True
        self.phone.save()
        resp = self.client.post(reverse("product_serial_stock", args=[self.phone.pk]),
                                {"warehouse": self.wh.pk, "numbers": "OLD-1\nOLD-2"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SerialNumber.objects.filter(warehouse=self.wh).count(), 2)
        self._shipment(["OLD-1"]).post()               # теперь их можно отгрузить
        self.assertEqual(self._serial("OLD-1").status, SerialNumber.STATUS_SOLD)

    def test_cannot_enter_more_numbers_than_stock(self):
        self.phone.track_serials = False
        self.phone.save()
        self._receipt([], qty=1)
        self.phone.track_serials = True
        self.phone.save()
        with self.assertRaisesMessage(StockError, "без номеров 1 шт., а введено 2"):
            serials.enter_stock(self.phone, self.wh, "OLD-1\nOLD-2")

    def test_product_card_shows_stock_without_numbers(self):
        self.phone.track_serials = False
        self.phone.save()
        self._receipt([], qty=3)
        self.phone.track_serials = True
        self.phone.save()
        resp = self.client.get(reverse("product_edit", args=[self.phone.pk]))
        self.assertContains(resp, "Ввести номера остатка")
        self.assertEqual(resp.context["serial_stock"][0]["without"], 3)

    def test_service_cannot_track_serials(self):
        resp = self.client.post(reverse("product_create"), {
            "item_type": "service", "name": "Настройка", "article": "", "code": "", "barcode": "",
            "unit": self.unit.pk, "vat_rate": "20", "purchase_price": "0", "sale_price": "100",
            "min_stock": "0", "description": "", "is_active": "on", "track_serials": "on",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Product.objects.get(name="Настройка").track_serials)

    # ---------- интерфейс, отчёт, печать ----------

    def test_available_api_lists_numbers_on_warehouse(self):
        self._receipt(["SN-2", "SN-1"])
        resp = self.client.get(reverse("api_serials_available"), {"product": self.phone.pk, "warehouse": self.wh.pk})
        self.assertEqual(resp.json()["serials"], ["SN-1", "SN-2"])

    def test_search_api_flags_tracked_products(self):
        resp = self.client.get(reverse("api_product_search"), {"id": self.phone.pk})
        self.assertTrue(resp.json()["results"][0]["track_serials"])

    def test_document_forms_have_serial_mode(self):
        self.assertContains(self.client.get(reverse("receipt_create")), 'data-serial-mode="enter"')
        self.assertContains(self.client.get(reverse("shipment_create")), 'data-serial-mode="pick"')
        self.assertContains(self.client.get(reverse("invoice_create")), 'data-serial-mode="pick-optional"')

    def test_report_shows_serial_and_history(self):
        self._receipt(["SN-1"])
        shipment = self._shipment(["SN-1"])
        shipment.post()
        resp = self.client.get(reverse("report_serials"), {"q": "SN-1"})
        self.assertContains(resp, "SN-1")
        self.assertContains(resp, "Продан")
        self.assertContains(resp, f"Отгрузка № {shipment.number}")
        self.assertContains(resp, "Покупатель")
        detail = self.client.get(reverse("report_serial_detail", args=[self._serial("SN-1").pk]))
        self.assertContains(detail, "Приёмка")
        self.assertContains(detail, "Отгрузка")

    def test_torg12_prints_serial_numbers(self):
        self._receipt(["SN-1"])
        shipment = self._shipment(["SN-1"])
        resp = self.client.get(reverse("print_shipment_torg12", args=[shipment.pk]), {"fmt": "html"})
        self.assertContains(resp, "S/N: SN-1")

    def test_invoice_serials_copied_to_shipment(self):
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        invoice.lines.create(product=self.phone, quantity=1, price=900, serial_numbers="SN-5")
        self.client.post(reverse("invoice_to_shipment", args=[invoice.pk]))
        self.assertEqual(Shipment.objects.get().lines.get().serial_numbers, "SN-5")
