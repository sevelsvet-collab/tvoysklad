"""Учётные реквизиты проведённого документа: склад, дата, время.

Дефект (19.10.2026, подтверждён независимо на приёмке):
save(update_fields=["warehouse" | "date" | "time"]) у проведённого документа
записывал новый склад, дату или время, а движения оставались прежними —
документ на одном складе или в один момент, товар — на другом.

Правило (posting, правило 9): учётные реквизиты проведённого документа —
организация, склад (у перемещения оба), дата, время — меняются только через
editing() вместе с перепроведением в одной транзакции. Обычное сохранение,
массовые операции и проведение объекта с несохранёнными реквизитами
отклоняются до записи; черновики редактируются как обычно.
"""
import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, models
from django.db.models import F
from django.test import TransactionTestCase
from django.urls import reverse

from apps.catalog.models import Product
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.purchases.admin import ReceiptAdmin
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Shipment

from . import services, transition
from .models import DeficitCover, SerialMovement, SerialNumber, StockMovement, Transfer
from .posting import TITLES, UNSAVED
from .services import OrganizationChangeError, PostedChangeError, StockError
from .tests_org_document_change import KINDS, ChangeWorld
from .tests_org_stock import OrgWorld

D = Decimal
ZERO = D("0")

JAN2, JAN10 = datetime.date(2026, 1, 2), datetime.date(2026, 1, 10)
NOON, EVENING = datetime.time(12, 0), datetime.time(18, 0)


class PlainSaveReproductionTests(ChangeWorld):
    """Три подтверждённых сценария на приёмке."""

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.receipt_doc = Receipt.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier,
                                                  date=JAN2, time=NOON)
        self.receipt_doc.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
        self.receipt_doc.post()

    def where(self):
        """Склад, дата и время — у документа и у его движений."""
        doc = Receipt.objects.get(pk=self.receipt_doc.pk)
        moves = StockMovement.objects.filter(doc_type="receipt", doc_id=doc.pk)
        return {"документ": (doc.warehouse.name, doc.date, doc.time),
                "движения": sorted({(m.warehouse.name, m.date, m.time) for m in moves})}

    def attempt(self, field, value):
        before = self.full_state()
        doc = Receipt.objects.get(pk=self.receipt_doc.pk)
        setattr(doc, field, value)
        rejected = None
        try:
            doc.save(update_fields=[field])
        except StockError as exc:
            rejected = exc
        return before, rejected

    def check(self, before, rejected):
        stored = ("W", JAN2, NOON)
        self.assertEqual(self.where(), {"документ": stored, "движения": [stored]})
        self.assertIsNotNone(rejected, "сохранение проведённого документа без перепроведения должно быть отклонено")
        self.assertIn("простым сохранением", str(rejected))
        self.assertEqual(self.full_state(), before)

    def test_warehouse(self):
        # было: {"документ": ("W2", 02.01, 12:00), "движения": [("W", 02.01, 12:00)]}
        self.check(*self.attempt("warehouse", self.w2))

    def test_date(self):
        # было: {"документ": ("W", 10.01, 12:00), "движения": [("W", 02.01, 12:00)]}
        self.check(*self.attempt("date", JAN10))

    def test_time(self):
        # было: {"документ": ("W", 02.01, 18:00), "движения": [("W", 02.01, 12:00)]}
        self.check(*self.attempt("time", EVENING))


# ---------------------------------------------------------------------------
# Все виды документов × все способы записи в обход перепроведения
# ---------------------------------------------------------------------------

def next_day(value):
    return value + datetime.timedelta(days=1)


