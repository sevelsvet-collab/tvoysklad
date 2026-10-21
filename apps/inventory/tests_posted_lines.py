"""Строки проведённых складских документов: только вместе с перепроведением.

Дефект (19.10.2026, воспроизведён независимо на временной базе):

1. У проведённой приёмки 1 шт. по 100 строка получила quantity = 2 через
   save(update_fields=["quantity"]): документ — 2 шт. на 200, движение —
   1 шт. на 100.
2. Строку удалили через delete(): документ пустой, движение 1 шт. на 100
   осталось.

Правило (posting, правило 10): строки проведённого документа добавляются,
меняются и удаляются только внутри editing() этого документа (или самим
post()), после всех изменений документ перепроводится один раз. Остальные
пути отклоняются до записи; черновики редактируются как обычно.
"""
from decimal import Decimal
from io import StringIO
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.models import F
from django.test import TransactionTestCase
from django.urls import reverse

from apps.catalog.models import Product
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.constants import DOC_DRAFT
from apps.purchases.admin import ReceiptAdmin
from apps.purchases.models import Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment, ShipmentLine

from . import services, transition
from .models import AdjustmentLine, StockAdjustment, StockMovement, Transfer, TransferLine
from .services import PostedChangeError, StockError
from .tests_costing import legacy
from .tests_org_document_change import KINDS, ChangeWorld
from .tests_org_stock import OrgWorld

D = Decimal
ZERO = D("0")


class LineReproductionTests(ChangeWorld):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.doc_ = Receipt.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier,
                                           **self._when())
        self.line_ = self.doc_.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
        self.doc_.post()

    def picture(self):
        """Что показывает документ и что записано в движениях."""
        doc = Receipt.objects.get(pk=self.doc_.pk)
        moves = StockMovement.objects.filter(doc_type="receipt", doc_id=doc.pk)
        return {"документ": (sum((line.quantity for line in doc.lines.all()), D("0")), doc.total),
                "движения": sorted((m.quantity, m.amount) for m in moves)}

    def attempt(self, action):
        before = self.full_state()
        rejected = None
        try:
            action()
        except StockError as exc:
            rejected = exc
        stored = (D("1.000"), D("100.00"))
        self.assertEqual(self.picture(), {"документ": stored, "движения": [stored]})
        self.assertIsNotNone(rejected, "запись строки проведённого документа без перепроведения должна быть отклонена")
        self.assertIn("отдельно от его перепроведения", str(rejected))
        self.assertEqual(self.full_state(), before)

    def test_quantity_saved_without_reposting(self):
        line = ReceiptLine.objects.get(pk=self.line_.pk)
        line.quantity = 2
        # было: {"документ": (2, 200), "движения": [(1, 100)]}
        self.attempt(lambda: line.save(update_fields=["quantity"]))

    def test_line_deleted_without_reposting(self):
        # было: {"документ": (0, 0), "движения": [(1, 100)]}
        self.attempt(ReceiptLine.objects.get(pk=self.line_.pk).delete)


# ---------------------------------------------------------------------------
# Все виды строк × все пути записи в обход перепроведения
# ---------------------------------------------------------------------------

LINE_MODELS = (ReceiptLine, SupplierReturnLine, ShipmentLine, CustomerReturnLine, TransferLine, AdjustmentLine)


