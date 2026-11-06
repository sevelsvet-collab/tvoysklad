"""Контрагент платежа: сторону задаёт назначение для взаиморасчётов, а не только
направление денег (apps/finance/forms.py, templates/finance/payment_form.html).

Возврат покупателю — исходящий платёж покупателю, возврат от поставщика —
входящий от поставщика. Раньше выбор зависел только от направления: поиск
отправлял type=supplier для любого исходящего и type=customer для любого
входящего, а форма отклоняла правильного контрагента (invalid_choice).

Правило: оплата от покупателя и возврат покупателю — покупатели, оплата
поставщику и возврат от поставщика — поставщики, в обоих случаях вместе с типом
«Покупатель и поставщик»; «не распределено» — любой действующий контрагент.
«Не указано» проверяется так, как назначение определится при записи
(services.auto_purpose): со счётом-основанием — оплата от покупателя.
Браузерная часть — tests_browser_payment_counterparty.py.
"""
import html
import json
import re
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.partners.models import Counterparty

from . import services
from .models import Account, Payment
from .tests_settlements import (
    CUSTOMER_PAYMENT, CUSTOMER_REFUND, SUPPLIER_PAYMENT, SUPPLIER_REFUND, UNALLOCATED, World,
)

D = Decimal
IN, OUT = Payment.KIND_IN, Payment.KIND_OUT
CREATE_URL = {IN: "payment_create_in", OUT: "payment_create_out"}


def ac_block(page):
    """Поле выбора контрагента на странице платежа: (параметры поиска, id в
    скрытом поле, видимое название)."""
    found = re.search(
        r"""<div class="ac"[^>]*data-ac-params='([^']*)'>\s*"""
        r"""<input type="hidden" name="counterparty" id="id_counterparty"\s+value="([^"]*)" class="ac-value">"""
        r""".*?class="form-control ac-input"[^>]*value="([^"]*)">""",
        page, re.S,
    )
    assert found, "поле контрагента не найдено"
    params, value, label = found.groups()
    return json.loads(html.unescape(params)), value, html.unescape(label)


class Base(World, TestCase):
    def setUp(self):
        self.make_world()
        self.customer = self.party("Только покупатель", Counterparty.TYPE_CUSTOMER)
        self.supplier = self.party("Только поставщик", Counterparty.TYPE_SUPPLIER)
        self.both = self.party("Покупатель и поставщик", Counterparty.TYPE_BOTH)
        self.account = self.bank[self.org_a.pk]
        self.login_accountant()

    def data(self, **changes):
        values = {
            "date": "2026-11-06", "number": "", "time": "10:00", "organization": self.org_a.pk,
            "account": self.account.pk, "counterparty": "", "invoice": "", "amount": "100",
            "settlement_purpose": "", "purpose": "", "action": "save",
        }
        values.update(changes)
        return values

    def create(self, kind, party, purpose="", **changes):
        return self.client.post(reverse(CREATE_URL[kind]),
                                self.data(counterparty=party.pk if party else "", settlement_purpose=purpose,
                                          **changes))

    def assertCreated(self, response, party, purpose):
        form = response.context["form"] if response.context else None
        self.assertEqual(response.status_code, 302, form.errors if form is not None else "")
        payment = Payment.objects.get()
        self.assertEqual((payment.counterparty_id, payment.settlement_purpose), (party.pk, purpose))
        return payment

    def assertRejected(self, response, code="partner_type"):
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors.as_data()
        self.assertEqual([e.code for e in errors.get("counterparty", [])], [code], errors)
        self.assertFalse(Payment.objects.exists())

    def found(self, params, q=""):
        """Кого найдёт живой поиск с этими параметрами (среди трёх типов)."""
        response = self.client.get(reverse("api_counterparty_search"), {**params, "q": q})
        names = {row["name"] for row in response.json()["results"]}
        return names & {self.customer.name, self.supplier.name, self.both.name}

    def searched(self, kind, purpose, **changes):
        """Параметры поиска на странице, показанной с этим назначением (ошибка в
        сумме — форма возвращается с тем, что выбрано), и кого они находят."""
        response = self.client.post(reverse(CREATE_URL[kind]),
                                    self.data(settlement_purpose=purpose, amount="не число", **changes))
        self.assertEqual(response.status_code, 200)
        params, _, _ = ac_block(response.content.decode())
        return self.found(params)

    def names(self, *parties):
        return {p.name for p in parties}