class BypassTests(ChangeWorld):
    def new_value(self, doc, name):
        """Другое значение реквизита: склад — другой склад, дата — следующий день, время — 23:59."""
        if name in ("receipt", "shipment"):
            # ПРАВИЛО ДОПОЛНЕНО (21.10.2026): основание возврата тоже входит в учётные реквизиты
            # (posting, правило 12) — другое основание: уже существующий документ того же вида
            other = type(doc)._meta.get_field(name).related_model.objects.exclude(
                pk=getattr(doc, f"{name}_id")).order_by("pk").first()
            assert other is not None, name
            return other
        if name == "warehouse":
            return self.w2
        if name in ("warehouse_from", "warehouse_to"):
            return self.w
        if name == "date":
            return next_day(doc.date)
        return datetime.time(23, 59)

    @staticmethod
    def expression(name):
        if name == "date":
            return F("date") + datetime.timedelta(days=1)
        return F(name)              # даже «то же значение» выражением считается изменением

    def attempts(self, doc, name):
        model = type(doc)
        field = model._meta.get_field(name)
        value = self.new_value(doc, name)
        names = [name] + ([field.attname] if field.attname != name else [])

        def plain(fields=None):
            obj = model.objects.get(pk=doc.pk)
            setattr(obj, name, value)
            return lambda: obj.save(**({"update_fields": fields} if fields is not None else {}))

        def bulk(column):
            obj = model.objects.get(pk=doc.pk)
            setattr(obj, name, value)
            return lambda: model.objects.bulk_update([obj], [column])

        def upsert():
            obj = model.objects.get(pk=doc.pk)
            setattr(obj, name, value)
            return lambda: model.objects.bulk_create([obj], update_conflicts=True, update_fields=[name],
                                                     unique_fields=["id"])

        raw = value.pk if isinstance(value, models.Model) else value
        found = {"save()": plain(), "bulk_create(update_conflicts)": upsert()}
        for column in names:
            found[f"save(update_fields=[{column}])"] = plain([column])
            found[f"QuerySet.update({column}=…)"] = (
                lambda column=column: model.objects.filter(pk=doc.pk).update(
                    **{column: raw if column == field.attname else value}))
            found[f"bulk_update([{column}])"] = bulk(column)
        found["QuerySet.update(выражение)"] = lambda: model.objects.filter(pk=doc.pk).update(
            **{name: self.expression(name)})
        return found

    def test_every_document_kind_rejects_every_bypass_of_warehouse_date_time(self):
        for kind in KINDS:
            doc = self.posted(kind)
            requisites = [name for name in type(doc).STOCK_FIELDS if name != "organization"]
            before = self.full_state()
            for name in requisites:
                for title, attempt in self.attempts(doc, name).items():
                    with self.subTest(kind=kind, field=name, attempt=title):
                        with self.assertRaises(PostedChangeError) as caught:
                            attempt()
                        self.assertNotIsInstance(caught.exception, OrganizationChangeError)
                        self.assertEqual(self.full_state(), before)

    def test_transfer_protects_both_warehouses(self):
        doc = self.posted("transfer")
        self.assertEqual(Transfer.STOCK_FIELDS, ("organization", "warehouse_from", "warehouse_to", "date", "time"))
        for name in ("warehouse_from", "warehouse_to"):
            with self.subTest(field=name):
                obj = Transfer.objects.get(pk=doc.pk)
                setattr(obj, name, self.w)
                with self.assertRaisesMessage(PostedChangeError, f"{TITLES[name]} «"):
                    obj.save()

    def test_message_names_old_and_new_values(self):
        doc = self.posted("receipt")
        obj = Receipt.objects.get(pk=doc.pk)
        obj.warehouse, obj.date, obj.time = self.w2, next_day(doc.date), datetime.time(18, 0)
        with self.assertRaises(PostedChangeError) as caught:
            obj.save()
        text = str(caught.exception)
        self.assertIn("простым сохранением", text)
        self.assertIn("склад «W» → «W2»", text)
        self.assertIn(f"дата {doc.date:%d.%m.%Y} → {next_day(doc.date):%d.%m.%Y}", text)
        self.assertIn(f"время {doc.time:%H:%M} → 18:00", text)

    def test_organization_protection_is_kept(self):
        doc = self.posted("receipt")
        obj = Receipt.objects.get(pk=doc.pk)
        obj.organization, obj.date = self.b, next_day(doc.date)
        with self.assertRaises(OrganizationChangeError):          # организация среди изменённых
            obj.save()

    def test_stale_object_cannot_write_back_old_warehouse_or_date(self):
        doc = self.posted("receipt")
        stale = Receipt.objects.get(pk=doc.pk)
        fresh = Receipt.objects.get(pk=doc.pk)
        with fresh.editing():                                    # другой пользователь перенёс приёмку
            fresh.warehouse, fresh.date = self.w2, next_day(doc.date)
            fresh.save()
        after = self.full_state()
        stale.comment = "правка из старой вкладки"
        with self.assertRaisesMessage(PostedChangeError, "загрузите его заново"):
            stale.save()
        self.assertEqual(self.full_state(), after)

    def test_stale_draft_object_after_posting_by_other_user(self):
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        stale = Receipt.objects.get(pk=draft.pk)
        Receipt.objects.get(pk=draft.pk).post()
        before = self.full_state()
        stale.date = next_day(stale.date)
        with self.assertRaises(PostedChangeError):
            stale.save()
        self.assertEqual(self.full_state(), before)

    def test_unsaved_change_in_memory_with_comment_only_save(self):
        """Склад или дата изменены только в памяти, сохраняется комментарий:
        в базу они не попадают, а проведение такого объекта отклоняется — иначе
        движения записались бы с несохранёнными реквизитами."""
        for kind in KINDS:
            doc = self.posted(kind)
            for name in [name for name in type(doc).STOCK_FIELDS if name != "organization"]:
                with self.subTest(kind=kind, field=name):
                    before = self.full_state()
                    obj = type(doc).objects.get(pk=doc.pk)
                    setattr(obj, name, self.new_value(obj, name))
                    obj.comment = "проверено"
                    obj.save(update_fields=["comment"])
                    stored = type(doc).objects.get(pk=doc.pk)
                    field = type(doc)._meta.get_field(name)
                    self.assertEqual(getattr(stored, field.attname), getattr(doc, field.attname))
                    with self.assertRaisesMessage(PostedChangeError, UNSAVED[name]):
                        obj.post()
                    type(doc).objects.filter(pk=doc.pk).update(comment="")
                    self.assertEqual(self.full_state(), before)

    def test_drafts_change_as_usual(self):
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        draft.warehouse, draft.date = self.w2, next_day(draft.date)
        draft.save()
        Receipt.objects.filter(pk=draft.pk).update(time=datetime.time(7, 30))
        Receipt.objects.filter(pk=draft.pk).update(date=F("date") + datetime.timedelta(days=1))
        draft = Receipt.objects.get(pk=draft.pk)
        draft.warehouse = self.w1
        Receipt.objects.bulk_update([draft], ["warehouse"])
        draft = Receipt.objects.get(pk=draft.pk)
        draft.post()
        move = StockMovement.objects.get(doc_type="receipt", doc_id=draft.pk)
        self.assertEqual((move.warehouse_id, move.date, move.time), (self.w1.pk, draft.date, datetime.time(7, 30)))