class LineWorld(ChangeWorld):
    def setUp(self):
        super().setUp()
        self.spare = Product.objects.create(name="Запасной", unit=self.unit, purchase_price=10)

    def snapshot(self):
        """Полное состояние учёта и все защищённые поля всех строк."""
        lines = sorted((model.__name__, *row) for model in LINE_MODELS for row in legacy(model).values_list(
            "pk", *[field.attname for field in model.guarded_fields()]))
        return self.full_state(), lines

    def draft_of(self, model, **header):
        """Черновик той же модели документа."""
        header = {**{Transfer: {"warehouse_from": self.w1, "warehouse_to": self.w2},
                     StockAdjustment: {"kind": StockAdjustment.KIND_INCOME}}.get(model, {}), **header}
        return self.doc(model, self.a, [(1, "", 100)], **header)

    def changed(self, line, field):
        """Другое значение защищённого поля строки."""
        old = getattr(line, field.attname)
        name = field.name
        if name == "quantity":
            return old + 1
        if name == "product":
            return self.spare
        if name in ("price", "fallback_cost"):
            return (old or 0) + 1
        if name == "discount":
            return old + 5
        if name == "vat_rate":
            return "10" if old != "10" else "20"
        if name == "serial_numbers":
            return "X-9"
        if name == "fallback_source":
            return "legacy" if old != "legacy" else "card"
        if name == "shipment_line":
            return None if old else ShipmentLine.objects.first()
        raise AssertionError(name)

    def new_line(self, model, doc):
        values = {model.DOCUMENT_FIELD: doc, "product": self.spare, "quantity": 1}
        if any(field.name == "price" for field in model._meta.fields):
            values["price"] = 100
        return values


