"""История денежного счёта: корректировка фиксирует фактический остаток на
момент документа (правило «остаток на момент», docs/account-correction-proposal.md).

Сценарии независимой модели docs/proposals/account_correction_model.py —
на настоящих документах; ожидаемые суммы записаны здесь явно. Касса:
начальный остаток 50, все документы — 14.10.2026, время указано в тестах.
"""
import datetime
import itertools
from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core import roles
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty

from . import services
from .models import Account, AccountCorrection, Payment
from .services import PaymentError

D = Decimal
DAY = datetime.date(2026, 10, 14)
POSTGRES = connection.vendor == "postgresql"


def at(hhmm):
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return datetime.time(hours, minutes)


class Book:
    """Документы кассы — как в программе: платёж проведён сразу, корректировка
    сохраняется формой (save) и проводится кнопкой (post)."""

    def setup_book(self, opening="50"):
        self.org = Organization.objects.create(name="Организация", is_default=True)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.cash = self.new_cash("Касса", opening, default=True)
        self.docs = {}

    def new_cash(self, name, opening="0", default=False):
        return Account.objects.create(organization=self.org, name=name, kind=Account.KIND_CASH,
                                      opening_balance=D(opening), is_default=default)

    def pay(self, label, hhmm, amount, cash=None, post=True):
        amount = D(str(amount))
        payment = Payment.objects.create(
            kind=Payment.KIND_IN if amount > 0 else Payment.KIND_OUT, organization=self.org,
            account=cash or self.cash, counterparty=self.customer, amount=abs(amount), date=DAY, time=at(hhmm),
            status="posted" if post else "draft")
        self.docs[label] = payment
        return payment

    def count(self, label, hhmm, actual, cash=None, post=True):
        correction = AccountCorrection(organization=self.org, account=cash or self.cash, actual_balance=D(str(actual)),
                                       date=DAY, time=at(hhmm))
        correction.save()
        if post:
            correction.post()
        self.docs[label] = correction
        return correction

    def fresh(self, label):
        doc = self.docs[label]
        return type(doc).objects.get(pk=doc.pk)

    def diff(self, label):
        return self.fresh(label).amount

    def balance(self, cash=None):
        return Account.objects.get(pk=(cash or self.cash).pk).balance

    def assertHistory(self, expected_diffs, balance, cash=None):
        """Разницы корректировок, остаток и совпадение с независимым расчётом истории."""
        self.assertEqual({label: self.diff(label) for label in expected_diffs},
                         {label: D(value) for label, value in expected_diffs.items()})
        self.assertEqual(self.balance(cash), D(balance))
        self.assertEqual(services.correction_history_differences(), [])

    def state(self):
        return (
            sorted(AccountCorrection._base_manager.values_list(
                "pk", "account_id", "status", "actual_balance", "balance_before", "amount", "date", "time")),
            sorted(Payment.objects.values_list("pk", "account_id", "status", "amount", "date", "time")),
            sorted((a.pk, a.opening_balance, a.balance) for a in Account.objects.all()),
        )


