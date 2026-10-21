"""Согласованность платежей, оплаты счетов и принадлежности счетов организациям.

Подтверждённые ошибки:
1. Проведённый платёж на 10 ₽ по счёту меняли через форму на 20: баланс
   денежного счёта становился 20, а Invoice.paid_amount оставался 10.
2. Проведённый платёж переносили на другой счёт покупателю: у прежнего
   оставалось 10, у нового — 0.
3. Account.ensure_default(kind=bank, organization=Б) возвращал счёт организации А.
"""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from . import services
from .models import Account, Payment
from .services import PaymentError

User = get_user_model()
D = Decimal


def legacy_update(queryset, **values):
    """Запись «как в старой базе» — мимо проверок PaymentQuerySet.update;
    оплата затронутых счетов пересчитывается. Только для подготовки данных."""
    from django.db import models, transaction

    with transaction.atomic():
        pks = list(queryset.values_list("pk", flat=True))
        invoices = set(Payment.objects.filter(pk__in=pks).values_list("invoice_id", flat=True))
        models.QuerySet.update(Payment.objects.filter(pk__in=pks), **values)
        invoices |= set(Payment.objects.filter(pk__in=pks).values_list("invoice_id", flat=True))
        services.recompute_invoices(invoices)


class FinanceBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Альфа", is_default=True)
        self.org_b = Organization.objects.create(name="Бета")
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.bank = Account.objects.create(organization=self.org, name="Р/с Альфа", kind=Account.KIND_BANK,
                                           is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.customer2 = Counterparty.objects.create(name="Второй покупатель",
                                                     partner_type=Counterparty.TYPE_CUSTOMER)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        self.invoice = self.make_invoice()
        self.invoice2 = self.make_invoice()
        self.user = User.objects.create_user("acc", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")

    def make_invoice(self, organization=None, customer=None, total=100):
        invoice = Invoice.objects.create(organization=organization or self.org, warehouse=self.wh,
                                         customer=customer or self.customer, status=Invoice.STATUS_ISSUED)
        invoice.lines.create(product=self.product, quantity=1, price=total, vat_rate="none")
        return invoice

    def payment(self, amount, invoice="default", post=True, kind=Payment.KIND_IN, **extra):
        values = {
            "kind": kind, "organization": self.org, "account": self.bank, "counterparty": self.customer,
            "invoice": self.invoice if invoice == "default" else invoice, "amount": amount,
        }
        values.update(extra)
        payment = Payment.objects.create(**values)
        if post:
            payment.post()
        return payment

    def paid(self, invoice=None):
        return Invoice.objects.get(pk=(invoice or self.invoice).pk).paid_amount

    def form_data(self, payment, **changes):
        data = {
            "date": payment.date.isoformat(), "number": payment.number, "time": payment.time.strftime("%H:%M"),
            "organization": payment.organization_id, "account": payment.account_id,
            "counterparty": payment.counterparty_id, "invoice": payment.invoice_id or "",
            "amount": str(payment.amount), "purpose": payment.purpose, "action": "save",
        }
        if payment.kind == Payment.KIND_OUT:
            data.pop("invoice")
        data.update(changes)
        return data

    def save_form(self, payment, **changes):
        return self.client.post(reverse("payment_edit", args=[payment.pk]), self.form_data(payment, **changes))

    def state(self):
        """Всё, что должно остаться прежним после отказа."""
        return (
            list(Payment.objects.order_by("pk").values_list(
                "pk", "status", "amount", "invoice_id", "account_id", "organization_id", "counterparty_id")),
            list(Invoice.objects.order_by("pk").values_list("pk", "paid_amount", "organization_id", "customer_id")),
            list(Account.objects.order_by("pk").values_list("pk", "organization_id", "kind", "is_default")),
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("check_payments", *args, stdout=out)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Три воспроизведённых сценария
# ---------------------------------------------------------------------------

class ReportedScenariosTests(FinanceBase):
    def test_1_changing_amount_of_posted_payment_updates_invoice(self):
        payment = self.payment(10)
        self.assertEqual((self.bank.balance, self.paid()), (D("10"), D("10")))

        response = self.save_form(payment, amount="20")

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertTrue(payment.is_posted)                 # остался проведённым
        self.assertEqual(self.bank.balance, D("20.00"))
        self.assertEqual(self.paid(), D("20.00"))          # было 10

    def test_2_moving_posted_payment_to_another_invoice_updates_both(self):
        payment = self.payment(20)
        response = self.save_form(payment, invoice=self.invoice2.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.paid(self.invoice), D("0.00"))     # было 10 (оставалось прежнее)
        self.assertEqual(self.paid(self.invoice2), D("20.00"))   # было 0

    def test_3_ensure_default_does_not_return_account_of_another_organization(self):
        account, created = Account.ensure_default(Account.KIND_BANK, self.org_b)
        self.assertTrue(created)
        self.assertNotEqual(account.pk, self.bank.pk)      # раньше возвращался счёт «Альфы»
        self.assertEqual(account.organization, self.org_b)
        self.assertEqual(account.kind, Account.KIND_BANK)
        self.assertEqual(Account.ensure_default(Account.KIND_BANK, self.org_b), (account, False))
        self.assertEqual(Account.ensure_default(Account.KIND_BANK, self.org), (self.bank, False))


# ---------------------------------------------------------------------------
# Оплата счёта = сумма проведённых входящих платежей
# ---------------------------------------------------------------------------

class PaidAmountTests(FinanceBase):
    def test_removing_link_to_invoice(self):
        payment = self.payment(20)
        self.save_form(payment, invoice="")
        payment.refresh_from_db()
        self.assertIsNone(payment.invoice_id)
        self.assertTrue(payment.is_posted)
        self.assertEqual(self.paid(), D("0.00"))
        self.assertEqual(self.bank.balance, D("20.00"))    # деньги на счёте остались

    def test_adding_link_to_invoice(self):
        payment = self.payment(20, invoice=None)
        self.assertEqual(self.paid(), D("0"))
        # ПРАВИЛО ДОПОЛНЕНО (этап 27, взаиморасчёты): платёж по счёту покупателю —
        # только «оплата от покупателя», а записанное назначение («не распределено»)
        # молча не подменяется. Поэтому при привязке счёта к уже записанному
        # платежу назначение указывается в той же операции. Ожидаемые значения
        # теста прежние; без назначения — ошибка (tests_settlements).
        self.save_form(payment, invoice=self.invoice.pk, settlement_purpose=Payment.PURPOSE_CUSTOMER_PAYMENT)
        self.assertEqual(self.paid(), D("20.00"))

    def test_several_payments_partial_payment_and_overpayment(self):
        self.payment(30)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).payment_status, "partial")
        self.payment(70)
        self.assertEqual(self.paid(), D("100.00"))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).payment_status, "paid")
        extra = self.payment(50)
        self.assertEqual(self.paid(), D("150.00"))         # переплата не обрезается до суммы счёта
        extra.unpost()
        self.assertEqual(self.paid(), D("100.00"))

    def test_draft_post_repost_unpost(self):
        payment = self.payment(40, post=False)
        self.assertEqual(self.paid(), D("0"))              # черновик в оплату не входит
        payment.post()
        self.assertEqual(self.paid(), D("40.00"))
        payment.post()
        Payment.objects.get(pk=payment.pk).post()
        self.client.post(reverse("payment_post", args=[payment.pk]))
        self.assertEqual(self.paid(), D("40.00"))          # повторное проведение не удваивает
        payment.unpost()
        self.assertEqual(self.paid(), D("0.00"))
        payment.unpost()
        self.assertEqual(self.paid(), D("0.00"))

    def test_post_and_unpost_buttons(self):
        payment = self.payment(40, post=False)
        self.client.post(reverse("payment_post", args=[payment.pk]))
        self.assertEqual(self.paid(), D("40.00"))
        self.client.post(reverse("payment_unpost", args=[payment.pk]))
        self.assertEqual(self.paid(), D("0.00"))

    def test_save_and_post_from_form(self):
        payment = self.payment(40, post=False)
        self.save_form(payment, amount="55", action="save_post")
        self.assertEqual(self.paid(), D("55.00"))

    def test_editing_draft_does_not_change_paid_amount(self):
        self.payment(30)
        draft = self.payment(40, post=False)
        self.save_form(draft, amount="99")
        self.assertEqual(self.paid(), D("30.00"))

    def test_creating_posted_payment_through_form(self):
        response = self.client.post(reverse("payment_create_in"), {
            "date": "2026-01-10", "number": "", "time": "12:00", "organization": self.org.pk,
            "account": self.bank.pk, "counterparty": self.customer.pk, "invoice": self.invoice.pk,
            "amount": "25", "purpose": "", "action": "save_post",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.paid(), D("25.00"))

    def test_outgoing_payment_never_increases_paid_amount(self):
        self.payment(30)
        outgoing = self.payment(500, invoice=None, kind=Payment.KIND_OUT, counterparty=self.supplier)
        self.assertEqual(self.paid(), D("30.00"))
        with self.assertRaises(PaymentError):              # и связать его со счётом покупателю нельзя
            outgoing.invoice = self.invoice
            outgoing.save()
        self.assertEqual(self.paid(), D("30.00"))

    def test_incoming_payment_without_invoice_still_works(self):
        payment = self.payment(70, invoice=None)
        self.assertTrue(payment.is_posted)
        self.assertEqual(self.bank.balance, D("70"))
        self.assertEqual(self.paid(), D("0"))

    def test_direct_model_changes(self):
        payment = self.payment(10)
        payment.amount = D("35")
        payment.save()
        self.assertEqual(self.paid(), D("35.00"))
        payment.invoice = self.invoice2
        payment.save()
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("0.00"), D("35.00")))
        payment.invoice = None
        payment.save(update_fields=["invoice"])
        self.assertEqual(self.paid(self.invoice2), D("0.00"))

    def test_stale_instance_still_recomputes_previous_invoice(self):
        """Прежний счёт берётся из базы, а не из объекта в памяти."""
        payment = self.payment(10)
        stale = Payment.objects.get(pk=payment.pk)
        payment.invoice = self.invoice2
        payment.save()
        stale.invoice = None                               # объект думает, что счёт был первым
        stale.save()
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("0.00"), D("0.00")))

    def test_saving_invoice_does_not_overwrite_paid_amount(self):
        """Счёт открыли, тем временем провели платёж, счёт сохранили — оплата не затёрта."""
        opened = Invoice.objects.get(pk=self.invoice.pk)
        self.payment(60)
        opened.comment = "правка"
        opened.save()
        self.assertEqual(self.paid(), D("60.00"))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).comment, "правка")


