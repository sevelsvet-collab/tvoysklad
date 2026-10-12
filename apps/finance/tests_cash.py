"""Оплата со счёта: входящий платёж на расчётный счёт и приходный ордер (наличные).

Раньше кнопка платежа падала с ошибкой, если в базе ещё не было ни одной
кассы или счёта.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from .models import Account, Payment

User = get_user_model()


class PaymentFromInvoiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        self.invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.invoice.lines.create(product=self.product, quantity=1, price=1000, vat_rate="none")
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_payment_works_without_any_account(self):
        """Касс и счетов нет — раньше была ошибка 500, теперь счёт заводится сам."""
        self.assertFalse(Account.objects.exists())
        resp = self.client.post(reverse("invoice_to_payment", args=[self.invoice.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        payment = Payment.objects.get()
        self.assertEqual(payment.account.kind, Account.KIND_BANK)
        self.assertEqual(payment.account.name, "Расчётный счёт")
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertContains(resp, "проверьте реквизиты")

    def test_cash_order_uses_cash_register(self):
        resp = self.client.post(reverse("invoice_to_cash_order", args=[self.invoice.pk]), follow=True)
        payment = Payment.objects.get()
        self.assertEqual(payment.account.kind, Account.KIND_CASH)
        self.assertEqual(payment.account.name, "Основная касса")
        self.assertEqual(payment.kind, Payment.KIND_IN)
        self.assertContains(resp, "Создан приходный ордер")

    def test_existing_accounts_are_reused(self):
        bank = Account.objects.create(organization=self.org, name="Р/с в банке", kind=Account.KIND_BANK)
        cash = Account.objects.create(organization=self.org, name="Касса магазина", kind=Account.KIND_CASH)
        self.client.post(reverse("invoice_to_payment", args=[self.invoice.pk]))
        self.client.post(reverse("invoice_to_cash_order", args=[self.invoice.pk]))
        kinds = {p.account for p in Payment.objects.all()}
        self.assertEqual(kinds, {bank, cash})
        self.assertEqual(Account.objects.count(), 2)   # ничего лишнего не создалось

    def test_invoice_card_has_both_buttons(self):
        manager = User.objects.create_user("mgr", password="pass12345")
        manager.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))   # счёт открывает менеджер
        self.client.force_login(manager)
        resp = self.client.get(reverse("invoice_edit", args=[self.invoice.pk]))
        self.assertContains(resp, reverse("invoice_to_payment", args=[self.invoice.pk]))
        self.assertContains(resp, reverse("invoice_to_cash_order", args=[self.invoice.pk]))
        self.assertContains(resp, "Приходный ордер")


class CashOrderPrintTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="ООО Пример", full_name="ООО Пример",
                                               inn="7712345678", accountant_name="Петрова П.П.", is_default=True)
        self.cash = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH)
        self.bank = Account.objects.create(organization=self.org, name="Р/с", kind=Account.KIND_BANK)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.payment = Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.cash,
            counterparty=self.customer, amount=Decimal("1500.00"), purpose="Оплата по счёту № 00001",
        )
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def test_cash_order_html(self):
        resp = self.client.get(reverse("print_payment_cash_order", args=[self.payment.pk]), {"fmt": "html"})
        self.assertEqual(resp.status_code, 200)
        page = resp.content.decode()
        self.assertIn("ПРИХОДНЫЙ КАССОВЫЙ ОРДЕР", page)
        self.assertIn("КВИТАНЦИЯ", page)
        self.assertIn("Одна тысяча пятьсот", page)       # сумма прописью
        self.assertIn("Петрова П.П.", page)              # главный бухгалтер
        self.assertIn(self.customer.name, page)

    def test_cash_order_pdf(self):
        resp = self.client.get(reverse("print_payment_cash_order", args=[self.payment.pk]))
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_print_button_only_for_cash_payment(self):
        cash_card = self.client.get(reverse("payment_edit", args=[self.payment.pk]))
        self.assertContains(cash_card, "Приходный ордер (ПКО)")

        bank_payment = Payment.objects.create(
            kind=Payment.KIND_IN, organization=self.org, account=self.bank,
            counterparty=self.customer, amount=Decimal("100"),
        )
        bank_card = self.client.get(reverse("payment_edit", args=[bank_payment.pk]))
        self.assertNotContains(bank_card, "Приходный ордер (ПКО)")