class ModelScenarioTests(Book, TestCase):
    """Сценарии модели: правило А, суммы — из docs/account-correction-proposal.md."""

    def setUp(self):
        self.setup_book()

    def test_1_fact_at_six(self):
        self.count("К", "18:00", 500)
        self.assertHistory({"К": "450"}, "500")
        self.assertEqual(self.fresh("К").balance_before, D("50.00"))

    def test_2_payment_after_the_count(self):
        self.count("К", "18:00", 500)
        self.pay("П19", "19:00", 30)
        self.assertHistory({"К": "450"}, "530")

    def test_2b_later_payment_entered_first(self):
        self.pay("П19", "19:00", 30)
        self.count("К", "18:00", 500)
        self.assertHistory({"К": "450"}, "530")

    def test_3_payment_before_the_count(self):
        self.pay("П17", "17:00", 30)
        self.count("К", "18:00", 500)
        self.assertHistory({"К": "420"}, "500")

    def test_4_draft_saved_before_payment_posted_after(self):
        """Время документа и время ввода разные: черновик на 18:00 сохранён до
        ввода платежа 17:00, проведён после — при проведении считается заново."""
        self.count("К", "18:00", 500, post=False)
        self.assertEqual(self.diff("К"), D("450.00"))                     # предварительно
        self.pay("П17", "17:00", 30)
        self.assertEqual(self.diff("К"), D("420.00"))                     # черновик уточнился
        self.assertEqual(self.balance(), D("80.00"))                      # черновик на деньги не влияет
        self.fresh("К").post()
        self.assertHistory({"К": "420"}, "500")

    def test_5_repost_comment_unpost(self):
        self.count("К", "18:00", 500)
        self.pay("П19", "19:00", 30)
        before = self.state()
        self.fresh("К").post()                                             # повторное проведение
        self.assertEqual(self.state(), before)
        correction = self.fresh("К")
        correction.comment = "поправлен комментарий"
        correction.save()
        self.assertEqual(self.state(), before)
        correction.save(update_fields=["comment"])
        self.assertEqual(self.state(), before)
        self.fresh("К").unpost()
        self.assertEqual(self.balance(), D("80.00"))
        self.fresh("К").post()
        self.assertHistory({"К": "450"}, "530")

    def test_6_two_counts(self):
        self.count("К18", "18:00", 500)
        self.pay("П19", "19:00", 30)
        self.count("К21", "21:00", 600)
        self.assertHistory({"К18": "450", "К21": "70"}, "600")
        self.assertEqual(self.fresh("К21").balance_before, D("530.00"))

    def test_6b_earlier_count_entered_last(self):
        self.pay("П19", "19:00", 30)
        self.count("К21", "21:00", 600)
        self.assertEqual(self.diff("К21"), D("520.00"))
        self.count("К18", "18:00", 500)
        self.assertHistory({"К18": "450", "К21": "70"}, "600")

    def test_6c_comment_in_earlier_count(self):
        self.count("К18", "18:00", 500)
        self.pay("П19", "19:00", 30)
        self.count("К21", "21:00", 600)
        correction = self.fresh("К18")
        correction.comment = "только комментарий"
        correction.save()
        self.assertHistory({"К18": "450", "К21": "70"}, "600")

    def test_7_backdated_payment_inserted_changed_cancelled_deleted(self):
        self.count("К", "18:00", 500)
        payment = self.pay("П17", "17:00", 30)
        self.assertHistory({"К": "420"}, "500")
        payment = Payment.objects.get(pk=payment.pk)
        payment.amount = D("40")
        payment.save()
        self.assertHistory({"К": "410"}, "500")
        Payment.objects.get(pk=payment.pk).unpost()
        self.assertHistory({"К": "450"}, "500")
        Payment.objects.get(pk=payment.pk).post()
        Payment.objects.get(pk=payment.pk).delete()
        self.assertHistory({"К": "450"}, "500")

    def test_8_opening_balance_changed(self):
        self.count("К", "18:00", 500)
        cash = Account.objects.get(pk=self.cash.pk)
        cash.opening_balance = D("80")
        cash.save()
        self.assertHistory({"К": "420"}, "500")

    def test_8b_stale_account_object(self):
        """Объект корректировки с кассой, загруженной до исправления начального
        остатка: recalc() и сохранение считают по базе, а не по объекту."""
        self.count("К", "18:00", 500)
        stale = self.fresh("К")
        self.assertEqual(stale.account.opening_balance, D("50.00"))
        cash = Account.objects.get(pk=self.cash.pk)
        cash.opening_balance = D("80")
        cash.save()
        stale.recalc()
        self.assertEqual((stale.balance_before, stale.amount), (D("80.00"), D("420.00")))
        stale.save(update_fields=["balance_before", "amount"])
        self.assertHistory({"К": "420"}, "500")

    def test_9_same_time_payment_entered_first_or_later(self):
        self.pay("П", "18:00", 30)
        self.count("К", "18:00", 500)
        self.assertHistory({"К": "420"}, "500")
        other = self.new_cash("Вторая касса", "50")
        self.count("К2", "18:00", 500, cash=other)
        self.pay("П2", "18:00", 30, cash=other)
        self.assertEqual(self.diff("К2"), D("450.00"))
        self.assertEqual(self.balance(other), D("530.00"))


