"""Корректировка остатка (AccountCorrection) ссылается только на кассу или
счёт своей организации.

Проверяется на сервере — в модели (save по данным базы под блокировкой,
clean для форм и админки) и при проведении. При отказе документ и денежные
остатки не меняются. Снять с проведения и удалить старую несовместимую
корректировку можно всегда; исправить связь — тоже.
"""
from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.core import roles
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization
from apps.partners.models import Counterparty

from . import services
from .models import Account, AccountCorrection, Payment
from .services import PaymentError

D = Decimal
REJECTED = (ValidationError, PaymentError)
POSTGRES = connection.vendor == "postgresql"


class CorrectionBase:
    def make_world(self):
        self.org_a = Organization.objects.create(name="Организация А", is_default=True)
        self.org_b = Organization.objects.create(name="Организация Б")
        self.cash_a = Account.objects.create(organization=self.org_a, name="Касса А", kind=Account.KIND_CASH,
                                             opening_balance=50, is_default=True)
        self.cash_b = Account.objects.create(organization=self.org_b, name="Касса Б", kind=Account.KIND_CASH,
                                             opening_balance=100, is_default=True)

    def correction(self, organization=None, account=None, actual="500", post=False):
        """Корректировка как в форме: сохранить, пересчитать разницу, (провести)."""
        c = AccountCorrection(organization=organization or self.org_a, account=account or self.cash_a,
                              actual_balance=D(actual))
        c.save()
        c.recalc()
        c.save(update_fields=["balance_before", "amount"])
        if post:
            c.post()
        return c

    def legacy(self, correction, **changes):
        """Старые данные: связь, записанная до появления проверки (в обход модели)."""
        AccountCorrection._base_manager.filter(pk=correction.pk).update(**changes)
        return AccountCorrection.objects.get(pk=correction.pk)

    def balances(self):
        return (Account.objects.get(pk=self.cash_a.pk).balance, Account.objects.get(pk=self.cash_b.pk).balance)

    def state(self):
        return (
            sorted(AccountCorrection._base_manager.values_list(
                "pk", "organization_id", "account_id", "status", "amount", "balance_before", "actual_balance")),
            sorted(Account.objects.values_list("pk", "organization_id")),
            self.balances(),
        )

    def assert_rejected(self, operation, text="принадлежит организации"):
        before = self.state()
        with self.assertRaises(REJECTED) as caught:
            with transaction.atomic():
                operation()
        self.assertIn(text, str(caught.exception))
        self.assertEqual(self.state(), before)

    def login(self):
        user = get_user_model().objects.create_user("money", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="money", password="pass12345")

    def form_data(self, organization, account, action="save", actual="500"):
        return {"date": "2026-10-16", "number": "", "time": "12:00", "organization": organization.pk,
                "account": account.pk, "actual_balance": actual, "comment": "", "action": action}


class ReportedCaseTests(CorrectionBase, TestCase):
    def setUp(self):
        self.make_world()

    def test_correction_of_org_a_with_cash_of_org_b_is_rejected(self):
        """Воспроизведение: организация А, касса Б (начальный остаток 100),
        корректировка А по кассе Б, фактический остаток 500; recalc(), save(),
        post(). Было: проведена, amount 400, остаток кассы Б — 500."""
        c = AccountCorrection(organization=self.org_a, account=self.cash_b, actual_balance=D("500"))
        c.recalc()
        self.assertEqual(c.amount, D("400.00"))
        self.assert_rejected(c.save)
        self.assertFalse(AccountCorrection._base_manager.exists())
        self.assertEqual(self.balances(), (D("50.00"), D("100.00")))


