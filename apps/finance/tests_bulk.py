"""Контракт массовых операций с платежами (PaymentQuerySet).

Раньше массовое изменение не проверяло совместимость связей, а счёт,
заданный выражением, блокировало уже после записи. Теперь: конкретные
значения — полная проверка каждой строки под блокировками в общем порядке;
выражения для проверяемых полей и режимы bulk_create с конфликтами —
отказ до записи.
"""
from decimal import Decimal

from django.db import models, transaction
from django.db.models import F, Value

from apps.sales.models import Invoice

from .models import Account, Payment
from .services import PaymentError, UnsupportedBulkChange, incompatible_payments
from .tests_consistency import FinanceBase, legacy_update

D = Decimal


class BulkBase(FinanceBase):
    def setUp(self):
        super().setUp()
        self.cash = Account.objects.create(organization=self.org, name="Касса Альфа", kind=Account.KIND_CASH)
        self.bank_b = Account.objects.create(organization=self.org_b, name="Р/с Бета", kind=Account.KIND_BANK)
        self.other_customer_invoice = self.make_invoice(customer=self.customer2)

    def assertNoIncompatible(self):
        self.assertEqual(incompatible_payments(), [])


class UpdateTests(BulkBase):
    def test_compatible_literal_move_recomputes_old_and_new_invoices(self):
        first, second = self.payment(10), self.payment(15)
        Payment.objects.filter(pk__in=[first.pk, second.pk]).update(invoice=self.invoice2, account=self.cash)
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("0.00"), D("25.00")))
        self.assertEqual(set(Payment.objects.values_list("account_id", flat=True)), {self.cash.pk})
        self.assertNoIncompatible()

    def test_incompatible_invoice_is_rejected_before_writing(self):
        first, second = self.payment(10), self.payment(15)
        before = self.state()
        with self.assertRaisesMessage(PaymentError, f"платёж id {first.pk}:"):
            Payment.objects.filter(pk__in=[first.pk, second.pk]).update(invoice=self.other_customer_invoice)
        self.assertEqual(self.state(), before)

    def test_foreign_account_is_rejected(self):
        payment = self.payment(10)
        before = self.state()
        with self.assertRaisesMessage(PaymentError, "принадлежит организации «Бета»"):
            Payment.objects.filter(pk=payment.pk).update(account=self.bank_b)
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(account_id=self.bank_b.pk)
        self.assertEqual(self.state(), before)

    def test_counterparty_and_organization_changes_are_checked(self):
        payment = self.payment(10)
        before = self.state()
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(counterparty=self.customer2)
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(organization=self.org_b)
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(kind=Payment.KIND_OUT)
        self.assertEqual(self.state(), before)
        Payment.objects.filter(pk=payment.pk).update(counterparty=self.customer2,
                                                     invoice=self.other_customer_invoice)
        self.assertEqual((self.paid(), self.paid(self.other_customer_invoice)), (D("0.00"), D("10.00")))
        self.assertNoIncompatible()

    def test_one_bad_row_rejects_whole_batch(self):
        good = self.payment(10, invoice=None)
        bad = self.payment(5, invoice=None, counterparty=self.customer2)    # покупатель не тот
        before = self.state()
        # ПРАВИЛО ДОПОЛНЕНО (этап 27, взаиморасчёты): платёж по счёту покупателю —
        # только «оплата от покупателя», а записанное назначение («не распределено»)
        # молча не подменяется. Поэтому при привязке счёта к уже записанному
        # платежу назначение указывается в той же операции. Ожидаемые значения
        # теста прежние; без назначения — ошибка (tests_settlements).
        with self.assertRaisesMessage(PaymentError, f"платёж id {bad.pk}:"):
            Payment.objects.filter(pk__in=[good.pk, bad.pk]).update(
                invoice=self.invoice, settlement_purpose=Payment.PURPOSE_CUSTOMER_PAYMENT)
        self.assertEqual(self.state(), before)

    def test_expressions_for_checked_fields_are_rejected_before_writing(self):
        payment = self.payment(10)
        before = self.state()
        for values in (
            {"invoice": F("invoice")}, {"invoice_id": Value(self.invoice2.pk)}, {"account": F("account")},
            {"amount": F("amount") * 2}, {"status": Value("draft")}, {"counterparty_id": F("counterparty_id")},
            {"organization": F("organization")}, {"kind": Value(Payment.KIND_OUT)},
        ):
            with self.subTest(values=values):
                with self.assertRaises(UnsupportedBulkChange):
                    Payment.objects.filter(pk=payment.pk).update(**values)
        self.assertEqual(self.state(), before)

    def test_expression_for_unchecked_field_is_allowed(self):
        payment = self.payment(10)
        Payment.objects.filter(pk=payment.pk).update(purpose=F("number"))
        self.assertEqual(Payment.objects.get(pk=payment.pk).purpose, payment.number)
        self.assertEqual(self.paid(), D("10.00"))

    def test_unposting_old_invalid_payments_in_bulk_is_allowed(self):
        bad_link = self.payment(10)
        legacy_update(Payment.objects.filter(pk=bad_link.pk), invoice=self.other_customer_invoice)
        bad_amount = self.payment(7, invoice=None)
        models.QuerySet.update(Payment.objects.filter(pk=bad_amount.pk), amount=D("-7"))
        Payment.objects.filter(pk__in=[bad_link.pk, bad_amount.pk]).update(status="draft")
        self.assertEqual(set(Payment.objects.values_list("status", flat=True)), {"draft"})
        self.assertEqual(self.paid(self.other_customer_invoice), D("0.00"))
        with self.assertRaises(PaymentError):                       # а провести их обратно нельзя
            Payment.objects.filter(pk__in=[bad_link.pk, bad_amount.pk]).update(status="posted")

    def test_editing_old_incompatible_payment_in_bulk_is_rejected(self):
        payment = self.payment(10)
        legacy_update(Payment.objects.filter(pk=payment.pk), invoice=self.other_customer_invoice)
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(purpose="правка")

    def test_delete_still_recomputes(self):
        self.payment(10)
        self.payment(5, invoice=self.invoice2)
        Payment.objects.all().delete()
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("0.00"), D("0.00")))


