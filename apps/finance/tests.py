from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from .models import Account, AccountCorrection, Payment, SettlementCorrection

User = get_user_model()


class PaymentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", opening_balance=1000, is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)

    def _invoice(self, amount):
        from apps.catalog.models import Product, Unit

        unit = Unit.objects.create(name="шт")
        product = Product.objects.create(name="Товар", unit=unit)
        invoice = Invoice.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer, status=Invoice.STATUS_ISSUED,
        )
        invoice.lines.create(product=product, quantity=1, price=amount, vat_rate="none")
        return invoice

    def test_incoming_payment_increases_account_balance(self):
        payment = Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.account,
            counterparty=self.customer, amount=500,
        )
        self.assertEqual(self.account.balance, Decimal("1000.00"))  # черновик не влияет
        payment.post()
        self.assertEqual(self.account.balance, Decimal("1500.00"))
        payment.unpost()
        self.assertEqual(self.account.balance, Decimal("1000.00"))

    def test_outgoing_payment_decreases_balance(self):
        payment = Payment.objects.create(
            kind=Payment.KIND_OUT, organization=self.org, account=self.account,
            counterparty=self.customer, amount=300,
        )
        payment.post()
        self.assertEqual(self.account.balance, Decimal("700.00"))

    def test_payment_updates_invoice_paid_and_status(self):
        invoice = self._invoice(1000)
        self.assertEqual(invoice.payment_status, "not_paid")

        p1 = Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.account,
            counterparty=self.customer, invoice=invoice, amount=400,
        )
        p1.post()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal("400.00"))
        self.assertEqual(invoice.payment_status, "partial")

        p2 = Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.account,
            counterparty=self.customer, invoice=invoice, amount=600,
        )
        p2.post()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal("1000.00"))
        self.assertEqual(invoice.payment_status, "paid")

        p2.unpost()
        invoice.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal("400.00"))

    def test_numbering_separate_series(self):
        p_in = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.account, counterparty=self.customer, amount=1)
        p_out = Payment.objects.create(kind=Payment.KIND_OUT, organization=self.org, account=self.account, counterparty=self.customer, amount=1)
        self.assertEqual(p_in.number, "00001")
        self.assertEqual(p_out.number, "00001")  # отдельная серия по виду

    def test_account_default_uniqueness(self):
        a2 = Account.objects.create(organization=self.org, name="Касса", is_default=True)
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_default)
        self.assertTrue(a2.is_default)


class InvoiceToPaymentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_create_payment_from_invoice_prefills_remaining(self):
        from apps.catalog.models import Product, Unit

        unit = Unit.objects.create(name="шт")
        product = Product.objects.create(name="Товар", unit=unit)
        invoice = Invoice.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer,
            status=Invoice.STATUS_ISSUED, paid_amount=Decimal("300"),
        )
        invoice.lines.create(product=product, quantity=1, price=1000, vat_rate="none")

        resp = self.client.post(reverse("invoice_to_payment", args=[invoice.pk]))
        self.assertEqual(resp.status_code, 302)
        payment = Payment.objects.get(invoice=invoice)
        self.assertEqual(payment.kind, Payment.KIND_IN)
        self.assertEqual(payment.amount, Decimal("700.00"))  # 1000 - 300 уже оплачено
        self.assertEqual(payment.counterparty, self.customer)


class SettlementsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_settlements_balance(self):
        from apps.catalog.models import Product, Unit

        unit = Unit.objects.create(name="шт")
        product = Product.objects.create(name="Товар", unit=unit)
        invoice = Invoice.objects.create(
            organization=self.org, warehouse=self.wh, customer=self.customer, status=Invoice.STATUS_ISSUED,
        )
        invoice.lines.create(product=product, quantity=1, price=1000, vat_rate="none")
        Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.account,
            counterparty=self.customer, invoice=invoice, amount=400, status="posted",
        )
        resp = self.client.get(reverse("settlements"))
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.context["rows"] if r["cp"] == self.customer)
        self.assertEqual(row["sales"], Decimal("1000"))
        self.assertEqual(row["paid_in"], Decimal("400"))
        self.assertEqual(row["balance"], Decimal("600"))  # клиент должен нам 600


class AccountCorrectionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.account = Account.objects.create(
            organization=self.org, name="Касса", kind=Account.KIND_CASH,
            opening_balance=0, is_default=True,
        )

    def _correction(self, actual):
        c = AccountCorrection(organization=self.org, account=self.account, actual_balance=Decimal(actual))
        c.save()
        c.recalc()
        c.save(update_fields=["balance_before", "amount"])
        return c

    def test_correction_sets_balance_to_actual(self):
        c = self._correction("5000")
        self.assertEqual(c.amount, Decimal("5000.00"))
        self.assertEqual(self.account.balance, Decimal("0.00"))  # черновик не влияет
        c.post()
        self.assertEqual(self.account.balance, Decimal("5000.00"))
        c.unpost()
        self.assertEqual(self.account.balance, Decimal("0.00"))

    def test_second_correction_uses_current_balance(self):
        self._correction("5000").post()
        c2 = self._correction("4200")  # было 5000
        self.assertEqual(c2.balance_before, Decimal("5000.00"))
        self.assertEqual(c2.amount, Decimal("-800.00"))
        c2.post()
        self.assertEqual(self.account.balance, Decimal("4200.00"))


class SettlementCorrectionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.customer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_posted_correction_shifts_balance(self):
        sc = SettlementCorrection.objects.create(
            organization=self.org, counterparty=self.customer,
            direction=SettlementCorrection.DIR_THEY_OWE, amount=Decimal("1500"),
        )
        # Черновик — не влияет
        resp = self.client.get(reverse("settlements"))
        rows = [r for r in resp.context["rows"] if r["cp"] == self.customer]
        self.assertEqual(rows, [])
        sc.post()
        resp = self.client.get(reverse("settlements"))
        row = next(r for r in resp.context["rows"] if r["cp"] == self.customer)
        self.assertEqual(row["correction"], Decimal("1500"))
        self.assertEqual(row["balance"], Decimal("1500"))

    def test_we_owe_direction_is_negative(self):
        sc = SettlementCorrection.objects.create(
            organization=self.org, counterparty=self.customer,
            direction=SettlementCorrection.DIR_WE_OWE, amount=Decimal("700"),
        )
        self.assertEqual(sc.signed_amount, Decimal("-700"))

    def test_correction_list_view(self):
        AccountCorrection.objects.create(organization=self.org, account=self.account, actual_balance=100, amount=100)
        resp = self.client.get(reverse("correction_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["rows"]), 1)
