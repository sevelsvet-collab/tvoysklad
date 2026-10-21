"""Одновременные операции с платежами — только PostgreSQL.

В SQLite блокировок строк нет, поэтому там эти тесты ничего не доказывают
и пропускаются. Запуск на PostgreSQL:

    DATABASE_URL=postgres://user:pass@host:5432/db \
        python manage.py test apps.finance.tests_concurrency

Django создаёт и удаляет отдельную тестовую базу test_<db>; рабочая база
не затрагивается.
"""
import time
from decimal import Decimal
from unittest import mock, skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.catalog.models import Product, Unit
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from . import services
from .models import Account, Payment

POSTGRES = connection.vendor == "postgresql"
D = Decimal


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentPaymentTests(ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Орг", is_default=True)
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=unit)
        self.first, self.second = self.invoice(), self.invoice()

    def invoice(self):
        doc = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                     status=Invoice.STATUS_ISSUED)
        doc.lines.create(product=self.product, quantity=1, price=1000, vat_rate="none")
        return doc

    def payment(self, amount, invoice, post=False):
        doc = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.account,
                                     counterparty=self.customer, invoice=invoice, amount=amount)
        if post:
            doc.post()
        return doc

    def paid(self, invoice):
        return Invoice.objects.get(pk=invoice.pk).paid_amount

    def assertConsistent(self):
        """Сохранённая оплата каждого счёта равна сумме его проведённых платежей."""
        self.assertEqual(services.paid_amount_differences(), [])

    def test_two_payments_posted_for_one_invoice(self):
        payments = [self.payment(amount, self.first) for amount in (100, 200, 300, 400)]
        results = self.run_together(*[
            (lambda pk=p.pk: Payment.objects.get(pk=pk).post()) for p in payments
        ])
        self.assertEqual(results, [None] * 4)
        self.assertEqual(self.paid(self.first), D("1000.00"))
        self.assertConsistent()

    def test_editing_payment_while_another_is_posted(self):
        posted = self.payment(100, self.first, post=True)
        draft = self.payment(200, self.first)

        def change_amount():
            doc = Payment.objects.get(pk=posted.pk)
            doc.amount = D("150")
            doc.save()

        for _ in range(5):
            results = self.run_together(change_amount, lambda: Payment.objects.get(pk=draft.pk).post())
            self.assertEqual(results, [None, None])
            self.assertEqual(self.paid(self.first), D("350.00"))
            Payment.objects.get(pk=draft.pk).unpost()
            Payment.objects.filter(pk=posted.pk).update(amount=D("100"))
        self.assertConsistent()

    def test_moving_payments_between_two_invoices_in_opposite_directions(self):
        """Платёж А: счёт 1 → 2, платёж Б: счёт 2 → 1. Счета блокируются по
        возрастанию id, поэтому взаимной блокировки нет."""
        a = self.payment(100, self.first, post=True)
        b = self.payment(300, self.second, post=True)

        def move(pk, invoice):
            doc = Payment.objects.get(pk=pk)
            doc.invoice = invoice
            doc.save()

        for _ in range(5):
            results = self.run_together(lambda: move(a.pk, self.second), lambda: move(b.pk, self.first))
            self.assertEqual(results, [None, None])
            self.assertEqual((self.paid(self.first), self.paid(self.second)), (D("300.00"), D("100.00")))
            results = self.run_together(lambda: move(a.pk, self.first), lambda: move(b.pk, self.second))
            self.assertEqual(results, [None, None])
            self.assertEqual((self.paid(self.first), self.paid(self.second)), (D("100.00"), D("300.00")))
        self.assertConsistent()

    def test_delete_while_recalculating(self):
        doomed = self.payment(100, self.first, post=True)
        other = self.payment(200, self.first)
        results = self.run_together(
            lambda: Payment.objects.get(pk=doomed.pk).delete(),
            lambda: Payment.objects.get(pk=other.pk).post(),
            lambda: services.recompute_invoices([self.first.pk]) or None,
        )
        self.assertEqual([r for r in results if r is not None], [])
        self.assertEqual(self.paid(self.first), D("200.00"))
        self.assertConsistent()

    def test_bulk_delete_while_single_payment_is_saved(self):
        """Массовое удаление и сохранение одного платежа берут блокировки в
        одном порядке (платежи, затем счета) — без взаимной блокировки."""
        victims = [self.payment(50, self.first, post=True) for _ in range(4)]
        survivor = self.payment(70, self.first, post=True)

        def change():
            doc = Payment.objects.get(pk=survivor.pk)
            doc.amount = D("80")
            doc.save()

        results = self.run_together(
            lambda: Payment.objects.filter(pk__in=[p.pk for p in victims]).delete(), change,
        )
        self.assertEqual(results, [None, None])
        self.assertEqual(self.paid(self.first), D("80.00"))
        self.assertConsistent()

    # ---------- платёж против смены сторон (порядок: платёж → касса → счёт покупателю) ----------

    def create_and_post(self, invoice=None, account=None):
        payment = Payment(kind=Payment.KIND_IN, organization=self.org, account=account or self.account,
                          counterparty=self.customer, invoice=invoice, amount=D("50"))
        payment.save()
        payment.post()

    def test_payment_creation_races_with_invoice_customer_change(self):
        """Допустим только согласованный исход: либо платёж отклонён (покупатель
        уже сменился), либо отклонена смена покупателя (платёж уже есть)."""
        from django.core.exceptions import ValidationError

        other = Counterparty.objects.create(name="Другой", partner_type=Counterparty.TYPE_CUSTOMER)
        for _ in range(6):
            invoice = self.invoice()

            def change_customer(pk=invoice.pk):
                doc = Invoice.objects.get(pk=pk)
                doc.customer = other
                doc.save()

            results = self.run_together(lambda inv=invoice: self.create_and_post(inv), change_customer)
            errors = [r for r in results if r is not None]
            self.assertEqual(len(errors), 1, results)
            self.assertIsInstance(errors[0], (services.PaymentError, ValidationError))
            customer = Invoice.objects.get(pk=invoice.pk).customer_id
            if Payment.objects.filter(invoice=invoice).exists():
                self.assertEqual(customer, self.customer.pk)
                self.assertEqual(self.paid(invoice), D("50.00"))
            else:
                self.assertEqual(customer, other.pk)
                self.assertEqual(self.paid(invoice), D("0.00"))
        self.assertEqual(services.incompatible_payments(), [])
        self.assertConsistent()

    def test_payment_creation_races_with_account_organization_change(self):
        from django.core.exceptions import ValidationError

        other_org = Organization.objects.create(name="Вторая")
        for index in range(6):
            account = Account.objects.create(organization=self.org, name=f"Касса {index}", kind=Account.KIND_CASH)

            def move(pk=account.pk):
                doc = Account.objects.get(pk=pk)
                doc.organization = other_org
                doc.save()

            results = self.run_together(lambda acc=account: self.create_and_post(account=acc), move)
            errors = [r for r in results if r is not None]
            self.assertEqual(len(errors), 1, results)
            self.assertIsInstance(errors[0], (services.PaymentError, ValidationError))
            organization = Account.objects.get(pk=account.pk).organization_id
            if Payment.objects.filter(account=account).exists():
                self.assertEqual(organization, self.org.pk)
            else:
                self.assertEqual(organization, other_org.pk)
        self.assertEqual(services.incompatible_payments(), [])

    def test_moving_payments_between_invoices_with_account_change(self):
        """Встречный перенос двух платежей и одновременная смена их кассы на
        другую кассу той же организации — без взаимной блокировки."""
        second_account = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH)
        a = self.payment(100, self.first, post=True)
        b = self.payment(300, self.second, post=True)

        def move(pk, invoice, account):
            doc = Payment.objects.get(pk=pk)
            doc.invoice, doc.account = invoice, account
            doc.save()

        for _ in range(5):
            results = self.run_together(
                lambda: move(a.pk, self.second, second_account), lambda: move(b.pk, self.first, self.account),
            )
            self.assertEqual(results, [None, None])
            self.assertEqual((self.paid(self.first), self.paid(self.second)), (D("300.00"), D("100.00")))
            results = self.run_together(
                lambda: move(a.pk, self.first, self.account), lambda: move(b.pk, self.second, second_account),
            )
            self.assertEqual(results, [None, None])
        self.assertConsistent()
        self.assertEqual(services.incompatible_payments(), [])


    # ---------- доказанное пересечение: вторая операция ждёт блокировку первой ----------

    def test_overlap_two_payments_for_one_invoice(self):
        """Первый платёж остановлен после записи, держит блокировку счёта;
        второй ждёт её и после этого считает сумму с учётом первого."""
        first, second = self.payment(100, self.first), self.payment(250, self.first)
        results = self.run_overlapping(
            lambda: Payment.objects.get(pk=first.pk).post(),
            lambda: Payment.objects.get(pk=second.pk).post(),
            pause_on=(services, "recompute_invoices"),
        )
        self.assertEqual(results, [None, None])
        self.assertEqual(self.paid(self.first), D("350.00"))
        self.assertConsistent()

    def test_overlap_payment_first_then_customer_change(self):
        """Платёж сохраняется первым — смена покупателя счёта ждёт и отклоняется."""
        from django.core.exceptions import ValidationError

        other = Counterparty.objects.create(name="Другой", partner_type=Counterparty.TYPE_CUSTOMER)
        invoice = self.invoice()

        def change_customer():
            doc = Invoice.objects.get(pk=invoice.pk)
            doc.customer = other
            doc.save()

        results = self.run_overlapping(lambda: self.create_and_post(invoice), change_customer,
                                       pause_on=(services, "recompute_invoices"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], ValidationError)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).customer_id, self.customer.pk)
        self.assertEqual(self.paid(invoice), D("50.00"))
        self.assertEqual(services.incompatible_payments(), [])

    def test_overlap_customer_change_first_then_payment(self):
        """Покупатель счёта меняется первым — платёж ждёт и отклоняется."""
        other = Counterparty.objects.create(name="Другой", partner_type=Counterparty.TYPE_CUSTOMER)
        invoice = self.invoice()

        def change_customer():
            doc = Invoice.objects.get(pk=invoice.pk)
            doc.customer = other
            doc.save()

        results = self.run_overlapping(change_customer, lambda: self.create_and_post(invoice),
                                       pause_on=(Invoice, "_linked_payments_error"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], services.PaymentError)
        self.assertFalse(Payment.objects.filter(invoice=invoice).exists())
        self.assertEqual(self.paid(invoice), D("0.00"))
        self.assertEqual(services.incompatible_payments(), [])

    def test_overlap_payment_first_then_account_move(self):
        from django.core.exceptions import ValidationError

        other_org = Organization.objects.create(name="Вторая")
        account = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH)

        def move():
            doc = Account.objects.get(pk=account.pk)
            doc.organization = other_org
            doc.save()

        results = self.run_overlapping(lambda: self.create_and_post(account=account), move,
                                       pause_on=(services, "recompute_invoices"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], ValidationError)
        self.assertEqual(Account.objects.get(pk=account.pk).organization_id, self.org.pk)
        self.assertEqual(services.incompatible_payments(), [])

    def test_overlap_account_move_first_then_payment(self):
        other_org = Organization.objects.create(name="Вторая")
        account = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH)

        def move():
            doc = Account.objects.get(pk=account.pk)
            doc.organization = other_org
            doc.save()

        results = self.run_overlapping(move, lambda: self.create_and_post(account=account),
                                       pause_on=(Account, "_organization_change_error"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], services.PaymentError)
        self.assertFalse(Payment.objects.filter(account=account).exists())
        self.assertEqual(services.incompatible_payments(), [])

    def test_overlap_queryset_update_with_single_save(self):
        """QuerySet.update() держит блокировки платежей и счетов; обычное
        сохранение другого платежа того же счёта ждёт и считает верно."""
        batch = [self.payment(10, self.first, post=True) for _ in range(3)]
        single = self.payment(40, self.first, post=True)

        def single_change():
            doc = Payment.objects.get(pk=single.pk)
            doc.amount = D("45")
            doc.save()

        results = self.run_overlapping(
            lambda: Payment.objects.filter(pk__in=[p.pk for p in batch]).update(invoice=self.second),
            single_change,
            pause_on=(services, "recompute_invoices"),
        )
        self.assertEqual(results, [None, None])
        self.assertEqual((self.paid(self.first), self.paid(self.second)), (D("45.00"), D("30.00")))
        self.assertConsistent()
        self.assertEqual(services.incompatible_payments(), [])

    def test_opposite_queryset_update_moves_between_invoices_and_accounts(self):
        """Два встречных QuerySet.update() (счета и кассы) — без взаимной блокировки."""
        cash = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH)
        to_second = [self.payment(10, self.first, post=True) for _ in range(3)]
        to_first = [self.payment(20, self.second, post=True) for _ in range(3)]
        for _ in range(5):
            results = self.run_together(
                lambda: Payment.objects.filter(pk__in=[p.pk for p in to_second]).update(
                    invoice=self.second, account=cash),
                lambda: Payment.objects.filter(pk__in=[p.pk for p in to_first]).update(
                    invoice=self.first, account=self.account),
            )
            self.assertEqual(results, [None, None])
            self.assertEqual((self.paid(self.first), self.paid(self.second)), (D("60.00"), D("30.00")))
            to_second, to_first = to_first, to_second
            results = self.run_together(
                lambda: Payment.objects.filter(pk__in=[p.pk for p in to_second]).update(
                    invoice=self.second, account=cash),
                lambda: Payment.objects.filter(pk__in=[p.pk for p in to_first]).update(
                    invoice=self.first, account=self.account),
            )
            self.assertEqual(results, [None, None])
            to_second, to_first = to_first, to_second
        self.assertConsistent()
        self.assertEqual(services.incompatible_payments(), [])


    # ---------- bulk_update: кассы пакетов в обратном порядке ----------

    def test_bulk_update_batches_with_opposite_account_order(self):
        """Два непересекающихся пакета bulk_update. В первом кассы по строкам
        идут А → Б, во втором Б → А. Каждый поток после проверки своей первой
        строки ждёт (до 3 с), пока другой поток тоже дойдёт до первой строки.

        Если пакет берёт блокировки касс построчно, оба потока встречаются:
        первый держит А и ждёт Б, второй держит Б и ждёт А — взаимная
        блокировка, PostgreSQL прерывает один из потоков, тест падает.
        Если все кассы пакета блокируются заранее по возрастанию id, второй
        поток ждёт первый ещё до своей первой строки (это фиксируется по
        pg_stat_activity), встречи нет, оба пакета проходят."""
        import threading

        cash_a = Account.objects.create(organization=self.org, name="Касса А", kind=Account.KIND_CASH)
        cash_b = Account.objects.create(organization=self.org, name="Касса Б", kind=Account.KIND_CASH)
        first_batch = [self.payment(10, None, post=True) for _ in range(2)]     # строки: → А, → Б
        second_batch = [self.payment(20, None, post=True) for _ in range(2)]    # строки: → Б, → А
        targets = {
            first_batch[0].pk: cash_a, first_batch[1].pk: cash_b,
            second_batch[0].pk: cash_b, second_batch[1].pk: cash_a,
        }

        arrived = {}
        local = threading.local()
        original = services.link_errors

        def meeting_point(values):
            result = original(values)
            name = threading.current_thread().name
            if name in ("op-0", "op-1") and not getattr(local, "met", False):
                local.met = True
                arrived[name] = threading.Event()
                arrived[name].set()
                other = "op-1" if name == "op-0" else "op-0"
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and other not in arrived:
                    time.sleep(0.01)
            return result

        def run_batch(batch):
            objs = list(Payment.objects.filter(pk__in=[p.pk for p in batch]).order_by("pk"))
            for obj in objs:
                obj.account = targets[obj.pk]
            return Payment.objects.filter(pk__in=[p.pk for p in batch]).bulk_update(objs, ["account"])

        with mock.patch.object(services, "link_errors", meeting_point):
            results, waited = self.run_together_watching(
                lambda: run_batch(first_batch), lambda: run_batch(second_batch),
            )

        self.assertEqual(results, [None, None])            # ни взаимной блокировки, ни таймаута
        self.assertTrue(waited, "пакеты не пересеклись: второй не ждал блокировку первого")
        self.assertEqual(
            {pk: Payment.objects.get(pk=pk).account_id for pk in targets},
            {pk: account.pk for pk, account in targets.items()},
        )
        self.assertEqual(services.incompatible_payments(), [])
