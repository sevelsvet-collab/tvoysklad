"""Взаиморасчёты: назначение платежей, сторона корректировок, расчёт по
парам «организация + контрагент» (apps/finance/settlements.py).

Сценарии независимой модели docs/proposals/settlements_model.py перенесены
на настоящие документы; ожидаемые суммы посчитаны вручную и записаны в
комментариях. Правила — docs/settlements-proposal.md.
"""
from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, connection, models, transaction
from django.db.models import F, Value
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization, Warehouse
from apps.inventory.models import StockAdjustment
from apps.partners.documents import counterparty_documents
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Invoice, Shipment

from . import services, settlements
from .models import Account, Payment, SettlementCorrection
from .services import PaymentError, UnsupportedBulkChange

User = get_user_model()
D = Decimal
POSTED = "posted"

CUSTOMER_PAYMENT = Payment.PURPOSE_CUSTOMER_PAYMENT
CUSTOMER_REFUND = Payment.PURPOSE_CUSTOMER_REFUND
SUPPLIER_PAYMENT = Payment.PURPOSE_SUPPLIER_PAYMENT
SUPPLIER_REFUND = Payment.PURPOSE_SUPPLIER_REFUND
UNALLOCATED = Payment.PURPOSE_UNALLOCATED
THEY_OWE, WE_OWE = SettlementCorrection.DIR_THEY_OWE, SettlementCorrection.DIR_WE_OWE
SALES, PURCHASES = SettlementCorrection.SIDE_SALES, SettlementCorrection.SIDE_PURCHASES
NO_SIDE = SettlementCorrection.SIDE_UNALLOCATED