# ---------------------------------------------------------------------------
# Дефект: выбор зависел только от направления денег
# ---------------------------------------------------------------------------

class ReturnDefectTests(Base):
    def test_customer_refund_search_finds_customers(self):
        self.assertEqual(self.searched(OUT, CUSTOMER_REFUND), self.names(self.customer, self.both))

    def test_customer_refund_is_saved_for_customer(self):
        self.assertCreated(self.create(OUT, self.customer, CUSTOMER_REFUND), self.customer, CUSTOMER_REFUND)

    def test_supplier_refund_search_finds_suppliers(self):
        self.assertEqual(self.searched(IN, SUPPLIER_REFUND), self.names(self.supplier, self.both))

    def test_supplier_refund_is_saved_for_supplier(self):
        self.assertCreated(self.create(IN, self.supplier, SUPPLIER_REFUND), self.supplier, SUPPLIER_REFUND)


# ---------------------------------------------------------------------------
# Правило по назначению: поиск и проверка формы согласованы
# ---------------------------------------------------------------------------

class PurposeRuleTests(Base):
    RULES = [
        # (направление, назначение, подходят, не подходит)
        (IN, CUSTOMER_PAYMENT, "customer", "supplier"),
        (OUT, CUSTOMER_REFUND, "customer", "supplier"),
        (OUT, SUPPLIER_PAYMENT, "supplier", "customer"),
        (IN, SUPPLIER_REFUND, "supplier", "customer"),
    ]

    def test_each_purpose_accepts_its_side_and_both(self):
        for kind, purpose, side, _ in self.RULES:
            for party in (getattr(self, side), self.both):
                with self.subTest(kind=kind, purpose=purpose, party=party.name):
                    Payment.objects.all().delete()
                    self.assertCreated(self.create(kind, party, purpose), party, purpose)

    def test_each_purpose_rejects_other_side_with_clear_message(self):
        for kind, purpose, _, other in self.RULES:
            party = getattr(self, other)
            with self.subTest(kind=kind, purpose=purpose):
                response = self.create(kind, party, purpose)
                self.assertRejected(response)
                text = response.context["form"].errors["counterparty"][0]
                self.assertIn(f"«{party.name}»", text)
                self.assertIn(dict(Payment.PURPOSE_CHOICES)[purpose], text)

    def test_search_matches_validation(self):
        for kind, purpose, side, _ in self.RULES:
            with self.subTest(kind=kind, purpose=purpose):
                self.assertEqual(self.searched(kind, purpose), self.names(getattr(self, side), self.both))

    def test_unallocated_is_not_limited_by_direction(self):
        for kind in (IN, OUT):
            with self.subTest(kind=kind):
                self.assertEqual(self.searched(kind, UNALLOCATED), self.names(self.customer, self.supplier, self.both))
                for party in (self.customer, self.supplier, self.both):
                    Payment.objects.all().delete()
                    self.assertCreated(self.create(kind, party, UNALLOCATED), party, UNALLOCATED)

    def test_not_specified_without_invoice_is_unallocated_for_any_party(self):
        for kind in (IN, OUT):
            with self.subTest(kind=kind):
                self.assertEqual(self.searched(kind, ""), self.names(self.customer, self.supplier, self.both))
                for party in (self.customer, self.supplier, self.both):
                    Payment.objects.all().delete()
                    self.assertCreated(self.create(kind, party), party, UNALLOCATED)

    def test_not_specified_with_invoice_stays_customer_payment(self):
        """Правило автоматического назначения по счёту — прежнее."""
        invoice = self.invoice(self.customer, 500)
        self.assertEqual(self.searched(IN, "", invoice=invoice.pk), self.names(self.customer, self.both))
        payment = self.assertCreated(self.create(IN, self.customer, invoice=invoice.pk), self.customer,
                                     CUSTOMER_PAYMENT)
        self.assertEqual(payment.invoice_id, invoice.pk)

    def test_invoice_with_supplier_side_is_rejected(self):
        invoice = self.invoice(self.both, 500)
        self.assertRejected(self.create(IN, self.supplier, invoice=invoice.pk))
        response = self.create(IN, self.both, SUPPLIER_REFUND, invoice=invoice.pk)
        self.assertIn("Платёж по счёту покупателю", response.context["form"].errors["settlement_purpose"][0])
        self.assertFalse(Payment.objects.exists())

    def test_payment_from_invoice_button_is_unchanged(self):
        invoice = self.invoice(self.customer, 500)
        self.client.post(reverse("invoice_to_payment", args=[invoice.pk]))
        payment = Payment.objects.get()
        self.assertEqual((payment.kind, payment.counterparty_id, payment.invoice_id, payment.settlement_purpose),
                         (IN, self.customer.pk, invoice.pk, CUSTOMER_PAYMENT))

    def test_rule_function(self):
        cases = {
            (IN, CUSTOMER_PAYMENT, None): "customer", (OUT, CUSTOMER_REFUND, None): "customer",
            (OUT, SUPPLIER_PAYMENT, None): "supplier", (IN, SUPPLIER_REFUND, None): "supplier",
            (IN, UNALLOCATED, None): "", (OUT, UNALLOCATED, None): "",
            (IN, "", None): "", (OUT, "", None): "", (IN, "", 7): "customer",
        }
        for (kind, purpose, invoice_id), expected in cases.items():
            with self.subTest(kind=kind, purpose=purpose, invoice=invoice_id):
                values = {"kind": kind, "settlement_purpose": purpose, "invoice_id": invoice_id}
                self.assertEqual(services.counterparty_type(values), expected)

    def test_archived_counterparty_is_still_refused(self):
        archived = self.party("Архивный покупатель", Counterparty.TYPE_CUSTOMER)
        Counterparty.objects.filter(pk=archived.pk).update(is_active=False)
        self.assertRejected(self.create(OUT, archived, CUSTOMER_REFUND), code="invalid_choice")

    def test_types_are_never_changed(self):
        before = dict(Counterparty.objects.values_list("pk", "partner_type"))
        for kind, purpose, side, other in self.RULES:
            self.create(kind, getattr(self, side), purpose)
            self.create(kind, getattr(self, other), purpose)
        self.assertEqual(dict(Counterparty.objects.values_list("pk", "partner_type")), before)