# ---------------------------------------------------------------------------
# Удаление: одиночное, массовое, административная панель
# ---------------------------------------------------------------------------

class DeleteTests(FinanceBase):
    def test_single_delete_button(self):
        payment = self.payment(20)
        self.payment(5)
        self.client.post(reverse("payment_delete", args=[payment.pk]))
        self.assertEqual(self.paid(), D("5.00"))

    def test_model_delete(self):
        self.payment(20).delete()
        self.assertEqual(self.paid(), D("0.00"))

    def test_bulk_delete_from_list(self):
        first, second, third = self.payment(20), self.payment(30), self.payment(7, invoice=self.invoice2)
        self.client.post(reverse("payment_bulk_delete"), {"ids": [first.pk, third.pk]})
        self.assertEqual(set(Payment.objects.values_list("pk", flat=True)), {second.pk})
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("30.00"), D("0.00")))

    def test_queryset_delete_bypassing_model_delete(self):
        self.payment(20)
        self.payment(30)
        keep = self.payment(7, invoice=self.invoice2)
        Payment.objects.filter(invoice=self.invoice).delete()
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("0.00"), D("7.00")))
        self.invoice2.payments.all().delete()              # через связанный менеджер
        self.assertFalse(Payment.objects.filter(pk=keep.pk).exists())
        self.assertEqual(self.paid(self.invoice2), D("0.00"))

    def test_queryset_update_bypassing_model_save(self):
        payment = self.payment(20)
        Payment.objects.filter(pk=payment.pk).update(amount=D("45"))
        self.assertEqual(self.paid(), D("45.00"))
        Payment.objects.filter(pk=payment.pk).update(invoice=self.invoice2)
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("0.00"), D("45.00")))
        Payment.objects.filter(pk=payment.pk).update(status="draft")
        self.assertEqual(self.paid(self.invoice2), D("0.00"))