# ---------------------------------------------------------------------------
# Безопасное изменение через editing() и форму: прежняя и новая история
# ---------------------------------------------------------------------------

JAN = {day: datetime.date(2026, 1, day) for day in range(1, 32)}


class MoveHelpers(ChangeWorld):
    """Документы на заданные даты января и правка через editing()."""

    def make(self, model, rows, day, **header):
        doc = self.doc(model, self.a, rows, date=JAN[day], time=NOON, **header)
        doc.post()
        return model.objects.get(pk=doc.pk)

    def edit(self, doc, **changes):
        with doc.editing():
            for name, value in changes.items():
                setattr(doc, name, value)
            doc.save()

    def assertRejectedWholly(self, doc, message, **changes):
        before = self.full_state()
        obj = type(doc).objects.get(pk=doc.pk)
        with self.assertRaisesMessage(StockError, message):
            self.edit(obj, **changes)
        self.assertEqual(self.full_state(), before)
        stored = type(doc).objects.get(pk=doc.pk)
        for name in changes:                                   # и объект вернулся к сохранённому
            field = type(doc)._meta.get_field(name)
            self.assertEqual(getattr(obj, field.attname), getattr(stored, field.attname))

    def stock(self):
        """Ненулевые остатки товара: пустая строка на прежнем складе — не товар."""
        return {key: value for key, value in self.balances().items() if value[0]}

    def moves(self, doc):
        return sorted((m.warehouse.name, m.date, m.time, m.quantity) for m in StockMovement.objects.filter(
            doc_type=doc.DOC_TYPE, doc_id=doc.pk))