class World:
    """Настоящие документы обычными методами программы."""

    def make_world(self):
        self.org_a = Organization.objects.create(name="Организация А", is_default=True)
        self.org_b = Organization.objects.create(name="Организация Б")
        self.bank = {
            self.org_a.pk: Account.objects.create(organization=self.org_a, name="Р/с А", is_default=True),
            self.org_b.pk: Account.objects.create(organization=self.org_b, name="Р/с Б", is_default=True),
        }
        self.wh = Warehouse.objects.create(name="Склад", is_default=True)
        self.product = Product.objects.create(name="Товар", unit=Unit.objects.create(name="шт"), purchase_price=10)
        for org in (self.org_a, self.org_b):
            stock = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=org,
                                                   warehouse=self.wh)
            stock.lines.create(product=self.product, quantity=10000, price=10)
            stock.post()                    # запас — оприходованием, не закупкой

    def party(self, name, kind=Counterparty.TYPE_BOTH):
        return Counterparty.objects.create(name=name, partner_type=kind)

    def invoice(self, cp, amount, org=None):
        doc = Invoice.objects.create(organization=org or self.org_a, warehouse=self.wh, customer=cp)
        doc.lines.create(product=self.product, quantity=1, price=amount, vat_rate="none")
        doc.post()
        return doc

    def ship(self, cp, amount, invoice=None, org=None):
        doc = Shipment.objects.create(organization=org or self.org_a, warehouse=self.wh, customer=cp,
                                      invoice=invoice)
        doc.lines.create(product=self.product, quantity=1, price=amount, vat_rate="none")
        doc.post()
        return doc

    def customer_return(self, cp, amount, org=None):
        doc = CustomerReturn.objects.create(organization=org or self.org_a, warehouse=self.wh, customer=cp)
        doc.lines.create(product=self.product, quantity=1, price=amount, vat_rate="none")
        doc.post()
        return doc

    def receive(self, cp, amount, org=None):
        doc = Receipt.objects.create(organization=org or self.org_a, warehouse=self.wh, supplier=cp)
        doc.lines.create(product=self.product, quantity=1, price=amount)
        doc.post()
        return doc

    def supplier_return(self, cp, amount, basis, org=None):
        doc = SupplierReturn.objects.create(organization=org or self.org_a, warehouse=self.wh, supplier=cp,
                                            receipt=basis)
        doc.lines.create(product=self.product, quantity=1, price=amount)
        doc.post()
        return doc

    def pay(self, cp, kind, amount, purpose="", org=None, invoice=None, post=True):
        org = org or self.org_a
        return Payment.objects.create(kind=kind, organization=org, account=self.bank[org.pk], counterparty=cp,
                                      invoice=invoice, amount=amount, settlement_purpose=purpose,
                                      status=POSTED if post else "draft")

    def pay_in(self, cp, amount, purpose="", **extra):
        return self.pay(cp, Payment.KIND_IN, amount, purpose, **extra)

    def pay_out(self, cp, amount, purpose="", **extra):
        return self.pay(cp, Payment.KIND_OUT, amount, purpose, **extra)

    def correct(self, cp, direction, amount, side=NO_SIDE, org=None):
        doc = SettlementCorrection.objects.create(organization=org or self.org_a, counterparty=cp,
                                                  direction=direction, side=side, amount=amount)
        doc.post()
        return doc

    # ---------- чтение ----------

    def pair(self, cp, org=None):
        org = org or self.org_a
        found = [p for p in settlements.settlement_pairs(counterparty=cp) if p.organization == org]
        self.assertEqual(len(found), 1, f"пара {org} + {cp}")
        return found[0]

    def sides(self, cp, org=None):
        """(по продажам, по закупкам, справочная разница) — как sides() модели."""
        item = self.pair(cp, org)
        return item.sales, item.purchases, item.difference

    def assertSides(self, cp, expected, org=None, final=True):
        item = self.pair(cp, org)
        self.assertEqual((item.sales, item.purchases, item.difference), tuple(D(str(v)) for v in expected))
        self.assertEqual(item.final, final)
        return item

    def login_accountant(self):
        user = User.objects.create_user("acc", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.login(username="acc", password="pass12345")


class SettlementsBase(World, TestCase):
    def setUp(self):
        self.make_world()
        self.cp = self.party("Контрагент")


# ---------------------------------------------------------------------------
# Сценарии независимой модели — на настоящих документах
# ---------------------------------------------------------------------------

class ModelScenarioTests(SettlementsBase):
    def test_same_incoming_different_purpose(self):
        """Отгрузили 1000; приняли 500, уплатили поставщику 800. Пришло 300."""
        as_payment, as_refund = self.cp, self.party("Второй")
        for cp, purpose in ((as_payment, CUSTOMER_PAYMENT), (as_refund, SUPPLIER_REFUND)):
            self.ship(cp, 1000)
            self.receive(cp, 500)
            self.pay_out(cp, 800, SUPPLIER_PAYMENT)
            self.pay_in(cp, 300, purpose)
        # оплата продажи: 1000 − 300 = 700; закупки 500 − 800 = −300 (аванс поставщику); разница 1000
        self.assertSides(as_payment, (700, -300, 1000))
        # возврат аванса поставщиком: продажи 1000; закупки 500 − 800 + 300 = 0; разница 1000
        self.assertSides(as_refund, (1000, 0, 1000))

    def test_same_outgoing_different_purpose(self):
        """Отгрузили 1000, покупатель заплатил 1200; приняли 500. Ушло 200."""
        to_supplier, to_customer = self.cp, self.party("Второй")
        for cp, purpose in ((to_supplier, SUPPLIER_PAYMENT), (to_customer, CUSTOMER_REFUND)):
            self.ship(cp, 1000)
            self.pay_in(cp, 1200, CUSTOMER_PAYMENT)
            self.receive(cp, 500)
            self.pay_out(cp, 200, purpose)
        # оплата поставщику: продажи 1000 − 1200 = −200 (переплата); закупки 500 − 200 = 300; разница −500
        self.assertSides(to_supplier, (-200, 300, -500))
        # возврат покупателю: продажи 1000 − 1200 + 200 = 0; закупки 500; разница −500
        self.assertSides(to_customer, (0, 500, -500))

    def test_first_purchase_and_its_cancellation_keep_old_purposes(self):
        """Отгрузили 1000, оплачено 1000, возврат товара 300, вернули деньги 300;
        затем первая закупка 500 и её отмена."""
        self.ship(self.cp, 1000)
        paid = self.pay_in(self.cp, 1000, CUSTOMER_PAYMENT)
        self.customer_return(self.cp, 300)
        refund = self.pay_out(self.cp, 300, CUSTOMER_REFUND)
        self.assertTrue(self.assertSides(self.cp, (0, 0, 0)).closed)
        receipt = self.receive(self.cp, 500)
        # возврат денег покупателю остаётся возвратом: 0; закупки 500; разница −500
        self.assertSides(self.cp, (0, 500, -500))
        receipt.unpost()
        self.assertSides(self.cp, (0, 0, 0))
        stored = dict(Payment.objects.filter(pk__in=[paid.pk, refund.pk]).values_list("pk", "settlement_purpose"))
        self.assertEqual(stored, {paid.pk: CUSTOMER_PAYMENT, refund.pk: CUSTOMER_REFUND})

    def test_first_sale_keeps_old_supplier_refund(self):
        """Приняли 1000, оплатили 1000, вернули товар на 200, поставщик вернул 200;
        затем первая продажа ему на 300."""
        receipt = self.receive(self.cp, 1000)
        self.pay_out(self.cp, 1000, SUPPLIER_PAYMENT)
        self.supplier_return(self.cp, 200, receipt)
        refund = self.pay_in(self.cp, 200, SUPPLIER_REFUND)
        self.ship(self.cp, 300)
        # закупки 1000 − 200 − 1000 + 200 = 0; продажи 300
        self.assertSides(self.cp, (300, 0, 300))
        self.assertEqual(Payment.objects.get(pk=refund.pk).settlement_purpose, SUPPLIER_REFUND)

    def test_organizations_do_not_offset(self):
        """Организация А отгрузила 1000; покупатель заплатил 1000 организации Б."""
        self.ship(self.cp, 1000, org=self.org_a)
        self.pay_in(self.cp, 1000, CUSTOMER_PAYMENT, org=self.org_b)
        self.assertSides(self.cp, (1000, 0, 1000), org=self.org_a)
        self.assertSides(self.cp, (-1000, 0, -1000), org=self.org_b)
        totals = settlements.totals(settlements.settlement_pairs())
        # нам должны 1000 (А) и мы должны 1000 (Б) — отдельно, не ноль
        self.assertEqual((totals["customers_owe"], totals["customer_advances"]), (D("1000"), D("1000")))

    def test_same_difference_different_debts(self):
        simple, crossed = self.cp, self.party("Встречный")
        self.ship(simple, 300)
        self.ship(crossed, 1000)
        self.receive(crossed, 700)
        self.assertSides(simple, (300, 0, 300))
        item = self.assertSides(crossed, (1000, 700, 300))
        self.assertIn("взаимозачёт автоматически не выполняется", " ".join(item.explanations))

    def test_unallocated_money_is_visible_until_clarified(self):
        self.ship(self.cp, 1000)
        self.receive(self.cp, 700)
        payment = self.pay_in(self.cp, 400)                 # без счёта — «не распределено»
        self.assertEqual(payment.settlement_purpose, UNALLOCATED)
        item = self.assertSides(self.cp, (1000, 700, -100), final=False)
        self.assertEqual((item.unallocated_in, len(item.unallocated_docs)), (D("400"), 1))
        payment.settlement_purpose = CUSTOMER_PAYMENT       # человек уточнил назначение
        payment.save()
        self.assertSides(self.cp, (600, 700, -100))         # разница та же, стороны окончательные

    def test_offset_is_not_a_balance_correction(self):
        self.ship(self.cp, 1000)
        self.receive(self.cp, 700)
        self.assertSides(self.cp, (1000, 700, 300))
        wrong = self.correct(self.cp, WE_OWE, 700)          # без стороны: меняет разницу
        self.assertSides(self.cp, (1000, 700, -400), final=False)
        wrong.unpost()
        self.correct(self.cp, WE_OWE, 700, side=SALES)      # долг покупателя −700
        self.correct(self.cp, THEY_OWE, 700, side=PURCHASES)  # наш долг −700
        self.assertSides(self.cp, (300, 0, 300))

    def test_corrections_need_a_side(self):
        on_sales, on_purchases, legacy = self.cp, self.party("Второй"), self.party("Третий")
        for cp, side in ((on_sales, SALES), (on_purchases, PURCHASES), (legacy, NO_SIDE)):
            self.ship(cp, 1000)
            self.receive(cp, 700)
            self.correct(cp, THEY_OWE, 100, side=side)
        self.assertSides(on_sales, (1100, 700, 400))
        self.assertSides(on_purchases, (1000, 600, 400))
        item = self.assertSides(legacy, (1000, 700, 400), final=False)
        self.assertEqual(item.unsided, D("100"))

    def test_two_unsided_corrections_with_zero_sum_stay_preliminary(self):
        """Регрессия дефекта модели: «он должен 100» и «мы должны 100» без
        стороны дают 0, но остаются двумя неразобранными документами."""
        self.ship(self.cp, 1000)
        first = self.correct(self.cp, THEY_OWE, 100)
        second = self.correct(self.cp, WE_OWE, 100)
        item = self.assertSides(self.cp, (1000, 0, 1000), final=False)
        self.assertEqual(item.unsided, D("0"))
        self.assertEqual([doc["pk"] for doc in item.unsided_corrections], [first.pk, second.pk])
        self.assertEqual(item.status, "Предварительно")
        self.login_accountant()
        response = self.client.get(reverse("settlements"))
        self.assertContains(response, f"Корректировка взаиморасчётов № {first.number}")
        self.assertContains(response, f"Корректировка взаиморасчётов № {second.number}")

    def test_zero_sum_unallocated_money_stays_preliminary(self):
        self.pay_in(self.cp, 50)
        self.pay_out(self.cp, 50)
        item = self.assertSides(self.cp, (0, 0, 0), final=False)
        self.assertEqual(len(item.unallocated_payments), 2)
        self.assertNotEqual(item.status, "Задолженностей нет")


# ---------------------------------------------------------------------------
# Счета, отгрузки, авансы и возвраты
# ---------------------------------------------------------------------------

class DocumentScenarioTests(SettlementsBase):
    def test_invoice_without_shipment_creates_no_debt(self):
        invoice = self.invoice(self.cp, 1000)
        item = self.assertSides(self.cp, (0, 0, 0))
        self.assertEqual((item.invoiced, item.invoice_paid, item.invoice_shipped), (D("1000"), D("0"), D("0")))
        self.assertIn("Счёт сам по себе задолженность не создаёт", " ".join(item.explanations))
        self.pay_in(self.cp, 400, invoice=invoice)          # оплата по счёту без отгрузки — аванс
        item = self.assertSides(self.cp, (-400, 0, -400))
        self.assertEqual(item.sales_label, "наш долг покупателю: аванс, переплата")
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).paid_amount, D("400.00"))

    def test_shipment_without_invoice(self):
        self.ship(self.cp, 700)
        item = self.assertSides(self.cp, (700, 0, 700))
        self.assertEqual(item.invoiced, D("0"))

    def test_partial_shipment_of_paid_invoice(self):
        invoice = self.invoice(self.cp, 1000)
        self.ship(self.cp, 600, invoice=invoice)
        self.pay_in(self.cp, 1000, invoice=invoice)
        # продажи 600 − 1000 = −400: аванс за неотгруженную часть
        item = self.assertSides(self.cp, (-400, 0, -400))
        self.assertEqual((item.invoiced, item.invoice_paid, item.invoice_shipped),
                         (D("1000"), D("1000"), D("600")))

    def test_customer_advance_shipment_and_refund(self):
        self.pay_in(self.cp, 500, CUSTOMER_PAYMENT)         # аванс
        self.assertSides(self.cp, (-500, 0, -500))
        self.ship(self.cp, 300)
        self.assertSides(self.cp, (-200, 0, -200))
        self.pay_out(self.cp, 200, CUSTOMER_REFUND)         # вернули остаток аванса
        self.assertTrue(self.assertSides(self.cp, (0, 0, 0)).closed)

    def test_supplier_advance_receipt_and_refund(self):
        self.pay_out(self.cp, 800, SUPPLIER_PAYMENT)        # аванс поставщику
        item = self.assertSides(self.cp, (0, -800, 800))
        self.assertEqual(item.purchases_label, "долг поставщика нам: аванс, переплата")
        self.receive(self.cp, 500)
        self.assertSides(self.cp, (0, -300, 300))
        self.pay_in(self.cp, 300, SUPPLIER_REFUND)          # поставщик вернул остаток
        self.assertSides(self.cp, (0, 0, 0))

    def test_customer_return_and_supplier_return(self):
        self.ship(self.cp, 1000)
        self.customer_return(self.cp, 250)
        receipt = self.receive(self.cp, 600)
        self.supplier_return(self.cp, 100, receipt)
        self.assertSides(self.cp, (750, 500, 250))

    def test_two_organizations_and_mixed_customer_supplier(self):
        """Покупатель-поставщик: в А продаём 1000 и покупаем 400, в Б покупаем
        600 и оплачиваем 600."""
        self.ship(self.cp, 1000, org=self.org_a)
        self.receive(self.cp, 400, org=self.org_a)
        self.receive(self.cp, 600, org=self.org_b)
        self.pay_out(self.cp, 600, SUPPLIER_PAYMENT, org=self.org_b)
        a = self.assertSides(self.cp, (1000, 400, 600), org=self.org_a)
        self.assertTrue(self.assertSides(self.cp, (0, 0, 0), org=self.org_b).closed)
        self.assertIn("встречные", " ".join(a.explanations))
        totals = settlements.totals(settlements.settlement_pairs())
        self.assertEqual((totals["customers_owe"], totals["we_owe_suppliers"]), (D("1000"), D("400")))

    def test_zero_difference_with_counter_debts_is_not_called_paid(self):
        self.ship(self.cp, 1000)
        self.receive(self.cp, 1000)
        item = self.assertSides(self.cp, (1000, 1000, 0))
        self.assertEqual(item.status, "Окончательно")
        self.assertFalse(item.closed)
        notes = " ".join(item.explanations)
        self.assertIn("Справочная разница равна нулю, но расчёты не закрыты", notes)
        self.assertNotIn("Задолженностей нет", notes)

    def test_draft_and_unposted_documents_do_not_count(self):
        shipment = self.ship(self.cp, 1000)
        payment = self.pay_in(self.cp, 300, CUSTOMER_PAYMENT)
        self.pay_in(self.cp, 999, CUSTOMER_PAYMENT, post=False)    # черновик
        self.assertSides(self.cp, (700, 0, 700))
        payment.unpost()
        self.assertSides(self.cp, (1000, 0, 1000))
        payment.post()                                              # повторное проведение не удваивает
        self.assertSides(self.cp, (700, 0, 700))
        shipment.unpost()
        self.assertSides(self.cp, (-300, 0, -300))

    def test_repeated_calculation_is_the_same(self):
        self.ship(self.cp, 1000)
        self.pay_in(self.cp, 400)
        self.correct(self.cp, THEY_OWE, 50, side=SALES)
        first = [(p.sales, p.purchases, p.difference, p.final) for p in settlements.settlement_pairs()]
        second = [(p.sales, p.purchases, p.difference, p.final) for p in settlements.settlement_pairs()]
        self.assertEqual(first, second)
        self.assertEqual(first, [(D("1050"), D("0"), D("650"), False)])

    def test_cash_correction_is_not_a_settlement(self):
        from .models import AccountCorrection

        self.ship(self.cp, 1000)
        AccountCorrection.objects.create(organization=self.org_a, account=self.bank[self.org_a.pk],
                                         actual_balance=5000, status=POSTED)
        self.assertSides(self.cp, (1000, 0, 1000))