class BulkUpdateTests(BulkBase):
    def test_swapping_invoices_and_accounts(self):
        first = self.payment(10)
        second = self.payment(25, invoice=self.invoice2, account=self.cash)
        first.invoice, second.invoice = self.invoice2, self.invoice
        first.account, second.account = self.cash, self.bank
        Payment.objects.bulk_update([first, second], ["invoice", "account"])
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("25.00"), D("10.00")))
        self.assertEqual(Payment.objects.get(pk=first.pk).account_id, self.cash.pk)
        self.assertNoIncompatible()

    def test_one_bad_object_rolls_back_whole_batch(self):
        first, second = self.payment(10), self.payment(25)
        first.amount = D("40")
        second.invoice = self.other_customer_invoice
        before = self.state()
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_update([first, second], ["amount", "invoice"])
        self.assertEqual(self.state(), before)                       # и первый не изменился

    def test_amount_rules_apply(self):
        payment = self.payment(10)
        payment.amount = D("-1")
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_update([payment], ["amount"])
        payment.amount = D("12")
        Payment.objects.bulk_update([payment], ["amount"])
        self.assertEqual(self.paid(), D("12.00"))


class BulkUpdateBoundaryTests(BulkBase):
    """Стандартные границы bulk_update."""

    def test_queryset_filter_is_kept_and_count_is_returned(self):
        inside = self.payment(10)
        outside = self.payment(15, invoice=self.invoice2)
        inside.amount, outside.amount = D("11"), D("16")
        count = Payment.objects.filter(invoice=self.invoice).bulk_update([inside, outside], ["amount"])
        self.assertEqual(count, 1)
        self.assertEqual(Payment.objects.get(pk=inside.pk).amount, D("11.00"))
        self.assertEqual(Payment.objects.get(pk=outside.pk).amount, D("15.00"))     # вне выборки — не тронут
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("11.00"), D("15.00")))

    def test_count_for_whole_batch_and_empty_batch(self):
        payments = [self.payment(10) for _ in range(3)]
        for obj in payments:
            obj.purpose = "пакет"
        self.assertEqual(Payment.objects.bulk_update(payments, ["purpose"]), 3)
        self.assertEqual(Payment.objects.bulk_update([], ["purpose"]), 0)
        self.assertEqual(set(Payment.objects.values_list("purpose", flat=True)), {"пакет"})

    def test_invalid_row_rejects_batch_before_writing(self):
        first, second, third = self.payment(10), self.payment(20), self.payment(30)
        first.account, second.account, third.account = self.cash, self.bank_b, self.cash
        before = self.state()
        with self.assertRaisesMessage(PaymentError, f"платёж id {second.pk}:"):
            Payment.objects.bulk_update([first, second, third], ["account"])
        self.assertEqual(self.state(), before)

    def test_expression_values_are_rejected_before_writing(self):
        payment = self.payment(10)
        payment.amount = F("amount") + 1
        before = self.state()
        with self.assertRaises(UnsupportedBulkChange):
            Payment.objects.bulk_update([payment], ["amount"])
        self.assertEqual(self.state(), before)

    def test_moves_recompute_all_old_and_new_invoices(self):
        first = self.payment(10)
        second = self.payment(25, invoice=self.invoice2)
        third = self.payment(5, invoice=None)
        first.invoice, second.invoice, third.invoice = self.invoice2, None, self.invoice
        # ПРАВИЛО ДОПОЛНЕНО (этап 27, взаиморасчёты): платёж по счёту покупателю —
        # только «оплата от покупателя», а записанное назначение («не распределено»)
        # молча не подменяется. Поэтому при привязке счёта к уже записанному
        # платежу назначение указывается в той же операции. Ожидаемые значения
        # теста прежние; без назначения — ошибка (tests_settlements).
        third.settlement_purpose = Payment.PURPOSE_CUSTOMER_PAYMENT
        Payment.objects.bulk_update([third, second, first], ["invoice", "settlement_purpose"])   # порядок не важен
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("5.00"), D("10.00")))
        self.assertNoIncompatible()

    def test_unposting_old_invalid_payments(self):
        bad = self.payment(10)
        legacy_update(Payment.objects.filter(pk=bad.pk), invoice=self.other_customer_invoice)
        bad = Payment.objects.get(pk=bad.pk)
        bad.status = "draft"
        self.assertEqual(Payment.objects.bulk_update([bad], ["status"]), 1)
        self.assertEqual(self.paid(self.other_customer_invoice), D("0.00"))

    def test_stale_object_values_are_checked_against_fresh_rows(self):
        """Объект прочитан раньше; в базе с тех пор счёт стал несовместимым —
        запись только суммы проверяется по актуальной строке."""
        payment = self.payment(10)
        stale = Payment.objects.get(pk=payment.pk)
        legacy_update(Payment.objects.filter(pk=payment.pk), invoice=self.other_customer_invoice)
        stale.amount = D("12")
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_update([stale], ["amount"])


