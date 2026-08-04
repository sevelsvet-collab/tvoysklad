from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance.models import Account, Payment
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import Invoice, Shipment

from . import services

User = get_user_model()


class SalesReportServiceTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        # приёмка 10×100, отгрузка 6×150 → выручка 900, себестоимость 600, прибыль 300
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        receipt.lines.create(product=self.product, quantity=10, price=100)
        receipt.post()
        self.shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.shipment.lines.create(product=self.product, quantity=6, price=150, vat_rate="none")
        self.shipment.post()

    def test_sales_summary(self):
        s = services.sales_summary()
        self.assertEqual(s["revenue"], Decimal("900.00"))
        self.assertEqual(s["cost"], Decimal("600.00"))
        self.assertEqual(s["profit"], Decimal("300.00"))
        self.assertEqual(s["count"], 1)

    def test_top_products(self):
        rows = services.top_products()
        self.assertEqual(rows[0]["product__name"], "Товар")
        self.assertEqual(rows[0]["revenue"], Decimal("900.00"))
        self.assertEqual(rows[0]["profit"], Decimal("300.00"))

    def test_top_customers(self):
        rows = services.top_customers()
        self.assertEqual(rows[0]["shipment__customer__name"], "Клиент")
        self.assertEqual(rows[0]["revenue"], Decimal("900.00"))

    def test_draft_shipment_excluded(self):
        draft = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        draft.lines.create(product=self.product, quantity=1, price=999, vat_rate="none")
        # черновик не проведён → не учитывается
        self.assertEqual(services.sales_summary()["revenue"], Decimal("900.00"))

    def test_sales_by_month_length(self):
        rows = services.sales_by_month(6)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[-1]["value"], Decimal("900.00"))  # текущий месяц


class CashflowReportServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.cp = Counterparty.objects.create(name="Контрагент", partner_type=Counterparty.TYPE_BOTH)
        Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.account,
                               counterparty=self.cp, amount=1000, status="posted")
        Payment.objects.create(kind=Payment.KIND_OUT, organization=self.org, account=self.account,
                               counterparty=self.cp, amount=300, status="posted")
        Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.account,
                               counterparty=self.cp, amount=500, status="draft")  # черновик не учитывается

    def test_cashflow_summary(self):
        s = services.cashflow_summary()
        self.assertEqual(s["income"], Decimal("1000.00"))
        self.assertEqual(s["outcome"], Decimal("300.00"))
        self.assertEqual(s["net"], Decimal("700.00"))

    def test_cashflow_by_account(self):
        rows = services.cashflow_by_account()
        self.assertEqual(rows[0]["account__name"], "Р/с")
        self.assertEqual(rows[0]["income"], Decimal("1000.00"))


class OverdueTests(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Товар", unit=self.unit)

    def _invoice(self, due_offset_days, paid=0):
        inv = Invoice.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer,
            status=Invoice.STATUS_ISSUED, paid_amount=Decimal(paid),
            due_date=timezone.localdate() + timedelta(days=due_offset_days),
        )
        inv.lines.create(product=self.product, quantity=1, price=1000, vat_rate="none")
        return inv

    def test_overdue_detected(self):
        overdue = self._invoice(-5)          # срок прошёл, не оплачен
        self._invoice(-5, paid=1000)         # просрочен, но оплачен → не в списке
        self._invoice(5)                     # срок в будущем → не в списке
        result = list(services.overdue_invoices())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pk, overdue.pk)
        self.assertEqual(result[0].calc_total, Decimal("1000.00"))


class ReportViewsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="m", password="pass12345")

    def test_sales_report_page(self):
        resp = self.client.get(reverse("report_sales"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Продажи и прибыль")

    def test_cashflow_report_page(self):
        resp = self.client.get(reverse("report_cashflow"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Движение денежных средств")

    def test_dashboard_page(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Показатели")
