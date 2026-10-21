"""Допустимые суммы платежей.

Подтверждённая ошибка: проведённый входящий платёж на 10 ₽ по счёту меняли
через форму на −20 и нажимали «Сохранить» — сервер отвечал как при успехе,
платёж оставался проведённым, amount = −20, Invoice.paid_amount = −20,
баланс денежного счёта = −20.

Правила: сумма проведённого платежа строго больше нуля; черновик может быть
нулевым; отрицательная сумма недопустима (направление задаёт вид платежа).
"""
from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.contrib.messages import get_messages
from django.db import models, transaction
from django.urls import reverse

from apps.core import roles
from apps.sales.models import Invoice

from .models import Account, Payment
from .services import NEGATIVE_AMOUNT, ZERO_POSTED, PaymentError
from .tests_consistency import FinanceBase, User

D = Decimal


def raw_update(queryset, **values):
    """Запись в обход PaymentQuerySet.update — только чтобы смоделировать
    старые данные, появившиеся до проверки."""
    return models.QuerySet.update(queryset, **values)


class ReportedScenarioTests(FinanceBase):
    def test_posted_payment_changed_to_minus_twenty_is_rejected_entirely(self):
        payment = self.payment(10)
        before = self.state()

        response = self.save_form(payment, amount="-20")

        self.assertEqual(response.status_code, 200)                     # было: перенаправление как при успехе
        self.assertEqual(response.context["form"].errors["amount"], [NEGATIVE_AMOUNT])
        self.assertContains(response, 'value="-20"')                    # введённое значение — для исправления
        self.assertEqual(self.state(), before)
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.status, stored.amount), ("posted", D("10.00")))
        self.assertEqual(self.paid(), D("10.00"))                       # было −20
        self.assertEqual(self.bank.balance, D("10.00"))                 # было −20

    def test_posted_payment_changed_to_zero_is_rejected(self):
        payment = self.payment(10)
        before = self.state()
        response = self.save_form(payment, amount="0")
        self.assertEqual(response.context["form"].errors["amount"], [ZERO_POSTED])
        self.assertEqual(self.state(), before)

    def test_posted_payment_changed_to_valid_amount_is_saved(self):
        payment = self.payment(10)
        self.assertEqual(self.save_form(payment, amount="0.01").status_code, 302)
        self.assertEqual((self.paid(), self.bank.balance), (D("0.01"), D("0.01")))


class NewPaymentTests(FinanceBase):
    def create(self, amount, action="save", kind="in"):
        url = reverse("payment_create_in" if kind == "in" else "payment_create_out")
        data = {
            "date": "2026-01-10", "number": "", "time": "12:00", "organization": self.org.pk,
            "account": self.bank.pk, "counterparty": (self.customer if kind == "in" else self.supplier).pk,
            "amount": amount, "purpose": "", "action": action,
        }
        if kind == "in":
            data["invoice"] = self.invoice.pk
        return self.client.post(url, data)

    def test_negative_new_payment_is_rejected(self):
        for action in ("save", "save_post"):
            response = self.create("-5", action)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["form"].errors["amount"], [NEGATIVE_AMOUNT])
        self.assertFalse(Payment.objects.exists())
        with self.assertRaisesMessage(PaymentError, "не может быть отрицательной"):
            self.payment(-5, post=False)
        self.assertFalse(Payment.objects.exists())

    def test_zero_draft_is_saved_but_cannot_be_posted(self):
        response = self.create("0", "save")
        self.assertEqual(response.status_code, 302)
        draft = Payment.objects.get()
        self.assertEqual((draft.status, draft.amount), ("draft", D("0.00")))

        before = self.state()
        with self.assertRaisesMessage(PaymentError, ZERO_POSTED):
            draft.post()
        response = self.client.post(reverse("payment_post", args=[draft.pk]), follow=True)
        self.assertContains(response, "Платёж не проведён")
        self.assertContains(response, ZERO_POSTED)
        self.assertEqual(self.state(), before)

    def test_zero_with_save_and_post_saves_nothing(self):
        response = self.create("0", "save_post")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["amount"], [ZERO_POSTED])
        self.assertFalse(Payment.objects.exists())

    def test_zero_draft_can_be_filled_in_and_posted(self):
        draft = self.payment(0, post=False)
        self.save_form(draft, amount="40", action="save_post")
        self.assertEqual(self.paid(), D("40.00"))

    def test_positive_incoming_and_outgoing(self):
        self.assertEqual(self.create("150", "save_post", "in").status_code, 302)
        self.assertEqual(self.create("60", "save_post", "out").status_code, 302)
        self.assertEqual(self.bank.balance, D("90.00"))                 # 150 − 60: знак задаёт вид
        self.assertEqual(self.paid(), D("150.00"))
        self.assertEqual(set(Payment.objects.values_list("amount", flat=True)), {D("150.00"), D("60.00")})

    def test_minimal_amount(self):
        self.assertEqual(self.create("0.01", "save_post").status_code, 302)
        self.assertEqual(self.paid(), D("0.01"))
        outgoing = self.payment(D("0.01"), invoice=None, kind=Payment.KIND_OUT, counterparty=self.supplier)
        self.assertTrue(outgoing.is_posted)

    def test_overpayment_is_allowed(self):
        self.payment(100)
        self.assertEqual(self.create("50", "save_post").status_code, 302)
        self.assertEqual(self.paid(), D("150.00"))


