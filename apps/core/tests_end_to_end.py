"""Сквозная проверка: склад, деньги, оплата счетов, прибыль и взаиморасчёты.

Всё — настоящими документами (создание и проведение теми же методами, что
у программы; уточнение назначения платежа — через форму). Ожидаемые числа
посчитаны вручную и записаны константами ДО запуска; проверяемые сервисы их
не вычисляют. После каждого шага сравниваются:

* остаток количества и точная стоимость (StockBalance);
* деньги по кассам и счетам (Account.balance и страница «Счета и кассы»);
* «Оплачено» и отгружено по счёту (Invoice);
* выручка, себестоимость, прибыль (reports.services.sales_summary и
  страница «Продажи и прибыль»);
* обе стороны взаиморасчётов по каждой паре (finance.settlements);
* итоги отчёта «Взаиморасчёты» и сводка в карточке контрагента (страницы);
* сверка «деньги + склад + разница по документам = прибыль» — при нулевых
  начальных остатках и без корректировок касс она обязана сходиться.

Сценарий описан классом Scenario без TestCase: его же печатает таблицей
docs/proposals/end_to_end_table.py.
"""
from datetime import date, time
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.finance import settlements
from apps.finance.models import Account, AccountCorrection, Payment, SettlementCorrection
from apps.finance.services import PaymentError
from apps.inventory.models import DeficitCover, StockBalance
from apps.inventory.services import StockError
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.reports.services import sales_summary
from apps.sales.models import CustomerReturn, Invoice, Shipment

D = Decimal
DAY = "2026-10-05"
POSTED, DRAFT = "posted", "draft"