class EveningTests(Book, TestCase):
    def test_evening_does_not_depend_on_input_order_or_comment(self):
        """Начальный 50, факт на 18:00 — 500, платежи +30 на 17:00 и +30 на
        19:00: разница +420, остаток 530 при любом порядке ввода и после
        правки комментария. Корректировка — через форму программы."""
        self.setup_book()
        user = get_user_model().objects.create_user("money", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="money", password="pass12345")

        def form(cash, action, comment=""):
            return {"date": "2026-10-14", "number": "", "time": "18:00", "organization": self.org.pk,
                    "account": cash.pk, "actual_balance": "500", "comment": comment, "action": action}

        def count(cash):
            self.client.post(reverse("cash_correction_create"), form(cash, "save_post"))
            self.docs["К"] = AccountCorrection.objects.get(account=cash)

        steps = (count,
                 lambda cash: self.pay("П17", "17:00", 30, cash=cash),
                 lambda cash: self.pay("П19", "19:00", 30, cash=cash))
        for number, order in enumerate(itertools.permutations(steps)):
            cash = self.new_cash(f"Касса {number}", "50")
            for step in order:
                step(cash)
            with self.subTest(order=number):
                self.assertEqual((self.diff("К"), self.balance(cash)), (D("420.00"), D("530.00")))
                self.client.post(reverse("account_correction_edit", args=[self.docs["К"].pk]),
                                 form(cash, "save", comment="пересчитали вечером"))
                self.assertEqual(self.fresh("К").comment, "пересчитали вечером")
                self.assertEqual((self.diff("К"), self.balance(cash)), (D("420.00"), D("530.00")))