class ModelPathTests(FinanceBase):
    def test_direct_save_and_full_clean(self):
        payment = self.payment(10)
        payment.amount = D("-20")
        with self.assertRaises(ValidationError):
            payment.full_clean()
        with self.assertRaisesMessage(PaymentError, "не может быть отрицательной"):
            payment.save()
        self.assertEqual((Payment.objects.get(pk=payment.pk).amount, self.paid()), (D("10.00"), D("10.00")))

    def test_update_fields_checks_what_is_written(self):
        payment = self.payment(10)
        payment.amount = D("-20")
        with self.assertRaises(PaymentError):
            payment.save(update_fields=["amount"])
        # сумма в памяти недопустима, но записывается только назначение — пусть
        payment.purpose = "уточнение"
        payment.save(update_fields=["purpose"])
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.amount, stored.purpose), (D("10.00"), "уточнение"))
        self.assertEqual(self.paid(), D("10.00"))

    def test_posting_checks_stored_amount_not_memory(self):
        """Устаревший объект: в памяти 50, в базе — нулевой черновик."""
        draft = self.payment(50, post=False)
        stale = Payment.objects.get(pk=draft.pk)
        draft.amount = D("0")
        draft.save()
        self.assertEqual(stale.amount, D("50.00"))
        before = self.state()
        with self.assertRaisesMessage(PaymentError, ZERO_POSTED):
            stale.post()                                                # пишет только статус
        self.assertEqual(self.state(), before)

    def test_writing_status_with_stale_positive_amount_is_checked(self):
        draft = self.payment(0, post=False)
        draft.amount = D("30")                     # не сохранено
        with self.assertRaises(PaymentError):
            draft.post()
        draft.save()                               # сохранили сумму — теперь можно
        draft.post()
        self.assertEqual(self.paid(), D("30.00"))

    def test_admin_rejects_negative_amount(self):
        User.objects.create_superuser("root", password="pass12345")
        self.client.login(username="root", password="pass12345")
        payment = self.payment(10)
        before = self.state()
        response = self.client.post(reverse("admin:finance_payment_change", args=[payment.pk]), {
            "kind": payment.kind, "number": payment.number, "date": payment.date.isoformat(),
            "time": payment.time.strftime("%H:%M:%S"), "organization": self.org.pk, "account": self.bank.pk,
            "counterparty": self.customer.pk, "invoice": self.invoice.pk, "amount": "-20", "purpose": "",
            "status": "posted", "_save": "Сохранить",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "не может быть отрицательной")
        self.assertEqual(self.state(), before)


class BulkPathTests(FinanceBase):
    def test_queryset_update_to_negative_is_rejected(self):
        payment = self.payment(10)
        before = self.state()
        with self.assertRaisesMessage(PaymentError, f"платёж id {payment.pk}: Сумма платежа не может быть отрицательной"):
            Payment.objects.filter(pk=payment.pk).update(amount=D("-20"))
        self.assertEqual(self.state(), before)

    def test_queryset_update_with_expression_is_checked_after_writing(self):
        payment = self.payment(10)
        before = self.state()
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=payment.pk).update(amount=models.F("amount") * -1)
        self.assertEqual(self.state(), before)

    def test_posting_zero_payments_in_bulk_is_rejected(self):
        good = self.payment(10, post=False)
        zero = self.payment(0, post=False)
        before = self.state()
        with self.assertRaisesMessage(PaymentError, f"платёж id {zero.pk}: {ZERO_POSTED}"):
            Payment.objects.filter(pk__in=[good.pk, zero.pk]).update(status="posted")
        self.assertEqual(self.state(), before)                        # откат целиком, и хороший не проведён
        Payment.objects.filter(pk=good.pk).update(status="posted")
        self.assertEqual(self.paid(), D("10.00"))

    def test_valid_bulk_update_recomputes(self):
        payment = self.payment(10)
        Payment.objects.filter(pk=payment.pk).update(amount=D("25"))
        self.assertEqual(self.paid(), D("25.00"))

    def test_bulk_update_goes_through_the_same_check(self):
        first, second = self.payment(10), self.payment(20)
        first.amount, second.amount = D("15"), D("-1")
        before = self.state()
        with self.assertRaises(PaymentError):
            with transaction.atomic():      # bulk_update пишет без своей точки сохранения
                Payment.objects.bulk_update([first, second], ["amount"])
        self.assertEqual(self.state(), before)
        second.amount = D("5")
        Payment.objects.bulk_update([first, second], ["amount"])
        self.assertEqual(self.paid(), D("20.00"))

    def test_bulk_update_moving_payments_between_invoices(self):
        first = self.payment(10)
        first.invoice = self.invoice2
        Payment.objects.bulk_update([first], ["invoice"])
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("0.00"), D("10.00")))

    def test_bulk_create_is_checked(self):
        objects = [
            Payment(kind=Payment.KIND_IN, organization=self.org, account=self.bank, counterparty=self.customer,
                    invoice=self.invoice, amount=D("30"), status="posted", number="B1"),
            Payment(kind=Payment.KIND_IN, organization=self.org, account=self.bank, counterparty=self.customer,
                    invoice=self.invoice, amount=D("0"), status="posted", number="B2"),
        ]
        with self.assertRaisesMessage(PaymentError, ZERO_POSTED):
            Payment.objects.bulk_create(objects)
        self.assertFalse(Payment.objects.exists())
        objects[1].status = "draft"
        Payment.objects.bulk_create(objects)
        self.assertEqual(self.paid(), D("30.00"))


