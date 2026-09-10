"""Списки как в МойСклад: поиск, фильтры, пагинация, действия с отмеченными,
окно подтверждения вместо системного confirm()."""
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, Payment
from apps.inventory.models import StockAdjustment, StockBalance
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import Invoice, Shipment

User = get_user_model()


class ListBase(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Альфа", is_default=True)
        self.org2 = Organization.objects.create(name="Бета")
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Магазин")
        self.customer = Counterparty.objects.create(name="Ромашка", partner_type=Counterparty.TYPE_CUSTOMER)
        self.other = Counterparty.objects.create(name="Лютик", partner_type=Counterparty.TYPE_CUSTOMER)
        self.supplier = Counterparty.objects.create(name="Опт-Снаб", partner_type=Counterparty.TYPE_SUPPLIER)
        self.product = Product.objects.create(name="Кабель", unit=self.unit, code="00001")
        self.user = User.objects.create_user("admin1", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="admin1", password="pass12345")

    def _shipment(self, **kw):
        data = dict(organization=self.org, warehouse=self.wh, customer=self.customer, date=date(2026, 9, 10))
        data.update(kw)
        return Shipment.objects.create(**data)

    def _numbers(self, resp, key):
        return sorted(obj.number for obj in resp.context[key])


class DocumentListFilterTests(ListBase):
    def test_period_filter(self):
        early = self._shipment(date=date(2026, 8, 1))
        late = self._shipment(date=date(2026, 9, 5))
        resp = self.client.get(reverse("shipment_list"), {"date_from": "2026-09-01", "date_to": "2026-09-30"})
        self.assertEqual(self._numbers(resp, "shipments"), [late.number])
        self.assertEqual(resp.context["filters_active"], 1)
        self.assertNotIn(early.number, self._numbers(resp, "shipments"))

    def test_status_organization_warehouse_partner_filters(self):
        target = self._shipment(organization=self.org2, warehouse=self.wh2, customer=self.other)
        self._shipment()
        resp = self.client.get(reverse("shipment_list"), {
            "organization": self.org2.pk, "warehouse": self.wh2.pk, "partner": "лют", "status": "draft",
        })
        self.assertEqual(self._numbers(resp, "shipments"), [target.number])
        self.assertEqual(resp.context["filters_active"], 4)

    def test_quick_search_still_works(self):
        target = self._shipment(customer=self.other)
        self._shipment()
        resp = self.client.get(reverse("shipment_list"), {"q": "Лютик"})
        self.assertEqual(self._numbers(resp, "shipments"), [target.number])

    def test_garbage_filter_values_ignored(self):
        self._shipment()
        resp = self.client.get(reverse("shipment_list"), {"status": "evil", "date_from": "not-a-date"})
        self.assertEqual(len(resp.context["shipments"]), 1)

    def test_pagination_at_bottom_with_page_size(self):
        for _ in range(3):
            self._shipment()
        resp = self.client.get(reverse("shipment_list"), {"per_page": 10})
        self.assertContains(resp, "Показаны 1–3 из 3")
        self.assertContains(resp, 'id="per-page-select"')

    def test_invoice_list_uses_shared_toolbar(self):
        Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        resp = self.client.get(reverse("invoice_list"), {"status": "draft"})
        self.assertContains(resp, 'id="list-filter-form"')
        self.assertContains(resp, 'id="col-settings"')       # настройка столбцов сохранилась
        self.assertEqual(len(resp.context["invoices"]), 1)

    def test_all_lists_have_toolbar_and_checkboxes(self):
        for name in ["invoice_list", "shipment_list", "customer_return_list", "receipt_list",
                     "supplier_return_list", "transfer_list", "adjustment_list_income", "payment_list",
                     "counterparty_list", "contract_list", "product_list"]:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, 'id="list-filter-form"', msg_prefix=name)
            self.assertContains(resp, "data-list-select-all", msg_prefix=name)