class LineBypassTests(LineWorld):
    def attempts(self, doc):
        line_model = doc.lines.model
        line = doc.lines.get()
        fk = line_model.DOCUMENT_FIELD
        draft = self.draft_of(type(doc))
        draft_line = draft.lines.get()
        found = {}

        def save(field, fields=None):
            obj = line_model.objects.get(pk=line.pk)
            setattr(obj, field.name, self.changed(obj, field))
            return lambda: obj.save(**({"update_fields": fields} if fields is not None else {}))

        for field in line_model.guarded_fields():
            if field.name == fk:
                continue
            found[f"save() — {field.name}"] = save(field)
            found[f"save(update_fields=[{field.name}])"] = save(field, [field.name])
            if field.attname != field.name:
                found[f"save(update_fields=[{field.attname}])"] = save(field, [field.attname])

        def move_out():
            obj = line_model.objects.get(pk=line.pk)
            setattr(obj, fk, draft)
            obj.save()

        def move_in():
            obj = line_model.objects.get(pk=draft_line.pk)
            setattr(obj, fk, doc)
            obj.save()

        def bulk_update():
            obj = line_model.objects.get(pk=line.pk)
            obj.quantity += 1
            line_model.objects.bulk_update([obj], ["quantity"])

        def upsert():
            obj = line_model.objects.get(pk=line.pk)
            obj.quantity += 1
            line_model.objects.bulk_create([obj], update_conflicts=True, update_fields=["quantity"],
                                           unique_fields=["id"])

        found.update({
            "delete()": line_model.objects.get(pk=line.pk).delete,
            "Line.objects.create()": lambda: line_model.objects.create(**self.new_line(line_model, doc)),
            "связанный менеджер: create()": lambda: doc.lines.create(
                **{k: v for k, v in self.new_line(line_model, doc).items() if k != fk}),
            "связанный менеджер: update()": lambda: doc.lines.update(quantity=9),
            "связанный менеджер: all().delete()": lambda: doc.lines.all().delete(),
            "связанный менеджер: update_or_create()": lambda: doc.lines.update_or_create(
                pk=line.pk, defaults={"quantity": 9}),
            "связанный менеджер: add() строки черновика": lambda: doc.lines.add(line_model.objects.get(pk=draft_line.pk)),
            "связанный менеджер черновика: add() строки проведённого": lambda: draft.lines.add(
                line_model.objects.get(pk=line.pk)),
            "смена документа строки: проведённый → черновик": move_out,
            "смена документа строки: черновик → проведённый": move_in,
            "QuerySet.update()": lambda: line_model.objects.filter(pk=line.pk).update(quantity=7),
            "QuerySet.update(выражение)": lambda: line_model.objects.filter(pk=line.pk).update(
                quantity=F("quantity") + 1),
            "QuerySet.update(product_id)": lambda: line_model.objects.filter(pk=line.pk).update(
                product_id=self.spare.pk),
            "QuerySet.delete()": lambda: line_model.objects.filter(pk=line.pk).delete(),
            "bulk_create()": lambda: line_model.objects.bulk_create([line_model(**self.new_line(line_model, doc))]),
            "bulk_update()": bulk_update,
            "bulk_create(update_conflicts)": upsert,
        })
        return found

    def test_every_line_kind_rejects_every_bypass(self):
        for kind in KINDS:
            doc = self.posted(kind)
            for title, attempt in self.attempts(doc).items():
                before = self.snapshot()
                with self.subTest(kind=kind, attempt=title):
                    with self.assertRaises(PostedChangeError):
                        attempt()
                    self.assertEqual(self.snapshot(), before)

    def test_drafts_lines_change_as_usual(self):
        for model in (Receipt, Shipment, CustomerReturn, SupplierReturn, Transfer, StockAdjustment):
            with self.subTest(model=model.__name__):
                draft, other = self.draft_of(model), self.draft_of(model)
                line_model = draft.lines.model
                line = draft.lines.get()
                line.quantity = 4
                line.save(update_fields=["quantity"])
                draft.lines.create(**{k: v for k, v in self.new_line(line_model, draft).items()
                                      if k != line_model.DOCUMENT_FIELD})
                line_model.objects.filter(pk=line.pk).update(quantity=F("quantity") + 1)
                other.lines.add(line_model.objects.get(pk=line.pk))           # перенос между черновиками
                line_model.objects.bulk_create([line_model(**self.new_line(line_model, other))])
                self.assertEqual(other.lines.count(), 3)
                self.assertEqual(line_model.objects.get(pk=line.pk).quantity, D("5"))
                other.lines.filter(product=self.spare).delete()
                self.assertEqual(other.lines.count(), 2)

    def test_stale_line_object_after_posting_by_other_user(self):
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        stale = draft.lines.get()                                  # открыт, пока документ — черновик
        Receipt.objects.get(pk=draft.pk).post()
        before = self.snapshot()
        stale.quantity = 5
        with self.assertRaisesMessage(PostedChangeError, "загрузите его заново"):
            stale.save()
        self.assertEqual(self.snapshot(), before)

    def test_document_with_movements_is_not_a_plain_draft(self):
        receipt = self.receipt(self.a, 1, 100)
        Receipt.objects.filter(pk=receipt.pk).update(status=DOC_DRAFT)   # статус переписан мимо отмены
        before = self.snapshot()
        with self.assertRaises(PostedChangeError):
            receipt.lines.update(quantity=3)
        with self.assertRaises(PostedChangeError):
            receipt.lines.get().delete()
        self.assertEqual(self.snapshot(), before)

    def test_mass_delete_of_posted_documents_is_rejected_drafts_allowed(self):
        posted = self.receipt(self.a, 1, 100)
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "Массовое удаление проведённых документов"):
            Receipt.objects.filter(pk__in=[posted.pk, draft.pk]).delete()
        self.assertEqual(self.snapshot(), before)
        Receipt.objects.filter(pk=draft.pk).delete()
        self.assertFalse(Receipt.objects.filter(pk=draft.pk).exists())
        Receipt.objects.get(pk=posted.pk).delete()                     # по одному — со снятием с проведения
        self.assertFalse(StockMovement.objects.exists())


# ---------------------------------------------------------------------------
# Разрешение editing(): конкретный документ, текущая транзакция, заранее
# заблокированные товары
# ---------------------------------------------------------------------------

