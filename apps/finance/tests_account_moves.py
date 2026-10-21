"""Смена организации кассы или счёта: оба имени внешнего ключа и массовые пути.

Кассу, по которой есть платежи или корректировки, нельзя передать другой
организации ни через save() (в том числе save(update_fields=["organization"])
и ["organization_id"]), ни массово (update, bulk_update). Свободную кассу —
можно, в том числе пакетом. Проверяется то, что действительно будет записано.
"""
from decimal import Decimal
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization
from apps.partners.models import Counterparty

from . import services
from .models import Account, AccountCorrection, Payment
from .services import PaymentError

D = Decimal
POSTGRES = connection.vendor == "postgresql"


class MovesBase(TestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="Организация А", is_default=True)
        self.org_b = Organization.objects.create(name="Организация Б")
        self.cash = Account.objects.create(organization=self.org_a, name="Касса А", kind=Account.KIND_CASH,
                                           opening_balance=50, is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)

    def posted_correction(self, account=None, actual="500"):
        c = AccountCorrection(organization=self.org_a, account=account or self.cash, actual_balance=D(actual))
        c.save()
        c.recalc()
        c.save(update_fields=["balance_before", "amount"])
        c.post()
        return c

    def posted_payment(self, account=None, amount=30):
        return Payment.objects.create(kind=Payment.KIND_IN, organization=self.org_a, account=account or self.cash,
                                      counterparty=self.customer, amount=amount, status="posted")

    def free_cash(self, name, organization=None):
        return Account.objects.create(organization=organization or self.org_a, name=name, kind=Account.KIND_CASH)

    def state(self):
        return (
            sorted(Account.objects.values_list("pk", "organization_id", "name", "is_default")),
            sorted(AccountCorrection._base_manager.values_list("pk", "organization_id", "account_id", "amount",
                                                               "status")),
            sorted(Payment.objects.values_list("pk", "organization_id", "account_id", "amount", "status")),
            sorted((a.pk, a.balance) for a in Account.objects.all()),
        )

    def assert_rejected(self, operation, text, exception=ValidationError):
        before = self.state()
        with self.assertRaises(exception) as caught:
            with transaction.atomic():
                operation()
        self.assertIn(text, str(caught.exception))
        self.assertEqual(self.state(), before)
        return caught.exception


class RegressionTests(MovesBase):
    def test_update_fields_with_attname_cannot_move_cash_with_posted_correction(self):
        """Воспроизведение: касса А с проведённой корректировкой А;
        cash.organization_id = Б; cash.save(update_fields=["organization_id"]).
        Было: касса уходила к Б, корректировка оставалась у А."""
        self.posted_correction()
        cash = Account.objects.get(pk=self.cash.pk)
        cash.organization_id = self.org_b.pk
        self.assert_rejected(lambda: cash.save(update_fields=["organization_id"]), "есть платежи или корректировки")
        self.assertEqual(Account.objects.get(pk=self.cash.pk).organization_id, self.org_a.pk)
        self.assertEqual(Account.objects.get(pk=self.cash.pk).balance, D("500.00"))

    def test_bulk_update_of_free_cash_organization_succeeds(self):
        """Воспроизведение: свободную кассу (без платежей и корректировок)
        передают Б через Account.objects.bulk_update([cash], ["organization"]).
        Было: UnsupportedBulkChange о «корректировках остатков»."""
        free = self.free_cash("Свободная касса")
        free.organization = self.org_b
        self.assertEqual(Account.objects.bulk_update([free], ["organization"]), 1)
        self.assertEqual(Account.objects.get(pk=free.pk).organization_id, self.org_b.pk)


