"""Совместимость связей платежа по актуальному состоянию базы.

Подтверждённые обходы защиты через устаревшие объекты в памяти:
А. Платёж подготовлен с объектом счёта покупателю; тем временем другой
   экземпляр счёта сменил покупателя (платежей ещё не было — разрешено).
   Платёж сохранялся и проводился: в базе платёж покупателя А по счёту для Б.
Б. То же с кассой: другой экземпляр передал её организации Б, после чего
   сохранялся платёж организации А по кассе организации Б.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.sales.models import Invoice

from . import services
from .models import Account, Payment
from .services import PaymentError
from .tests_consistency import FinanceBase, legacy_update

D = Decimal


class StaleInvoiceTests(FinanceBase):
    def prepared(self, invoice):
        """Платёж собран в памяти с объектом счёта, ещё не сохранён."""
        return Payment(kind=Payment.KIND_IN, organization=self.org, account=self.bank,
                       counterparty=self.customer, invoice=invoice, amount=D("10"))

    def change_customer_elsewhere(self, invoice):
        other = Invoice.objects.get(pk=invoice.pk)
        other.customer = self.customer2
        other.save()                                   # платежей нет — смена разрешена

    def test_scenario_a_stale_invoice_object(self):
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        payment = self.prepared(invoice)
        self.change_customer_elsewhere(invoice)
        self.assertEqual(invoice.customer, self.customer)       # объект в памяти устарел
        before = self.state()
        with self.assertRaisesMessage(PaymentError, "выставлен покупателю «Второй покупатель», а в платеже указан «Покупатель»"):
            payment.save()
        self.assertEqual(self.state(), before)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.paid(), D("0"))
        self.assertEqual(services.incompatible_payments(), [])

    def test_scenario_a_with_select_related(self):
        invoice = Invoice.objects.select_related("customer", "organization").get(pk=self.invoice.pk)
        payment = Payment(kind=Payment.KIND_IN, organization=invoice.organization, account=self.bank,
                          counterparty=invoice.customer, invoice=invoice, amount=D("10"))
        self.change_customer_elsewhere(invoice)
        with self.assertRaises(PaymentError):
            payment.save()
        self.assertFalse(Payment.objects.exists())

    def test_existing_payment_loaded_with_select_related_and_relinked(self):
        """Платёж без счёта загружен вместе со связями; ему подставляют устаревший
        объект счёта, покупатель которого уже сменился."""
        self.payment(10, invoice=None)
        payment = Payment.objects.select_related("invoice", "account", "counterparty").get()
        stale = Invoice.objects.select_related("customer").get(pk=self.invoice2.pk)
        self.change_customer_elsewhere(stale)
        payment.invoice = stale
        before = self.state()
        with self.assertRaises(PaymentError):
            payment.save()
        with self.assertRaises(PaymentError):
            payment.save(update_fields=["invoice"])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.paid(self.invoice2), D("0"))

    def test_clean_reads_fresh_invoice(self):
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        payment = self.prepared(invoice)
        self.change_customer_elsewhere(invoice)
        with self.assertRaises(ValidationError):
            payment.full_clean()

    def test_form_shows_error_when_customer_changed_meanwhile(self):
        """Форма открыта со старым счётом; покупатель счёта успел смениться."""
        self.change_customer_elsewhere(self.invoice)
        response = self.client.post(reverse("payment_create_in"), {
            "date": "2026-01-10", "number": "", "time": "12:00", "organization": self.org.pk,
            "account": self.bank.pk, "counterparty": self.customer.pk, "invoice": self.invoice.pk,
            "amount": "25", "purpose": "", "action": "save_post",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "выставлен покупателю «Второй покупатель»")
        self.assertFalse(Payment.objects.exists())

    def test_customer_change_after_payment_is_rejected(self):
        """Обратный порядок: платёж сохранён первым — смена сторон отклоняется."""
        invoice = Invoice.objects.get(pk=self.invoice.pk)
        self.prepared(invoice).save()
        other = Invoice.objects.get(pk=self.invoice.pk)
        other.customer = self.customer2
        with self.assertRaises(ValidationError):
            other.save()
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).customer, self.customer)

    def test_status_only_update_of_invoice_is_not_blocked(self):
        """Счёт с платежами проводится/снимается (update_fields=["status"]) —
        стороны не записываются, проверка их не касается."""
        self.payment(10)
        stale = Invoice.objects.get(pk=self.invoice.pk)
        stale.customer = self.customer2                        # в памяти другое, но не записывается
        stale.unpost()
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).customer, self.customer)


class StaleAccountTests(FinanceBase):
    def setUp(self):
        super().setUp()
        self.cash = Account.objects.create(organization=self.org, name="Касса Альфа", kind=Account.KIND_CASH)

    def move_elsewhere(self, account):
        other = Account.objects.get(pk=account.pk)
        other.organization = self.org_b
        other.save()                                   # платежей нет — передача разрешена

    def test_scenario_b_stale_account_object(self):
        account = Account.objects.get(pk=self.cash.pk)
        payment = Payment(kind=Payment.KIND_IN, organization=self.org, account=account,
                          counterparty=self.customer, amount=D("10"))
        self.move_elsewhere(account)
        self.assertEqual(account.organization_id, self.org.pk)  # объект в памяти устарел
        before = self.state()
        with self.assertRaisesMessage(PaymentError, "«Касса Альфа» принадлежит организации «Бета», а платёж оформлен на «Альфа»"):
            payment.save()
        self.assertEqual(self.state(), before)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(services.incompatible_payments(), [])

    def test_scenario_b_with_select_related(self):
        account = Account.objects.select_related("organization").get(pk=self.cash.pk)
        payment = Payment(kind=Payment.KIND_IN, organization=account.organization, account=account,
                          counterparty=self.customer, invoice=self.invoice, amount=D("10"))
        self.move_elsewhere(account)
        with self.assertRaises(PaymentError):
            payment.save()
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.paid(), D("0"))

    def test_existing_payment_switched_to_stale_account(self):
        payment = self.payment(10)
        stale = Account.objects.select_related("organization").get(pk=self.cash.pk)
        self.move_elsewhere(stale)
        loaded = Payment.objects.select_related("account").get(pk=payment.pk)
        loaded.account = stale
        before = self.state()
        with self.assertRaises(PaymentError):
            loaded.save()
        with self.assertRaises(PaymentError):
            loaded.save(update_fields=["account"])
        self.assertEqual(self.state(), before)
        self.assertEqual((self.bank.balance, self.paid()), (D("10"), D("10.00")))

    def test_account_move_after_payment_is_rejected(self):
        account = Account.objects.get(pk=self.cash.pk)
        Payment(kind=Payment.KIND_IN, organization=self.org, account=account,
                counterparty=self.customer, amount=D("10")).save()
        other = Account.objects.get(pk=self.cash.pk)
        other.organization = self.org_b
        with self.assertRaises(ValidationError):
            other.save()
        self.assertEqual(Account.objects.get(pk=self.cash.pk).organization, self.org)

    def test_saving_other_fields_of_account_with_payments_is_allowed(self):
        self.payment(10)
        account = Account.objects.get(pk=self.bank.pk)
        account.name = "Р/с Альфа (основной)"
        account.save()
        account.save(update_fields=["name"])
        self.assertEqual(Account.objects.get(pk=self.bank.pk).name, "Р/с Альфа (основной)")


class UpdateFieldsTests(FinanceBase):
    """save(update_fields=...) проверяет то, что действительно будет записано."""

    def test_field_outside_update_fields_is_not_checked_as_written(self):
        payment = self.payment(10)
        payment.counterparty = self.customer2      # в памяти несовместимо, но не записывается
        payment.amount = D("30")
        payment.save(update_fields=["amount"])
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.counterparty_id, stored.amount), (self.customer.pk, D("30.00")))
        self.assertEqual(self.paid(), D("30.00"))

    def test_written_link_is_checked_against_stored_fields(self):
        """В памяти покупатель и счёт согласованы, но записывается только счёт —
        в базе он оказался бы рядом со старым покупателем."""
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10)
        payment.counterparty = self.customer2
        payment.invoice = other
        before = self.state()
        with self.assertRaisesMessage(PaymentError, "а в платеже указан «Покупатель»"):
            payment.save(update_fields=["invoice"])
        self.assertEqual(self.state(), before)
        self.assertEqual((self.paid(), self.paid(other)), (D("10.00"), D("0")))

    def test_written_counterparty_is_checked_against_stored_invoice(self):
        payment = self.payment(10)
        payment.counterparty = self.customer2
        payment.invoice = None                     # в памяти связи нет, но записывается только покупатель
        before = self.state()
        with self.assertRaises(PaymentError):
            payment.save(update_fields=["counterparty"])
        self.assertEqual(self.state(), before)

    def test_both_fields_written_together_are_allowed(self):
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10)
        payment.counterparty = self.customer2
        payment.invoice = other
        payment.save(update_fields=["counterparty", "invoice"])
        self.assertEqual((self.paid(), self.paid(other)), (D("0.00"), D("10.00")))

    def test_posting_checks_stored_links_not_memory(self):
        """Проведение пишет только статус; связи проверяются такими, какие в базе."""
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10, post=False)
        legacy_update(Payment.objects.filter(pk=payment.pk), invoice=other)     # в базе уже несовместимо
        self.assertEqual(payment.invoice_id, self.invoice.pk)          # а в памяти — прежний счёт
        before = self.state()
        with self.assertRaises(PaymentError):
            payment.post()
        self.assertEqual(self.state(), before)

    def test_unposting_old_incompatible_payment_still_works(self):
        other = self.make_invoice(customer=self.customer2)
        payment = self.payment(10)
        legacy_update(Payment.objects.filter(pk=payment.pk), invoice=other)
        self.assertEqual(self.paid(other), D("10.00"))
        payment.unpost()                                                # в памяти — прежний счёт
        self.assertEqual(Payment.objects.get(pk=payment.pk).status, "draft")
        self.assertEqual((self.paid(), self.paid(other)), (D("0"), D("0.00")))

    def test_moving_between_invoices_keeps_paid_amounts(self):
        first = self.payment(10)
        second = self.payment(25, invoice=self.invoice2)
        first.invoice, second.invoice = self.invoice2, self.invoice
        first.save(update_fields=["invoice"])
        second.save(update_fields=["invoice"])
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("25.00"), D("10.00")))
        self.payment(100)                                               # переплата сохраняется
        self.assertEqual(self.paid(), D("125.00"))