# ---------------------------------------------------------------------------
# Редактирование и повторный показ формы
# ---------------------------------------------------------------------------

class EditAndRedisplayTests(Base):
    def edit(self, payment, **changes):
        values = {"number": payment.number, "counterparty": payment.counterparty_id,
                  "settlement_purpose": payment.settlement_purpose, "amount": str(payment.amount)}
        values.update(changes)
        return self.client.post(reverse("payment_edit", args=[payment.pk]), self.data(**values))

    def stored(self, payment):
        return Payment.objects.filter(pk=payment.pk).values(*services.LINK_FIELDS, "purpose", "number").get()

    def test_edit_keeps_fitting_counterparty(self):
        payment = self.pay_out(self.customer, 100, CUSTOMER_REFUND, post=False)
        response = self.edit(payment, amount="150")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored(payment)["amount"], D("150.00"))

    def test_changing_purpose_against_chosen_counterparty_is_refused(self):
        payment = self.pay_out(self.customer, 100, CUSTOMER_REFUND, post=False)
        before = self.stored(payment)
        response = self.edit(payment, settlement_purpose=SUPPLIER_PAYMENT, amount="150")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e.code for e in response.context["form"].errors.as_data()["counterparty"]],
                         ["partner_type"])
        self.assertEqual(self.stored(payment), before)
        self.assertEqual(self.edit(payment, settlement_purpose=SUPPLIER_PAYMENT,
                                   counterparty=self.supplier.pk).status_code, 302)
        self.assertEqual((self.stored(payment)["settlement_purpose"], self.stored(payment)["counterparty_id"]),
                         (SUPPLIER_PAYMENT, self.supplier.pk))

    def test_both_survives_purpose_change(self):
        payment = self.pay_out(self.both, 100, CUSTOMER_REFUND, post=False)
        self.assertEqual(self.edit(payment, settlement_purpose=SUPPLIER_PAYMENT).status_code, 302)
        self.assertEqual(self.stored(payment)["settlement_purpose"], SUPPLIER_PAYMENT)

    def test_edit_page_searches_by_stored_purpose(self):
        for kind, purpose, side in ((OUT, CUSTOMER_REFUND, self.customer), (IN, SUPPLIER_REFUND, self.supplier),
                                    (OUT, UNALLOCATED, self.customer)):
            with self.subTest(kind=kind, purpose=purpose):
                payment = self.pay(side, kind, 100, purpose, post=False)
                params, value, label = ac_block(
                    self.client.get(reverse("payment_edit", args=[payment.pk])).content.decode())
                expected = (self.names(side, self.both) if purpose != UNALLOCATED
                            else self.names(self.customer, self.supplier, self.both))
                self.assertEqual(self.found(params), expected)
                self.assertEqual((value, label), (str(side.pk), side.name))

    def test_redisplay_shows_submitted_counterparty_not_hidden_one(self):
        """Название рядом с полем — того контрагента, чей id уйдёт с формой."""
        payment = self.pay_out(self.customer, 100, CUSTOMER_REFUND, post=False)
        response = self.edit(payment, counterparty=self.supplier.pk)       # поставщик для возврата покупателю
        params, value, label = ac_block(response.content.decode())
        self.assertEqual((value, label), (str(self.supplier.pk), self.supplier.name))
        self.assertIn(f"«{self.supplier.name}»", response.context["form"].errors["counterparty"][0])
        self.assertEqual(self.found(params), self.names(self.customer, self.both))
        self.assertEqual(self.stored(payment)["counterparty_id"], self.customer.pk)

    def test_redisplay_after_other_error_keeps_choice_and_search(self):
        response = self.create(OUT, self.customer, CUSTOMER_REFUND, amount="0", action="save_post")
        self.assertEqual(response.status_code, 200)
        self.assertIn("amount", response.context["form"].errors)
        params, value, label = ac_block(response.content.decode())
        self.assertEqual((value, label), (str(self.customer.pk), self.customer.name))
        self.assertEqual(self.found(params), self.names(self.customer, self.both))
        self.assertFalse(Payment.objects.exists())

    def test_unknown_counterparty_is_not_kept_hidden(self):
        for value in ("999999", "abc"):
            with self.subTest(value=value):
                response = self.client.post(reverse("payment_create_out"),
                                            self.data(counterparty=value, settlement_purpose=CUSTOMER_REFUND))
                self.assertEqual(response.status_code, 200)
                self.assertIn("counterparty", response.context["form"].errors)
                self.assertEqual(ac_block(response.content.decode())[1:], ("", ""))
        self.assertFalse(Payment.objects.exists())

    def test_prefill_from_card_of_customer_on_outgoing(self):
        page = self.client.get(reverse("payment_create_out"), {"partner": self.customer.pk}).content.decode()
        _, value, _ = ac_block(page)
        self.assertEqual(value, str(self.customer.pk))
        self.assertCreated(self.create(OUT, self.customer, CUSTOMER_REFUND), self.customer, CUSTOMER_REFUND)

    def test_page_rules_follow_server_rule(self):
        """Правила для смены назначения в браузере берутся из той же функции."""
        for kind in (IN, OUT):
            with self.subTest(kind=kind):
                page = self.client.get(reverse(CREATE_URL[kind])).content.decode()
                rules = json.loads(html.unescape(re.search(
                    r'<script id="payment-counterparty-rules" type="application/json">(.*?)</script>',
                    page, re.S).group(1)))
                for purpose in [""] + list(Payment.PURPOSES_BY_KIND[kind]):
                    for has_invoice in (False, True):
                        if has_invoice and kind == OUT:
                            continue
                        values = {"kind": kind, "settlement_purpose": purpose,
                                  "invoice_id": 1 if has_invoice else None}
                        rule = rules["purposes"][purpose]["invoice" if has_invoice else "none"]
                        self.assertEqual(rule["type"], services.counterparty_type(values))


