"""Отчёт «Продажи и прибыль» с возвратами покупателей.

Правила: отгрузка относится к дате отгрузки, возврат — к дате возврата.
Выручку уменьшает сумма проведённого возврата по его ценам и скидкам,
себестоимость — точная сумма, восстановленная складским движением возврата.
Прибыль = выручка после возвратов − себестоимость после возвратов; это не
движение денег. Ожидаемые суммы посчитаны вручную и записаны константами.
"""
import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product
from apps.core import roles
from apps.inventory.models import StockMovement
from apps.inventory.tests_costing import NOON
from apps.inventory.tests_deficit import DeficitBase
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import CustomerReturn, Shipment, ShipmentLine

from . import services

D = Decimal
ZERO = D("0.00")


def day(month, number):
    return datetime.date(2026, month, number)


class ReturnsInReportBase(DeficitBase):
    """Документы — помощники DeficitBase (минус разрешён) и свои — с ценой и скидкой."""

    def summary(self, date_from=None, date_to=None):
        return services.sales_summary(date_from, date_to)

    def money(self, summary):
        return summary["revenue"], summary["cost"], summary["profit"]

    def sale_doc(self, when, rows, customer=None):
        """Отгрузка: rows — [(товар, кол-во, цена, скидка %)]."""
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=customer or self.customer,
                                      date=when, time=NOON)
        for product, qty, price, discount in rows:
            doc.lines.create(product=product, quantity=D(str(qty)), price=D(str(price)), discount=D(str(discount)))
        doc.post()
        return doc

    def return_doc(self, when, rows, shipment=None, customer=None, post=True):
        """Возврат: rows — [(товар, кол-во, цена, скидка %, строка отгрузки или None)]."""
        doc = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh,
                                            customer=customer or self.customer, shipment=shipment,
                                            date=when, time=NOON)
        for product, qty, price, discount, line in rows:
            doc.lines.create(product=product, quantity=D(str(qty)), price=D(str(price)), discount=D(str(discount)),
                             shipment_line=line)
        if post:
            doc.post()
        return doc

    def assertDetailsMatch(self, date_from=None, date_to=None):
        """Итог = сумма по товарам = сумма по покупателям (выручка, себестоимость, прибыль)."""
        total = self.summary(date_from, date_to)
        for rows in (services.product_rows(date_from, date_to), services.customer_rows(date_from, date_to)):
            for key in ("revenue", "cost", "profit"):
                self.assertEqual(sum((row[key] for row in rows), ZERO), total[key], key)
            self.assertEqual(sum((row["sales_revenue"] for row in rows), ZERO), total["sales_revenue"])
            self.assertEqual(sum((row["returns_revenue"] for row in rows), ZERO), total["returns_revenue"])

    def login(self):
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")