class SaveTests(MovesBase):
    def test_both_field_names_are_checked_with_posted_correction(self):
        self.posted_correction()
        for fields in (None, ["organization"], ["organization_id"], ["organization_id", "name"]):
            cash = Account.objects.get(pk=self.cash.pk)
            cash.organization_id = self.org_b.pk
            with self.subTest(update_fields=fields):
                self.assert_rejected(lambda: cash.save(update_fields=fields), "есть платежи или корректировки")

    def test_both_field_names_are_checked_with_posted_payment(self):
        self.posted_payment()
        for fields in (["organization"], ["organization_id"]):
            cash = Account.objects.get(pk=self.cash.pk)
            cash.organization = self.org_b
            with self.subTest(update_fields=fields):
                self.assert_rejected(lambda: cash.save(update_fields=fields), "есть платежи или корректировки")
        self.assertEqual(Account.objects.get(pk=self.cash.pk).balance, D("80.00"))

    def test_saving_only_the_name_ignores_unsaved_organization_change(self):
        """В памяти организация сменилась, но записывается только имя: проходит,
        организация остаётся прежней, «основная» касса Б не теряет отметку."""
        self.posted_correction()
        cash_b = Account.objects.create(organization=self.org_b, name="Касса Б", kind=Account.KIND_CASH,
                                        is_default=True)
        cash = Account.objects.get(pk=self.cash.pk)
        cash.organization_id = self.org_b.pk
        cash.name = "Касса А (переименована)"
        cash.save(update_fields=["name"])
        stored = Account.objects.get(pk=self.cash.pk)
        self.assertEqual((stored.name, stored.organization_id, stored.is_default),
                         ("Касса А (переименована)", self.org_a.pk, True))
        self.assertTrue(Account.objects.get(pk=cash_b.pk).is_default)

    def test_free_cash_moves_with_either_name(self):
        for fields in (["organization"], ["organization_id"]):
            free = self.free_cash(f"Свободная {fields[0]}")
            free.organization_id = self.org_b.pk
            free.save(update_fields=fields)
            self.assertEqual(Account.objects.get(pk=free.pk).organization_id, self.org_b.pk)

    def test_attnames_in_payment_and_correction_update_fields(self):
        foreign = self.free_cash("Касса Б", organization=self.org_b)
        payment = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org_a, account=self.cash,
                                         counterparty=self.customer, amount=30)
        payment.account_id = foreign.pk
        self.assert_rejected(lambda: payment.save(update_fields=["account_id"]), "принадлежит организации",
                             PaymentError)
        correction = self.posted_correction()
        correction.account_id = foreign.pk
        self.assert_rejected(lambda: correction.save(update_fields=["account_id"]), "принадлежит организации",
                             PaymentError)
        correction.organization_id = self.org_b.pk
        correction.save(update_fields=["account_id", "organization_id"])      # совместимая пара
        self.assertEqual(AccountCorrection.objects.get(pk=correction.pk).account_id, foreign.pk)


class QuerySetUpdateTests(MovesBase):
    def test_both_field_names_in_update(self):
        self.posted_correction()
        rows = Account.objects.filter(pk=self.cash.pk)
        self.assert_rejected(lambda: rows.update(organization=self.org_b), "есть платежи или корректировки")
        self.assert_rejected(lambda: rows.update(organization_id=self.org_b.pk), "есть платежи или корректировки")
        free = self.free_cash("Свободная")
        self.assertEqual(Account.objects.filter(pk=free.pk).update(organization_id=self.org_b.pk), 1)

    def test_user_expression_is_rejected_with_account_wording(self):
        from django.db.models import F

        error = self.assert_rejected(lambda: Account.objects.update(organization=F("organization")),
                                     "касс и счетов", PaymentError)
        self.assertNotIn("корректировок", str(error))

    def test_correction_update_with_attname(self):
        foreign = self.free_cash("Касса Б", organization=self.org_b)
        correction = self.posted_correction()
        rows = AccountCorrection.objects.filter(pk=correction.pk)
        self.assert_rejected(lambda: rows.update(account_id=foreign.pk), "принадлежит организации", PaymentError)