# ---------------------------------------------------------------------------
# Быстрое создание из поля: без дубликатов и без смены типа
# ---------------------------------------------------------------------------

class QuickCreateTests(Base):
    def quick(self, name, partner_type):
        return self.client.post(reverse("api_counterparty_quick_create"), {"name": name, "type": partner_type}).json()

    def test_existing_name_of_other_type_is_returned_unchanged(self):
        count = Counterparty.objects.count()
        answer = self.quick(self.supplier.name, Counterparty.TYPE_CUSTOMER)
        self.assertEqual((answer["created"], answer["counterparty"]["id"]), (False, self.supplier.pk))
        self.assertEqual(answer["counterparty"]["partner_type"], Counterparty.TYPE_SUPPLIER)
        self.assertEqual(Counterparty.objects.count(), count)
        self.assertEqual(Counterparty.objects.get(pk=self.supplier.pk).partner_type, Counterparty.TYPE_SUPPLIER)

    def test_search_results_carry_type(self):
        rows = self.client.get(reverse("api_counterparty_search"), {"q": "Только"}).json()["results"]
        self.assertEqual({r["name"]: r["partner_type"] for r in rows},
                         {self.customer.name: "customer", self.supplier.name: "supplier"})
        row = self.client.get(reverse("api_counterparty_search"), {"id": self.both.pk}).json()["results"][0]
        self.assertEqual(row["partner_type"], "both")