class LinePermitTests(LineWorld):
    def test_permit_is_for_this_document_only_and_only_inside_the_block(self):
        first, second = self.receipt(self.a, 1, 100), self.receipt(self.a, 1, 100)
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "отдельно от его перепроведения"):
            with first.editing():
                second.lines.update(quantity=5)                          # чужой проведённый документ
        self.assertEqual(self.snapshot(), before)
        with first.editing():
            first.lines.update(quantity=2)
        with self.assertRaises(PostedChangeError):                        # блок закончился — разрешения нет
            first.lines.update(quantity=3)
        self.assertEqual(self.balances(), {("А", "W"): (D("3"), D("300.00"))})

    def test_new_product_must_be_locked_in_advance(self):
        receipt = self.receipt(self.a, 1, 100)
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "не заблокирован заранее"):
            with receipt.editing():
                receipt.lines.create(product=self.spare, quantity=1, price=10, vat_rate="none")
        with self.assertRaisesMessage(PostedChangeError, "не заблокирован заранее"):
            with receipt.editing():
                receipt.lines.update(product=self.spare)
        self.assertEqual(self.snapshot(), before)
        with receipt.editing(products=[self.spare.pk]):
            receipt.lines.create(product=self.spare, quantity=1, price=10, vat_rate="none")
        self.assertEqual(self.balances(self.spare), {("А", "W"): (D("1"), D("10.00"))})

    def test_lines_of_draft_inside_other_editing_only_by_locked_products(self):
        receipt = self.receipt(self.a, 1, 100)
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        with self.assertRaisesMessage(PostedChangeError, "уже заблокированным товарам"):
            with receipt.editing():
                draft.lines.create(product=self.spare, quantity=1, price=10, vat_rate="none")
        with receipt.editing():
            draft.lines.update(quantity=4)                               # товар уже заблокирован
        self.assertEqual(draft.lines.get().quantity, D("4"))

    def test_post_itself_writes_own_lines_of_posted_return(self):
        """Перепроведение проведённого возврата (кнопка «Провести») само пишет
        его строки — связь со строкой отгрузки и резервную стоимость: у post()
        разрешение на строки своего документа."""
        self.receipt(self.a, 2, 100)
        sale = self.ship(self.a, 1)
        back = self.customer_return(self.a, 1, basis=sale)
        # старые данные: связь и резервная стоимость не записаны
        legacy(CustomerReturnLine).filter(document=back).update(shipment_line=None, fallback_cost=None,
                                                                fallback_source="")
        CustomerReturn.objects.get(pk=back.pk).post()
        line = back.lines.get()
        self.assertEqual(line.shipment_line_id, sale.lines.get().pk)
        self.assertEqual(line.fallback_cost, D("70.00"))                 # закупочная цена карточки
        with self.assertRaises(PostedChangeError):                        # а вне проведения — нельзя
            back.lines.update(fallback_cost=1)

    def test_editing_reposts_once_after_all_line_changes(self):
        receipt = self.receipt(self.a, 2, 100)
        sale = self.ship(self.a, 1)
        with mock.patch.object(Receipt, "post", autospec=True, side_effect=Receipt.post) as post:
            with receipt.editing(products=[self.spare.pk]) as posted:
                self.assertTrue(posted)
                line = receipt.lines.get()
                line.quantity, line.price = 3, 120
                line.save()
                receipt.lines.create(product=self.spare, quantity=2, price=50, vat_rate="none")
                receipt.lines.filter(product=self.spare).update(discount=10)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(sorted(StockMovement.objects.filter(doc_type="receipt").values_list("product_id", "quantity",
                                                                                           "amount")),
                         sorted([(self.product.pk, D("3.000"), D("360.00")), (self.spare.pk, D("2.000"), D("90.00"))]))
        self.assertEqual(self.line(sale), (D("120.00"), ZERO))           # себестоимость отгрузки пересчитана


# ---------------------------------------------------------------------------
# Безопасные изменения и отказы: наличие, возвраты, серийная история, связь
# ---------------------------------------------------------------------------