class ReportedCaseTests(ReturnsInReportBase):
    def test_full_return_leaves_nothing(self):
        """Воспроизведение: приёмка 1 по 100 → отгрузка 1 по 500 → полный возврат
        по этой строке по 500. Склад: 1 шт. на 100. Было: выручка 500,
        себестоимость 100, прибыль 400 — возврат не учитывался."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1)
        self.give_back(3, 1, shipment=sale, line=sale.lines.get())
        self.assertEqual(self.stock(), (D("1"), D("100.00"), D("0")))
        result = self.summary()
        self.assertEqual(self.money(result), (ZERO, ZERO, ZERO))
        # продажи по-прежнему видны отдельно
        self.assertEqual((result["sales_revenue"], result["sales_cost"], result["sales_profit"]),
                         (D("500.00"), D("100.00"), D("400.00")))
        self.assertEqual((result["returns_revenue"], result["returns_cost"], result["returns_profit"]),
                         (D("500.00"), D("100.00"), D("400.00")))
        self.assertIsNone(result["margin"])                              # выручка 0 — маржи нет
        self.assertEqual((result["count"], result["returns_count"]), (1, 1))
        self.assertDetailsMatch()

    def test_sale_in_january_return_in_february(self):
        """Возврат относится к дате возврата: январь +500/+100/+400,
        февраль −500/−100/−400, вместе ноль. Январь задним числом не меняется."""
        self.receive(day(1, 1), 1, "100.00")
        sale = self.sell(day(1, 20), 1)
        january = self.summary(day(1, 1), day(1, 31))
        self.give_back(day(2, 3), 1, shipment=sale, line=sale.lines.get())
        self.assertEqual(self.summary(day(1, 1), day(1, 31)), january)
        self.assertEqual(self.money(january), (D("500.00"), D("100.00"), D("400.00")))
        self.assertEqual(january["margin"], D("80"))
        february = self.summary(day(2, 1), day(2, 28))
        self.assertEqual(self.money(february), (D("-500.00"), D("-100.00"), D("-400.00")))
        self.assertEqual((february["sales_revenue"], february["returns_revenue"]), (ZERO, D("500.00")))
        self.assertEqual((february["count"], february["returns_count"]), (0, 1))
        self.assertIsNone(february["margin"])
        self.assertEqual(self.money(self.summary(day(1, 1), day(2, 28))), (ZERO, ZERO, ZERO))
        for period in ((day(1, 1), day(1, 31)), (day(2, 1), day(2, 28)), (day(1, 1), day(2, 28))):
            self.assertDetailsMatch(*period)


class AmountsTests(ReturnsInReportBase):
    def test_partial_returns_discount_fraction_and_document_rounding(self):
        """Приёмка 3 по 120,01 = 360,03. Отгрузка 2,5 по 199,99 со скидкой 7,5 %:
        2,5 × 199,99 × 0,925 = 462,476875 → 462,48; себестоимость
        r(360,03 × 2,5/3) = r(300,025) = 300,03. Возврат 0,7 по той же цене:
        129,493525 → 129,49; себестоимость r(300,03 × 0,7/2,5) = 84,01."""
        self.receive(1, 3, "120.01")
        sale = self.sale_doc(day(1, 2), [(self.product, "2.5", "199.99", "7.5")])
        line = sale.lines.get()
        first = self.return_doc(day(1, 3), [(self.product, "0.7", "199.99", "7.5", line)], shipment=sale)
        result = self.summary()
        self.assertEqual((result["sales_revenue"], result["sales_cost"]), (D("462.48"), D("300.03")))
        self.assertEqual((result["returns_revenue"], result["returns_cost"]), (D("129.49"), D("84.01")))
        self.assertEqual(self.money(result), (D("332.99"), D("216.02"), D("116.97")))
        # выручка сходится с итогами документов
        self.assertEqual(result["revenue"], Shipment.objects.get(pk=sale.pk).total - CustomerReturn.objects.get(
            pk=first.pk).total)
        # остаток 1,8: 332,98335 → 332,98; себестоимость 300,03 − 84,01 = 216,02
        second = self.return_doc(day(1, 4), [(self.product, "1.8", "199.99", "7.5", line)], shipment=sale)
        result = self.summary()
        self.assertEqual(result["returns_cost"], D("300.03"))            # восстановлено ровно всё
        self.assertEqual(result["cost"], ZERO)
        # выручка — по документам: 462,48 − 129,49 − 332,98 = 0,01 (каждый документ округляет свою строку)
        self.assertEqual(result["revenue"], D("0.01"))
        self.assertEqual(result["revenue"], sale.total - first.total - second.total)
        self.assertEqual(result["profit"], D("0.01"))
        self.assertDetailsMatch()

    def test_return_at_another_price_uses_document_amount(self):
        """Вернули по 450 (продано по 500): выручка уменьшается на 450, а не на
        500; прибыль после возврата 50, а не ноль."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1)
        self.return_doc(day(1, 3), [(self.product, 1, 450, 0, sale.lines.get())], shipment=sale)
        self.assertEqual(self.money(self.summary()), (D("50.00"), ZERO, D("50.00")))
        self.assertEqual(self.summary()["margin"], D("100"))

    def test_lines_of_one_product_returned_by_lines(self):
        """Строки 100 и 300 (вторая продана в минус, покрыта приходом по 300).
        Возврат второй строки снимает 300 себестоимости."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1, 1)
        self.receive(3, 1, "300.00")
        second = ShipmentLine.objects.filter(shipment=sale).order_by("pk")[1]
        self.give_back(4, 1, shipment=sale, line=second)
        result = self.summary()
        self.assertEqual((result["returns_revenue"], result["returns_cost"]), (D("500.00"), D("300.00")))
        self.assertEqual(self.money(result), (D("500.00"), D("100.00"), D("400.00")))
        self.assertDetailsMatch()

    def test_lines_of_one_product_returned_as_whole_shipment(self):
        """Те же строки, возвраты без строки (по отгрузке в целом): по 200 —
        после обоих себестоимость, выручка и прибыль нулевые."""
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1, 1)
        self.receive(3, 1, "300.00")
        self.give_back(4, 1, shipment=sale)
        self.assertEqual(self.money(self.summary()), (D("500.00"), D("200.00"), D("300.00")))
        self.give_back(5, 1, shipment=sale)
        self.assertEqual(self.money(self.summary()), (ZERO, ZERO, ZERO))
        self.assertEqual(self.summary()["returns_cost"], D("400.00"))

    def test_return_without_basis(self):
        """Продано с пустого склада, возврат без основания — по цене карточки,
        зафиксированной в возврате (77); он покрывает дефицит продажи. Продажа и
        возврат — по 77, итог нулевой, без пометки «предварительная»."""
        sale = self.sell(2, 1)
        self.assertTrue(self.summary()["preliminary_count"])
        self.give_back(3, 1)
        result = self.summary()
        self.assertEqual((result["sales_cost"], result["returns_cost"]), (D("77.00"), D("77.00")))
        self.assertEqual(self.money(result), (ZERO, ZERO, ZERO))
        self.assertEqual((result["preliminary_count"], result["preliminary_returns"]), (0, 0))
        self.assertEqual(ShipmentLine.objects.get(shipment=sale).cost_open_qty, D("0"))

    def test_services_are_counted_without_cost(self):
        service = Product.objects.create(name="Доставка", unit=self.unit, item_type=Product.TYPE_SERVICE)
        sale = self.sale_doc(day(1, 2), [(service, 1, 300, 0)])
        self.return_doc(day(1, 3), [(service, 1, 300, 0, None)], shipment=sale)
        result = self.summary()
        self.assertEqual(self.money(result), (ZERO, ZERO, ZERO))
        self.assertEqual((result["estimated_count"], result["missing_cost_count"]), (0, 0))


class DeficitTests(ReturnsInReportBase):
    def test_full_return_of_uncovered_sale_leaves_no_profit(self):
        """Продано с пустого склада, полностью возвращено до поступления: за
        общий период — ноль и без пометки. Январь 2 отдельно — продажа без
        себестоимости (помечена), 3-е отдельно — возврат такой единицы (помечен)."""
        sale = self.sell(2, 1)
        self.give_back(3, 1, shipment=sale)
        result = self.summary()
        self.assertEqual(self.money(result), (ZERO, ZERO, ZERO))
        self.assertEqual((result["open_qty"], result["preliminary_count"], result["returns_open_qty"],
                          result["preliminary_returns"]), (D("0"), 0, D("0"), 0))
        sold = self.summary(day(1, 2), day(1, 2))
        self.assertEqual(self.money(sold), (D("500.00"), ZERO, D("500.00")))
        self.assertEqual((sold["open_qty"], sold["preliminary_count"]), (D("1"), 1))
        returned = self.summary(day(1, 3), day(1, 3))
        self.assertEqual(self.money(returned), (D("-500.00"), ZERO, D("-500.00")))
        self.assertEqual((returned["returns_open_qty"], returned["preliminary_returns"]), (D("1"), 1))

    def test_return_before_cover(self):
        """Отгрузка 2 с пустого (2-е), возврат 1 по строке (3-е, гасит непокрытую
        единицу — себестоимость 0), приход 1 по 250 (4-е) покрывает вторую.
        Итог: выручка 500, себестоимость 250, прибыль 250; не предварительная."""
        sale = self.sell(2, 2)
        self.give_back(3, 1, shipment=sale)
        self.receive(4, 1, "250.00")
        result = self.summary()
        self.assertEqual((result["sales_cost"], result["returns_cost"]), (D("250.00"), ZERO))
        self.assertEqual(self.money(result), (D("500.00"), D("250.00"), D("250.00")))
        self.assertEqual((result["open_qty"], result["preliminary_count"], result["preliminary_returns"]),
                         (D("0"), 0, 0))
        self.assertTrue(self.summary(day(1, 2), day(1, 2))["preliminary_count"])
        self.assertTrue(self.summary(day(1, 3), day(1, 3))["preliminary_returns"])

    def test_partial_return_of_uncovered_sale_stays_preliminary(self):
        sale = self.sell(2, 2)
        self.give_back(3, 1, shipment=sale)
        result = self.summary()
        self.assertEqual(self.money(result), (D("500.00"), ZERO, D("500.00")))
        self.assertEqual((result["open_qty"], result["preliminary_count"]), (D("1"), 1))

    def test_return_after_cover(self):
        """Отгрузка 2 с пустого, приход 2 по 250 покрыл её (500), возврат 1 —
        восстановлено 250. Итог: 500 / 250 / 250."""
        sale = self.sell(2, 2)
        self.receive(3, 2, "250.00")
        self.give_back(4, 1, shipment=sale)
        result = self.summary()
        self.assertEqual((result["sales_cost"], result["returns_cost"]), (D("500.00"), D("250.00")))
        self.assertEqual(self.money(result), (D("500.00"), D("250.00"), D("250.00")))
        self.assertEqual((result["preliminary_count"], result["preliminary_returns"]), (0, 0))


class StatusTests(ReturnsInReportBase):
    def test_draft_unpost_and_repost(self):
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1)
        only_sale = self.summary()
        back = self.give_back(3, 1, shipment=sale, post=False)
        self.assertEqual(self.summary(), only_sale)                      # черновик не учитывается
        CustomerReturn.objects.get(pk=back.pk).post()
        with_return = self.summary()
        self.assertEqual(self.money(with_return), (ZERO, ZERO, ZERO))
        CustomerReturn.objects.get(pk=back.pk).unpost()
        self.assertEqual(self.summary(), only_sale)                      # снятый с проведения — тоже
        CustomerReturn.objects.get(pk=back.pk).post()
        self.assertEqual(self.summary(), with_return)

    def test_old_return_without_exact_amount_is_estimated_and_flagged(self):
        """Сумма движения старого возврата ещё не записана: берётся прежняя
        оценка «количество × цена единицы» (как у строк отгрузки) и отчёт
        требует пересчёта. Чтение отчёта сумму не записывает."""
        self.login()
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1)
        back = self.give_back(3, 1, shipment=sale)
        StockMovement.objects.filter(doc_type="customer_return").update(amount=None)
        result = self.summary()
        self.assertEqual((result["returns_cost"], result["estimated_count"]), (D("100.00"), 1))
        page = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
        self.assertContains(page, "data-report-needs-recalc")
        self.assertContains(page, "Для 1 строк продаж и возвратов точная себестоимость ещё не записана")
        self.assertIsNone(StockMovement.objects.get(doc_type="customer_return", doc_id=back.pk).amount)

    def test_return_without_stock_movement_is_flagged_not_zero(self):
        self.login()
        self.receive(1, 1, "100.00")
        sale = self.sell(2, 1)
        self.give_back(3, 1, shipment=sale)
        StockMovement.objects.filter(doc_type="customer_return").delete()   # повреждённые старые данные
        result = self.summary()
        self.assertEqual((result["returns_cost"], result["missing_cost_count"]), (ZERO, 1))
        page = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
        self.assertContains(page, "нет складского движения")

    def test_reading_writes_nothing(self):
        self.login()
        sale = self.sell(2, 2)
        self.give_back(3, 1, shipment=sale)
        self.receive(4, 1, "250.00")
        with CaptureQueriesContext(connection) as queries:
            self.summary(day(1, 2), day(1, 2))
            services.product_rows()
            services.customer_rows()
            services.sales_by_month(6)
            self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
            self.client.get(reverse("dashboard"))
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])


class PeriodAndDetailsTests(ReturnsInReportBase):
    def setUp(self):
        super().setUp()
        self.other_customer = Counterparty.objects.create(name="Другой покупатель",
                                                          partner_type=Counterparty.TYPE_CUSTOMER)
        self.receive(day(1, 1), 10, "100.00")                             # «Кабель» 10 по 100
        receipt = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                         date=day(1, 1), time=NOON)
        receipt.lines.create(product=self.other, quantity=10, price=40)   # «Провод» 10 по 40
        receipt.post()
        self.sale_a = self.sale_doc(day(1, 10), [(self.product, 3, 500, 0), (self.other, 4, 90, "10")])
        self.sale_b = self.sale_doc(day(2, 5), [(self.product, 2, 480, 0)], customer=self.other_customer)
        self.return_doc(day(2, 7), [(self.product, 1, 500, 0, None), (self.other, 1, 90, "10", None)],
                        shipment=self.sale_a)
        self.return_doc(day(3, 2), [(self.product, 1, 480, 0, None)], shipment=self.sale_b,
                        customer=self.other_customer)

    def test_periods_and_details(self):
        """Январь: продажи 1500 + 4 × 81 = 1824, себестоимость 300 + 160.
        Февраль: продажа 960 (себестоимость 200), возврат 500 + 81
        (себестоимость 100 + 40). Март: возврат 480 (себестоимость 100)."""
        january = self.summary(day(1, 1), day(1, 31))
        self.assertEqual(self.money(january), (D("1824.00"), D("460.00"), D("1364.00")))
        february = self.summary(day(2, 1), day(2, 28))
        self.assertEqual((february["sales_revenue"], february["returns_revenue"]), (D("960.00"), D("581.00")))
        self.assertEqual(self.money(february), (D("379.00"), D("60.00"), D("319.00")))
        march = self.summary(day(3, 1), day(3, 31))
        self.assertEqual(self.money(march), (D("-480.00"), D("-100.00"), D("-380.00")))
        whole = self.summary(day(1, 1), day(3, 31))
        self.assertEqual(self.money(whole), tuple(a + b + c for a, b, c in zip(
            self.money(january), self.money(february), self.money(march))))
        for period in ((day(1, 1), day(1, 31)), (day(2, 1), day(2, 28)), (day(3, 1), day(3, 31)),
                       (day(1, 1), day(3, 31)), (None, None)):
            self.assertDetailsMatch(*period)
        rows = {row["product"]: row for row in services.product_rows(day(1, 1), day(3, 31))}
        self.assertEqual((rows["Кабель"]["qty"], rows["Кабель"]["revenue"]), (D("3"), D("1480.00")))
        self.assertEqual((rows["Провод"]["qty"], rows["Провод"]["revenue"]), (D("3"), D("243.00")))
        customers = {row["customer"]: row for row in services.customer_rows(day(2, 1), day(3, 31))}
        self.assertEqual(customers["Другой покупатель"]["revenue"], D("480.00"))
        self.assertEqual(customers["Покупатель"]["revenue"], D("-581.00"))

    def test_report_page_shows_sales_returns_and_dash_for_margin(self):
        self.login()
        page = self.client.get(reverse("report_sales"), {"date_from": "2026-03-01", "date_to": "2026-03-31"})
        self.assertContains(page, "data-margin-none")
        self.assertContains(page, "не считается: выручка после возвратов не больше нуля")
        self.assertContains(page, "отгрузок: 0 · возвратов: 1")
        self.assertContains(page, "Возвраты покупателей")
        self.assertEqual(page.context["summary"]["revenue"], D("-480.00"))
        page = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-01-31"})
        self.assertNotContains(page, "data-margin-none")


class MonthsAndDashboardTests(ReturnsInReportBase):
    def test_months_chart_and_dashboard(self):
        """Продажа в прошлом месяце, возврат — в текущем: в графике прошлый
        месяц +500, текущий −500 (продажи 0); сумма месяцев = итог."""
        self.login()
        today = timezone.localdate()
        this_month = today.replace(day=1)
        last_month = (this_month - datetime.timedelta(days=1)).replace(day=1)
        self.receive(last_month, 1, "100.00")
        sale = self.sell(last_month, 1)
        self.give_back(this_month, 1, shipment=sale)
        rows = services.sales_by_month(6)
        self.assertEqual([(r["sales"], r["returns"], r["value"]) for r in rows[-2:]],
                         [(D("500.00"), ZERO, D("500.00")), (ZERO, D("500.00"), D("-500.00"))])
        self.assertEqual(sum((r["value"] for r in rows), ZERO), self.summary(last_month, today)["revenue"])
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.context["sales_month"]["revenue"], D("-500.00"))
        self.assertEqual(dashboard.context["chart_values"][-2:], [500.0, -500.0])
        self.assertEqual(dashboard.context["chart_sales"][-2:], [500.0, 0.0])
        self.assertContains(dashboard, "возвратов покупателей: 1")
        self.assertEqual(dashboard.context["top_products"][0]["revenue"], D("-500.00"))