class AdminTests(FinanceBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("root", password="pass12345")
        self.client.login(username="root", password="pass12345")

    def admin_data(self, payment, **changes):
        data = {
            "kind": payment.kind, "number": payment.number, "date": payment.date.isoformat(),
            "time": payment.time.strftime("%H:%M:%S"), "organization": payment.organization_id,
            "account": payment.account_id, "counterparty": payment.counterparty_id,
            "invoice": payment.invoice_id or "", "amount": str(payment.amount), "purpose": payment.purpose,
            "status": payment.status, "_save": "Сохранить",
        }
        data.update(changes)
        return data

    def test_admin_change_updates_invoice(self):
        payment = self.payment(10)
        url = reverse("admin:finance_payment_change", args=[payment.pk])
        response = self.client.post(url, self.admin_data(payment, amount="20", invoice=self.invoice2.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.paid(self.invoice), self.paid(self.invoice2)), (D("0.00"), D("20.00")))

    def test_admin_status_change_updates_invoice(self):
        payment = self.payment(10)
        url = reverse("admin:finance_payment_change", args=[payment.pk])
        self.client.post(url, self.admin_data(payment, status="draft"))
        self.assertEqual(self.paid(), D("0.00"))

    def test_admin_single_delete(self):
        payment = self.payment(10)
        self.client.post(reverse("admin:finance_payment_delete", args=[payment.pk]), {"post": "yes"})
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.paid(), D("0.00"))

    def test_admin_delete_selected(self):
        """«Удалить выбранные» идёт через QuerySet.delete(), мимо Payment.delete()."""
        first, second = self.payment(10), self.payment(15)
        self.client.post(reverse("admin:finance_payment_changelist"), {
            "action": "delete_selected", "_selected_action": [first.pk, second.pk], "post": "yes",
        })
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.paid(), D("0.00"))

    def test_admin_rejects_account_of_another_organization(self):
        foreign = Account.objects.create(organization=self.org_b, name="Р/с Бета", kind=Account.KIND_BANK)
        payment = self.payment(10)
        before = self.state()
        url = reverse("admin:finance_payment_change", args=[payment.pk])
        response = self.client.post(url, self.admin_data(payment, account=foreign.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "принадлежит организации")
        self.assertEqual(self.state(), before)


# ---------------------------------------------------------------------------
# Атомарность
# ---------------------------------------------------------------------------

class AtomicityTests(FinanceBase):
    def test_form_change_is_rolled_back_when_recalculation_fails(self):
        payment = self.payment(10)
        before = self.state()
        with mock.patch.object(services, "recompute_invoices", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.save_form(payment, amount="20", invoice=self.invoice2.pk)
        self.assertEqual(self.state(), before)             # ни новой суммы, ни старой оплаты порознь
        self.assertEqual(self.bank.balance, D("10"))

    def test_posting_is_rolled_back_when_recalculation_fails(self):
        payment = self.payment(10, post=False)
        with mock.patch.object(services, "recompute_invoices", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                payment.post()
        self.assertEqual(Payment.objects.get(pk=payment.pk).status, "draft")
        self.assertEqual(self.paid(), D("0"))

    def test_delete_is_rolled_back_when_recalculation_fails(self):
        payment = self.payment(10)
        with mock.patch.object(services, "recompute_invoices", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                Payment.objects.get(pk=payment.pk).delete()
            with self.assertRaises(RuntimeError):
                Payment.objects.filter(pk=payment.pk).delete()
        self.assertTrue(Payment.objects.filter(pk=payment.pk, status="posted").exists())
        self.assertEqual(self.paid(), D("10.00"))

    def test_save_and_post_with_failed_post_keeps_nothing(self):
        payment = self.payment(10, post=False)
        before = self.state()
        with mock.patch.object(Payment, "post", side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.save_form(payment, amount="77", action="save_post")
        self.assertEqual(self.state(), before)


# ---------------------------------------------------------------------------
# Счета и кассы организаций
# ---------------------------------------------------------------------------

class AccountOwnershipTests(FinanceBase):
    def setUp(self):
        super().setUp()
        self.cash = Account.objects.create(organization=self.org, name="Касса Альфа", kind=Account.KIND_CASH)
        self.bank_b = Account.objects.create(organization=self.org_b, name="Р/с Бета", kind=Account.KIND_BANK)
        self.cash_b = Account.objects.create(organization=self.org_b, name="Касса Бета", kind=Account.KIND_CASH)

    def test_get_default_respects_organization_and_kind(self):
        self.assertEqual(Account.get_default(Account.KIND_BANK, self.org), self.bank)
        self.assertEqual(Account.get_default(Account.KIND_CASH, self.org), self.cash)
        self.assertEqual(Account.get_default(Account.KIND_BANK, self.org_b), self.bank_b)
        self.assertEqual(Account.get_default(Account.KIND_CASH, self.org_b), self.cash_b)

    def test_ensure_default_with_two_organizations(self):
        for kind, organization, expected in (
            (Account.KIND_BANK, self.org, self.bank), (Account.KIND_CASH, self.org, self.cash),
            (Account.KIND_BANK, self.org_b, self.bank_b), (Account.KIND_CASH, self.org_b, self.cash_b),
        ):
            self.assertEqual(Account.ensure_default(kind, organization), (expected, False))
        self.assertEqual(Account.objects.count(), 4)

    def test_missing_account_is_created_for_the_right_organization(self):
        self.cash_b.delete()
        account, created = Account.ensure_default(Account.KIND_CASH, self.org_b)
        self.assertTrue(created)
        self.assertEqual((account.organization, account.kind, account.name),
                         (self.org_b, Account.KIND_CASH, "Основная касса"))
        self.bank.refresh_from_db()
        self.assertTrue(self.bank.is_default)              # основной счёт «Альфы» не сброшен

    def test_inactive_account_is_not_offered(self):
        Account.objects.filter(pk=self.bank_b.pk).update(is_active=False)
        account, created = Account.ensure_default(Account.KIND_BANK, self.org_b)
        self.assertTrue(created)
        self.assertNotEqual(account.pk, self.bank_b.pk)
        self.assertEqual(account.organization, self.org_b)

    def test_default_flag_is_per_organization_and_kind(self):
        Account.objects.filter(pk=self.bank_b.pk).update(is_default=True)
        second = Account.objects.create(organization=self.org, name="Второй р/с", kind=Account.KIND_BANK,
                                        is_default=True)
        self.bank.refresh_from_db()
        self.bank_b.refresh_from_db()
        self.assertFalse(self.bank.is_default)             # тот же вид той же организации
        self.assertTrue(self.bank_b.is_default)            # чужая организация не затронута
        self.assertEqual(Account.get_default(Account.KIND_BANK, self.org), second)

    def test_payment_from_invoice_of_second_organization(self):
        invoice_b = self.make_invoice(organization=self.org_b)
        self.client.post(reverse("invoice_to_payment", args=[invoice_b.pk]))
        self.client.post(reverse("invoice_to_cash_order", args=[invoice_b.pk]))
        accounts = set(Payment.objects.filter(invoice=invoice_b).values_list("account_id", flat=True))
        self.assertEqual(accounts, {self.bank_b.pk, self.cash_b.pk})
        self.assertEqual(set(Payment.objects.values_list("organization_id", flat=True)), {self.org_b.pk})

    def test_payment_from_invoice_creates_account_for_organization_without_one(self):
        Account.objects.filter(organization=self.org_b).delete()
        invoice_b = self.make_invoice(organization=self.org_b)
        response = self.client.post(reverse("invoice_to_payment", args=[invoice_b.pk]), follow=True)
        payment = Payment.objects.get(invoice=invoice_b)
        self.assertEqual(payment.account.organization, self.org_b)
        self.assertNotEqual(payment.account_id, self.bank.pk)
        self.assertContains(response, "проверьте реквизиты")

    def test_form_rejects_account_of_another_organization(self):
        payment = self.payment(10)
        before = self.state()
        response = self.save_form(payment, account=self.bank_b.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "принадлежит организации «Бета», а платёж оформлен на «Альфа»")
        self.assertEqual(self.state(), before)

    def test_new_payment_form_rejects_foreign_account(self):
        response = self.client.post(reverse("payment_create_in"), {
            "date": "2026-01-10", "number": "", "time": "12:00", "organization": self.org.pk,
            "account": self.cash_b.pk, "counterparty": self.customer.pk, "invoice": "",
            "amount": "25", "purpose": "", "action": "save_post",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "принадлежит организации")
        self.assertFalse(Payment.objects.exists())

    def test_model_rejects_foreign_account_on_direct_save(self):
        with self.assertRaises(PaymentError):
            Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.bank_b,
                                   counterparty=self.customer, amount=5)
        payment = self.payment(10)
        payment.account = self.cash_b
        with self.assertRaises(PaymentError):
            payment.save()
        with self.assertRaises(ValidationError):
            payment.full_clean()
        self.assertEqual(Payment.objects.get(pk=payment.pk).account_id, self.bank.pk)

    def test_account_with_payments_cannot_move_to_another_organization(self):
        self.payment(10, post=False)
        account = Account.objects.get(pk=self.bank.pk)
        account.organization = self.org_b
        with self.assertRaises(ValidationError):
            account.full_clean()
        with self.assertRaises(ValidationError):
            account.save()
        self.assertEqual(Account.objects.get(pk=self.bank.pk).organization, self.org)
        response = self.client.post(reverse("account_edit", args=[self.bank.pk]), {
            "organization": self.org_b.pk, "name": self.bank.name, "kind": self.bank.kind, "bank_account": "",
            "opening_balance": "0", "is_default": "on", "is_active": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Account.objects.get(pk=self.bank.pk).organization, self.org)

        empty = Account.objects.get(pk=self.cash.pk)          # без платежей — можно
        empty.organization = self.org_b
        empty.full_clean()
        empty.save()
        self.assertEqual(Account.objects.get(pk=self.cash.pk).organization, self.org_b)

    def test_new_payment_form_offers_own_default_account(self):
        Account.objects.filter(organization=self.org).update(is_default=False)
        Account.objects.filter(pk=self.bank_b.pk).update(is_default=True)   # единственный «основной» — чужой
        response = self.client.get(reverse("payment_create_in"))
        self.assertEqual(response.context["form"].fields["account"].initial.organization, self.org)


# ---------------------------------------------------------------------------
# Соответствие платежа счёту покупателю
# ---------------------------------------------------------------------------

class InvoiceCompatibilityTests(FinanceBase):
    def test_other_customer_is_rejected(self):
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10)
        before = self.state()
        response = self.save_form(payment, invoice=other.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "выставлен покупателю «Второй покупатель», а в платеже указан «Покупатель»")
        self.assertEqual(self.state(), before)
        with self.assertRaises(PaymentError):
            Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.bank,
                                   counterparty=self.customer, invoice=other, amount=5)

    def test_changing_counterparty_of_linked_payment_is_rejected(self):
        payment = self.payment(10)
        before = self.state()
        response = self.save_form(payment, counterparty=self.customer2.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state(), before)

    def test_other_organization_is_rejected(self):
        bank_b = Account.objects.create(organization=self.org_b, name="Р/с Бета", kind=Account.KIND_BANK)
        payment = self.payment(10)
        before = self.state()
        response = self.save_form(payment, organization=self.org_b.pk, account=bank_b.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "выставлен организацией «Альфа», а платёж оформлен на «Бета»")
        self.assertEqual(self.state(), before)

    def test_posting_incompatible_payment_is_rejected_but_unpost_and_delete_work(self):
        """Старые несовместимые связи: провести нельзя, снять и удалить — можно."""
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10)
        legacy_update(Payment.objects.filter(pk=payment.pk), invoice=other)     # как будто так было в базе
        payment = Payment.objects.get(pk=payment.pk)
        with self.assertRaises(PaymentError):
            payment.post()
        response = self.client.post(reverse("payment_post", args=[payment.pk]), follow=True)
        self.assertContains(response, "Платёж не проведён")
        Payment.objects.get(pk=payment.pk).unpost()
        self.assertEqual(self.paid(other), D("0.00"))
        Payment.objects.get(pk=payment.pk).delete()
        self.assertFalse(Payment.objects.exists())

    def test_changing_invoice_parties_with_linked_payments_is_blocked(self):
        self.payment(10, post=False)                       # даже черновик — связь уже есть
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.customer = self.customer2
        with self.assertRaises(ValidationError) as caught:
            invoice.full_clean()
        self.assertIn("Со счётом связаны платежи", str(caught.exception))
        with self.assertRaises(ValidationError):
            invoice.save()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.organization = self.org_b
        with self.assertRaises(ValidationError):
            invoice.save()
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).customer, self.customer)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).organization, self.org)

    def test_changing_invoice_customer_through_form_is_blocked(self):
        self.payment(10)
        manager = User.objects.create_user("mgr", password="pass12345")
        manager.groups.add(Group.objects.get(name=roles.ROLE_MANAGER))
        self.client.login(username="mgr", password="pass12345")
        line = self.invoice.lines.get()
        before = self.state()
        response = self.client.post(reverse("invoice_edit", args=[self.invoice.pk]), {
            "date": self.invoice.date.isoformat(), "number": self.invoice.number, "time": "12:00",
            "organization": self.org.pk, "warehouse": self.wh.pk, "customer": self.customer2.pk,
            "comment": "", "action": "save",
            "lines-TOTAL_FORMS": 1, "lines-INITIAL_FORMS": 1, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
            "lines-0-id": line.pk, "lines-0-product": self.product.pk, "lines-0-quantity": "1",
            "lines-0-price": "100", "lines-0-discount": "0", "lines-0-vat_rate": "none",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Со счётом связаны платежи")
        self.assertEqual(self.state(), before)

    def test_invoice_parties_can_change_when_no_payments_are_linked(self):
        payment = self.payment(10)
        payment.invoice = None
        payment.save()
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.customer = self.customer2
        invoice.full_clean()
        invoice.save()
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).customer, self.customer2)

    def test_other_invoice_fields_can_change_with_linked_payments(self):
        self.payment(10)
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        invoice.comment = "уточнение"
        invoice.full_clean()
        invoice.save()
        self.assertEqual(self.paid(), D("10.00"))