class LineEditScenarioTests(LineWorld):
    def edit(self, doc, change, products=()):
        with type(doc).objects.get(pk=doc.pk).editing(products=products):
            change()

    def assertRejectedWholly(self, doc, change, message, products=()):
        before = self.snapshot()
        with self.assertRaisesMessage(StockError, message):
            self.edit(doc, change, products)
        self.assertEqual(self.snapshot(), before)

    def test_quantity_below_later_shipment_is_rejected(self):
        receipt = self.receipt(self.a, 2, 100)
        self.ship(self.a, 2)
        self.assertRejectedWholly(receipt, lambda: receipt.lines.update(quantity=1), "остаток стал бы -1")
        self.assertRejectedWholly(receipt, lambda: receipt.lines.all().delete(), "остаток стал бы -2")

    def test_return_quantity_above_basis_is_rejected(self):
        self.receipt(self.a, 3, 100)
        sale = self.ship(self.a, 1)
        back = self.customer_return(self.a, 1, basis=sale)
        self.assertRejectedWholly(back, lambda: back.lines.update(quantity=2), "доступно к возврату")
        self.assertRejectedWholly(sale, lambda: sale.lines.update(quantity=D("0.5")), "уже возвращено")

    def test_serial_history_is_checked(self):
        self.product.track_serials = True
        self.product.save()
        receipt = self.receipt(self.a, 1, 100, serial="S-1")
        self.ship(self.a, 1, serial="S-1")
        self.assertRejectedWholly(receipt, lambda: receipt.lines.update(serial_numbers="S-2"),
                                  "номер S-1 товара «Товар» участвует в проведённых документах")

    def test_price_discount_and_vat_change_recalculates_cost(self):
        receipt = self.receipt(self.a, 2, 100)
        sale = self.ship(self.a, 1)
        self.edit(receipt, lambda: receipt.lines.update(price=150, discount=20, vat_rate="20"))
        self.assertEqual(self.amount(receipt), D("240.00"))              # 2 × 150 − 20 %
        self.assertEqual(self.line(sale), (D("120.00"), ZERO))
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).lines.get().vat_rate, "20")

    def test_product_change_moves_stock_to_other_product(self):
        receipt = self.receipt(self.a, 1, 100)
        self.edit(receipt, lambda: receipt.lines.update(product=self.spare), products=[self.spare.pk])
        self.assertEqual(self.stock_of(self.product), {})
        self.assertEqual(self.stock_of(self.spare), {("А", "W"): (D("1"), D("100.00"))})

    def test_return_line_link_changes_only_through_editing(self):
        self.receipt(self.a, 2, 100)
        sale = Shipment.objects.create(organization=self.a, warehouse=self.w, customer=self.customer, **self._when())
        first = sale.lines.create(product=self.product, quantity=1, price=300, vat_rate="none")
        second = sale.lines.create(product=self.product, quantity=1, price=300, vat_rate="none")
        sale.post()
        back = CustomerReturn.objects.create(organization=self.a, warehouse=self.w, customer=self.customer,
                                             shipment=sale, **self._when())
        back.lines.create(product=self.product, quantity=1, price=300, vat_rate="none", shipment_line=first)
        back.post()
        self.edit(back, lambda: back.lines.update(shipment_line=second))
        self.assertEqual(back.lines.get().shipment_line_id, second.pk)
        other_sale = self.ship(self.a, 1)
        self.assertRejectedWholly(back, lambda: back.lines.update(shipment_line=other_sale.lines.get()),
                                  "указывает на строку другой отгрузки или другого товара")

    def test_deleting_shipment_line_referenced_by_return(self):
        self.receipt(self.a, 3, 100)
        sale = Shipment.objects.create(organization=self.a, warehouse=self.w, customer=self.customer, **self._when())
        first = sale.lines.create(product=self.product, quantity=1, price=300, vat_rate="none")
        sale.lines.create(product=self.product, quantity=1, price=300, vat_rate="none")
        sale.post()
        back = CustomerReturn.objects.create(organization=self.a, warehouse=self.w, customer=self.customer,
                                             shipment=sale, **self._when())
        back.lines.create(product=self.product, quantity=1, price=300, vat_rate="none", shipment_line=first)
        back.post()
        self.assertRejectedWholly(sale, lambda: sale.lines.filter(pk=first.pk).delete(),
                                  "На документ ссылается проведённый возврат покупателя")
        CustomerReturn.objects.get(pk=back.pk).unpost()                 # черновик: связь просто обнуляется
        self.edit(sale, lambda: sale.lines.filter(pk=first.pk).delete())
        self.assertIsNone(back.lines.get().shipment_line_id)

    def test_form_with_several_lines_reposts_once(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        receipt = Receipt.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier,
                                         **self._when())
        keep = receipt.lines.create(product=self.product, quantity=2, price=100, vat_rate="none")
        drop = receipt.lines.create(product=self.product, quantity=1, price=100, vat_rate="none")
        receipt.post()
        data = {
            "date": receipt.date.isoformat(), "time": receipt.time.strftime("%H:%M:%S"), "number": receipt.number,
            "organization": self.a.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
            "supplier_invoice": "", "comment": "", "action": "save",
            "lines-TOTAL_FORMS": "3", "lines-INITIAL_FORMS": "2", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        rows = [(keep.pk, self.product.pk, "4", "110", ""), (drop.pk, self.product.pk, "1", "100", "on"),
                ("", self.spare.pk, "3", "20", "")]
        for index, (pk, product, quantity, price, delete) in enumerate(rows):
            data.update({f"lines-{index}-id": pk, f"lines-{index}-product": product,
                         f"lines-{index}-quantity": quantity, f"lines-{index}-price": price,
                         f"lines-{index}-discount": "0", f"lines-{index}-vat_rate": "none",
                         f"lines-{index}-serial_numbers": "", f"lines-{index}-DELETE": delete})
        with mock.patch.object(Receipt, "post", autospec=True, side_effect=Receipt.post) as post:
            response = self.client.post(reverse("receipt_edit", args=[receipt.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(sorted(StockMovement.objects.filter(doc_type="receipt").values_list(
            "product_id", "quantity", "amount")),
            sorted([(self.product.pk, D("4.000"), D("440.00")), (self.spare.pk, D("3.000"), D("60.00"))]))
        self.assertEqual(transition.line_mismatches(), [])

    def test_unpost_and_delete_of_whole_document_still_work(self):
        receipt = self.receipt(self.a, 1, 100)
        Receipt.objects.get(pk=receipt.pk).unpost()
        receipt.lines.update(quantity=2)                                 # черновик — правится как обычно
        Receipt.objects.get(pk=receipt.pk).post()
        self.assertEqual(self.amount(receipt), D("200.00"))
        Receipt.objects.get(pk=receipt.pk).delete()
        self.assertFalse(ReceiptLine.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_admin_edits_draft_lines_only(self):
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="pass12345"))
        draft = self.doc(Receipt, self.a, [(1, "", 100)])
        posted = self.receipt(self.a, 1, 100)

        def data(doc, quantity):
            line = doc.lines.get()
            return {
                "number": doc.number, "date": doc.date.isoformat(), "time": doc.time.strftime("%H:%M:%S"),
                "organization": self.a.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
                "supplier_invoice": "", "comment": "",
                "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
                "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-receipt": doc.pk,
                "lines-0-product": line.product_id, "lines-0-quantity": quantity, "lines-0-serial_numbers": "",
                "lines-0-price": "100", "lines-0-discount": "0", "lines-0-vat_rate": "none",
            }

        response = self.client.post(reverse("admin:purchases_receipt_change", args=[draft.pk]), data(draft, "5"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(draft.lines.get().quantity, D("5"))
        url = reverse("admin:purchases_receipt_change", args=[posted.pk])
        before = self.snapshot()
        self.assertEqual(self.client.post(url, data(posted, "5")).status_code, 403)
        with mock.patch.object(ReceiptAdmin, "has_change_permission", return_value=True):
            response = self.client.post(url, data(posted, "5"), follow=True)
        self.assertContains(response, "Изменения не сохранены")
        self.assertEqual(self.snapshot(), before)

    def stock_of(self, product):
        return {key: value for key, value in self.balances(product).items() if value[0]}


# ---------------------------------------------------------------------------
# Диагностика старых расхождений строк (без исправления)
# ---------------------------------------------------------------------------

class LineDiagnosticsTests(LineWorld):
    def command(self, name, *args):
        out = StringIO()
        call_command(name, *args, stdout=out)
        return out.getvalue()

    def test_reproduced_and_other_mismatches_are_found_and_nothing_is_rewritten(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        changed = self.receipt(self.a, 1, 100)
        removed = self.receipt(self.a, 1, 100)
        added = self.receipt(self.a, 1, 100)
        # старые данные: прямая запись мимо программы
        legacy(ReceiptLine).filter(receipt=changed).update(quantity=2)
        legacy(ReceiptLine).filter(receipt=removed).delete()
        legacy(ReceiptLine).bulk_create([ReceiptLine(receipt=added, product=self.product, quantity=1, price=100,
                                                     vat_rate="none")])
        phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        numbered = self.receipt(self.a, 1, 100, product=phone, serial="S-1")
        legacy(ReceiptLine).filter(receipt=numbered).update(serial_numbers="S-2")
        before = self.snapshot()
        for name in ("balance_list", "report_sales", "settlements"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        found = {(kind, pk): texts for kind, pk, texts in transition.line_mismatches()}
        self.assertEqual(found, {
            ("receipt", changed.pk): ["товар «Товар», приход: в строках 2, в движениях 1"],
            ("receipt", removed.pk): ["товар «Товар», приход: в строках 0, в движениях 1"],
            ("receipt", added.pk): ["товар «Товар», приход: в строках 2, в движениях 1"],
            ("receipt", numbered.pk): ["номера «Телефон»: в строках S-2, в движениях S-1"],
        })
        costs = self.command("check_costs", "--limit", "0")
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — движения не сопоставляются со строками документа", costs)   # и раньше
        self.assertIn("Строки документа и его движения расходятся: 4", costs)
        output = self.command("stock_transition", "--limit", "0")
        self.assertIn("строки документа и его движения расходятся (количество, серийные номера; требует решения, "
                      "автоматически не исправляется): 4", output)
        self.assertIn("строк с движениями — 4", output)
        self.assertEqual(self.snapshot(), before)                           # только чтение

    def test_transfer_sides_and_services_are_respected(self):
        service = Product.objects.create(name="Доставка", unit=self.unit, item_type=Product.TYPE_SERVICE)
        self.receipt(self.a, 2, 100, wh=self.w1)
        transfer = self.transfer(self.a, self.w1, self.w2, 1)
        sale = Shipment.objects.create(organization=self.a, warehouse=self.w2, customer=self.customer,
                                       **self._when())
        sale.lines.create(product=service, quantity=1, price=500, vat_rate="none")   # услуга — без движений
        sale.post()
        self.assertEqual(transition.line_mismatches(), [])
        legacy(TransferLine).filter(transfer=transfer).update(quantity=2)
        self.assertEqual(transition.line_mismatches(), [("transfer", transfer.pk, [
            "товар «Товар», расход: в строках 2, в движениях 1",
            "товар «Товар», приход: в строках 2, в движениях 1",
        ])])


# ---------------------------------------------------------------------------
# Одновременные операции (только PostgreSQL)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == "postgresql", "блокировки строк проверяются только на PostgreSQL")
class ConcurrentLineTests(OrgWorld, ConcurrencyMixin, TransactionTestCase):
    """Изменение строк против проведения и отмены — в обоих порядках. Каждая
    проверка доказывает, что вторая операция ждала блокировку первой
    (run_overlapping по pg_stat_activity). Порядок блокировок прежний:
    документ → товары; запись строки без editing() блокирует строку своего
    документа и видит его актуальный статус."""

    def setUp(self):
        self.make_world()

    def post(self, doc):
        return lambda: type(doc).objects.get(pk=doc.pk).post()

    def unpost(self, doc):
        return lambda: type(doc).objects.get(pk=doc.pk).unpost()

    def edit_lines(self, doc, **values):
        def operation():
            obj = type(doc).objects.get(pk=doc.pk)
            with obj.editing():
                obj.lines.update(**values)
        return operation

    def overlap(self, first, operations, pause_on):
        second = next(name for name in operations if name != first)
        return dict(zip((first, second), self.run_overlapping(operations[first], operations[second],
                                                              pause_on=pause_on)))

    def moves(self, doc):
        return sorted(StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk).values_list(
            "quantity", flat=True))

    def test_line_edit_against_shipment_in_any_order(self):
        for first in ("edit", "shipment"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Строка {first}", unit=self.unit)
                receipt = self.receipt(self.a, 2, 100)
                shipment = self.ship(self.a, 2, post=False)
                results = self.overlap(first, {"edit": self.edit_lines(receipt, quantity=1),
                                               "shipment": self.post(shipment)},
                                       pause_on=(services, "document_outflow"))
                if first == "edit":
                    self.assertIsNone(results["edit"])
                    self.assertIsInstance(results["shipment"], StockError)    # в приёмке осталось 1
                    self.assertEqual((receipt.lines.get().quantity, self.moves(receipt)), (D("1"), [D("1")]))
                    self.assertFalse(Shipment.objects.get(pk=shipment.pk).is_posted)
                else:
                    self.assertIsNone(results["shipment"])
                    self.assertIsInstance(results["edit"], StockError)        # отгрузка осталась бы без товара
                    self.assertIn("остаток стал бы -1", str(results["edit"]))
                    self.assertEqual((receipt.lines.get().quantity, self.moves(receipt)), (D("2"), [D("2")]))
                self.assertEqual(transition.line_mismatches(), [])

    def test_plain_line_update_against_posting_in_any_order(self):
        from . import posting

        for first in ("post", "update"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Черновик {first}", unit=self.unit)
                draft = self.receipt(self.a, 1, 100, post=False)
                line = draft.lines.get()
                pause = (services, "document_outflow") if first == "post" else (posting, "_stock_bound")
                results = self.overlap(first, {
                    "post": self.post(draft),
                    "update": lambda: ReceiptLine.objects.filter(pk=line.pk).update(quantity=5),
                }, pause_on=pause)
                doc = Receipt.objects.get(pk=draft.pk)
                if first == "post":     # запись увидела проведённый документ — отказ
                    self.assertIsNone(results["post"])
                    self.assertIsInstance(results["update"], PostedChangeError)
                    self.assertEqual((doc.lines.get().quantity, self.moves(doc)), (D("1"), [D("1")]))
                else:                   # проведение дождалось записи и провело уже 5 шт.
                    self.assertEqual(results, {"update": None, "post": None})
                    self.assertEqual((doc.lines.get().quantity, self.moves(doc)), (D("5"), [D("5")]))
                self.assertTrue(doc.is_posted)
                self.assertEqual(transition.line_mismatches(), [])

    def test_line_writes_against_unpost(self):
        for first, way in (("edit", "editing"), ("unpost", "editing"), ("unpost", "plain")):
            with self.subTest(first=first, way=way):
                self.product = Product.objects.create(name=f"Отмена {first} {way}", unit=self.unit)
                receipt = self.receipt(self.a, 1, 100)
                write = self.edit_lines(receipt, quantity=3) if way == "editing" else (
                    lambda receipt=receipt: ReceiptLine.objects.filter(receipt=receipt).update(quantity=3))
                pause = (services, "document_outflow") if first == "edit" else (services, "history_snapshot")
                results = self.overlap(first, {"edit": write, "unpost": self.unpost(receipt)}, pause_on=pause)
                self.assertEqual(results, {"edit": None, "unpost": None})
                doc = Receipt.objects.get(pk=receipt.pk)
                # обе операции прошли по очереди: строка — 3 шт., документ — черновик без движений
                self.assertEqual((doc.lines.get().quantity, doc.is_posted, self.moves(doc)), (D("3"), False, []))
                self.assertEqual(transition.line_mismatches(), [])