class OrderTests(Book, TestCase):
    def setUp(self):
        self.setup_book()

    def test_full_tie_payment_before_correction(self):
        """Одинаковые дата, время и момент создания у платежа и корректировки:
        вид документа решает (сначала платёж), даже при совпадающих id."""
        payment = self.pay("П", "18:00", 30)
        correction = self.count("К", "18:00", 500)
        Payment.objects.filter(pk=payment.pk).update(purpose="x")      # created_at не трогаем
        Payment._base_manager.filter(pk=payment.pk).update(created_at=correction.created_at)
        services.fix_correction_history([self.cash.pk])
        self.assertEqual(self.diff("К"), D("420.00"))
        same_id = 7
        self.assertLess(services.money_key(DAY, at("18:00"), correction.created_at, services.PAYMENT_RANK, same_id),
                        services.money_key(DAY, at("18:00"), correction.created_at, services.CORRECTION_RANK, same_id))

    def test_earlier_date_any_time(self):
        self.pay("П", "23:59", 30)
        Payment.objects.filter(pk=self.docs["П"].pk).update(date=DAY - datetime.timedelta(days=1))
        self.count("К", "00:01", 500)
        self.assertEqual(self.diff("К"), D("420.00"))

    def test_repeated_recalculation_changes_nothing(self):
        self.count("К18", "18:00", 500)
        self.pay("П19", "19:00", 30)
        self.count("К21", "21:00", 600)
        before = self.state()
        with CaptureQueriesContext(connection) as queries:
            services.fix_correction_history([self.cash.pk])
            services.fix_correction_history([self.cash.pk])
        self.assertEqual(self.state(), before)
        self.assertFalse([q for q in queries.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")])


class TransferAndPartialTests(Book, TestCase):
    def setUp(self):
        self.setup_book()
        self.other = self.new_cash("Касса Б", "0")
        self.count("КА", "18:00", 500)
        self.count("КБ", "18:00", 200, cash=self.other)
        self.payment = self.pay("П17", "17:00", 30)
        self.assertEqual((self.diff("КА"), self.diff("КБ")), (D("420.00"), D("200.00")))

    def test_payment_moved_between_cashes_recalculates_both(self):
        payment = Payment.objects.get(pk=self.payment.pk)
        payment.account = self.other
        payment.save()
        self.assertEqual((self.diff("КА"), self.diff("КБ")), (D("450.00"), D("170.00")))
        self.assertEqual((self.balance(), self.balance(self.other)), (D("500.00"), D("200.00")))

    def test_partial_save_with_attname(self):
        payment = Payment.objects.get(pk=self.payment.pk)
        payment.account_id = self.other.pk
        payment.amount = D("999")                                       # в памяти, не записывается
        payment.save(update_fields=["account_id"])
        self.assertEqual((self.diff("КА"), self.diff("КБ")), (D("450.00"), D("170.00")))
        payment = Payment.objects.get(pk=self.payment.pk)
        payment.time = at("19:00")
        payment.save(update_fields=["time"])
        self.assertEqual(self.diff("КБ"), D("200.00"))
        self.assertEqual(self.balance(self.other), D("230.00"))

    def test_correction_moved_and_retimed(self):
        correction = self.fresh("КА")
        correction.time = at("16:00")
        correction.save(update_fields=["time"])
        self.assertEqual((self.diff("КА"), self.balance()), (D("450.00"), D("530.00")))
        correction = self.fresh("КА")
        correction.account_id = self.other.pk
        correction.save(update_fields=["account_id"])                   # обе кассы
        self.assertEqual(self.balance(), D("80.00"))
        self.assertEqual(self.diff("КА"), D("500.00"))                   # в кассе Б раньше ничего
        self.assertEqual(self.diff("КБ"), D("-300.00"))                  # 200 − 500
        self.assertEqual(services.correction_history_differences(), [])

    def test_correction_deleted(self):
        self.fresh("КА").delete()
        self.assertEqual(self.balance(), D("80.00"))
        self.assertEqual(services.correction_history_differences(), [])


class BulkAndAdminTests(Book, TestCase):
    def setUp(self):
        self.setup_book()
        self.count("К", "18:00", 500)
        self.payment = self.pay("П17", "17:00", 30)
        self.assertEqual(self.diff("К"), D("420.00"))

    def test_payment_bulk_paths(self):
        rows = Payment.objects.filter(pk=self.payment.pk)
        rows.update(time=at("19:00"))
        self.assertEqual((self.diff("К"), self.balance()), (D("450.00"), D("530.00")))
        payment = Payment.objects.get(pk=self.payment.pk)
        payment.time = at("17:00")
        payment.amount = D("40")
        Payment.objects.bulk_update([payment], ["time", "amount"])
        self.assertEqual(self.diff("К"), D("410.00"))
        rows.update(status="draft")
        self.assertEqual(self.diff("К"), D("450.00"))
        Payment.objects.bulk_create([Payment(kind=Payment.KIND_OUT, organization=self.org, account=self.cash,
                                             counterparty=self.customer, amount=D("20"), date=DAY,
                                             time=at("12:00"), status="posted")])
        self.assertEqual(self.diff("К"), D("470.00"))                    # 500 − (50 − 20)
        Payment.objects.filter(kind=Payment.KIND_OUT).delete()
        self.assertEqual(self.diff("К"), D("450.00"))
        self.assertEqual(services.correction_history_differences(), [])

    def test_correction_bulk_paths(self):
        rows = AccountCorrection.objects.filter(pk=self.docs["К"].pk)
        rows.update(actual_balance=D("600"))
        self.assertEqual((self.diff("К"), self.balance()), (D("520.00"), D("600.00")))
        correction = self.fresh("К")
        correction.time = at("16:00")
        AccountCorrection.objects.bulk_update([correction], ["time"])
        self.assertEqual(self.diff("К"), D("550.00"))
        AccountCorrection.objects.bulk_create([AccountCorrection(
            organization=self.org, account=self.cash, actual_balance=D("700"), date=DAY, time=at("20:00"),
            status="posted", number="М-1")])
        self.assertEqual(self.balance(), D("700.00"))
        self.assertEqual(AccountCorrection.objects.get(number="М-1").amount, D("70.00"))   # 700 − (600 + 30)
        rows.update(status="draft")
        self.assertEqual(AccountCorrection.objects.get(number="М-1").amount, D("620.00"))  # 700 − 80
        AccountCorrection.objects.filter(number="М-1").delete()
        self.assertEqual(self.balance(), D("80.00"))
        self.assertEqual(services.correction_history_differences(), [])

    def test_account_bulk_opening(self):
        Account.objects.filter(pk=self.cash.pk).update(opening_balance=D("80"))
        self.assertEqual((self.diff("К"), self.balance()), (D("390.00"), D("500.00")))
        cash = Account.objects.get(pk=self.cash.pk)
        cash.opening_balance = D("10")
        self.assertEqual(Account.objects.bulk_update([cash], ["opening_balance"]), 1)
        self.assertEqual((self.diff("К"), self.balance()), (D("460.00"), D("500.00")))

    def test_admin_paths(self):
        root = get_user_model().objects.create_superuser("root", password="pass12345")
        self.client.force_login(root)
        payment = Payment.objects.get(pk=self.payment.pk)
        response = self.client.post(reverse("admin:finance_payment_change", args=[payment.pk]), {
            "kind": payment.kind, "number": payment.number, "date": "2026-10-14", "time": "17:00:00",
            "organization": self.org.pk, "account": self.cash.pk, "counterparty": self.customer.pk,
            "invoice": "", "amount": "40", "purpose": "", "status": "posted", "_save": "Сохранить"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.diff("К"), D("410.00"))
        response = self.client.post(reverse("admin:finance_account_change", args=[self.cash.pk]), {
            "organization": self.org.pk, "name": "Касса", "kind": "cash", "bank_account": "",
            "opening_balance": "60", "is_default": "on", "is_active": "on", "_save": "Сохранить"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.diff("К"), D("400.00"))
        response = self.client.post(reverse("admin:finance_payment_delete", args=[payment.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.diff("К"), self.balance()), (D("440.00"), D("500.00")))


class RejectionAndReadingTests(Book, TestCase):
    def setUp(self):
        self.setup_book()
        self.count("К", "18:00", 500)

    def test_rejected_payment_changes_nothing_including_invoice(self):
        from apps.catalog.models import Product, Unit
        from apps.sales.models import Invoice

        warehouse = Warehouse.objects.create(name="Склад", is_default=True)
        product = Product.objects.create(name="Товар", unit=Unit.objects.create(name="шт"))
        invoice = Invoice.objects.create(organization=self.org, warehouse=warehouse, customer=self.customer,
                                         status=Invoice.STATUS_ISSUED)
        invoice.lines.create(product=product, quantity=1, price=100, vat_rate="none")
        payment = Payment.objects.create(kind=Payment.KIND_IN, organization=self.org, account=self.cash,
                                         counterparty=self.customer, invoice=invoice, amount=30, date=DAY,
                                         time=at("17:00"), status="posted")
        self.assertEqual((self.diff("К"), Invoice.objects.get(pk=invoice.pk).paid_amount), (D("420.00"), D("30.00")))
        foreign = Account.objects.create(organization=Organization.objects.create(name="Б"), name="Чужая",
                                         kind=Account.KIND_CASH)
        before = (self.state(), Invoice.objects.get(pk=invoice.pk).paid_amount)
        payment = Payment.objects.get(pk=payment.pk)
        payment.account = foreign
        payment.time = at("19:00")
        with self.assertRaises(PaymentError):
            with transaction.atomic():
                payment.save()
        self.assertEqual((self.state(), Invoice.objects.get(pk=invoice.pk).paid_amount), before)

    def test_rejected_correction_changes_nothing(self):
        self.pay("П17", "17:00", 30)
        foreign = Account.objects.create(organization=Organization.objects.create(name="Б"), name="Чужая",
                                         kind=Account.KIND_CASH)
        before = self.state()
        correction = self.fresh("К")
        correction.account = foreign
        correction.actual_balance = D("900")
        with self.assertRaises(PaymentError):
            with transaction.atomic():
                correction.save()
        self.assertEqual(self.state(), before)

    def test_reading_form_and_reports_writes_nothing(self):
        user = get_user_model().objects.create_user("money", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="money", password="pass12345")
        self.pay("П17", "17:00", 30)
        AccountCorrection._base_manager.filter(pk=self.docs["К"].pk).update(amount=D("1"))   # старое значение
        with CaptureQueriesContext(connection) as queries:
            page = self.client.get(reverse("account_correction_edit", args=[self.docs["К"].pk]))
            self.client.get(reverse("account_list"))
            self.client.get(reverse("report_cashflow"))
            self.client.get(reverse("dashboard"))
            services.correction_history_differences()
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertContains(page, "Расчётный остаток на 14.10.2026 18:00")
        self.assertContains(page, "может уточниться")

    def test_form_shows_fact_balance_before_and_difference(self):
        user = get_user_model().objects.create_user("money", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="money", password="pass12345")
        self.pay("П17", "17:00", 30)
        page = self.client.get(reverse("account_correction_edit", args=[self.docs["К"].pk])).content.decode()
        self.assertIn("Фактический остаток на дату и время документа", page)
        self.assertIn("Излишек", page)
        self.assertIn("+420,00", page)
        self.assertIn("80,00", page)

    def test_cashflow_report_does_not_include_corrections(self):
        from apps.reports import services as reports

        self.pay("П17", "17:00", 30)
        summary = reports.cashflow_summary(DAY, DAY)
        self.assertEqual((summary["income"], summary["outcome"], summary["net"]),
                         (D("30.00"), D("0"), D("30.00")))


class DiagnosticsTests(Book, TestCase):
    def setUp(self):
        self.setup_book()
        self.count("К", "18:00", 500)
        self.pay("П17", "17:00", 30)
        # старые данные: разница 450 записана по прежнему правилу
        AccountCorrection._base_manager.filter(pk=self.docs["К"].pk).update(balance_before=D("50"), amount=D("450"))

    def run_check(self, *args):
        out = StringIO()
        call_command("check_payments", *args, stdout=out)
        return out.getvalue()

    def test_read_only_report_and_explicit_fix(self):
        before = self.state()
        self.assertEqual(self.balance(), D("530.00"))
        output = self.run_check()
        self.assertIn("записано не по правилу «остаток на момент» (касс: 1)", output)
        self.assertIn("остаток записан 530.00, по правилу 500.00", output)
        self.assertIn("записано — расчётный 50.00, разница 450.00; по правилу — расчётный 80.00, разница 420.00",
                      output)
        self.assertEqual(self.state(), before)
        with self.assertRaisesMessage(CommandError, "--i-have-a-backup"):
            self.run_check("--fix-corrections")
        self.assertEqual(self.state(), before)
        self.assertIn("Пересчитана история касс: 1", self.run_check("--fix-corrections", "--i-have-a-backup"))
        correction = self.fresh("К")
        self.assertEqual((correction.actual_balance, correction.date, correction.time, correction.amount),
                         (D("500.00"), DAY, at("18:00"), D("420.00")))
        self.assertEqual(Payment.objects.get(pk=self.docs["П17"].pk).amount, D("30.00"))
        self.assertEqual(self.balance(), D("500.00"))
        self.assertIn("Расхождений нет", self.run_check())

    def test_ordinary_operation_recalculates_old_history(self):
        """После обновления старая история кассы пересчитывается первой же
        операцией с её платежом или корректировкой."""
        payment = Payment.objects.get(pk=self.docs["П17"].pk)
        payment.purpose = "уточнено назначение"
        payment.save()
        self.assertEqual((self.diff("К"), self.balance()), (D("420.00"), D("500.00")))


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentHistoryTests(Book, ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.setup_book()

    def assertConsistent(self):
        self.assertEqual(services.correction_history_differences(), [])

    def test_payment_against_correction_both_orders(self):
        for first_is_payment in (True, False):
            with self.subTest(first_is_payment=first_is_payment):
                cash = self.new_cash(f"Касса {first_is_payment}", "50")
                draft = self.count("К", "18:00", 500, cash=cash, post=False)

                def post_correction():
                    AccountCorrection.objects.get(pk=draft.pk).post()

                def add_payment():
                    self.pay("П17", "17:00", 30, cash=cash)

                operations = (add_payment, post_correction) if first_is_payment else (post_correction, add_payment)
                results = self.run_overlapping(*operations, pause_on=(services, "recalculate_accounts"))
                self.assertEqual(results, [None, None])
                self.assertEqual((AccountCorrection.objects.get(pk=draft.pk).amount, self.balance(cash)),
                                 (D("420.00"), D("500.00")))
                self.assertConsistent()

    def test_payment_delete_against_recalculation(self):
        for first_is_delete in (True, False):
            with self.subTest(first_is_delete=first_is_delete):
                cash = self.new_cash(f"Касса {first_is_delete}", "50")
                payment = self.pay("П17", "17:00", 30, cash=cash)
                draft = self.count("К", "18:00", 500, cash=cash, post=False)

                def delete_payment():
                    Payment.objects.get(pk=payment.pk).delete()

                def post_correction():
                    AccountCorrection.objects.get(pk=draft.pk).post()

                operations = (delete_payment, post_correction) if first_is_delete else (post_correction, delete_payment)
                results = self.run_overlapping(*operations, pause_on=(services, "recalculate_accounts"))
                self.assertEqual(results, [None, None])
                self.assertEqual((AccountCorrection.objects.get(pk=draft.pk).amount, self.balance(cash)),
                                 (D("450.00"), D("500.00")))
                self.assertConsistent()

    def test_opposite_transfers_between_cashes(self):
        """Платёж П1 переносят А → Б, П2 — Б → А одновременно: без взаимной
        блокировки, история обеих касс пересчитана."""
        other = self.new_cash("Касса Б", "0")
        self.count("КА", "18:00", 500)
        self.count("КБ", "18:00", 200, cash=other)
        first = self.pay("П1", "17:00", 30)
        second = self.pay("П2", "17:00", 40, cash=other)

        def move(pk, target):
            def operation():
                payment = Payment.objects.get(pk=pk)
                payment.account_id = target.pk
                payment.save()
            return operation

        for _ in range(3):
            results = self.run_together(move(first.pk, other), move(second.pk, self.cash))
            self.assertEqual(results, [None, None])
            self.assertEqual((self.diff("КА"), self.diff("КБ")), (D("410.00"), D("170.00")))   # 500 − 90; 200 − 30
            self.assertEqual((self.balance(), self.balance(other)), (D("500.00"), D("200.00")))
            self.assertConsistent()
            results = self.run_together(move(first.pk, self.cash), move(second.pk, other))
            self.assertEqual(results, [None, None])
            self.assertEqual((self.diff("КА"), self.diff("КБ")), (D("420.00"), D("160.00")))
            self.assertConsistent()

    def test_opening_change_against_payment_both_orders(self):
        for first_is_opening in (True, False):
            with self.subTest(first_is_opening=first_is_opening):
                cash = self.new_cash(f"Касса {first_is_opening}", "50")
                correction = self.count("К", "18:00", 500, cash=cash)

                def change_opening():
                    account = Account.objects.get(pk=cash.pk)
                    account.opening_balance = D("80")
                    account.save()

                def add_payment():
                    self.pay("П17", "17:00", 30, cash=cash)

                operations = (change_opening, add_payment) if first_is_opening else (add_payment, change_opening)
                results = self.run_overlapping(*operations, pause_on=(services, "recalculate_accounts"))
                self.assertEqual(results, [None, None])
                self.assertEqual((AccountCorrection.objects.get(pk=correction.pk).amount, self.balance(cash)),
                                 (D("390.00"), D("500.00")))
                self.assertConsistent()