# ---------------------------------------------------------------------------
# Права доступа
# ---------------------------------------------------------------------------

class PermissionTests(FinanceBase):
    def as_role(self, role):
        user = User.objects.create_user(f"user-{role}", password="pass12345")
        user.groups.add(Group.objects.get(name=role))
        self.client.login(username=user.username, password="pass12345")

    def attempt_all(self, payment):
        return [
            self.save_form(payment, amount="999"),
            self.client.post(reverse("payment_post", args=[payment.pk])),
            self.client.post(reverse("payment_unpost", args=[payment.pk])),
            self.client.post(reverse("payment_delete", args=[payment.pk])),
            self.client.post(reverse("payment_bulk_delete"), {"ids": [payment.pk]}),
            self.client.post(reverse("invoice_to_payment", args=[self.invoice2.pk])),
        ]

    def test_storekeeper_cannot_change_payments(self):
        payment = self.payment(10)
        before = self.state()
        self.as_role(roles.ROLE_STOREKEEPER)
        self.assertEqual({r.status_code for r in self.attempt_all(payment)}, {403})
        self.assertEqual(self.state(), before)

    def test_guest_is_sent_to_login(self):
        payment = self.payment(10)
        before = self.state()
        self.client.logout()
        for response in self.attempt_all(payment):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.url)
        self.assertEqual(self.state(), before)

    def test_manager_can_change_payment(self):
        payment = self.payment(10)
        self.as_role(roles.ROLE_MANAGER)
        self.assertEqual(self.save_form(payment, amount="20").status_code, 302)
        self.assertEqual(self.paid(), D("20.00"))