class OldInvalidPaymentTests(FinanceBase):
    """Платёж с отрицательной суммой, появившийся до проверки."""

    def make_old(self, amount="-20", posted=True):
        payment = self.payment(10, post=posted)
        raw_update(Payment.objects.filter(pk=payment.pk), amount=D(amount))
        return Payment.objects.get(pk=payment.pk)

    def test_unpost_through_button_and_model(self):
        payment = self.make_old()
        self.client.post(reverse("payment_unpost", args=[payment.pk]))
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.status, stored.amount), ("draft", D("-20.00")))   # сумма не тронута
        self.assertEqual(stored.kind, Payment.KIND_IN)                              # вид не изменён

    def test_delete_through_button_and_bulk(self):
        first, second = self.make_old(), self.make_old("-5")
        self.client.post(reverse("payment_delete", args=[first.pk]))
        self.client.post(reverse("payment_bulk_delete"), {"ids": [second.pk]})
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.paid(), D("0.00"))

    def test_fix_to_valid_amount(self):
        payment = self.make_old()
        response = self.save_form(payment, amount="20")
        self.assertEqual(response.status_code, 302)
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.status, stored.amount, stored.kind), ("posted", D("20.00"), Payment.KIND_IN))
        self.assertEqual(self.paid(), D("20.00"))

    def test_cannot_keep_negative_while_editing_or_posting(self):
        payment = self.make_old()
        before = self.state()
        response = self.save_form(payment, purpose="правка")            # сумма остаётся −20, платёж проведён
        self.assertEqual(response.context["form"].errors["amount"], [NEGATIVE_AMOUNT])
        self.assertEqual(self.state(), before)
        payment.unpost()
        with self.assertRaises(PaymentError):
            Payment.objects.get(pk=payment.pk).post()

    def test_old_negative_draft_can_be_unposted_through_admin_status(self):
        """Снятие с проведения полной записью (как в админке), сумма прежняя."""
        payment = self.make_old()
        payment.status = "draft"
        payment.save()
        self.assertEqual(Payment.objects.get(pk=payment.pk).status, "draft")

    def test_diagnostics_lists_and_writes_nothing(self):
        old = self.make_old()
        zero = self.payment(0, post=False)
        raw_update(Payment.objects.filter(pk=zero.pk), status="posted")      # проведённый ноль из старых данных
        draft_zero = self.payment(0, post=False)                            # обычный нулевой черновик
        before = self.state()
        output = self.run_command()
        self.assertIn("Платежи с недопустимой суммой (не исправляются автоматически): 2", output)
        self.assertIn(f"(id {old.pk}, проведён): отрицательная сумма -20.00", output)
        self.assertIn(f"(id {zero.pk}, проведён): проведён с нулевой суммой 0.00", output)
        self.assertNotIn(f"(id {draft_zero.pk}", output)
        self.assertIn("недопустимых сумм — 2. Ничего не изменено", output)
        self.assertEqual(self.state(), before)
        self.run_command("--fix-paid-amount", "--i-have-a-backup")          # пересчёт оплаты суммы не трогает
        self.assertEqual(Payment.objects.get(pk=old.pk).amount, D("-20.00"))