class BulkDeleteTests(ListBase):
    def test_bulk_delete_unposts_and_deletes(self):
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=Product.objects.create(name="Товар", unit=self.unit), quantity=5, price=10)
        receipt.post()
        draft = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        keep = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        resp = self.client.post(reverse("receipt_bulk_delete"), {"ids": [receipt.pk, draft.pk]},
                                HTTP_REFERER="http://testserver/purchases/receipts/?status=draft")
        self.assertEqual(list(Receipt.objects.values_list("pk", flat=True)), [keep.pk])
        self.assertFalse(StockBalance.objects.exists())             # остаток снят вместе с приёмкой
        self.assertEqual(resp.url, "http://testserver/purchases/receipts/?status=draft")  # фильтры сохранены

    def test_bulk_delete_posted_payment_recomputes_invoice(self):
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        account = Account.objects.create(organization=self.org, name="Р/с")
        payment = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=account,
                                         counterparty=self.customer, invoice=invoice, amount=500)
        payment.post()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, 500)
        self.client.post(reverse("payment_bulk_delete"), {"ids": [payment.pk]})
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, 0)

    def test_adjustment_bulk_delete_not_caught_by_kind_url(self):
        adj = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.org, warehouse=self.wh)
        self.client.post(reverse("adjustment_bulk_delete"), {"ids": [adj.pk]})
        self.assertFalse(StockAdjustment.objects.exists())

    def test_nothing_selected(self):
        self._shipment()
        resp = self.client.post(reverse("shipment_bulk_delete"), {}, follow=True)
        self.assertContains(resp, "Ничего не выбрано")
        self.assertEqual(Shipment.objects.count(), 1)


class ProductListTests(ListBase):
    def test_archived_hidden_by_default(self):
        Product.objects.create(name="Старый", unit=self.unit, is_active=False)
        names = [p.name for p in self.client.get(reverse("product_list")).context["products"]]
        self.assertEqual(names, ["Кабель"])
        names = [p.name for p in self.client.get(reverse("product_list"), {"show": "all"}).context["products"]]
        self.assertEqual(sorted(names), ["Кабель", "Старый"])
        names = [p.name for p in self.client.get(reverse("product_list"), {"show": "archived"}).context["products"]]
        self.assertEqual(names, ["Старый"])

    def test_field_filters(self):
        Product.objects.create(name="Настройка", unit=self.unit, item_type=Product.TYPE_SERVICE, code="00002")
        resp = self.client.get(reverse("product_list"), {"item_type": "service"})
        self.assertEqual([p.name for p in resp.context["products"]], ["Настройка"])
        resp = self.client.get(reverse("product_list"), {"code": "00001"})
        self.assertEqual([p.name for p in resp.context["products"]], ["Кабель"])

    def test_bulk_archive_and_restore(self):
        self.client.post(reverse("product_bulk", args=["archive"]), {"ids": [self.product.pk]})
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.client.post(reverse("product_bulk", args=["restore"]), {"ids": [self.product.pk]})
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)

    def test_bulk_delete_keeps_products_used_in_documents(self):
        spare = Product.objects.create(name="Лишний", unit=self.unit)
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=1, price=10)
        resp = self.client.post(reverse("product_bulk", args=["delete"]), {"ids": [self.product.pk, spare.pk]},
                                follow=True)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertFalse(Product.objects.filter(pk=spare.pk).exists())
        self.assertContains(resp, "отправьте их в архив")

    def test_service_button_prefills_type(self):
        resp = self.client.get(reverse("product_create"), {"item_type": "service"})
        self.assertEqual(resp.context["form"].initial["item_type"], "service")


class CounterpartyListTests(ListBase):
    def test_type_filter_includes_both(self):
        both = Counterparty.objects.create(name="Универсал", partner_type=Counterparty.TYPE_BOTH)
        names = {c.name for c in self.client.get(reverse("counterparty_list"), {"type": "supplier"}).context["counterparties"]}
        self.assertEqual(names, {"Опт-Снаб", both.name})

    def test_bulk_archive(self):
        self.client.post(reverse("counterparty_bulk", args=["archive"]), {"ids": [self.other.pk]})
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_active)
        names = {c.name for c in self.client.get(reverse("counterparty_list"), {"active": "archived"}).context["counterparties"]}
        self.assertEqual(names, {"Лютик"})


class ConfirmDialogTests(TestCase):
    def test_no_native_confirm_left_in_templates(self):
        """Удаление подтверждается окном программы (data-confirm), а не системным confirm()."""
        offenders = [str(p) for p in Path(settings.BASE_DIR, "templates").rglob("*.html")
                     if "return confirm(" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])