# ---------------------------------------------------------------------------
# Назначение платежа: модель, форма, массовые пути
# ---------------------------------------------------------------------------

class PurposeRuleTests(SettlementsBase):
    def setUp(self):
        super().setUp()
        self.invoice_doc = self.invoice(self.cp, 1000)

    def state(self):
        return list(Payment.objects.order_by("pk").values_list(
            "pk", "kind", "status", "amount", "invoice_id", "settlement_purpose"))

    def test_not_specified_is_filled_from_invoice(self):
        by_invoice = self.pay_in(self.cp, 100, invoice=self.invoice_doc)
        plain_in = self.pay_in(self.cp, 100)
        plain_out = self.pay_out(self.cp, 100)
        self.assertEqual([p.settlement_purpose for p in (by_invoice, plain_in, plain_out)],
                         [CUSTOMER_PAYMENT, UNALLOCATED, UNALLOCATED])
        stored = Payment.objects.filter(pk__in=[by_invoice.pk, plain_in.pk, plain_out.pk]).order_by("pk")
        self.assertEqual([p.settlement_purpose for p in stored], [CUSTOMER_PAYMENT, UNALLOCATED, UNALLOCATED])

    def test_purpose_must_match_direction(self):
        before = self.state()
        for kind, purpose in ((Payment.KIND_IN, SUPPLIER_PAYMENT), (Payment.KIND_IN, CUSTOMER_REFUND),
                              (Payment.KIND_OUT, CUSTOMER_PAYMENT), (Payment.KIND_OUT, SUPPLIER_REFUND)):
            with self.subTest(kind=kind, purpose=purpose):
                with self.assertRaisesMessage(PaymentError, "такое назначение бывает только у"):
                    self.pay(self.cp, kind, 100, purpose)
        self.assertEqual(self.state(), before)
        for kind, purpose in ((Payment.KIND_IN, SUPPLIER_REFUND), (Payment.KIND_OUT, SUPPLIER_PAYMENT),
                              (Payment.KIND_OUT, CUSTOMER_REFUND), (Payment.KIND_IN, UNALLOCATED)):
            self.assertEqual(self.pay(self.cp, kind, 100, purpose).settlement_purpose, purpose)

    def test_invoice_payment_is_only_customer_payment(self):
        before = self.state()
        for purpose in (UNALLOCATED, SUPPLIER_REFUND):
            with self.subTest(purpose=purpose):
                with self.assertRaisesMessage(PaymentError, "Платёж по счёту покупателю — это «Оплата от покупателя»"):
                    self.pay_in(self.cp, 100, purpose, invoice=self.invoice_doc)
        self.assertEqual(self.state(), before)
        self.assertEqual(Invoice.objects.get(pk=self.invoice_doc.pk).paid_amount, D("0.00"))

    def test_linking_invoice_keeps_explicit_purpose(self):
        """Записанное «не распределено» при привязке счёта молча не меняется."""
        payment = self.pay_in(self.cp, 100)
        before = self.state()
        payment.invoice = self.invoice_doc
        with self.assertRaisesMessage(PaymentError, "Выберите это назначение или уберите счёт-основание"):
            payment.save()
        self.assertEqual(self.state(), before)
        payment = Payment.objects.get(pk=payment.pk)
        payment.invoice, payment.settlement_purpose = self.invoice_doc, CUSTOMER_PAYMENT
        payment.save()
        self.assertEqual(Invoice.objects.get(pk=self.invoice_doc.pk).paid_amount, D("100.00"))

    def test_removing_invoice_keeps_customer_payment(self):
        payment = self.pay_in(self.cp, 100, invoice=self.invoice_doc)
        payment.invoice = None
        payment.save()
        self.assertEqual(Payment.objects.get(pk=payment.pk).settlement_purpose, CUSTOMER_PAYMENT)

    def test_update_fields_are_checked_against_stored_row(self):
        payment = self.pay_in(self.cp, 100, invoice=self.invoice_doc)
        before = self.state()
        payment.settlement_purpose = SUPPLIER_REFUND
        with self.assertRaises(PaymentError):
            payment.save(update_fields=["settlement_purpose"])      # счёт — из базы
        self.assertEqual(self.state(), before)
        payment.amount = D("120")
        payment.save(update_fields=["amount"])                     # назначение не пишется — не проверяется
        self.assertEqual(Payment.objects.get(pk=payment.pk).settlement_purpose, CUSTOMER_PAYMENT)

    def test_stale_object_is_checked_against_fresh_row(self):
        payment = self.pay_in(self.cp, 100)
        stale = Payment.objects.get(pk=payment.pk)
        fresh = Payment.objects.get(pk=payment.pk)
        fresh.invoice, fresh.settlement_purpose = self.invoice_doc, CUSTOMER_PAYMENT
        fresh.save()
        stale.settlement_purpose = SUPPLIER_REFUND                 # объект не знает о новом счёте
        with self.assertRaises(PaymentError):
            stale.save(update_fields=["settlement_purpose"])
        self.assertEqual(Payment.objects.get(pk=payment.pk).settlement_purpose, CUSTOMER_PAYMENT)

    def test_direction_change_is_rechecked(self):
        refund = self.pay_in(self.cp, 100, SUPPLIER_REFUND)
        before = self.state()
        refund.kind = Payment.KIND_OUT
        with self.assertRaisesMessage(PaymentError, "«Возврат от поставщика»"):
            refund.save()
        with self.assertRaises(PaymentError):
            Payment.objects.filter(pk=refund.pk).update(kind=Payment.KIND_OUT)
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_update([refund], ["kind"])
        self.assertEqual(self.state(), before)
        Payment.objects.filter(pk=refund.pk).update(kind=Payment.KIND_OUT, settlement_purpose=SUPPLIER_PAYMENT)
        self.assertEqual(Payment.objects.get(pk=refund.pk).settlement_purpose, SUPPLIER_PAYMENT)

    def test_bulk_update_and_update_check_every_row(self):
        good = self.pay_in(self.cp, 10)
        bad = self.pay_in(self.cp, 20, invoice=self.invoice_doc)
        before = self.state()
        with self.assertRaisesMessage(PaymentError, f"платёж id {bad.pk}:"):
            Payment.objects.filter(pk__in=[good.pk, bad.pk]).update(settlement_purpose=SUPPLIER_REFUND)
        good.settlement_purpose, bad.settlement_purpose = SUPPLIER_REFUND, UNALLOCATED
        with self.assertRaisesMessage(PaymentError, f"платёж id {bad.pk}:"):
            Payment.objects.bulk_update([good, bad], ["settlement_purpose"])
        self.assertEqual(self.state(), before)                     # отказ без частичной записи
        Payment.objects.bulk_update([good], ["settlement_purpose"])
        self.assertEqual(Payment.objects.get(pk=good.pk).settlement_purpose, SUPPLIER_REFUND)

    def test_bulk_paths_reject_blank_and_expressions(self):
        payment = self.pay_in(self.cp, 10)
        before = self.state()
        for value in ("", None, F("settlement_purpose"), Value(SUPPLIER_REFUND)):
            with self.subTest(value=value):
                with self.assertRaises(UnsupportedBulkChange):
                    Payment.objects.filter(pk=payment.pk).update(settlement_purpose=value)
        payment.settlement_purpose = ""
        with self.assertRaises(UnsupportedBulkChange):
            Payment.objects.bulk_update([payment], ["settlement_purpose"])
        self.assertEqual(self.state(), before)

    def test_bulk_create_fills_and_checks(self):
        account = self.bank[self.org_a.pk]
        common = {"organization": self.org_a, "account": account, "counterparty": self.cp, "amount": 10}
        created = Payment.objects.bulk_create([
            Payment(kind=Payment.KIND_IN, invoice=self.invoice_doc, **common),
            Payment(kind=Payment.KIND_OUT, **common),
        ])
        self.assertEqual([p.settlement_purpose for p in created], [CUSTOMER_PAYMENT, UNALLOCATED])
        before = self.state()
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_create([Payment(kind=Payment.KIND_OUT, settlement_purpose=CUSTOMER_PAYMENT,
                                                 **common)])
        self.assertEqual(self.state(), before)

    def test_unposting_old_incompatible_payment_is_allowed(self):
        payment = self.pay_in(self.cp, 10)
        models.QuerySet.update(Payment.objects.filter(pk=payment.pk), invoice=self.invoice_doc)   # «старая база»
        self.assertIn("settlement_purpose", services.link_errors(services.link_values(
            Payment.objects.get(pk=payment.pk))))
        Payment.objects.get(pk=payment.pk).unpost()
        self.assertEqual(Payment.objects.get(pk=payment.pk).status, "draft")

    def test_database_rejects_missing_or_wrong_purpose_and_side(self):
        payment = self.pay_in(self.cp, 10)
        for value in ("", "something", SUPPLIER_PAYMENT):
            with self.subTest(value=value):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    models.QuerySet.update(Payment.objects.filter(pk=payment.pk), settlement_purpose=value)
        correction = self.correct(self.cp, THEY_OWE, 10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementCorrection.objects.filter(pk=correction.pk).update(side="")

    def test_clarifying_purpose_changes_only_settlements(self):
        """Уточнение назначения: сумма платежа, деньги на счёте и оплата счёта
        (ссылка на счёт не менялась) — прежние; меняется только распределение."""
        self.ship(self.cp, 1000)
        self.receive(self.cp, 700)
        self.pay_in(self.cp, 300, invoice=self.invoice_doc)          # оплата этого счёта — отдельно
        payment = self.pay_in(self.cp, 400)
        account = self.bank[self.org_a.pk]
        before = (Payment.objects.get(pk=payment.pk).amount, account.balance,
                  Invoice.objects.get(pk=self.invoice_doc.pk).paid_amount, payment.invoice_id)
        self.assertSides(self.cp, (700, 700, -400), final=False)
        payment.settlement_purpose = SUPPLIER_REFUND
        payment.save()
        after = (Payment.objects.get(pk=payment.pk).amount, Account.objects.get(pk=account.pk).balance,
                 Invoice.objects.get(pk=self.invoice_doc.pk).paid_amount, payment.invoice_id)
        self.assertEqual(after, before)
        self.assertEqual(before[1:3], (D("700.00"), D("300.00")))
        self.assertSides(self.cp, (700, 1100, -400))                 # разница та же, стороны другие


class PurposeFormTests(SettlementsBase):
    def setUp(self):
        super().setUp()
        self.invoice_doc = self.invoice(self.cp, 1000)
        self.login_accountant()

    def data(self, **changes):
        values = {
            "date": "2026-10-18", "number": "", "time": "10:00", "organization": self.org_a.pk,
            "account": self.bank[self.org_a.pk].pk, "counterparty": self.cp.pk, "invoice": "",
            "amount": "100", "settlement_purpose": "", "purpose": "текст назначения", "action": "save",
        }
        values.update(changes)
        return values

    def test_choices_match_direction(self):
        incoming = self.client.get(reverse("payment_create_in")).context["form"]
        outgoing = self.client.get(reverse("payment_create_out")).context["form"]
        self.assertEqual([v for v, _ in incoming.fields["settlement_purpose"].choices],
                         ["", CUSTOMER_PAYMENT, SUPPLIER_REFUND, UNALLOCATED])
        self.assertEqual([v for v, _ in outgoing.fields["settlement_purpose"].choices],
                         ["", SUPPLIER_PAYMENT, CUSTOMER_REFUND, UNALLOCATED])

    def test_not_specified_with_invoice_becomes_customer_payment(self):
        self.client.post(reverse("payment_create_in"), self.data(invoice=self.invoice_doc.pk))
        payment = Payment.objects.get()
        self.assertEqual((payment.settlement_purpose, payment.purpose), (CUSTOMER_PAYMENT, "текст назначения"))

    def test_explicit_contradicting_purpose_is_an_error(self):
        response = self.client.post(reverse("payment_create_in"),
                                    self.data(invoice=self.invoice_doc.pk, settlement_purpose=UNALLOCATED))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Платёж по счёту покупателю", response.context["form"].errors["settlement_purpose"][0])
        self.assertFalse(Payment.objects.exists())

    def test_wrong_direction_value_is_rejected_by_form(self):
        response = self.client.post(reverse("payment_create_in"), self.data(settlement_purpose=SUPPLIER_PAYMENT))
        self.assertIn("settlement_purpose", response.context["form"].errors)
        self.assertFalse(Payment.objects.exists())

    def test_editing_shows_error_instead_of_silent_change(self):
        payment = self.pay_in(self.cp, 100)
        url = reverse("payment_edit", args=[payment.pk])
        response = self.client.post(url, self.data(number=payment.number, invoice=self.invoice_doc.pk,
                                                   settlement_purpose=UNALLOCATED))
        self.assertIn("settlement_purpose", response.context["form"].errors)
        self.assertEqual(Payment.objects.get(pk=payment.pk).invoice_id, None)
        self.client.post(url, self.data(number=payment.number, invoice=self.invoice_doc.pk,
                                        settlement_purpose=CUSTOMER_PAYMENT))
        self.assertEqual(Payment.objects.get(pk=payment.pk).invoice_id, self.invoice_doc.pk)

    def test_payment_created_from_invoice_is_customer_payment(self):
        self.client.post(reverse("invoice_to_payment", args=[self.invoice_doc.pk]))
        payment = Payment.objects.get()
        self.assertEqual((payment.invoice_id, payment.settlement_purpose), (self.invoice_doc.pk, CUSTOMER_PAYMENT))
        self.assertTrue(payment.purpose.startswith("Оплата по счёту"))     # текст назначения — прежний

    def test_settlement_correction_form_requires_side(self):
        data = {"date": "2026-10-18", "number": "", "time": "10:00", "organization": self.org_a.pk,
                "counterparty": self.cp.pk, "direction": THEY_OWE, "amount": "100", "comment": "", "side": "",
                "action": "save"}
        response = self.client.post(reverse("settlement_correction_create"), data)
        self.assertIn("side", response.context["form"].errors)
        self.assertFalse(SettlementCorrection.objects.exists())
        data["side"] = PURCHASES
        self.client.post(reverse("settlement_correction_create"), data)
        self.assertEqual(SettlementCorrection.objects.get().side, PURCHASES)


# ---------------------------------------------------------------------------
# Отчёт, карточка контрагента, диагностика, только чтение
# ---------------------------------------------------------------------------

class ReportAndCardTests(SettlementsBase):
    def setUp(self):
        super().setUp()
        self.login_accountant()
        self.ship(self.cp, 1000, org=self.org_a)
        self.receive(self.cp, 700, org=self.org_a)
        self.payment = self.pay_in(self.cp, 400, org=self.org_a)               # не распределено
        self.correction = self.correct(self.cp, WE_OWE, 50, side=NO_SIDE, org=self.org_b)

    def test_report_and_card_use_the_same_calculation(self):
        report = self.client.get(reverse("settlements"))
        card = self.client.get(reverse("counterparty_edit", args=[self.cp.pk]))
        numbers = lambda pairs: [(p.organization.pk, p.sales, p.purchases, p.difference, p.final) for p in pairs]  # noqa: E731
        expected = [(self.org_a.pk, D("1000"), D("700"), D("-100"), False),
                    (self.org_b.pk, D("0"), D("0"), D("-50"), False)]
        self.assertEqual(numbers(report.context["rows"]), expected)
        self.assertEqual(numbers(card.context["settlement_pairs"]), expected)
        self.assertContains(card, "Есть нераспределённые документы (2)")
        self.assertContains(report, f"Входящий платёж № {self.payment.number}")

    def test_card_lists_settlement_corrections(self):
        docs = counterparty_documents(self.cp)
        found = [d for d in docs if d["type_label"] == "Корректировка взаиморасчётов"]
        self.assertEqual([(d["number"], d["amount"]) for d in found], [(self.correction.number, D("-50"))])

    def test_reading_writes_nothing(self):
        with CaptureQueriesContext(connection) as queries:
            pairs = settlements.settlement_pairs()
            settlements.totals(pairs)
            [p.explanations for p in pairs]
            call_command("check_settlements", stdout=StringIO())
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}]
        self.assertEqual(writes, [])

    def test_totals_keep_opposite_debts_apart(self):
        other = self.party("Покупатель Б")
        self.pay_in(other, 300, CUSTOMER_PAYMENT, org=self.org_b)               # аванс в Б
        totals = settlements.totals(settlements.settlement_pairs())
        self.assertEqual(totals["customers_owe"], D("1000"))
        self.assertEqual(totals["customer_advances"], D("300"))
        self.assertEqual(totals["we_owe_suppliers"], D("700"))
        self.assertEqual((totals["unallocated_in"], totals["unsided_we_owe"], totals["unallocated_count"]),
                         (D("400"), D("50"), 2))
        self.assertEqual(totals["preliminary_pairs"], 2)

    def test_diagnostics_old_report_new_calculation_and_documents(self):
        invoice = self.invoice(self.cp, 900)
        legacy = self.pay_in(self.cp, 25)
        models.QuerySet.update(Payment.objects.filter(pk=legacy.pk), invoice=invoice)    # несовместимо
        before = list(Payment.objects.order_by("pk").values_list("pk", "settlement_purpose", "invoice_id"))
        out = StringIO()
        call_command("check_settlements", stdout=out)
        text = out.getvalue()
        # прежний отчёт: счета 900 − получено 425 − (приёмки 700 − 0) − 50 = −275
        self.assertIn("прежний отчёт: сальдо -275,00 (мы должны)", text)
        self.assertIn("Организация А: по продажам 1\xa0000,00, по закупкам 700,00", text)   # разделитель — неразрывный пробел
        self.assertIn(f"Входящий платёж № {self.payment.number}", text)
        self.assertIn("у пары есть и продажи, и закупки", text)
        self.assertIn(f"Корректировка взаиморасчётов № {self.correction.number}", text)
        self.assertIn("Платежи с несовместимым назначением (не исправляются автоматически): 1", text)
        self.assertIn("Ничего не изменено", text)
        self.assertEqual(list(Payment.objects.order_by("pk").values_list(
            "pk", "settlement_purpose", "invoice_id")), before)

    def test_hints_follow_goods_documents_only(self):
        buyer = self.party("Только покупатель", Counterparty.TYPE_SUPPLIER)   # тип карточки не учитывается
        self.ship(buyer, 100)
        payment = self.pay_in(buyer, 100)
        item = self.pair(buyer)
        self.assertEqual(settlements.purpose_hint(item, payment.kind),
                         "у пары только продажи — вероятно, «Оплата от покупателя»")
        self.assertEqual(Payment.objects.get(pk=payment.pk).settlement_purpose, UNALLOCATED)