class MoveScenarioTests(MoveHelpers):
    def test_receipt_moved_after_dependent_shipment(self):
        receipt = self.make(Receipt, [(1, "", 100)], 2)
        sale = self.make(Shipment, [(1, "", 300)], 5)
        # минус запрещён: расход 05.01 остался бы без товара — отказ целиком
        self.assertRejectedWholly(receipt, "остаток стал бы -1", date=JAN[10])
        # минус разрешён: отгрузка уходит в дефицит, перенесённая приёмка его покрывает
        self.negative(self.a, True)
        self.edit(Receipt.objects.get(pk=receipt.pk), date=JAN[10])
        self.assertEqual(self.moves(receipt), [("W", JAN[10], NOON, D("1.000"))])
        self.assertEqual(self.line(sale), (D("100.00"), ZERO))
        cover = DeficitCover.objects.get()
        self.assertEqual((cover.issue.doc_id, cover.cover.doc_id, cover.quantity, cover.amount),
                         (sale.pk, receipt.pk, D("1.000"), D("100.00")))
        self.edit(Receipt.objects.get(pk=receipt.pk), date=JAN[2])       # и обратно: покрытия нет
        self.assertFalse(DeficitCover.objects.exists())
        self.assertEqual(self.line(sale), (D("100.00"), ZERO))

    def test_return_moved_before_its_basis_is_rejected(self):
        receipt = self.make(Receipt, [(3, "", 100)], 1)
        sale = self.make(Shipment, [(1, "", 300)], 3)
        back = self.make(CustomerReturn, [(1, "", 300)], 5, shipment=sale)
        self.assertRejectedWholly(back, "стоит в хронологии не раньше возврата", date=JAN[2])
        self.assertRejectedWholly(sale, "На документ ссылается проведённый возврат", date=JAN[6])
        supplier_back = self.make(SupplierReturn, [(1, "", 100)], 5, receipt=receipt)
        self.assertRejectedWholly(supplier_back, "стоит в хронологии не раньше возврата", date=JAN[1],
                                  time=datetime.time(8, 0))
        self.edit(CustomerReturn.objects.get(pk=back.pk), date=JAN[7])   # позже основания — можно
        self.assertEqual(self.moves(back), [("W", JAN[7], NOON, D("1.000"))])

    def test_receipt_warehouse_change_under_later_shipment(self):
        receipt = self.make(Receipt, [(1, "", 100)], 2)
        self.make(Shipment, [(1, "", 300)], 5)
        self.assertRejectedWholly(receipt, "остаток стал бы -1", warehouse=self.w2)

    def test_receipt_warehouse_change_moves_stock_and_serial(self):
        self.product = Product.objects.create(name="Серийный", unit=self.unit, track_serials=True)
        receipt = self.make(Receipt, [(1, "S-1", 100)], 2)
        self.edit(receipt, warehouse=self.w2, time=datetime.time(18, 0))
        self.assertEqual(self.moves(receipt), [("W2", JAN[2], datetime.time(18, 0), D("1.000"))])
        self.assertEqual(self.stock(), {("А", "W2"): (D("1"), D("100.00"))})
        serial = SerialNumber.objects.get(number="S-1")
        self.assertEqual(serial.warehouse_id, self.w2.pk)
        self.assertEqual(list(serial.movements.values_list("warehouse_id", "date", "time")),
                         [(self.w2.pk, JAN[2], datetime.time(18, 0))])
        self.assertEqual(transition.requisite_mismatches(), [])

    def test_transfer_both_warehouses_change(self):
        self.make(Receipt, [(1, "", 100)], 1, warehouse=self.w1)
        self.make(Receipt, [(1, "", 200)], 1, warehouse=self.w)
        transfer = self.make(Transfer, [(1, "", None)], 3, warehouse_from=self.w1, warehouse_to=self.w2)
        # оба склада сразу: со склада W2 товара не будет — его туда привезло само это перемещение
        self.assertRejectedWholly(transfer, "Недостаточно товара",
                                  warehouse_from=self.w2, warehouse_to=self.w)
        self.edit(Transfer.objects.get(pk=transfer.pk), warehouse_from=self.w, warehouse_to=self.w1)
        self.assertEqual(self.moves(transfer), [("W", JAN[3], NOON, D("-1.000")), ("W1", JAN[3], NOON, D("1.000"))])
        self.assertEqual(self.stock(), {("А", "W1"): (D("2"), D("300.00"))})
        self.assertEqual(transition.requisite_mismatches(), [])

    def test_form_moves_posted_receipt_and_rejects_wholly(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        receipt = self.make(Receipt, [(2, "", 100)], 2)
        line = receipt.lines.get()

        def data(day, warehouse):
            return {
                "date": JAN[day].isoformat(), "time": "12:00", "number": receipt.number,
                "organization": self.a.pk, "warehouse": warehouse.pk, "supplier": self.supplier.pk, "contract": "",
                "supplier_invoice": "", "comment": "", "action": "save",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-product": line.product_id,
                "lines-0-quantity": "2", "lines-0-price": "100", "lines-0-discount": "0",
                "lines-0-vat_rate": "none", "lines-0-serial_numbers": "",
            }

        url = reverse("receipt_edit", args=[receipt.pk])
        self.assertEqual(self.client.post(url, data(4, self.w2)).status_code, 302)
        self.assertEqual(self.moves(receipt), [("W2", JAN[4], NOON, D("2.000"))])
        self.make(Shipment, [(2, "", 300)], 5, warehouse=self.w2)
        before = self.full_state()
        response = self.client.post(url, data(6, self.w2))       # позже отгрузки, которая его расходует
        self.assertContains(response, "Изменения не сохранены")
        self.assertEqual(self.full_state(), before)

    def test_admin_keeps_drafts_only_rule(self):
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="pass12345"))
        receipt = self.make(Receipt, [(1, "", 100)], 2)
        draft = self.doc(Receipt, self.a, [(1, "", 100)], date=JAN[2], time=NOON)

        def data(doc, day):
            line = doc.lines.get()
            return {
                "number": doc.number, "date": JAN[day].isoformat(), "time": "12:00:00",
                "organization": self.a.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
                "supplier_invoice": "", "comment": "",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-receipt": doc.pk,
                "lines-0-product": line.product_id, "lines-0-quantity": "1", "lines-0-serial_numbers": "",
                "lines-0-price": "100", "lines-0-discount": "0", "lines-0-vat_rate": "none",
            }

        response = self.client.post(reverse("admin:purchases_receipt_change", args=[draft.pk]), data(draft, 9))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Receipt.objects.get(pk=draft.pk).date, JAN[9])
        url = reverse("admin:purchases_receipt_change", args=[receipt.pk])
        before = self.full_state()
        self.assertEqual(self.client.post(url, data(receipt, 9)).status_code, 403)
        with mock.patch.object(ReceiptAdmin, "has_change_permission", return_value=True):
            response = self.client.post(url, data(receipt, 9), follow=True)   # провели, пока страница открыта
        self.assertContains(response, "Изменения не сохранены")
        self.assertEqual(self.full_state(), before)