class BulkUpdateTests(MovesBase):
    def test_valid_batch_with_either_name(self):
        for fields in (["organization"], ["organization_id"]):
            first, second = self.free_cash(f"Первая {fields[0]}"), self.free_cash(f"Вторая {fields[0]}")
            for cash in (first, second):
                cash.organization_id = self.org_b.pk
            with self.subTest(fields=fields):
                self.assertEqual(Account.objects.bulk_update([first, second], fields), 2)
                self.assertEqual(set(Account.objects.filter(pk__in=[first.pk, second.pk])
                                     .values_list("organization_id", flat=True)), {self.org_b.pk})

    def test_batch_with_busy_cash_is_rejected_entirely(self):
        self.posted_payment()
        free = self.free_cash("Свободная")
        busy = Account.objects.get(pk=self.cash.pk)
        for cash in (free, busy):
            cash.organization_id = self.org_b.pk
            cash.name = cash.name + " (Б)"
        error = self.assert_rejected(lambda: Account.objects.bulk_update([free, busy], ["organization", "name"]),
                                     f"По счёту «{self.cash.name}» есть платежи или корректировки")
        self.assertNotIn("выражением", str(error))
        self.assertEqual(Account.objects.get(pk=free.pk).organization_id, self.org_a.pk)

    def test_queryset_filter_is_kept_and_actual_count_returned(self):
        """В пакете три кассы, в выборке — одна свободная: записывается только
        она; занятая касса вне выборки не записывается и пакет не отклоняет."""
        self.posted_correction()
        inside, outside = self.free_cash("В выборке"), self.free_cash("Вне выборки")
        busy = Account.objects.get(pk=self.cash.pk)
        for cash in (inside, outside, busy):
            cash.organization_id = self.org_b.pk
        count = Account.objects.filter(name="В выборке").bulk_update([inside, outside, busy], ["organization"])
        self.assertEqual(count, 1)
        self.assertEqual(dict(Account.objects.values_list("name", "organization_id")),
                         {"Касса А": self.org_a.pk, "В выборке": self.org_b.pk, "Вне выборки": self.org_a.pk})

    def test_empty_batch_and_unchecked_fields(self):
        self.assertEqual(Account.objects.bulk_update([], ["organization"]), 0)
        self.posted_correction()
        busy = Account.objects.get(pk=self.cash.pk)
        busy.name = "Касса А — новое имя"
        busy.opening_balance = D("70")
        self.assertEqual(Account.objects.bulk_update([busy], ["name", "opening_balance"]), 1)   # без организации
        self.assertEqual(Account.objects.get(pk=busy.pk).name, "Касса А — новое имя")

    def test_user_expression_in_object_is_rejected_before_writing(self):
        from django.db.models import F

        free = self.free_cash("Свободная")
        free.organization_id = F("organization_id")
        error = self.assert_rejected(lambda: Account.objects.bulk_update([free], ["organization_id"]),
                                     "касс и счетов", PaymentError)
        self.assertNotIn("корректировок", str(error))

    def test_correction_bulk_update_with_link_fields(self):
        """Допустимый пакет корректировок с организацией и кассой проходит
        (раньше стандартный bulk_update подставлял Case/When в update() и
        получал отказ «выражением не поддерживается»)."""
        foreign = self.free_cash("Касса Б", organization=self.org_b)
        first, second = self.posted_correction(), self.posted_correction(actual="700")
        for correction in (first, second):
            correction.organization_id, correction.account_id = self.org_b.pk, foreign.pk
        self.assertEqual(AccountCorrection.objects.filter(pk=first.pk).bulk_update(
            [first, second], ["organization_id", "account_id"]), 1)
        self.assertEqual(AccountCorrection.objects.get(pk=first.pk).account_id, foreign.pk)
        self.assertEqual(AccountCorrection.objects.get(pk=second.pk).account_id, self.cash.pk)
        second.organization_id = self.org_a.pk                                # касса Б, организация А
        self.assert_rejected(lambda: AccountCorrection.objects.bulk_update([second], ["account"]),
                             "принадлежит организации", PaymentError)
        self.assertEqual(AccountCorrection.objects.bulk_update([], ["account"]), 0)


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentMoveTests(ConcurrencyMixin, TransactionTestCase):
    """Массовая смена организации касс против платежа по одной из них."""

    def setUp(self):
        self.org_a = Organization.objects.create(name="Организация А", is_default=True)
        self.org_b = Organization.objects.create(name="Организация Б")
        self.first = Account.objects.create(organization=self.org_a, name="Первая", kind=Account.KIND_CASH)
        self.second = Account.objects.create(organization=self.org_a, name="Вторая", kind=Account.KIND_CASH)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)

    def move_both(self):
        accounts = list(Account.objects.filter(pk__in=[self.first.pk, self.second.pk]).order_by("-pk"))
        for account in accounts:
            account.organization_id = self.org_b.pk
        return Account.objects.bulk_update(accounts, ["organization"])

    def pay_second(self):
        Payment.objects.create(kind=Payment.KIND_IN, organization=self.org_a, account_id=self.second.pk,
                               counterparty=self.customer, amount=30, status="posted")

    def assert_consistent(self):
        self.assertEqual(services.incompatible_payments(), [])

    def test_bulk_move_first_then_payment_waits_and_is_rejected(self):
        results = self.run_overlapping(self.move_both, self.pay_second, pause_on=(services, "write_rows"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], PaymentError)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(set(Account.objects.values_list("organization_id", flat=True)), {self.org_b.pk})
        self.assert_consistent()

    def test_payment_first_then_bulk_move_waits_and_is_rejected(self):
        results = self.run_overlapping(self.pay_second, self.move_both, pause_on=(services, "link_errors"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], ValidationError)
        self.assertEqual(set(Account.objects.values_list("organization_id", flat=True)), {self.org_a.pk})
        self.assert_consistent()
