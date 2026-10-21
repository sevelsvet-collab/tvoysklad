"""Перенос старых данных на учёт по организациям: миграции inventory 0007–0009
на базе старой схемы (inventory 0006) и явный пересчёт stock_transition.

Отдельный TransactionTestCase: схема откатывается к 0006, данные создаются
моделями той версии, затем миграции применяются вперёд (в finally — всегда).
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from . import transition
from .models import SerialMovement, SerialNumber, StockBalance, StockLedgerState, StockMovement, StockTransitionIssue
from .services import StockError

D = Decimal
BEFORE = ("inventory", "0006_deficit_cover")


class OldSchemaMigrationTests(TransactionTestCase):
    def test_old_data_is_migrated_from_sources_and_recalculated_explicitly(self):
        executor = MigrationExecutor(connection)
        targets = [node for node in executor.loader.graph.leaf_nodes() if node[0] != "inventory"] + [BEFORE]
        executor.migrate(targets)
        try:
            ids = self.make_old_data(executor.loader.project_state(targets).apps)
        finally:
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())

        # 1. организация — из документа-источника; без источника — пусто
        orgs = dict(StockMovement.objects.values_list("pk", "organization_id"))
        self.assertEqual(orgs[ids["receipt_move"]], ids["a"])
        self.assertEqual(orgs[ids["shipment_move"]], ids["b"])
        self.assertIsNone(orgs[ids["orphan_move"]])
        moves = {m.doc_type: m for m in SerialMovement.objects.all()}
        self.assertEqual(moves["receipt"].organization_id, ids["a"])
        self.assertEqual(moves["receipt"].time, datetime.time(9, 0))
        self.assertIsNone(moves["serial_stock"].organization_id)       # ввод остатка — решает человек
        self.assertEqual(SerialNumber.objects.get(number="SN-A").organization_id, ids["a"])
        # 2. остатки не делятся: старый общий кэш удалён, статус — ожидает пересчёта
        self.assertFalse(StockBalance.objects.exists())
        self.assertEqual(StockLedgerState.objects.get(pk=1).status, StockLedgerState.STATUS_PENDING)
        self.assertFalse(transition.is_ready())

        # 3. диагностика — только чтение
        out = StringIO()
        call_command("stock_transition", "--limit", "0", stdout=out)
        text = out.getvalue()
        self.assertIn("Движения без организации (источник не найден, требует решения): 1", text)
        self.assertIn(f"--assign receipt:{ids['orphan_doc']}=ID_ОРГАНИЗАЦИИ", text)
        self.assertIn("Серийные номера без определённой организации (требует решения): 1", text)
        self.assertIn("«Б», товар «Товар» на складе «W»", text)                 # Б продала товар А
        self.assertIn("Ничего не изменено", text)
        self.assertFalse(StockBalance.objects.exists())

        # 4. пересчёт не начнётся без решения по движению без источника
        with self.assertRaises(transition.TransitionError):
            transition.apply()
        transition.assign_organization("receipt", ids["orphan_doc"], ids["a"])   # решение человека
        transition.apply()
        self.assertTrue(transition.is_ready())
        balances = {(b.organization_id, b.product_id): (b.quantity, b.value) for b in StockBalance.objects.all()}
        # А: 1 × 100 + решённое движение 2 × 50; Б: −1 (продала чужой товар)
        self.assertEqual(balances[(ids["a"], ids["product"])], (D("3"), D("200.00")))
        self.assertEqual(balances[(ids["b"], ids["product"])], (D("-1"), D("0.00")))
        issue = StockTransitionIssue.objects.get()
        self.assertEqual((issue.organization_id, issue.product_id), (ids["b"], ids["product"]))

        # 5. нарушение блокирует операции с товаром; ввод номера без организации — не тронут
        from apps.purchases.models import Receipt

        draft = Receipt.objects.create(organization_id=ids["a"], warehouse_id=ids["w"], supplier_id=ids["supplier"])
        draft.lines.create(product_id=ids["product"], quantity=1, price=100, vat_rate="none")
        with self.assertRaisesMessage(StockError, "расходовала чужой товар"):
            draft.post()
        self.assertEqual([m.organization_id for m in SerialMovement.objects.filter(doc_type="serial_stock")], [None])

    def make_old_data(self, old):
        org_model, wh_model = old.get_model("core", "Organization"), old.get_model("core", "Warehouse")
        unit_model, product_model = old.get_model("catalog", "Unit"), old.get_model("catalog", "Product")
        cp_model = old.get_model("partners", "Counterparty")
        receipt_model, shipment_model = old.get_model("purchases", "Receipt"), old.get_model("sales", "Shipment")
        movement_model = old.get_model("inventory", "StockMovement")
        balance_model = old.get_model("inventory", "StockBalance")
        serial_model, serial_move_model = old.get_model("inventory", "SerialNumber"), old.get_model(
            "inventory", "SerialMovement")
        a = org_model.objects.create(name="А", allow_negative_stock=False)
        b = org_model.objects.create(name="Б", allow_negative_stock=False)
        w = wh_model.objects.create(name="W")
        unit = unit_model.objects.create(name="шт")
        product = product_model.objects.create(name="Товар", unit=unit)
        phone = product_model.objects.create(name="Телефон", unit=unit, track_serials=True)
        supplier = cp_model.objects.create(name="Поставщик", partner_type="supplier")
        customer = cp_model.objects.create(name="Покупатель", partner_type="customer")
        day = datetime.date(2026, 10, 1)
        created = timezone.now()
        receipt = receipt_model.objects.create(organization=a, warehouse=w, supplier=supplier, number="1", date=day,
                                               time=datetime.time(9, 0), status="posted")
        shipment = shipment_model.objects.create(organization=b, warehouse=w, customer=customer, number="1",
                                                 date=day, time=datetime.time(10, 0), status="posted")
        serial_receipt = receipt_model.objects.create(organization=a, warehouse=w, supplier=supplier, number="2",
                                                      date=day, time=datetime.time(9, 0), status="posted")

        def move(doc_type, doc_id, qty, amount, at, prod=product):
            return movement_model.objects.create(
                product=prod, warehouse=w, quantity=qty, cost=abs(amount / qty), amount=amount, doc_type=doc_type,
                doc_id=doc_id, doc_number="1", date=day, time=at, doc_created_at=created).pk

        ids = {
            "a": a.pk, "b": b.pk, "w": w.pk, "product": product.pk, "supplier": supplier.pk,
            "receipt_move": move("receipt", receipt.pk, D("1"), D("100"), datetime.time(9, 0)),
            # старая программа разрешила: Б отгрузила единицу А (склад в целом 0)
            "shipment_move": move("shipment", shipment.pk, D("-1"), D("100"), datetime.time(10, 0)),
            "orphan_doc": 999,
        }
        ids["orphan_move"] = move("receipt", 999, D("2"), D("100"), datetime.time(8, 0))   # документа нет
        move("receipt", serial_receipt.pk, D("1"), D("500"), datetime.time(9, 0), prod=phone)
        balance_model.objects.create(product=product, warehouse=w, quantity=D("2"), avg_cost=D("50"), value=D("100"))
        serial = serial_model.objects.create(product=phone, number="SN-A", warehouse=w, status="in_stock")
        serial_move_model.objects.create(serial=serial, doc_type="receipt", doc_id=serial_receipt.pk, doc_number="2",
                                         date=day, warehouse=w, quantity=1)
        old_entry = serial_model.objects.create(product=phone, number="SN-OLD", warehouse=w, status="in_stock")
        serial_move_model.objects.create(serial=old_entry, doc_type="serial_stock", doc_id=0, doc_number="Ввод остатка",
                                         date=day, warehouse=w, quantity=1)
        return ids
