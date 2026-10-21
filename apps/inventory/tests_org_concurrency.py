"""Учёт по организациям: одновременные операции (только PostgreSQL).

Блокировка — по товару целиком (services.lock_products): строже, чем по
«организация + товар», и покрывает смену организации документа. Каждая
проверка доказывает, что вторая операция ждала первую (run_overlapping по
pg_stat_activity), и что исход не зависит от порядка там, где по правилам он
один: расход Б никогда не берётся из поступления А, а организация документа
не расходится с организацией его движений (смена организации — только
change_organization(), массовая смена проведённого — отказ).
"""
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.core.concurrency_testing import ConcurrencyMixin
from apps.sales.models import Shipment, ShipmentLine

from . import services
from .models import SerialNumber, StockBalance
from .services import StockError
from .tests_org_stock import OrgWorld

POSTGRES = connection.vendor == "postgresql"
D = Decimal


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentOrganizationTests(OrgWorld, ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.make_world()

    def post(self, doc):
        return lambda: type(doc).objects.get(pk=doc.pk).post()

    def balances_of(self, product):
        return {(b.organization.name, b.warehouse.name): (b.quantity, b.value)
                for b in StockBalance.objects.filter(product=product).select_related("organization", "warehouse")}

    def test_b_never_ships_stock_of_a_in_any_order(self):
        for first_name in ("А", "Б"):
            with self.subTest(first=first_name):
                from apps.catalog.models import Product

                product = Product.objects.create(name=f"Товар {first_name}", unit=self.unit)
                self.receipt(self.a, 1, 100, product=product)
                docs = {"А": self.ship(self.a, 1, product=product, post=False),
                        "Б": self.ship(self.b, 1, product=product, post=False)}
                second_name = "Б" if first_name == "А" else "А"
                results = self.run_overlapping(self.post(docs[first_name]), self.post(docs[second_name]),
                                               pause_on=(services, "document_outflow"))
                outcome = dict(zip((first_name, second_name), results))
                self.assertIsNone(outcome["А"])
                self.assertIsInstance(outcome["Б"], StockError)
                self.assertTrue(Shipment.objects.get(pk=docs["А"].pk).is_posted)
                self.assertFalse(Shipment.objects.get(pk=docs["Б"].pk).is_posted)
                self.assertEqual(self.balances_of(product), {("А", "W"): (D("0"), D("0.00"))})

    def test_receipt_of_b_does_not_cover_deficit_of_a_in_any_order(self):
        from apps.catalog.models import Product

        self.negative(self.a, True)
        for order in (("ship", "receipt"), ("receipt", "ship")):
            with self.subTest(order=order):
                product = Product.objects.create(name=f"Товар {order[0]}", unit=self.unit)
                docs = {"ship": self.ship(self.a, 2, product=product, post=False),
                        "receipt": self.receipt(self.b, 2, 100, product=product, post=False)}
                results = self.run_overlapping(self.post(docs[order[0]]), self.post(docs[order[1]]),
                                               pause_on=(services, "document_outflow"))
                self.assertEqual(results, [None, None])
                line = ShipmentLine.objects.get(shipment=docs["ship"])
                self.assertEqual((line.cost_amount, line.cost_open_qty), (D("0.00"), D("2.000")))
                self.assertEqual(self.balances_of(product), {("А", "W"): (D("-2"), D("0.00")),
                                                             ("Б", "W"): (D("2"), D("200.00"))})

    def test_organization_change_and_shipment_from_it(self):
        receipt = self.receipt(self.a, 1, 100)
        shipment = self.ship(self.a, 1, post=False)

        def change():
            # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): смена организации проведённого документа — только
            # change_organization() (сохранение и перепроведение одной транзакцией); ожидания прежние
            type(receipt).objects.get(pk=receipt.pk).change_organization(self.b)

        results = self.run_overlapping(change, self.post(shipment), pause_on=(services, "document_outflow"))
        self.assertEqual(results[0], None)                    # смена прошла первой
        self.assertIsInstance(results[1], StockError)         # у А больше нет товара
        self.assertEqual(self.balances_of(self.product), {("Б", "W"): (D("1"), D("100.00"))})

    def test_shipment_then_organization_change_is_rejected(self):
        receipt = self.receipt(self.a, 1, 100)
        shipment = self.ship(self.a, 1, post=False)

        def change():
            # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): смена организации проведённого документа — только
            # change_organization() (сохранение и перепроведение одной транзакцией); ожидания прежние
            type(receipt).objects.get(pk=receipt.pk).change_organization(self.b)

        results = self.run_overlapping(self.post(shipment), change, pause_on=(services, "document_outflow"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)         # прежняя история А ушла бы в минус
        self.assertEqual(type(receipt).objects.get(pk=receipt.pk).organization_id, self.a.pk)
        self.assertEqual(self.balances_of(self.product), {("А", "W"): (D("0"), D("0.00"))})

    def test_two_product_document_change_locks_all_products(self):
        from apps.catalog.models import Product
        from apps.purchases.models import Receipt

        second = Product.objects.create(name="Второй", unit=self.unit)
        receipt = Receipt.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier, **self._when())
        receipt.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
        receipt.lines.create(product=second, quantity=1, price=50, vat_rate="none")
        receipt.post()
        shipment = self.ship(self.a, 1, product=second, post=False)

        def change():
            # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): смена организации проведённого документа — только
            # change_organization() (сохранение и перепроведение одной транзакцией); ожидания прежние
            Receipt.objects.get(pk=receipt.pk).change_organization(self.b)

        results = self.run_overlapping(self.post(shipment), change, pause_on=(services, "document_outflow"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).organization_id, self.a.pk)
        self.assertEqual(self.balances_of(self.product), {("А", "W"): (D("1"), D("100.00"))})
        self.assertEqual(self.balances_of(second), {("А", "W"): (D("0"), D("0.00"))})

    def test_basis_organization_change_against_return_in_any_order(self):
        """Смена организации основания (через change_organization) против
        проведения возврата по нему. Возврат не переносится вслед за
        основанием, основание не уходит от проведённого возврата; документ и
        его движения не расходятся ни при каком порядке."""
        from apps.catalog.models import Product
        from apps.purchases.models import SupplierReturn

        from . import transition

        for basis_kind in ("receipt", "shipment"):
            for first in ("change", "return"):
                with self.subTest(basis=basis_kind, first=first):
                    self.product = Product.objects.create(name=f"{basis_kind} {first}", unit=self.unit)
                    receipt = self.receipt(self.a, 2, 100)
                    if basis_kind == "receipt":
                        basis = receipt
                        back = SupplierReturn.objects.create(organization=self.a, warehouse=self.w,
                                                             supplier=self.supplier, receipt=basis, **self._when())
                        back.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
                    else:
                        self.receipt(self.b, 2, 100)          # у Б свой товар: по складу смена совместима
                        basis = self.ship(self.a, 1)
                        back = self.customer_return(self.a, 1, basis=basis, post=False)
                    operations = {
                        "change": lambda: type(basis).objects.get(pk=basis.pk).change_organization(self.b),
                        "return": self.post(back),
                    }
                    second = "return" if first == "change" else "change"
                    results = dict(zip((first, second), self.run_overlapping(
                        operations[first], operations[second], pause_on=(services, "document_outflow"))))
                    basis_now, back_now = type(basis).objects.get(pk=basis.pk), type(back).objects.get(pk=back.pk)
                    if first == "change":
                        self.assertIsNone(results["change"])
                        self.assertIsInstance(results["return"], StockError)   # основание теперь у Б
                        self.assertIn("не совпадает с организацией документа-основания", str(results["return"]))
                        self.assertEqual((basis_now.organization_id, back_now.organization_id, back_now.is_posted),
                                         (self.b.pk, self.a.pk, False))
                    else:
                        self.assertIsNone(results["return"])
                        self.assertIsInstance(results["change"], StockError)   # по основанию есть возврат А
                        self.assertIn("должны совпадать с возвратом", str(results["change"]))
                        self.assertEqual((basis_now.organization_id, back_now.organization_id, back_now.is_posted),
                                         (self.a.pk, self.a.pk, True))
                    self.assertEqual(transition.organization_mismatches(), [])

    def test_mass_organization_update_against_posting_in_any_order(self):
        """QuerySet.update(organization=…) черновика против его проведения.
        Проведение первым — массовая смена видит проведённый документ и
        отклоняется. Смена первой — проведение видит в базе другую
        организацию, чем у своего объекта, и отклоняется. Документ и движения
        не расходятся."""
        from apps.catalog.models import Product
        from apps.purchases.models import Receipt

        from . import posting, services as stock, transition

        for first in ("post", "update"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Массовая {first}", unit=self.unit)
                draft = self.receipt(self.a, 1, 100, post=False)
                operations = {
                    "post": self.post(draft),
                    "update": lambda: Receipt.objects.filter(pk=draft.pk).update(organization=self.b),
                }
                second = "update" if first == "post" else "post"
                pause = (stock, "document_outflow") if first == "post" else (posting, "_stock_bound")
                results = dict(zip((first, second), self.run_overlapping(operations[first], operations[second],
                                                                         pause_on=pause)))
                self.assertIsNone(results[first])
                self.assertIsInstance(results[second], stock.OrganizationChangeError)
                doc = Receipt.objects.get(pk=draft.pk)
                if first == "post":
                    self.assertEqual((doc.organization_id, doc.is_posted), (self.a.pk, True))
                    self.assertEqual(self.balances_of(self.product), {("А", "W"): (D("1"), D("100.00"))})
                else:
                    self.assertEqual((doc.organization_id, doc.is_posted), (self.b.pk, False))
                    self.assertEqual(self.balances_of(self.product), {})
                self.assertEqual(transition.organization_mismatches(), [])

    def test_serial_number_is_consumed_once_by_its_owner(self):
        self.product.track_serials = True
        self.product.save()
        self.receipt(self.a, 1, 100, wh=self.w1, serial="SN-1")
        move = self.transfer(self.a, self.w1, self.w2, 1, serial="SN-1", post=False)
        sale = self.ship(self.a, 1, wh=self.w1, serial="SN-1", post=False)
        results = self.run_overlapping(self.post(move), self.post(sale), pause_on=(services, "document_outflow"))
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], StockError)
        serial = SerialNumber.objects.get(number="SN-1")
        self.assertEqual((serial.organization_id, serial.warehouse_id), (self.a.pk, self.w2.pk))