class ModelPathTests(CorrectionBase, TestCase):
    def setUp(self):
        self.make_world()

    def test_valid_correction_is_saved_and_posted(self):
        c = self.correction(actual="500", post=True)
        self.assertEqual((c.balance_before, c.amount), (D("50.00"), D("450.00")))
        self.assertEqual(self.balances(), (D("500.00"), D("100.00")))

    def test_changing_account_to_foreign_is_rejected(self):
        c = self.correction()
        c.account = self.cash_b
        self.assert_rejected(c.save)
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).account_id, self.cash_a.pk)

    def test_changing_organization_is_rejected(self):
        c = self.correction(post=True)
        c.organization = self.org_b
        self.assert_rejected(c.save)

    def test_post_of_old_incompatible_draft_is_rejected(self):
        old = self.legacy(self.correction(), account_id=self.cash_b.pk)
        self.assert_rejected(old.post)
        self.assertEqual(AccountCorrection.objects.get(pk=old.pk).status, "draft")

    def test_stale_account_object_after_its_organization_changed(self):
        """Объект кассы загружен, пока она была у А; потом кассу (без документов)
        передали Б. Корректировка А с этим объектом отклоняется — по базе."""
        stale = Account.objects.get(pk=self.cash_a.pk)
        moved = Account.objects.get(pk=self.cash_a.pk)
        moved.organization = self.org_b
        moved.save()
        self.assertEqual(stale.organization_id, self.org_a.pk)            # объект в памяти устарел
        c = AccountCorrection(organization=self.org_a, account=stale, actual_balance=D("500"))
        self.assert_rejected(c.save)

    def test_stale_account_object_that_looks_foreign_is_accepted(self):
        stale = Account.objects.get(pk=self.cash_a.pk)
        stale.organization = self.org_b                                   # в памяти — чужая, в базе — своя
        c = AccountCorrection(organization=self.org_a, account=stale, actual_balance=D("500"))
        c.save()
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).account_id, self.cash_a.pk)

    def test_update_fields_check_what_is_written(self):
        c = self.correction()
        c.organization = self.org_b                                       # не записывается
        c.comment = "только комментарий"
        c.save(update_fields=["comment"])
        stored = AccountCorrection.objects.get(pk=c.pk)
        self.assertEqual((stored.organization_id, stored.comment), (self.org_a.pk, "только комментарий"))

        c = AccountCorrection.objects.get(pk=c.pk)
        c.account = self.cash_b
        c.organization = self.org_b
        self.assert_rejected(lambda: c.save(update_fields=["account"]))   # в базе останется А + касса Б
        c.save(update_fields=["account", "organization"])                 # Б + касса Б — совместимо
        stored = AccountCorrection.objects.get(pk=c.pk)
        self.assertEqual((stored.organization_id, stored.account_id), (self.org_b.pk, self.cash_b.pk))

        c = AccountCorrection.objects.get(pk=c.pk)
        c.account = self.cash_a                                           # в памяти А + касса А
        c.organization = self.org_a
        self.assert_rejected(lambda: c.save(update_fields=["organization"]))   # записалось бы А + касса Б

    def test_clean_reads_the_database(self):
        c = AccountCorrection(organization=self.org_a, account=self.cash_b, actual_balance=D("500"))
        with self.assertRaises(ValidationError) as caught:
            c.full_clean()
        self.assertIn("account", caught.exception.message_dict)


class ReverseAndLegacyTests(CorrectionBase, TestCase):
    def setUp(self):
        self.make_world()

    def test_moving_cash_with_corrections_to_other_organization_is_rejected(self):
        """Обратный путь: у кассы есть корректировка — передать кассу другой
        организации нельзя (как и при платежах)."""
        self.correction()
        cash = Account.objects.get(pk=self.cash_a.pk)
        cash.organization = self.org_b
        self.assert_rejected(cash.save, "есть платежи или корректировки")
        with self.assertRaises(ValidationError):
            cash.full_clean()

    def test_bulk_move_of_cash_with_corrections_is_rejected(self):
        self.correction()
        self.assert_rejected(lambda: Account.objects.filter(pk=self.cash_a.pk).update(organization=self.org_b),
                             "есть платежи или корректировки")

    def test_bulk_changes_of_corrections_are_checked(self):
        c = self.correction()
        rows = AccountCorrection.objects.filter(pk=c.pk)
        self.assert_rejected(lambda: rows.update(account=self.cash_b))
        self.assert_rejected(lambda: rows.update(organization_id=self.org_b.pk))
        c.account = self.cash_b
        self.assert_rejected(lambda: AccountCorrection.objects.bulk_update([c], ["account"]))
        self.assert_rejected(lambda: AccountCorrection.objects.bulk_create([
            AccountCorrection(organization=self.org_a, account=self.cash_b, number="X-1")]))
        rows.update(comment="можно")                                      # прочие поля — без ограничений
        rows.update(account=self.cash_b, organization=self.org_b)          # совместимая пара — можно
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).account_id, self.cash_b.pk)

    def test_old_violation_is_shown_and_can_be_unposted_deleted_or_fixed(self):
        posted = self.legacy(self.correction(post=True), account_id=self.cash_b.pk)
        self.assertEqual(self.balances(), (D("50.00"), D("550.00")))       # 450 попали в кассу Б
        out = StringIO()
        call_command("check_payments", stdout=out)
        self.assertIn("Корректировки остатков с кассой или счётом другой организации "
                      "(не исправляются автоматически): 1", out.getvalue())
        self.assertIn(f"корректировка остатка № {posted.number}", out.getvalue())
        self.assertEqual(AccountCorrection.objects.get(pk=posted.pk).account_id, self.cash_b.pk)   # не изменена

        self.assert_rejected(lambda: AccountCorrection.objects.get(pk=posted.pk).save())   # прочее — нельзя
        AccountCorrection.objects.get(pk=posted.pk).unpost()               # снять с проведения — можно
        self.assertEqual(self.balances(), (D("50.00"), D("100.00")))
        fixed = AccountCorrection.objects.get(pk=posted.pk)
        fixed.account = self.cash_a                                        # исправить связь — можно
        fixed.save()
        fixed.recalc()
        fixed.save(update_fields=["balance_before", "amount"])
        fixed.post()
        self.assertEqual(self.balances(), (D("500.00"), D("100.00")))
        self.assertNotIn("Корректировки остатков", self.check_output())

        other = self.legacy(self.correction(post=True), account_id=self.cash_b.pk)
        AccountCorrection.objects.get(pk=other.pk).delete()                # удалить — можно
        self.assertEqual(self.balances(), (D("500.00"), D("100.00")))

    def check_output(self):
        out = StringIO()
        call_command("check_payments", stdout=out)
        return out.getvalue()


