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


class OrganizationVatTests(TestCase):
    """Ставка НДС в строках документа берётся из организации документа."""

    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        # товар заведён со ставкой 20% (как было по умолчанию)
        self.product = Product.objects.create(name="Товар", unit=self.unit, sale_price=490, vat_rate="20")
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def _post_invoice(self, org, vat_rate):
        data = {
            "date": "2026-09-10", "organization": org.pk, "warehouse": self.wh.pk,
            "customer": self.customer.pk, "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk, "lines-0-quantity": "1",
            "lines-0-price": "490", "lines-0-vat_rate": vat_rate,
        }
        resp = self.client.post(reverse("invoice_create"), data)
        self.assertEqual(resp.status_code, 302)
        return Invoice.objects.latest("id")

    def test_non_payer_saves_lines_without_vat(self):
        """Фирма на УСН: даже если в строке осталось 20%, сохраняется «Без НДС»."""
        usn = Organization.objects.create(name="ИП на УСН", vat_payer=False, default_vat_rate="none")
        invoice = self._post_invoice(usn, "20")
        self.assertEqual(invoice.lines.get().vat_rate, "none")

    def test_payer_keeps_product_rate(self):
        """Плательщик НДС: ставка из строки (товара) сохраняется, например 10%."""
        payer = Organization.objects.create(name="ООО с НДС", vat_payer=True, default_vat_rate="20")
        invoice = self._post_invoice(payer, "10")
        self.assertEqual(invoice.lines.get().vat_rate, "10")

    def test_new_invoice_form_defaults_to_org_rate(self):
        Organization.objects.create(name="ИП на УСН", vat_payer=False, default_vat_rate="20",
                                    is_default=True)
        resp = self.client.get(reverse("invoice_create"))
        # неплательщик с ошибочно оставленной ставкой 20% всё равно «Без НДС»
        self.assertEqual(resp.context["default_vat_rate"], "none")
        self.assertContains(resp, 'data-org-vat="')

    def test_org_vat_map_lists_all_organizations(self):
        import json

        usn = Organization.objects.create(name="ИП на УСН", vat_payer=False, default_vat_rate="none")
        payer = Organization.objects.create(name="ООО с НДС", vat_payer=True, default_vat_rate="10")
        resp = self.client.get(reverse("invoice_create"))
        vat_map = json.loads(resp.context["org_vat_json"])
        self.assertEqual(vat_map[str(usn.pk)], {"charges": False, "rate": "none"})
        self.assertEqual(vat_map[str(payer.pk)], {"charges": True, "rate": "10"})

    def test_vat_for_rules(self):
        usn = Organization(vat_payer=False, default_vat_rate="20")
        payer = Organization(vat_payer=True, default_vat_rate="20")
        self.assertEqual(usn.vat_for("20"), "none")
        self.assertEqual(payer.vat_for("10"), "10")
        self.assertEqual(payer.vat_for(""), "20")

    def test_new_product_gets_org_rate(self):
        """Новый товар получает ставку фирмы, а не жёсткие 20%."""
        Organization.objects.create(name="ИП на УСН", vat_payer=False, default_vat_rate="none",
                                    is_default=True)
        resp = self.client.get(reverse("product_create"))
        self.assertEqual(resp.context["form"].initial["vat_rate"], "none")

        resp = self.client.post(reverse("api_product_quick_create"), {"name": "Новая услуга"})
        self.assertEqual(Product.objects.get(name="Новая услуга").vat_rate, "none")
        self.assertEqual(resp.json()["product"]["vat_rate"], "none")


class DocumentNumberDateTests(TestCase):
    """Номер, дата и время документа в заголовке; защита от двойного сохранения."""

    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit, sale_price=490)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def _data(self, **extra):
        data = {
            "date": "2026-09-10", "time": "", "number": "",
            "organization": self.org.pk, "warehouse": self.wh.pk,
            "customer": self.customer.pk, "contract": "", "due_date": "", "comment": "",
            "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk, "lines-0-quantity": "1",
            "lines-0-price": "490", "lines-0-vat_rate": "none",
        }
        data.update(extra)
        return data

    def test_empty_number_assigned_automatically(self):
        self.client.post(reverse("invoice_create"), self._data())
        self.assertEqual(Invoice.objects.get().number, "00001")

    def test_manual_number_date_and_time_saved(self):
        resp = self.client.post(reverse("invoice_create"),
                                self._data(number="А-17", date="2026-08-01", time="14:35"))
        self.assertEqual(resp.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.number, "А-17")
        self.assertEqual(str(invoice.date), "2026-08-01")
        self.assertEqual(invoice.time.strftime("%H:%M"), "14:35")

    def test_number_editable_on_existing_document(self):
        self.client.post(reverse("invoice_create"), self._data())
        invoice = Invoice.objects.get()
        self.client.post(reverse("invoice_edit", args=[invoice.pk]), self._data(number="00099"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.number, "00099")

    def test_duplicate_number_rejected(self):
        self.client.post(reverse("invoice_create"), self._data(number="00005"))
        resp = self.client.post(reverse("invoice_create"), self._data(number="00005"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "уже есть в 2026 году")
        self.assertEqual(Invoice.objects.count(), 1)

    def test_auto_number_continues_after_manual(self):
        """Вписали вручную 00007 — следующий автономер будет 00008, а не 00001."""
        self.client.post(reverse("invoice_create"), self._data(number="00007"))
        self.client.post(reverse("invoice_create"), self._data())
        self.assertEqual(sorted(Invoice.objects.values_list("number", flat=True)), ["00007", "00008"])

    def test_auto_number_skips_taken(self):
        """Счётчик отстал от ручного номера — занятый номер пропускается."""
        from apps.core.models import DocumentNumber

        Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                               number="00001")
        DocumentNumber.objects.filter(organization=self.org).update(last_number=0)
        second = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.assertEqual(second.number, "00002")

    def test_time_defaults_when_not_sent(self):
        data = self._data()
        del data["time"]
        self.client.post(reverse("invoice_create"), data)
        self.assertIsNotNone(Invoice.objects.get().time)

    def test_double_submit_creates_one_invoice(self):
        """Двойной клик: та же форма с тем же токеном — второй счёт не создаётся."""
        data = self._data(submit_token="tok-123")
        first = self.client.post(reverse("invoice_create"), data)
        second = self.client.post(reverse("invoice_create"), data)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, first.url)   # ведёт на уже созданный счёт

    def test_new_form_has_token_and_heading_fields(self):
        resp = self.client.get(reverse("invoice_create"))
        self.assertContains(resp, 'name="submit_token"')
        self.assertContains(resp, 'id="doc-form"')
        self.assertContains(resp, 'name="number"')
        self.assertContains(resp, 'name="time"')
        self.assertContains(resp, 'placeholder="авто"')

    def test_adjustment_series_separate_by_kind(self):
        """У оприходований и списаний свои серии: одинаковый номер допустим."""
        from apps.inventory.models import StockAdjustment

        StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org,
                                       warehouse=self.wh, number="00003")
        other = StockAdjustment(kind=StockAdjustment.KIND_EXPENSE, organization=self.org, warehouse=self.wh)
        from apps.inventory.forms import AdjustmentForm

        form = AdjustmentForm(data={"date": "2026-09-10", "number": "00003", "time": "",
                                    "organization": self.org.pk, "warehouse": self.wh.pk,
                                    "reason": "", "comment": ""}, instance=other)
        self.assertTrue(form.is_valid(), form.errors)