# ---------------------------------------------------------------------------
# Проведённые возвраты и оплаты: деньги и взаиморасчёты; отказ ничего не меняет
# ---------------------------------------------------------------------------

class PostedEffectTests(Base):
    def balance(self):
        return Account.objects.get(pk=self.account.pk).balance

    def post_payment(self, kind, party, purpose, amount):
        response = self.client.post(reverse(CREATE_URL[kind]), self.data(
            counterparty=party.pk, settlement_purpose=purpose, amount=str(amount), action="save_post"))
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        return Payment.objects.order_by("-pk").first()

    def snapshot(self, party):
        item = self.pair(party)
        return (self.balance(), Payment.objects.count(), item.sales, item.purchases, item.difference, item.final)

    def test_customer_refund_takes_money_and_closes_advance(self):
        self.ship(self.customer, 1000)
        self.customer_return(self.customer, 400)
        self.pay_in(self.customer, 1000, CUSTOMER_PAYMENT)
        self.assertSides(self.customer, (-400, 0, -400))            # мы должны покупателю 400
        before = self.balance()
        refund = self.post_payment(OUT, self.customer, CUSTOMER_REFUND, 400)
        self.assertEqual((refund.kind, refund.status), (OUT, "posted"))
        self.assertEqual(self.balance(), before - D("400"))
        item = self.assertSides(self.customer, (0, 0, 0))
        self.assertEqual(item.customer_refunded, D("400"))

    def test_supplier_refund_brings_money_and_closes_overpayment(self):
        receipt = self.receive(self.supplier, 700)
        self.pay_out(self.supplier, 700, SUPPLIER_PAYMENT)
        self.supplier_return(self.supplier, 200, receipt)
        self.assertSides(self.supplier, (0, -200, 200))             # поставщик должен нам 200
        before = self.balance()
        refund = self.post_payment(IN, self.supplier, SUPPLIER_REFUND, 200)
        self.assertEqual((refund.kind, refund.status), (IN, "posted"))
        self.assertEqual(self.balance(), before + D("200"))
        item = self.assertSides(self.supplier, (0, 0, 0))
        self.assertEqual(item.supplier_refunded, D("200"))

    def test_ordinary_payments_of_both_party(self):
        self.ship(self.both, 1000)
        self.receive(self.both, 300)
        before = self.balance()
        self.post_payment(IN, self.both, CUSTOMER_PAYMENT, 1000)
        self.post_payment(OUT, self.both, SUPPLIER_PAYMENT, 300)
        self.assertEqual(self.balance(), before + D("700"))
        self.assertSides(self.both, (0, 0, 0))

    def test_unallocated_payment_is_shown_apart(self):
        self.ship(self.supplier, 100)               # тип карточки расчётам не важен
        self.post_payment(IN, self.supplier, UNALLOCATED, 100)
        self.assertSides(self.supplier, (100, 0, 0), final=False)

    def test_refusal_changes_nothing(self):
        self.ship(self.customer, 1000)
        self.pay_in(self.customer, 1000, CUSTOMER_PAYMENT)
        before = (self.snapshot(self.customer), self.snapshot(self.supplier) if self.pairs(self.supplier) else None)
        cases = [(OUT, self.supplier, CUSTOMER_REFUND), (IN, self.customer, SUPPLIER_REFUND),
                 (OUT, self.customer, SUPPLIER_PAYMENT), (IN, self.supplier, CUSTOMER_PAYMENT)]
        for kind, party, purpose in cases:
            with self.subTest(kind=kind, purpose=purpose):
                response = self.client.post(reverse(CREATE_URL[kind]), self.data(
                    counterparty=party.pk, settlement_purpose=purpose, amount="250", action="save_post"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("counterparty", response.context["form"].errors)
        after = (self.snapshot(self.customer), self.snapshot(self.supplier) if self.pairs(self.supplier) else None)
        self.assertEqual(after, before)

    def pairs(self, party):
        from . import settlements
        return settlements.settlement_pairs(counterparty=party)