# ---------------------------------------------------------------------------
# Перенос старых данных: миграция старой схемы на изолированной базе
# ---------------------------------------------------------------------------

class MigrationTests(TransactionTestCase):
    """finance.0003 (до этапа) → последняя: правила переноса из 0004.

    Как и остальные TransactionTestCase, после теста очищает таблицы; схема
    возвращается к последней миграции в finally."""

    before = ("finance", "0003_alter_accountcorrection_options_and_more")

    def test_old_schema_is_migrated_without_guessing(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        targets = [node for node in leaves if node[0] != "finance"] + [self.before]
        executor.migrate(targets)
        try:
            old = executor.loader.project_state(targets).apps
            ids = self.make_old_data(old)
        finally:
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())
        stored = dict(Payment.objects.values_list("pk", "settlement_purpose"))
        self.assertEqual(stored, {
            ids["valid"]: CUSTOMER_PAYMENT,
            ids["other_customer"]: UNALLOCATED,       # счёт другого покупателя — несовместимо
            ids["other_org"]: UNALLOCATED,            # счёт другой организации — несовместимо
            ids["no_invoice"]: UNALLOCATED,           # не угадывается, хотя контрагент — покупатель
            ids["outgoing_with_invoice"]: UNALLOCATED,
            ids["outgoing"]: UNALLOCATED,             # не угадывается, хотя есть только закупки
        })
        self.assertEqual(list(SettlementCorrection.objects.values_list("side", flat=True)), [NO_SIDE])
        bad = {payment.pk for payment, _ in services.incompatible_payments()}
        self.assertEqual(bad, {ids["other_customer"], ids["other_org"], ids["outgoing_with_invoice"]})

    def make_old_data(self, old):
        org_model, wh_model = old.get_model("core", "Organization"), old.get_model("core", "Warehouse")
        cp_model, account_model = old.get_model("partners", "Counterparty"), old.get_model("finance", "Account")
        invoice_model, payment_model = old.get_model("sales", "Invoice"), old.get_model("finance", "Payment")
        org, other_org = org_model.objects.create(name="А"), org_model.objects.create(name="Б")
        wh = wh_model.objects.create(name="Склад")
        customer = cp_model.objects.create(name="Покупатель", partner_type="customer")
        other = cp_model.objects.create(name="Другой", partner_type="customer")
        supplier = cp_model.objects.create(name="Поставщик", partner_type="supplier")
        account = account_model.objects.create(organization=org, name="Р/с")
        invoice = invoice_model.objects.create(organization=org, warehouse=wh, customer=customer, status="issued")
        foreign = invoice_model.objects.create(organization=other_org, warehouse=wh, customer=customer,
                                               status="issued")

        def pay(kind, cp, invoice=None):
            return payment_model.objects.create(kind=kind, organization=org, account=account, counterparty=cp,
                                                invoice=invoice, amount=10, status="posted").pk

        ids = {
            "valid": pay("incoming", customer, invoice),
            "other_customer": pay("incoming", other, invoice),
            "other_org": pay("incoming", customer, foreign),
            "no_invoice": pay("incoming", customer),
            "outgoing_with_invoice": pay("outgoing", supplier, invoice),
            "outgoing": pay("outgoing", supplier),
        }
        old.get_model("finance", "SettlementCorrection").objects.create(
            organization=org, counterparty=customer, direction="they_owe", amount=5, status="posted")
        return ids