# ---------------------------------------------------------------------------
# Диагностика старых расхождений склада, даты и времени (без исправления)
# ---------------------------------------------------------------------------

class RequisiteDiagnosticsTests(MoveHelpers):
    def command(self, name, *args):
        out = StringIO()
        call_command(name, *args, stdout=out)
        return out.getvalue()

    def test_mismatches_are_listed_by_field_and_nothing_is_rewritten(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        by_warehouse = self.make(Receipt, [(1, "", 100)], 2)
        by_date = self.make(Receipt, [(1, "", 100)], 2)
        by_time = self.make(Receipt, [(1, "", 100)], 2)
        self.make(Receipt, [(2, "", 100)], 1, warehouse=self.w1)
        out_side = self.make(Transfer, [(1, "", None)], 3, warehouse_from=self.w1, warehouse_to=self.w2)
        in_side = self.make(Transfer, [(1, "", None)], 3, warehouse_from=self.w1, warehouse_to=self.w2)
        # старые данные: прямая запись мимо программы
        Receipt._base_manager.filter(pk=by_warehouse.pk).update(warehouse=self.w2)
        Receipt._base_manager.filter(pk=by_date.pk).update(date=JAN[10])
        Receipt._base_manager.filter(pk=by_time.pk).update(time=datetime.time(18, 0))
        Transfer._base_manager.filter(pk=out_side.pk).update(warehouse_from=self.w)    # сторона расхода
        Transfer._base_manager.filter(pk=in_side.pk).update(warehouse_to=self.w)       # сторона прихода
        before = self.full_state()
        for name in ("balance_list", "report_sales", "settlements"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        found = {(kind, pk): texts for kind, pk, texts in transition.requisite_mismatches()}
        self.assertEqual(found, {
            ("receipt", by_warehouse.pk): ["склад: в документе «W2», в движениях «W»"],
            ("receipt", by_date.pk): ["дата: в документе 10.01.2026, в движениях 02.01.2026"],
            ("receipt", by_time.pk): ["время: в документе 18:00, в движениях 12:00"],
            ("transfer", out_side.pk): ["склад-отправитель: в документе «W», в движениях «W1»"],
            ("transfer", in_side.pk): ["склад-получатель: в документе «W», в движениях «W2»"],
        })
        transition_out = self.command("stock_transition", "--limit", "0")
        self.assertIn("склад, дата или время документа и его движений расходятся (требует решения, "
                      "автоматически не исправляется): 5", transition_out)
        self.assertIn(f"приёмка id {by_date.pk}: дата: в документе 10.01.2026, в движениях 02.01.2026", transition_out)
        self.assertIn("склада, даты или времени с движениями — 5", transition_out)
        costs = self.command("check_costs", "--limit", "0")
        self.assertIn("Склад, дата или время документа и его движений расходятся: 5", costs)
        self.assertEqual(self.full_state(), before)                       # только чтение
        self.edit(Receipt.objects.get(pk=by_date.pk))                     # решение: перепроведение
        self.assertNotIn(("receipt", by_date.pk), {row[:2] for row in transition.requisite_mismatches()})

    def test_serial_movements_are_checked_too(self):
        self.product = Product.objects.create(name="Серийный", unit=self.unit, track_serials=True)
        receipt = self.make(Receipt, [(1, "S-9", 100)], 2)
        SerialMovement.objects.filter(doc_type="receipt", doc_id=receipt.pk).update(time=datetime.time(9, 0))
        self.assertEqual(transition.requisite_mismatches(),
                         [("receipt", receipt.pk, ["время: в документе 12:00, в движениях 09:00"])])


# ---------------------------------------------------------------------------
# Одновременные операции (только PostgreSQL)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == "postgresql", "блокировки строк проверяются только на PostgreSQL")
class ConcurrentRequisiteTests(OrgWorld, ConcurrencyMixin, TransactionTestCase):
    """Перенос проведённого документа (editing) против складской операции по
    тому же товару и массовая смена даты против проведения — в обоих порядках.
    Каждая проверка доказывает, что вторая операция ждала блокировку первой
    (run_overlapping по pg_stat_activity); порядок блокировок прежний:
    документ → товары по возрастанию id."""

    def setUp(self):
        self.make_world()

    def doc(self, model, day, **header):
        defaults = {"organization": self.a, "warehouse": self.w, "date": JAN[day], "time": NOON}
        if model is Receipt:
            defaults["supplier"] = self.supplier
        else:
            defaults["customer"] = self.customer
        doc = model.objects.create(**{**defaults, **header})
        doc.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
        return doc

    def change(self, doc, **changes):
        def operation():
            obj = type(doc).objects.get(pk=doc.pk)
            with obj.editing():
                for name, value in changes.items():
                    setattr(obj, name, value)
                obj.save()
        return operation

    def post(self, doc):
        return lambda: type(doc).objects.get(pk=doc.pk).post()

    def overlap(self, first, operations, pause_on):
        second = next(name for name in operations if name != first)
        results = self.run_overlapping(operations[first], operations[second], pause_on=pause_on)
        return dict(zip((first, second), results))

    def test_receipt_move_against_shipment_in_any_order(self):
        for change in ({"warehouse": "W2"}, {"date": 10}):
            for first in ("change", "shipment"):
                with self.subTest(change=change, first=first):
                    self.product = Product.objects.create(name=f"{change} {first}", unit=self.unit)
                    receipt = self.doc(Receipt, 2)
                    receipt.post()
                    shipment = self.doc(Shipment, 5)
                    values = {"warehouse": self.w2} if "warehouse" in change else {"date": JAN[10]}
                    results = self.overlap(first, {"change": self.change(receipt, **values),
                                                   "shipment": self.post(shipment)},
                                           pause_on=(services, "document_outflow"))
                    stored = Receipt.objects.get(pk=receipt.pk)
                    if first == "change":
                        self.assertIsNone(results["change"])
                        self.assertIsInstance(results["shipment"], StockError)   # товара на W 05.01 больше нет
                        self.assertFalse(Shipment.objects.get(pk=shipment.pk).is_posted)
                        self.assertEqual((stored.warehouse_id, stored.date),
                                         (values.get("warehouse", self.w).pk, values.get("date", JAN[2])))
                    else:
                        self.assertIsNone(results["shipment"])
                        self.assertIsInstance(results["change"], StockError)     # отгрузка осталась бы без товара
                        self.assertIn("остаток стал бы -1", str(results["change"]))
                        self.assertEqual((stored.warehouse_id, stored.date), (self.w.pk, JAN[2]))
                    self.assertEqual(transition.requisite_mismatches(), [])

    def test_mass_date_update_against_posting_in_any_order(self):
        from . import posting

        for first in ("post", "update"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Массовая {first}", unit=self.unit)
                draft = self.doc(Receipt, 2)
                pause = (services, "document_outflow") if first == "post" else (posting, "_stock_bound")
                results = self.overlap(first, {
                    "post": self.post(draft),
                    "update": lambda: Receipt.objects.filter(pk=draft.pk).update(date=JAN[10]),
                }, pause_on=pause)
                second = "update" if first == "post" else "post"
                self.assertIsNone(results[first])
                self.assertIsInstance(results[second], PostedChangeError)
                stored = Receipt.objects.get(pk=draft.pk)
                if first == "post":     # смена увидела проведённый документ
                    self.assertEqual((stored.date, stored.is_posted), (JAN[2], True))
                else:                   # проведение увидело в базе другую дату, чем у своего объекта
                    self.assertEqual((stored.date, stored.is_posted), (JAN[10], False))
                    self.assertFalse(StockMovement.objects.filter(doc_type="receipt", doc_id=draft.pk).exists())
                self.assertEqual(transition.requisite_mismatches(), [])