class FormAndButtonTests(CorrectionBase, TestCase):
    def setUp(self):
        self.make_world()
        self.login()

    def test_create_form_rejects_foreign_cash_without_changes(self):
        before = self.state()
        response = self.client.post(reverse("cash_correction_create"),
                                    self.form_data(self.org_a, self.cash_b, action="save_post"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("account", response.context["form"].errors)
        self.assertContains(response, "принадлежит организации")
        self.assertEqual(self.state(), before)

    def test_edit_form_rejects_foreign_cash_without_changes(self):
        c = self.correction(post=True)
        before = self.state()
        response = self.client.post(reverse("account_correction_edit", args=[c.pk]),
                                    self.form_data(self.org_a, self.cash_b, actual="900"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("account", response.context["form"].errors)
        self.assertEqual(self.state(), before)

    def test_create_form_accepts_own_cash(self):
        response = self.client.post(reverse("cash_correction_create"),
                                    self.form_data(self.org_a, self.cash_a, action="save_post"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.balances(), (D("500.00"), D("100.00")))

    def test_post_button_rejects_old_incompatible_draft(self):
        old = self.legacy(self.correction(), account_id=self.cash_b.pk)
        before = self.state()
        response = self.client.post(reverse("account_correction_post", args=[old.pk]), follow=True)
        self.assertContains(response, "Не удалось провести")
        self.assertContains(response, "принадлежит организации")
        self.assertEqual(self.state(), before)

    def test_unpost_button_and_edit_form_fix_old_violation(self):
        old = self.legacy(self.correction(post=True), account_id=self.cash_b.pk)
        self.client.post(reverse("account_correction_unpost", args=[old.pk]))
        self.assertEqual(AccountCorrection.objects.get(pk=old.pk).status, "draft")
        response = self.client.post(reverse("account_correction_edit", args=[old.pk]),
                                    self.form_data(self.org_a, self.cash_a, action="save_post"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.balances(), (D("500.00"), D("100.00")))

    def test_account_form_and_admin_reject_moving_cash_with_corrections(self):
        self.correction()
        before = self.state()
        response = self.client.post(reverse("account_edit", args=[self.cash_a.pk]), {
            "organization": self.org_b.pk, "name": "Касса А", "kind": "cash", "bank_account": "",
            "opening_balance": "50", "is_default": "on", "is_active": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "есть платежи или корректировки")
        superuser = get_user_model().objects.create_superuser("root", password="pass12345")
        self.client.force_login(superuser)
        response = self.client.post(reverse("admin:finance_account_change", args=[self.cash_a.pk]), {
            "organization": self.org_b.pk, "name": "Касса А", "kind": "cash", "bank_account": "",
            "opening_balance": "50", "is_default": "on", "is_active": "on", "_save": "Сохранить"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "есть платежи или корректировки")
        self.assertEqual(self.state(), before)

    def test_corrections_are_not_editable_in_admin(self):
        """Админка корректировок не подключена — пути записи через неё нет."""
        self.assertNotIn(AccountCorrection, admin.site._registry)


class RecalcBehaviourTests(CorrectionBase, TestCase):
    """ПОВЕДЕНИЕ ИЗМЕНЕНО решением владельца (правило «остаток на момент»,
    docs/account-correction-proposal.md). Раньше здесь было закреплено:
    разница считалась при сохранении формы по всем платежам без учёта дат,
    кнопка «Провести» её не пересчитывала, а пересохранение формы считало
    заново от текущего остатка. Теперь разница — факт минус расчётный остаток
    по документам раньше момента корректировки; она пересчитывается при
    изменении этой истории, а не при пересохранении."""

    def setUp(self):
        self.make_world()
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)

    def pay(self, when, amount=30):
        import datetime

        return Payment.objects.create(kind=Payment.KIND_IN, organization=self.org_a, account=self.cash_a,
                                      counterparty=self.customer, amount=amount, status="posted",
                                      date=datetime.date(2026, 10, 14), time=datetime.time(*when))

    def at_six(self, actual="500"):
        import datetime

        c = AccountCorrection(organization=self.org_a, account=self.cash_a, actual_balance=D(actual),
                              date=datetime.date(2026, 10, 14), time=datetime.time(18, 0))
        c.save()
        return c

    def test_payment_entered_between_saving_and_posting(self):
        """Было: разница 450 зафиксирована при сохранении черновика, платёж
        17:00, внесённый до проведения, не учитывался — остаток 530. Теперь
        при проведении — по свежим данным: +420, остаток 500."""
        c = self.at_six()
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).amount, D("450.00"))   # предварительно
        self.pay((17, 0))
        AccountCorrection.objects.get(pk=c.pk).post()
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).amount, D("420.00"))
        self.assertEqual(self.balances()[0], D("500.00"))

    def test_payment_dated_after_the_correction_is_not_counted(self):
        """Было: платёж, датированный позже корректировки, входил в её
        разницу. Теперь — нет: расчётный 50, разница +450, остаток 530."""
        self.pay((19, 0))
        c = self.at_six()
        c.post()
        stored = AccountCorrection.objects.get(pk=c.pk)
        self.assertEqual((stored.balance_before, stored.amount), (D("50.00"), D("450.00")))
        self.assertEqual(self.balances()[0], D("530.00"))

    def test_saving_through_form_does_not_change_money(self):
        """Было: пересохранение формы считало разницу заново от текущего
        остатка (поздний платёж «поглощался»). Теперь без изменения истории
        до момента корректировки денежный результат не меняется."""
        import datetime

        self.login()
        data = dict(self.form_data(self.org_a, self.cash_a, action="save_post"), date="2026-10-14", time="18:00")
        self.client.post(reverse("cash_correction_create"), data)
        c = AccountCorrection.objects.get()
        self.pay((19, 0))
        before = self.state()
        data.update(action="save", comment="поправлен комментарий")
        self.client.post(reverse("account_correction_edit", args=[c.pk]), data)
        self.assertEqual(self.state(), before)
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).comment, "поправлен комментарий")
        self.assertEqual(AccountCorrection.objects.get(pk=c.pk).date, datetime.date(2026, 10, 14))
        self.assertEqual(self.balances()[0], D("530.00"))


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentCorrectionTests(CorrectionBase, ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.make_world()

    def move_cash(self):
        cash = Account.objects.get(pk=self.cash_a.pk)
        cash.organization = self.org_b
        cash.save()

    def save_correction(self):
        c = AccountCorrection(organization=self.org_a, account=Account.objects.get(pk=self.cash_a.pk),
                              actual_balance=D("500"))
        c.save()

    def test_correction_first_then_moving_cash_waits_and_is_rejected(self):
        results = self.run_overlapping(self.save_correction, self.move_cash,
                                       pause_on=(services, "correction_link_errors"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], ValidationError)
        self.assertEqual(Account.objects.get(pk=self.cash_a.pk).organization_id, self.org_a.pk)
        self.assertEqual(services.incompatible_corrections(), [])

    def test_moving_cash_first_then_correction_waits_and_is_rejected(self):
        results = self.run_overlapping(self.move_cash, self.save_correction,
                                       pause_on=(Account, "_organization_change_error"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], PaymentError)
        self.assertFalse(AccountCorrection.objects.exists())
        self.assertEqual(services.incompatible_corrections(), [])
