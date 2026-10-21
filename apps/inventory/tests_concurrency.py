"""Одновременные складские операции — только PostgreSQL.

В SQLite блокировок строк нет (select_for_update игнорируется, а запись идёт
по одной транзакции на всю базу), поэтому там эти тесты ничего не доказывают
и пропускаются. Запуск на PostgreSQL:

    DATABASE_URL=postgres://user:pass@host:5432/db \
        python manage.py test apps.inventory.tests_concurrency

Django сам создаст и удалит отдельную тестовую базу test_<db>; рабочая база
не затрагивается. Ограничения ожидания и проверка того, что операции
действительно пересеклись, — в apps/core/concurrency_testing.py.
"""
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.catalog.models import Product, Unit
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import Shipment

from . import services
from .models import StockBalance, StockMovement
from .services import PostedChangeError, StockError

POSTGRES = connection.vendor == "postgresql"


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentPostingTests(ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True, allow_negative_stock=False)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Кабель", unit=self.unit)
        self.other = Product.objects.create(name="Провод", unit=self.unit)

    def receipt(self, lines, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        for product, qty in lines:
            doc.lines.create(product=product, quantity=qty, price=100)
        if post:
            doc.post()
        return doc

    def shipment(self, lines):
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        for product, qty in lines:
            doc.lines.create(product=product, quantity=qty, price=150)
        return doc

    def stock(self, product=None):
        return services.current_qty((product or self.product).pk, self.wh.pk)

    def test_two_shipments_of_last_item(self):
        """Последнюю единицу товара получает только одна из двух одновременных отгрузок."""
        self.receipt([(self.product, 1)])
        first, second = self.shipment([(self.product, 1)]), self.shipment([(self.product, 1)])
        results = self.run_together(
            lambda: Shipment.objects.get(pk=first.pk).post(),
            lambda: Shipment.objects.get(pk=second.pk).post(),
        )
        failures = [r for r in results if r is not None]
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], StockError)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(StockMovement.objects.filter(doc_type="shipment").count(), 1)
        self.assertEqual(Shipment.objects.filter(status="posted").count(), 1)

    def test_repeated_requests_to_same_document(self):
        """Два одновременных «Провести» одного документа не создают двойных движений."""
        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 2)])
        results = self.run_together(*[
            (lambda: Shipment.objects.get(pk=shipment.pk).post()) for _ in range(4)
        ])
        self.assertEqual(results, [None] * 4)
        self.assertEqual(StockMovement.objects.filter(doc_type="shipment", doc_id=shipment.pk).count(), 1)
        self.assertEqual(self.stock(), 8)

    def test_first_receipts_when_balance_row_is_missing(self):
        """Записи остатка ещё нет: два одновременных прихода не теряют друг друга
        и не падают на уникальности остатка."""
        self.assertFalse(StockBalance.objects.exists())
        first = self.receipt([(self.product, 3)], post=False)
        second = self.receipt([(self.product, 4)], post=False)
        results = self.run_together(
            lambda: Receipt.objects.get(pk=first.pk).post(),
            lambda: Receipt.objects.get(pk=second.pk).post(),
        )
        self.assertEqual(results, [None, None])
        self.assertEqual(StockBalance.objects.count(), 1)
        self.assertEqual(self.stock(), 7)

    def test_opposite_product_order_does_not_deadlock(self):
        """Документы с товарами в разном порядке блокируют их в одном порядке."""
        self.receipt([(self.product, 5), (self.other, 5)])
        forward = self.shipment([(self.product, 1), (self.other, 1)])
        backward = self.shipment([(self.other, 1), (self.product, 1)])
        for _ in range(5):
            results = self.run_together(
                lambda: Shipment.objects.get(pk=forward.pk).post(),
                lambda: Shipment.objects.get(pk=backward.pk).post(),
            )
            self.assertEqual(results, [None, None])
        self.assertEqual((self.stock(), self.stock(self.other)), (3, 3))

    def test_unpost_receipt_races_with_shipment(self):
        """Отмена приёмки и отгрузка того же товара одновременно: остаток не уходит в минус."""
        receipt = self.receipt([(self.product, 1)])
        shipment = self.shipment([(self.product, 1)])
        results = self.run_together(
            lambda: Receipt.objects.get(pk=receipt.pk).unpost(),
            lambda: Shipment.objects.get(pk=shipment.pk).post(),
        )
        self.assertEqual(sum(1 for r in results if r is not None), 1, results)
        self.assertGreaterEqual(self.stock(), 0)

    def test_backdated_receipt_races_with_shipment(self):
        """Приёмка задним числом и отгрузка того же товара одновременно:
        себестоимость, остаток и строка отгрузки после обеих операций согласованы."""
        from datetime import timedelta

        from django.utils import timezone

        from . import costing

        self.receipt([(self.product, 10)])
        early = Receipt.objects.create(
            organization=self.org, warehouse=self.wh, supplier=self.supplier,
            date=timezone.localdate() - timedelta(days=1),
        )
        early.lines.create(product=self.product, quantity=10, price=200)
        shipment = self.shipment([(self.product, 5)])
        results = self.run_together(
            lambda: Receipt.objects.get(pk=early.pk).post(),
            lambda: Shipment.objects.get(pk=shipment.pk).post(),
        )
        self.assertEqual(results, [None, None])
        self.assertEqual(costing.differences(costing.build_plan(self.product.pk)), [])
        self.assertEqual(shipment.lines.get().cost_price, 150)
        self.assertEqual(self.stock(), 15)

    # ---------- возвраты по основанию (apps/inventory/return_limits.py) ----------

    def customer_return(self, shipment, qty):
        from apps.sales.models import CustomerReturn

        doc = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh,
                                            customer=self.customer, shipment=shipment)
        doc.lines.create(product=self.product, quantity=qty, price=150)
        return doc

    def supplier_return(self, receipt, qty):
        from apps.purchases.models import SupplierReturn

        doc = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh,
                                            supplier=self.supplier, receipt=receipt)
        doc.lines.create(product=self.product, quantity=qty, price=100)
        return doc

    def test_two_customer_returns_of_last_available_unit(self):
        """Два возврата последней доступной единицы одновременно: проходит один."""
        from apps.sales.models import CustomerReturn

        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 1)])
        shipment.post()
        first, second = self.customer_return(shipment, 1), self.customer_return(shipment, 1)
        results = self.run_together(
            lambda: CustomerReturn.objects.get(pk=first.pk).post(),
            lambda: CustomerReturn.objects.get(pk=second.pk).post(),
        )
        failures = [r for r in results if r is not None]
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], StockError)
        self.assertEqual(CustomerReturn.objects.filter(status="posted").count(), 1)
        self.assertEqual(self.stock(), 10)

    def test_two_supplier_returns_of_last_available_unit(self):
        from apps.purchases.models import SupplierReturn

        self.receipt([(self.product, 10)])
        basis = self.receipt([(self.product, 1)])
        first, second = self.supplier_return(basis, 1), self.supplier_return(basis, 1)
        results = self.run_together(
            lambda: SupplierReturn.objects.get(pk=first.pk).post(),
            lambda: SupplierReturn.objects.get(pk=second.pk).post(),
        )
        failures = [r for r in results if r is not None]
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], StockError)
        self.assertEqual(SupplierReturn.objects.filter(status="posted").count(), 1)
        self.assertEqual(self.stock(), 10)

    def test_return_races_with_decreasing_basis(self):
        """Возврат 2 шт. и одновременное уменьшение отгрузки с 2 до 1: проходит
        только одна из операций, возвращённое не превышает основание."""
        from apps.sales.models import CustomerReturn

        from . import return_limits

        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 2)])
        shipment.post()
        back = self.customer_return(shipment, 2)

        def shrink():
            # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): как форма — через editing() (posting, правило 10):
            # документ и товары блокируются до изменения строк, перепроведение — при выходе.
            # Прежняя ручная запись строк теперь отклонялась бы всегда, и гонки не было бы.
            doc = Shipment.objects.get(pk=shipment.pk)
            with doc.editing():
                doc.lines.update(quantity=1)

        results = self.run_together(lambda: CustomerReturn.objects.get(pk=back.pk).post(), shrink)
        self.assertFalse(any(isinstance(r, PostedChangeError) for r in results), results)
        self.assertEqual(sum(1 for r in results if r is not None), 1, results)
        self.assertEqual(return_limits.violations(), [])

    def test_return_races_with_unposting_basis(self):
        from apps.sales.models import CustomerReturn

        from . import return_limits

        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 1)])
        shipment.post()
        back = self.customer_return(shipment, 1)
        results = self.run_together(
            lambda: CustomerReturn.objects.get(pk=back.pk).post(),
            lambda: Shipment.objects.get(pk=shipment.pk).unpost(),
        )
        self.assertEqual(sum(1 for r in results if r is not None), 1, results)
        self.assertEqual(return_limits.violations(), [])


    # ---------- доказанное пересечение: вторая операция ждёт блокировку первой ----------

    def test_overlap_two_shipments_of_last_item(self):
        """Первая отгрузка держит блокировку товара (остановлена в пересчёте
        стоимости), вторая обязана её ждать и затем получить отказ."""
        from . import costing

        self.receipt([(self.product, 1)])
        first, second = self.shipment([(self.product, 1)]), self.shipment([(self.product, 1)])
        results = self.run_overlapping(
            lambda: Shipment.objects.get(pk=first.pk).post(),
            lambda: Shipment.objects.get(pk=second.pk).post(),
            pause_on=(costing, "recalculate"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        self.assertEqual(self.stock(), 0)
        self.assertEqual(Shipment.objects.filter(status="posted").count(), 1)

    def test_overlap_two_returns_of_last_available_unit(self):
        from apps.sales.models import CustomerReturn

        from . import costing

        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 1)])
        shipment.post()
        first, second = self.customer_return(shipment, 1), self.customer_return(shipment, 1)
        results = self.run_overlapping(
            lambda: CustomerReturn.objects.get(pk=first.pk).post(),
            lambda: CustomerReturn.objects.get(pk=second.pk).post(),
            pause_on=(costing, "recalculate"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        self.assertIn("доступно к возврату — 0", str(results[1]))
        self.assertEqual(self.stock(), 10)

    def test_overlap_return_against_shrinking_basis(self):
        """Отгрузку уменьшают с 2 до 1, пока возврат 2 шт. ждёт блокировку."""
        from apps.sales.models import CustomerReturn

        from . import costing, return_limits

        self.receipt([(self.product, 10)])
        shipment = self.shipment([(self.product, 2)])
        shipment.post()
        back = self.customer_return(shipment, 2)

        def shrink():
            # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе с
            # перепроведением — editing() (posting, правило 10); раньше тест повторял прежний код формы:
            # блокировка документа и товара, правка строк, post(). Порядок блокировок тот же.
            doc = Shipment.objects.get(pk=shipment.pk)
            with doc.editing():
                doc.lines.update(quantity=1)

        results = self.run_overlapping(shrink, lambda: CustomerReturn.objects.get(pk=back.pk).post(),
                                       pause_on=(costing, "recalculate"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        self.assertEqual(return_limits.violations(), [])


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentDeficitTests(ConcurrencyMixin, TransactionTestCase):
    """Минус разрешён: продажа раньше поступления и проверка итоговой истории
    при одновременных операциях. Пересчёт идёт под блокировкой товара, поэтому
    вторая операция видит результат первой целиком."""

    def setUp(self):
        import datetime

        self.day = lambda n: datetime.date(2026, 1, n)      # noqa: E731
        self.noon = datetime.time(12)
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True, allow_negative_stock=True)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Магазин")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Кабель", unit=self.unit)

    def receipt(self, n, qty, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                     date=self.day(n), time=self.noon)
        doc.lines.create(product=self.product, quantity=qty, price=100)
        if post:
            doc.post()
        return doc

    def assertHistoryCorrect(self):
        from . import costing

        self.assertEqual(costing.remaining_problems([self.product.pk]), [])
        found = costing.differences(costing.build_plan(self.product.pk))
        self.assertEqual(costing.fixable(found), [], [d.description for d in found])

    def test_overlap_receipt_covers_concurrent_sale_before_it(self):
        """Отгрузка 1 января с пустого склада проводится; одновременно
        проводится приход 2 января — он ждёт блокировку и покрывает дефицит."""
        from apps.sales.models import ShipmentLine

        from . import costing
        from .models import DeficitCover

        sale = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                       date=self.day(1), time=self.noon)
        sale.lines.create(product=self.product, quantity=2, price=150)
        receipt = self.receipt(2, 3, post=False)
        results = self.run_overlapping(
            lambda: Shipment.objects.get(pk=sale.pk).post(),
            lambda: Receipt.objects.get(pk=receipt.pk).post(),
            pause_on=(costing, "recalculate"),
        )
        self.assertEqual(results, [None, None])
        line = ShipmentLine.objects.get(shipment=sale)
        self.assertEqual((line.cost_amount, line.cost_open_qty), (Decimal("200.00"), Decimal("0")))
        self.assertEqual(DeficitCover.objects.count(), 1)
        balance = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual((balance.quantity, balance.value), (Decimal("1"), Decimal("100.00")))
        self.assertHistoryCorrect()

    def test_overlap_transfer_and_unposting_its_receipt(self):
        """Перемещение 2 января проводится, пока снимают с проведения приход
        1 января, который его обеспечивает. Проходит только одна операция;
        итоговая история корректна при любом порядке."""
        from . import costing
        from .models import Transfer

        for first_is_transfer in (True, False):
            with self.subTest(first_is_transfer=first_is_transfer):
                # очистка между подтестами — прямое удаление мимо программы: массово удалять
                # проведённые документы правило 10 теперь не даёт (ПРАВИЛО ДОПОЛНЕНО 19.10.2026)
                Transfer._base_manager.all().delete()
                StockMovement.objects.all().delete()
                StockBalance.objects.all().delete()
                Receipt._base_manager.all().delete()
                receipt = self.receipt(1, 1)
                transfer = Transfer.objects.create(organization=self.org, warehouse_from=self.wh,
                                                   warehouse_to=self.wh2, date=self.day(2), time=self.noon)
                transfer.lines.create(product=self.product, quantity=1)
                post = lambda: Transfer.objects.get(pk=transfer.pk).post()      # noqa: E731
                unpost = lambda: Receipt.objects.get(pk=receipt.pk).unpost()    # noqa: E731
                operations = (post, unpost) if first_is_transfer else (unpost, post)
                results = self.run_overlapping(*operations, pause_on=(costing, "recalculate"))
                self.assertIsNone(results[0])
                self.assertIsInstance(results[1], StockError)
                self.assertHistoryCorrect()

    def test_overlap_unlinked_and_linked_returns_of_one_shipment(self):
        """Строки отгрузки по 100 и 300. Возврат без строки (по отгрузке в
        целом) проводится, пока ждёт возврат по второй строке: второй видит
        первый и отклоняется — способы распределения не смешиваются."""
        from apps.sales.models import CustomerReturn, ShipmentLine

        from . import costing

        self.receipt(1, 1)
        sale = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                       date=self.day(2), time=self.noon)
        sale.lines.create(product=self.product, quantity=1, price=150)
        sale.lines.create(product=self.product, quantity=1, price=150)
        sale.post()
        cover = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                       date=self.day(3), time=self.noon)
        cover.lines.create(product=self.product, quantity=1, price=300)
        cover.post()
        second = ShipmentLine.objects.filter(shipment=sale).order_by("pk").last()
        backs = []
        for n, line in ((4, None), (5, second)):
            back = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                                 shipment=sale, date=self.day(n), time=self.noon)
            back.lines.create(product=self.product, quantity=1, price=150, shipment_line=line)
            backs.append(back)
        results = self.run_overlapping(
            lambda: CustomerReturn.objects.get(pk=backs[0].pk).post(),
            lambda: CustomerReturn.objects.get(pk=backs[1].pk).post(),
            pause_on=(costing, "recalculate"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        self.assertIn(f"— возврат покупателя № {backs[0].number} от", str(results[1]))
        balance = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        self.assertEqual((balance.quantity, balance.value), (Decimal("1"), Decimal("200.00")))
        self.assertHistoryCorrect()