class PaymentFromInvoiceTests(FinanceBase):
    def test_fully_paid_invoice_gives_message_instead_of_zero_payment(self):
        self.payment(100)
        response = self.client.post(reverse("invoice_to_payment", args=[self.invoice.pk]))
        self.assertRedirects(response, reverse("invoice_edit", args=[self.invoice.pk]), fetch_redirect_response=False)
        texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("уже оплачен полностью — неоплаченного остатка нет" in text for text in texts), texts)
        self.assertEqual(Payment.objects.count(), 1)
        self.client.post(reverse("invoice_to_cash_order", args=[self.invoice.pk]))
        self.assertEqual(Payment.objects.count(), 1)

    def test_overpaid_invoice_also_gives_message(self):
        self.payment(130)
        self.client.post(reverse("invoice_to_payment", args=[self.invoice.pk]))
        self.assertEqual(Payment.objects.count(), 1)

    def test_partly_paid_invoice_gives_remaining_amount(self):
        self.payment(30)
        self.client.post(reverse("invoice_to_payment", args=[self.invoice.pk]))
        self.assertEqual(Payment.objects.filter(status="draft").get().amount, D("70.00"))

    def test_manual_overpayment_still_possible(self):
        self.payment(100)
        extra = self.payment(25)
        self.assertTrue(extra.is_posted)
        self.assertEqual(self.paid(), D("125.00"))


class PermissionTests(FinanceBase):
    def test_storekeeper_cannot_change_amount(self):
        payment = self.payment(10)
        user = User.objects.create_user("store", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_STOREKEEPER))
        self.client.login(username="store", password="pass12345")
        before = self.state()
        self.assertEqual(self.save_form(payment, amount="20").status_code, 403)
        self.assertEqual(self.save_form(payment, amount="-20").status_code, 403)
        self.assertEqual(self.state(), before)

    def test_recalculation_still_follows_valid_changes(self):
        payment = self.payment(10)
        self.save_form(payment, amount="20", invoice=self.invoice2.pk)
        self.assertEqual((self.paid(), self.paid(self.invoice2)), (D("0.00"), D("20.00")))
        self.assertEqual(Invoice.objects.get(pk=self.invoice2.pk).payment_status, "partial")
        self.assertEqual(Account.objects.get(pk=self.bank.pk).balance, D("20.00"))