# ---------------------------------------------------------------------------
# Одновременные операции (PostgreSQL)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == "postgresql", "блокировки строк проверяются только на PostgreSQL")
class ConcurrentPurposeTests(World, ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.make_world()
        self.cp = self.party("Контрагент")
        self.invoice_doc = self.invoice(self.cp, 1000)

    def test_linking_invoice_and_changing_purpose_at_once(self):
        """Один привязывает счёт (с «оплатой от покупателя»), другой ставит
        «возврат от поставщика». Строка платежа блокируется первой (уровень 1),
        поэтому вторая операция ждёт и проверяет уже записанное: в базе не
        может оказаться счёт с «возвратом от поставщика»."""
        payment = self.pay_in(self.cp, 100)

        def link():
            doc = Payment.objects.get(pk=payment.pk)
            doc.invoice, doc.settlement_purpose = self.invoice_doc, CUSTOMER_PAYMENT
            doc.save(update_fields=["invoice", "settlement_purpose"])

        def refund():
            doc = Payment.objects.get(pk=payment.pk)
            doc.settlement_purpose = SUPPLIER_REFUND
            doc.save(update_fields=["settlement_purpose"])

        results = self.run_overlapping(link, refund, pause_on=(services, "lock_invoices"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], PaymentError)
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.invoice_id, stored.settlement_purpose), (self.invoice_doc.pk, CUSTOMER_PAYMENT))
        self.assertEqual(Invoice.objects.get(pk=self.invoice_doc.pk).paid_amount, D("100.00"))
        self.assertEqual(services.incompatible_payments(), [])