class OuterTransactionTests(BulkBase):
    """Ошибка массовой операции, перехваченная во внешней транзакции, не
    делает её непригодной и не отменяет сделанного до неё."""

    def run_inside(self, operation):
        with transaction.atomic():
            note = self.payment(3, invoice=self.invoice2)            # запись до ошибки
            with self.assertRaises(PaymentError):
                operation()
            self.assertTrue(Payment.objects.filter(pk=note.pk).exists())   # запросы работают
            Payment.objects.filter(pk=note.pk).update(amount=D("4"))
        self.assertEqual(self.paid(self.invoice2), D("4.00"))

    def test_update(self):
        payment = self.payment(10)
        self.run_inside(lambda: Payment.objects.filter(pk=payment.pk).update(account=self.bank_b))
        self.assertEqual(Payment.objects.get(pk=payment.pk).account_id, self.bank.pk)

    def test_bulk_update(self):
        payment = self.payment(10)
        payment.amount = D("-5")
        self.run_inside(lambda: Payment.objects.bulk_update([payment], ["amount"]))
        self.assertEqual(self.paid(), D("10.00"))

    def test_bulk_create(self):
        bad = Payment(kind=Payment.KIND_IN, organization=self.org, account=self.bank_b,
                      counterparty=self.customer, amount=D("5"))
        self.run_inside(lambda: Payment.objects.bulk_create([bad]))

    def test_unsupported_expression(self):
        payment = self.payment(10)
        self.run_inside(lambda: Payment.objects.filter(pk=payment.pk).update(invoice=F("invoice")))


class BulkCreateTests(BulkBase):
    def make(self, **extra):
        values = {"kind": Payment.KIND_IN, "organization": self.org, "account": self.bank,
                  "counterparty": self.customer, "invoice": self.invoice, "amount": D("10"), "status": "posted"}
        values.update(extra)
        return Payment(**values)

    def test_conflict_modes_are_rejected_before_writing(self):
        with self.assertRaises(UnsupportedBulkChange):
            Payment.objects.bulk_create([self.make()], ignore_conflicts=True)
        with self.assertRaises(UnsupportedBulkChange):
            Payment.objects.bulk_create([self.make()], update_conflicts=True, unique_fields=["id"],
                                        update_fields=["amount"])
        self.assertFalse(Payment.objects.exists())

    def test_checked_and_recomputed(self):
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_create([self.make(), self.make(invoice=self.other_customer_invoice)])
        self.assertFalse(Payment.objects.exists())
        Payment.objects.bulk_create([self.make(), self.make(amount=D("5"))])
        self.assertEqual(self.paid(), D("15.00"))
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).paid_amount, D("15.00"))