# ---------------------------------------------------------------------------
# Диагностика существующих данных
# ---------------------------------------------------------------------------

class DiagnosticsTests(FinanceBase):
    def make_legacy(self):
        """Состояние «как до исправления»: оплата устарела, связи несовместимы."""
        self.payment(20)
        Invoice.objects.filter(pk=self.invoice.pk).update(paid_amount=D("10"))      # устаревшая оплата
        foreign = Account.objects.create(organization=self.org_b, name="Р/с Бета", kind=Account.KIND_BANK)
        wrong_account = self.payment(5, invoice=None)
        legacy_update(Payment.objects.filter(pk=wrong_account.pk), account=foreign)
        other = self.make_invoice(customer=self.customer2)
        wrong_customer = self.payment(7, invoice=None)
        legacy_update(Payment.objects.filter(pk=wrong_customer.pk), invoice=other)           # заодно пересчитает other
        Invoice.objects.filter(pk=other.pk).update(paid_amount=D("0"))
        return other

    def test_clean_database(self):
        self.payment(20)
        self.assertIn("Расхождений нет", self.run_command())

    def test_dry_run_reports_everything_and_writes_nothing(self):
        other = self.make_legacy()
        before = self.state()
        output = self.run_command()
        self.assertIn("запись выключена", output)
        self.assertIn("«Оплачено» в счёте не равно сумме проведённых платежей: 2", output)
        self.assertIn(f"(id {self.invoice.pk}), Покупатель: в счёте 10.00, по платежам 20.00, разница -10.00", output)
        self.assertIn(f"(id {other.pk}), Второй покупатель: в счёте 0.00, по платежам 7.00", output)
        self.assertIn("Платежи с несовместимыми связями (не исправляются автоматически): 2", output)
        self.assertIn("«Р/с Бета» принадлежит организации «Бета», а платёж оформлен на «Альфа»", output)
        self.assertIn("выставлен покупателю «Второй покупатель», а в платеже указан «Покупатель»", output)
        self.assertIn("Ничего не изменено", output)
        self.assertEqual(self.state(), before)

    def test_fix_requires_backup_confirmation(self):
        self.make_legacy()
        before = self.state()
        with self.assertRaises(CommandError):
            self.run_command("--fix-paid-amount")
        self.assertEqual(self.state(), before)

    def test_fix_recomputes_paid_amount_only(self):
        other = self.make_legacy()
        payments_before = self.state()[0]
        output = self.run_command("--fix-paid-amount", "--i-have-a-backup")
        self.assertIn("Пересчитано счетов: 2", output)
        self.assertEqual((self.paid(), self.paid(other)), (D("20.00"), D("7.00")))
        self.assertEqual(self.state()[0], payments_before)           # платежи и их связи не тронуты
        again = self.run_command()
        self.assertNotIn("«Оплачено» в счёте не равно", again)
        self.assertIn("Платежи с несовместимыми связями (не исправляются автоматически): 2", again)

    def test_outgoing_payment_linked_to_invoice_is_reported(self):
        outgoing = self.payment(9, invoice=None, kind=Payment.KIND_OUT, counterparty=self.supplier)
        legacy_update(Payment.objects.filter(pk=outgoing.pk), invoice=self.invoice)
        self.assertEqual(self.paid(), D("0.00"))                     # исходящий оплату не увеличивает
        self.assertIn("Со счётом покупателю связывается только входящий платёж", self.run_command())