# ---------------------------------------------------------------------------
# Основной сценарий: ожидаемые значения после каждого шага (посчитаны вручную)
# stock — (количество, стоимость) «Товар» на «Основном складе»; bank — «Р/с»;
# invoice — (оплачено, статус оплаты, отгружено по счёту);
# profit — (выручка, себестоимость, прибыль) после возвратов, за всё время;
# customer/supplier — (по продажам, по закупкам, разница, окончательно, состояние);
# totals — (покупатели должны, мы должны покупателям, мы должны поставщикам,
#           поставщики должны нам, нераспределённых документов).
# ---------------------------------------------------------------------------
MAIN = [
    ("1. Приёмка 10 шт. по 100", {
        "stock": ("10", "1000.00"), "bank": "0.00", "invoice": None,
        "profit": ("0.00", "0.00", "0.00"),
        "customer": None, "supplier": ("0", "1000", "-1000", True, "Окончательно"),
        "totals": ("0", "0", "1000", "0", 0)}),
    ("2. Оплата поставщику 1000 («оплата поставщику»)", {
        "stock": ("10", "1000.00"), "bank": "-1000.00", "invoice": None,
        "profit": ("0.00", "0.00", "0.00"),
        "customer": None, "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "0", "0", "0", 0)}),
    ("3. Счёт покупателю 5 шт. по 200", {
        "stock": ("10", "1000.00"), "bank": "-1000.00", "invoice": ("0.00", "Не оплачен", "0"),
        "profit": ("0.00", "0.00", "0.00"),
        "customer": ("0", "0", "0", True, "Задолженностей нет"),
        "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "0", "0", "0", 0)}),
    ("4. Аванс покупателя 1000 по счёту", {
        "stock": ("10", "1000.00"), "bank": "0.00", "invoice": ("1000.00", "Оплачен", "0"),
        "profit": ("0.00", "0.00", "0.00"),
        "customer": ("-1000", "0", "-1000", True, "Окончательно"),
        "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "1000", "0", "0", 0)}),
    ("5. Отгрузка 5 шт. по счёту", {
        "stock": ("5", "500.00"), "bank": "0.00", "invoice": ("1000.00", "Оплачен", "1000"),
        "profit": ("1000.00", "500.00", "500.00"),
        "customer": ("0", "0", "0", True, "Задолженностей нет"),
        "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "0", "0", "0", 0)}),
    ("6. Возврат покупателем 2 шт. по 200", {
        "stock": ("7", "700.00"), "bank": "0.00", "invoice": ("1000.00", "Оплачен", "1000"),
        "profit": ("600.00", "300.00", "300.00"),
        "customer": ("-400", "0", "-400", True, "Окончательно"),
        "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "400", "0", "0", 0)}),
    ("7. Возврат денег покупателю 400 («возврат покупателю»)", {
        "stock": ("7", "700.00"), "bank": "-400.00", "invoice": ("1000.00", "Оплачен", "1000"),
        "profit": ("600.00", "300.00", "300.00"),
        "customer": ("0", "0", "0", True, "Задолженностей нет"),
        "supplier": ("0", "0", "0", True, "Задолженностей нет"),
        "totals": ("0", "0", "0", "0", 0)}),
]

# Итог основного сценария — общий для дополнительных проверок поверх него.
FINAL = MAIN[-1][1]


def _d(value):
    return None if value is None else D(str(value))


class Scenario:
    """Мир документов и снимок всех показателей. assert_equal — функция
    сравнения (TestCase.assertEqual или своя в скрипте), client — клиент
    Django с вошедшим бухгалтером."""

    def __init__(self, client, assert_equal):
        self.client = client
        self.assert_equal = assert_equal
        self.org = Organization.objects.create(name="Альфа", is_default=True)
        self.wh = Warehouse.objects.create(name="Основной склад", is_default=True)
        self.bank = Account.objects.create(organization=self.org, name="Р/с", kind=Account.KIND_BANK,
                                           is_default=True)
        self.cash = Account.objects.create(organization=self.org, name="Касса", kind=Account.KIND_CASH,
                                           is_default=True)
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=self.unit)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.rows = []          # (шаг, показатель, ожидалось, получено) — для таблицы

    # ---------- документы ----------

    def receipt(self, cp, qty, price, at, product=None, org=None, wh=None, day=DAY):
        doc = Receipt.objects.create(organization=org or self.org, warehouse=wh or self.wh, supplier=cp,
                                     date=date.fromisoformat(day), time=time.fromisoformat(at))
        doc.lines.create(product=product or self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        return doc

    def invoice(self, cp, qty, price, at, org=None, wh=None, day=DAY):
        doc = Invoice.objects.create(organization=org or self.org, warehouse=wh or self.wh, customer=cp,
                                     date=date.fromisoformat(day), time=time.fromisoformat(at))
        doc.lines.create(product=self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        return doc

    def ship(self, cp, qty, price, at, invoice=None, product=None, org=None, wh=None, day=DAY):
        doc = Shipment.objects.create(organization=org or self.org, warehouse=wh or self.wh, customer=cp,
                                      invoice=invoice, date=date.fromisoformat(day), time=time.fromisoformat(at))
        doc.lines.create(product=product or self.product, quantity=qty, price=price, vat_rate="none")
        doc.post()
        return doc

    def customer_return(self, cp, qty, price, at, basis, product=None, post=True, day=DAY):
        doc = CustomerReturn.objects.create(organization=basis.organization, warehouse=basis.warehouse,
                                            customer=cp, shipment=basis, date=date.fromisoformat(day), time=time.fromisoformat(at))
        doc.lines.create(product=product or self.product, quantity=qty, price=price, vat_rate="none")
        if post:
            doc.post()
        return doc

    def pay(self, kind, cp, amount, at, purpose="", invoice=None, account=None, org=None, day=DAY):
        org = org or self.org
        return Payment.objects.create(kind=kind, organization=org, account=account or self.bank, counterparty=cp,
                                      invoice=invoice, amount=amount, settlement_purpose=purpose,
                                      status=POSTED, date=date.fromisoformat(day), time=time.fromisoformat(at))

    def run_main(self, check=True):
        """Основной сценарий. check=False — только документы (для сценариев поверх него)."""
        steps = [
            lambda: self.receipt(self.supplier, 10, 100, "09:00"),
            lambda: self.pay(Payment.KIND_OUT, self.supplier, 1000, "09:30", Payment.PURPOSE_SUPPLIER_PAYMENT),
            lambda: setattr(self, "main_invoice", self.invoice(self.customer, 5, 200, "10:00")),
            lambda: self.pay(Payment.KIND_IN, self.customer, 1000, "10:30", invoice=self.main_invoice),
            lambda: setattr(self, "main_shipment",
                            self.ship(self.customer, 5, 200, "11:00", invoice=self.main_invoice)),
            lambda: setattr(self, "main_return",
                            self.customer_return(self.customer, 2, 200, "12:00", basis=self.main_shipment)),
            lambda: setattr(self, "refund", self.pay(Payment.KIND_OUT, self.customer, 400, "13:00",
                                                     Payment.PURPOSE_CUSTOMER_REFUND)),
        ]
        for action, (title, expected) in zip(steps, MAIN):
            action()
            if check:
                self.check_main(title, expected)

    # ---------- снимок ----------

    def pairs(self):
        return {(p.organization.name, p.counterparty.name): p for p in settlements.settlement_pairs()}

    @staticmethod
    def pair_values(item):
        return (item.sales, item.purchases, item.difference, item.final, item.status)

    def snapshot(self):
        """Все показатели; заодно проверяет, что страницы показывают то же,
        что сервисы (отчёт «Взаиморасчёты», карточки, «Продажи и прибыль»,
        «Счета и кассы»)."""
        # остаток ведётся по «организация + склад + товар»; здесь — сумма строк
        # организаций по складу и товару (для сверки с деньгами и прибылью)
        stock = {}
        for b in StockBalance.objects.select_related("product", "warehouse"):
            qty, value = stock.get((b.product.name, b.warehouse.name), (D("0"), D("0.00")))
            stock[(b.product.name, b.warehouse.name)] = (qty + b.quantity, value + (b.value or D("0")))
        money = {a.name: a.balance for a in Account.objects.order_by("pk")}
        invoices = {i.pk: (i.paid_amount, i.payment_status_display, i.shipped_amount)   # номера у организаций свои
                    for i in Invoice.objects.prefetch_related("lines")}
        summary = sales_summary()
        pairs = self.pairs()
        totals = settlements.totals(list(pairs.values()))

        report = self.client.get(reverse("settlements"))
        self.assert_equal([(p.organization.name, p.counterparty.name, *self.pair_values(p))
                           for p in report.context["rows"]],
                          [(*key, *self.pair_values(p)) for key, p in sorted(pairs.items())])
        self.assert_equal(report.context["totals"], totals)
        for cp in {p.counterparty for p in pairs.values()}:
            card = self.client.get(reverse("counterparty_edit", args=[cp.pk]))
            self.assert_equal([(p.organization.name, *self.pair_values(p)) for p in card.context["settlement_pairs"]],
                              [(org, *self.pair_values(p)) for (org, name), p in sorted(pairs.items())
                               if name == cp.name])
        page = self.client.get(reverse("report_sales") + "?date_from=2000-01-01&date_to=2100-12-31")
        self.assert_equal({k: page.context["summary"][k] for k in ("revenue", "cost", "profit")},
                          {k: summary[k] for k in ("revenue", "cost", "profit")})
        accounts = self.client.get(reverse("account_list"))
        self.assert_equal(accounts.context["total"], sum(money.values(), D("0")))

        return {
            "stock": stock, "money": money, "invoices": invoices,
            "profit": (summary["revenue"], summary["cost"], summary["profit"]),
            "preliminary": summary["preliminary_count"],
            "pairs": {key: self.pair_values(p) for key, p in pairs.items()},
            "totals": (totals["customers_owe"], totals["customer_advances"], totals["we_owe_suppliers"],
                       totals["supplier_advances"], totals["unallocated_count"]),
            # деньги + склад + разница по документам; при нулевых началах = прибыль
            "identity": (sum(money.values(), D("0"))
                         + sum((value or D("0") for _, value in stock.values()), D("0"))
                         + sum((p.difference for p in pairs.values()), D("0"))),
        }

    def check(self, title, name, expected, actual):
        self.rows.append((title, name, expected, actual))
        self.assert_equal(actual, expected)

    def check_main(self, title, expected):
        shot = self.snapshot()
        stock = shot["stock"].get(("Товар", "Основной склад"))
        self.check(title, "склад: количество и стоимость", tuple(map(_d, expected["stock"])), stock)
        self.check(title, "деньги на «Р/с»", _d(expected["bank"]), shot["money"]["Р/с"])
        self.check(title, "деньги в кассе", D("0"), shot["money"]["Касса"])
        invoice = shot["invoices"].get(getattr(self, "main_invoice", None) and self.main_invoice.pk)
        want_invoice = expected["invoice"] and (_d(expected["invoice"][0]), expected["invoice"][1],
                                                _d(expected["invoice"][2]))
        self.check(title, "счёт: оплачено, статус, отгружено", want_invoice, invoice)
        self.check(title, "выручка, себестоимость, прибыль", tuple(map(_d, expected["profit"])), shot["profit"])
        for role, cp in (("customer", self.customer), ("supplier", self.supplier)):
            want = expected[role] and (*map(_d, expected[role][:3]), *expected[role][3:])
            self.check(title, f"взаиморасчёты: {cp.name}", want, shot["pairs"].get(("Альфа", cp.name)))
        self.check(title, "итоги отчёта", (*map(_d, expected["totals"][:4]), expected["totals"][4]),
                   shot["totals"])
        self.check(title, "сверка: деньги + склад + разница = прибыль", shot["profit"][2], shot["identity"])
        return shot


class EndToEndBase(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("e2e", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ACCOUNTANT))
        self.client.force_login(user)
        self.s = Scenario(self.client, self.assertEqual)

    def state(self):
        """Снимок для «ничего не изменилось» (без проверки страниц — их
        сверяет snapshot)."""
        shot = self.s.snapshot()
        return {key: shot[key] for key in ("stock", "money", "invoices", "profit", "pairs", "totals")}, (
            sorted(Payment.objects.values_list("pk", "status", "amount", "settlement_purpose", "invoice_id")),
            sorted(Shipment.objects.values_list("pk", "status")),
            sorted(CustomerReturn.objects.values_list("pk", "status")),
            sorted(Receipt.objects.values_list("pk", "status")),
        )

    def diagnostics(self):
        outputs = {}
        for name in ("check_costs", "check_returns", "check_payments", "check_settlements"):
            out = StringIO()
            call_command(name, stdout=out)
            outputs[name] = out.getvalue()
        return outputs


# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------

class MainScenarioTests(EndToEndBase):
    def test_main_scenario_step_by_step(self):
        self.s.run_main()

    def test_invoice_paid_shows_payments_by_invoice_not_debt(self):
        """«Оплачено по счёту» = сумма проведённых входящих платежей со ссылкой
        на этот счёт. Возврат товара — складской документ, возврат денег —
        исходящий платёж, а исходящий со счётом покупателю не связывается.
        Поэтому после шагов 6–7 счёт остаётся «Оплачен» на 1000, хотя чистые
        деньги покупателя 600 и долга нет; «отгружено по счёту» — 1000
        (возврат его не уменьшает)."""
        self.s.run_main(check=False)
        invoice = Invoice.objects.get(pk=self.s.main_invoice.pk)
        self.assertEqual((invoice.paid_amount, invoice.payment_status_display, invoice.shipped_amount),
                         (D("1000.00"), "Оплачен", D("1000")))
        with self.assertRaises(PaymentError):                  # возврат денег к счёту не привязать
            Payment.objects.filter(pk=self.s.refund.pk).update(invoice=self.s.main_invoice)
        item = self.s.pairs()[("Альфа", "Покупатель")]
        self.assertEqual((item.customer_paid, item.customer_refunded, item.sales), (D("1000"), D("400"), D("0")))

    def test_diagnostics_are_clean_after_scenario(self):
        self.s.run_main(check=False)
        out = self.diagnostics()
        self.assertIn("Расхождений нет", out["check_costs"])
        self.assertIn("Нарушений нет", out["check_returns"])
        self.assertIn("Расхождений нет", out["check_payments"])
        self.assertIn("документов для уточнения — 0, несовместимых назначений — 0", out["check_settlements"])


# ---------------------------------------------------------------------------
# Дополнительные сценарии
# ---------------------------------------------------------------------------

class AdditionalScenarioTests(EndToEndBase):
    def test_sale_before_receipt_then_deficit_is_covered(self):
        self.s.org.allow_negative_stock = True
        self.s.org.save()
        shipment = self.s.ship(self.s.customer, 3, 200, "10:00")
        shot = self.s.snapshot()
        # продано 3 при нулевом остатке: −3 шт., стоимость 0; себестоимость не определена
        self.assertEqual(shot["stock"][("Товар", "Основной склад")], (D("-3"), D("0.00")))
        self.assertEqual((shot["profit"], shot["preliminary"]), ((D("600"), D("0"), D("600")), 1))
        self.assertEqual(shot["pairs"][("Альфа", "Покупатель")][:4], (D("600"), D("0"), D("600"), True))
        self.s.receipt(self.s.supplier, 5, 100, "11:00")
        shot = self.s.snapshot()
        # поступление покрыло 3 шт. по 100: себестоимость 300, на складе 2 шт. на 200
        self.assertEqual(shot["stock"][("Товар", "Основной склад")], (D("2"), D("200.00")))
        self.assertEqual((shot["profit"], shot["preliminary"]), ((D("600"), D("300"), D("300")), 0))
        self.assertEqual(Shipment.objects.get(pk=shipment.pk).lines.get().cost_amount, D("300.00"))
        self.assertEqual(DeficitCover.objects.count(), 1)
        self.assertEqual(shot["pairs"][("Альфа", "Поставщик")][:4], (D("0"), D("500"), D("-500"), True))
        self.assertEqual(shot["identity"], D("300"))           # 0 + 200 + 600 − 500

    def test_sale_and_return_in_different_months(self):
        self.s.receipt(self.s.supplier, 10, 100, "09:00", day="2026-09-01")
        shipment = self.s.ship(self.s.customer, 5, 200, "10:00", day="2026-09-15")
        self.s.customer_return(self.s.customer, 2, 200, "10:00", basis=shipment, day="2026-10-03")
        september = sales_summary(date(2026, 9, 1), date(2026, 9, 30))
        october = sales_summary(date(2026, 10, 1), date(2026, 10, 31))
        whole = sales_summary()
        self.assertEqual((september["revenue"], september["cost"], september["profit"], september["returns_revenue"]),
                         (D("1000"), D("500"), D("500"), D("0")))
        self.assertEqual((october["revenue"], october["cost"], october["profit"],
                          october["returns_revenue"], october["returns_cost"]),
                         (D("-400"), D("-200"), D("-200"), D("400"), D("200")))
        self.assertEqual((whole["revenue"], whole["cost"], whole["profit"]), (D("600"), D("300"), D("300")))
        page = self.client.get(reverse("report_sales") + "?date_from=2026-10-01&date_to=2026-10-31")
        self.assertEqual(page.context["summary"]["profit"], D("-200"))
        shot = self.s.snapshot()
        self.assertEqual(shot["stock"][("Товар", "Основной склад")], (D("7"), D("700.00")))
        self.assertEqual(shot["pairs"][("Альфа", "Покупатель")][:4], (D("600"), D("0"), D("600"), True))

    def test_second_organization_does_not_affect_first(self):
        s = self.s
        s.receipt(s.supplier, 10, 100, "09:00")
        invoice_a = s.invoice(s.customer, 2, 200, "10:00")
        s.ship(s.customer, 2, 200, "11:00", invoice=invoice_a)
        s.pay(Payment.KIND_IN, s.customer, 400, "12:00", invoice=invoice_a)
        first = s.snapshot()
        alfa = {key: value for key, value in first["pairs"].items() if key[0] == "Альфа"}
        beta = Organization.objects.create(name="Бета")
        wh_b = Warehouse.objects.create(name="Склад Бета")
        bank_b = Account.objects.create(organization=beta, name="Р/с Бета", is_default=True)
        s.receipt(s.supplier, 10, 150, "09:00", org=beta, wh=wh_b)
        invoice_b = s.invoice(s.customer, 1, 300, "10:00", org=beta, wh=wh_b)
        s.ship(s.customer, 1, 300, "11:00", invoice=invoice_b, org=beta, wh=wh_b)
        s.pay(Payment.KIND_IN, s.customer, 300, "12:00", invoice=invoice_b, account=bank_b, org=beta)
        with self.assertRaises(PaymentError):                  # счёт Альфы платежом Беты не оплатить
            s.pay(Payment.KIND_IN, s.customer, 100, "13:00", invoice=invoice_a, account=bank_b, org=beta)
        shot = s.snapshot()
        # Альфа — без изменений
        self.assertEqual(shot["stock"][("Товар", "Основной склад")], (D("8"), D("800.00")))
        self.assertEqual(shot["stock"][("Товар", "Основной склад")], first["stock"][("Товар", "Основной склад")])
        self.assertEqual(shot["money"]["Р/с"], D("400.00"))
        self.assertEqual(shot["invoices"][invoice_a.pk][0], D("400.00"))
        self.assertEqual({k: v for k, v in shot["pairs"].items() if k[0] == "Альфа"}, alfa)
        self.assertEqual(alfa[("Альфа", "Покупатель")][:4], (D("0"), D("0"), D("0"), True))
        self.assertEqual(alfa[("Альфа", "Поставщик")][:4], (D("0"), D("1000"), D("-1000"), True))
        # Бета — своё
        self.assertEqual(shot["stock"][("Товар", "Склад Бета")], (D("9"), D("1350.00")))
        self.assertEqual(shot["money"]["Р/с Бета"], D("300.00"))
        self.assertEqual(shot["invoices"][invoice_b.pk][0], D("300.00"))
        self.assertEqual(shot["pairs"][("Бета", "Покупатель")][:4], (D("0"), D("0"), D("0"), True))
        self.assertEqual(shot["pairs"][("Бета", "Поставщик")][:4], (D("0"), D("1500"), D("-1500"), True))
        # итоги — без сведения: мы должны поставщику 1000 + 1500
        self.assertEqual(shot["totals"], (D("0"), D("0"), D("2500"), D("0"), 0))
        # отчёт «Продажи и прибыль» общий по организациям (фильтра по организации нет)
        self.assertEqual(shot["profit"], (D("700"), D("350"), D("350")))

    def test_two_organizations_on_one_warehouse(self):
        """Учёт по организациям (18.10.2026): А и Б покупают один товар на общий
        склад по разным ценам и продают одному покупателю. Ожидания — вручную:
        А: 10 × 100, продано 5 × 200 → себестоимость 500, прибыль 500, склад 5 / 500;
        Б: 10 × 200, продано 2 × 300 → себестоимость 400, прибыль 200, склад 8 / 1 600;
        деньги: А −1 000 + 1 000 = 0, Б −2 000 + 600 = −1 400; долгов нет;
        сверка в целом: −1 400 + 2 100 + 0 = 700 = прибыль 500 + 200."""
        s = self.s
        beta = Organization.objects.create(name="Бета")
        bank_b = Account.objects.create(organization=beta, name="Р/с Бета", is_default=True)
        s.receipt(s.supplier, 10, 100, "09:00")
        s.receipt(s.supplier, 10, 200, "09:10", org=beta)
        s.pay(Payment.KIND_OUT, s.supplier, 1000, "09:20", Payment.PURPOSE_SUPPLIER_PAYMENT)
        s.pay(Payment.KIND_OUT, s.supplier, 2000, "09:30", Payment.PURPOSE_SUPPLIER_PAYMENT, account=bank_b, org=beta)
        s.ship(s.customer, 5, 200, "10:00")
        s.ship(s.customer, 2, 300, "10:10", org=beta)
        s.pay(Payment.KIND_IN, s.customer, 1000, "11:00", Payment.PURPOSE_CUSTOMER_PAYMENT)
        s.pay(Payment.KIND_IN, s.customer, 600, "11:10", Payment.PURPOSE_CUSTOMER_PAYMENT, account=bank_b, org=beta)
        shot = s.snapshot()
        stock = {(b.organization.name, b.warehouse.name): (b.quantity, b.value)
                 for b in StockBalance.objects.select_related("organization", "warehouse")}
        self.assertEqual(stock, {("Альфа", "Основной склад"): (D("5"), D("500.00")),
                                 ("Бета", "Основной склад"): (D("8"), D("1600.00"))})
        alfa, beta_summary = (sales_summary(organization=s.org.pk), sales_summary(organization=beta.pk))
        self.assertEqual((alfa["revenue"], alfa["cost"], alfa["profit"]), (D("1000"), D("500"), D("500")))
        self.assertEqual((beta_summary["revenue"], beta_summary["cost"], beta_summary["profit"]),
                         (D("600"), D("400"), D("200")))
        self.assertEqual(shot["profit"], (D("1600"), D("900"), D("700")))
        self.assertEqual((shot["money"]["Р/с"], shot["money"]["Р/с Бета"]), (D("0.00"), D("-1400.00")))
        for key in (("Альфа", "Покупатель"), ("Бета", "Покупатель"), ("Альфа", "Поставщик"), ("Бета", "Поставщик")):
            self.assertEqual(shot["pairs"][key][:4], (D("0"), D("0"), D("0"), True), key)
        self.assertEqual(shot["identity"], D("700"))

    def test_customer_who_is_also_supplier(self):
        s = self.s
        both = Counterparty.objects.create(name="Партнёр", partner_type=Counterparty.TYPE_BOTH)
        s.receipt(both, 4, 100, "09:00")
        s.ship(both, 2, 300, "10:00")
        shot = s.snapshot()
        self.assertEqual(shot["pairs"][("Альфа", "Партнёр")], (D("600"), D("400"), D("200"), True, "Окончательно"))
        self.assertEqual(shot["totals"], (D("600"), D("0"), D("400"), D("0"), 0))      # не 200: без зачёта
        item = s.pairs()[("Альфа", "Партнёр")]
        self.assertIn("встречные", " ".join(item.explanations))
        s.pay(Payment.KIND_OUT, both, 400, "11:00", Payment.PURPOSE_SUPPLIER_PAYMENT)
        self.assertEqual(s.snapshot()["pairs"][("Альфа", "Партнёр")][:3], (D("600"), D("0"), D("600")))
        s.pay(Payment.KIND_IN, both, 600, "12:00", Payment.PURPOSE_CUSTOMER_PAYMENT)
        shot = s.snapshot()
        self.assertEqual(shot["pairs"][("Альфа", "Партнёр")], (D("0"), D("0"), D("0"), True, "Задолженностей нет"))
        self.assertEqual((shot["money"]["Р/с"], shot["stock"][("Товар", "Основной склад")], shot["profit"]),
                         (D("200.00"), (D("2"), D("200.00")), (D("600"), D("200"), D("400"))))
        self.assertEqual(shot["identity"], D("400"))

    def test_unallocated_payment_then_clarified_without_money_change(self):
        s = self.s
        buyer = Counterparty.objects.create(name="Клиент", partner_type=Counterparty.TYPE_CUSTOMER)
        s.receipt(s.supplier, 10, 100, "09:00")
        s.pay(Payment.KIND_OUT, s.supplier, 1000, "09:30", Payment.PURPOSE_SUPPLIER_PAYMENT)
        s.ship(buyer, 1, 500, "10:00")
        payment = s.pay(Payment.KIND_IN, buyer, 500, "11:00")             # без счёта и назначения
        shot = s.snapshot()
        self.assertEqual(Payment.objects.get(pk=payment.pk).settlement_purpose, Payment.PURPOSE_UNALLOCATED)
        self.assertEqual(shot["pairs"][("Альфа", "Клиент")], (D("500"), D("0"), D("0"), False, "Предварительно"))
        self.assertEqual(shot["totals"][4], 1)
        before = (shot["money"], shot["stock"], shot["profit"], shot["invoices"])
        response = self.client.post(reverse("payment_edit", args=[payment.pk]), {
            "date": DAY, "number": payment.number, "time": "11:00", "organization": s.org.pk,
            "account": s.bank.pk, "counterparty": buyer.pk, "invoice": "", "amount": "500.00",
            "settlement_purpose": Payment.PURPOSE_CUSTOMER_PAYMENT, "purpose": "", "action": "save",
        })
        self.assertEqual(response.status_code, 302)
        shot = s.snapshot()
        self.assertEqual(shot["pairs"][("Альфа", "Клиент")], (D("0"), D("0"), D("0"), True, "Задолженностей нет"))
        self.assertEqual((shot["money"], shot["stock"], shot["profit"], shot["invoices"]), before)
        self.assertEqual(shot["money"]["Р/с"], D("-500.00"))
        self.assertEqual(Payment.objects.get(pk=payment.pk).amount, D("500.00"))
        self.assertEqual(shot["identity"], D("400"))           # −500 + 900 + 0

    def test_cash_correction_changes_money_only(self):
        self.s.run_main(check=False)
        before = self.state()[0]
        AccountCorrection.objects.create(organization=self.s.org, account=self.s.cash, actual_balance=250,
                                         date=date.fromisoformat(DAY), time=time(14, 0)).post()
        after = self.state()[0]
        self.assertEqual(after["money"], {**before["money"], "Касса": D("250.00")})
        self.assertEqual({k: v for k, v in after.items() if k != "money"},
                         {k: v for k, v in before.items() if k != "money"})
        shot = self.s.snapshot()
        # излишек кассы 250 — не прибыль от продаж: сверка расходится ровно на него
        self.assertEqual(shot["identity"] - shot["profit"][2], D("250"))

    def test_settlement_correction_changes_chosen_side_only(self):
        self.s.run_main(check=False)
        before = self.state()[0]
        SettlementCorrection.objects.create(organization=self.s.org, counterparty=self.s.customer,
                                            direction=SettlementCorrection.DIR_THEY_OWE, amount=100,
                                            side=SettlementCorrection.SIDE_SALES).post()
        SettlementCorrection.objects.create(organization=self.s.org, counterparty=self.s.supplier,
                                            direction=SettlementCorrection.DIR_WE_OWE, amount=50,
                                            side=SettlementCorrection.SIDE_PURCHASES).post()
        after = self.state()[0]
        self.assertEqual(after["pairs"][("Альфа", "Покупатель")], (D("100"), D("0"), D("100"), True, "Окончательно"))
        self.assertEqual(after["pairs"][("Альфа", "Поставщик")], (D("0"), D("50"), D("-50"), True, "Окончательно"))
        for key in ("stock", "money", "invoices", "profit"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["totals"], (D("100"), D("0"), D("50"), D("0"), 0))

    def test_unpost_and_repost(self):
        s = self.s
        s.run_main(check=False)
        final = self.state()
        # снять возврат денег: деньги +400, покупателю снова должны 400
        s.refund.unpost()
        shot = s.snapshot()
        self.assertEqual((shot["money"]["Р/с"], shot["pairs"][("Альфа", "Покупатель")][:3]),
                         (D("0.00"), (D("-400"), D("0"), D("-400"))))
        Payment.objects.get(pk=s.refund.pk).post()
        self.assertEqual(self.state(), final)
        # снять возврат товара: склад 5/500, прибыль 500; деньги покупателю уже вернули — он должен 400
        CustomerReturn.objects.get(pk=s.main_return.pk).unpost()
        shot = s.snapshot()
        self.assertEqual((shot["stock"][("Товар", "Основной склад")], shot["profit"],
                          shot["pairs"][("Альфа", "Покупатель")][:3]),
                         ((D("5"), D("500.00")), (D("1000"), D("500"), D("500")), (D("400"), D("0"), D("400"))))
        self.assertEqual(shot["identity"], D("500"))           # −400 + 500 + 400
        CustomerReturn.objects.get(pk=s.main_return.pk).post()
        self.assertEqual(self.state(), final)
        # снять оплату по счёту: счёт «Не оплачен», деньги −1400, покупатель должен 1000
        invoice_payment = Payment.objects.get(invoice=s.main_invoice)
        invoice_payment.unpost()
        shot = s.snapshot()
        self.assertEqual((shot["invoices"][s.main_invoice.pk][:2], shot["money"]["Р/с"],
                          shot["pairs"][("Альфа", "Покупатель")][:3]),
                         ((D("0.00"), "Не оплачен"), D("-1400.00"), (D("1000"), D("0"), D("1000"))))
        Payment.objects.get(pk=invoice_payment.pk).post()
        Payment.objects.get(pk=invoice_payment.pk).post()       # повторное проведение не удваивает
        self.assertEqual(self.state(), final)
        self.assertIn("Расхождений нет", self.diagnostics()["check_costs"])


class RejectedOperationTests(EndToEndBase):
    """Недопустимая операция отклоняется целиком: ни склад, ни деньги, ни
    оплата счёта, ни прибыль, ни взаиморасчёты, ни статусы документов не
    меняются."""

    def setUp(self):
        super().setUp()
        self.s.run_main(check=False)
        self.before = self.state()

    def test_return_more_than_left_to_return(self):
        doc = self.s.customer_return(self.s.customer, 4, 200, "14:00", basis=self.s.main_shipment, post=False)
        before = self.state()                                  # черновик на показатели не влияет
        with self.assertRaises(StockError):
            doc.post()                                         # отгружено 5, возвращено 2, можно 3
        self.assertEqual(self.state(), before)
        self.assertEqual(CustomerReturn.objects.get(pk=doc.pk).status, DRAFT)

    def test_unposting_shipment_with_posted_return(self):
        with self.assertRaises(StockError):
            Shipment.objects.get(pk=self.s.main_shipment.pk).unpost()
        self.assertEqual(self.state(), self.before)

    def test_shipment_beyond_stock_when_negative_is_not_allowed(self):
        shipment = Shipment.objects.create(organization=self.s.org, warehouse=self.s.wh, customer=self.s.customer,
                                           date=date.fromisoformat(DAY), time=time(14, 0))
        shipment.lines.create(product=self.s.product, quantity=8, price=200, vat_rate="none")
        before = self.state()
        with self.assertRaises(StockError):
            shipment.post()                                    # на складе 7
        self.assertEqual(self.state(), before)

    def test_payment_with_contradicting_purpose(self):
        with self.assertRaises(PaymentError):
            self.s.pay(Payment.KIND_IN, self.s.customer, 100, "14:00", Payment.PURPOSE_SUPPLIER_REFUND,
                       invoice=self.s.main_invoice)
        self.assertEqual(self.state(), self.before)

    def test_bulk_change_with_one_bad_row(self):
        refund = Payment.objects.get(pk=self.s.refund.pk)
        invoice_payment = Payment.objects.get(invoice=self.s.main_invoice)
        refund.amount, invoice_payment.settlement_purpose = D("450"), Payment.PURPOSE_UNALLOCATED
        with self.assertRaises(PaymentError):
            Payment.objects.bulk_update([refund, invoice_payment], ["amount", "settlement_purpose"])
        self.assertEqual(self.state(), self.before)
