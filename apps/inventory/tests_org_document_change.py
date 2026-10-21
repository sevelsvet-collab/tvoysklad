"""Смена организации проведённого документа: только через перепроведение.

Дефект (19.10.2026): у проведённой приёмки организации А выполняли
receipt.organization = Б; receipt.save(update_fields=["organization"]).
Документ и взаиморасчёты переходили к Б, а движение и остаток оставались у А:
закупка числилась за Б, складской актив — за А.

Контракт (posting, правило 9): организацию проведённого документа меняет
только PostableMixin.editing() / change_organization() — сохранение и
перепроведение одной транзакцией с проверкой обеих историй, возвратов и
оснований. Им же пользуются форма документа и административная панель.
Обычное сохранение, массовые update() / bulk_update() / bulk_create() и
проведение объекта с несохранённой сменой организации отклоняются до записи.
"""
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import F, Value
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product
from apps.core.constants import DOC_DRAFT, DOC_POSTED
from apps.core.models import DocumentNumber
from apps.finance.settlements import settlement_pairs
from apps.purchases.admin import ReceiptAdmin
from apps.purchases.models import Receipt, ReceiptLine, SupplierReturn, SupplierReturnLine
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment, ShipmentLine

from . import transition
from .models import AdjustmentLine, SerialMovement, StockAdjustment, StockBalance, StockMovement, Transfer, TransferLine
from .services import OrganizationChangeError, StockError
from .tests_org_stock import OrgStockBase

DOCS = (Receipt, SupplierReturn, Shipment, CustomerReturn, Transfer, StockAdjustment)
LINES = (ReceiptLine, SupplierReturnLine, ShipmentLine, CustomerReturnLine, TransferLine, AdjustmentLine)
KINDS = ("receipt", "shipment", "customer_return", "supplier_return", "transfer", "adjustment_income",
         "adjustment_expense")


class ChangeWorld(OrgStockBase):
    """Документы всех семи видов; серийный товар — свой для каждого вида."""

    def doc(self, model, org, rows, **header):
        """Документ с серийными строками rows=[(количество, номера, цена)]."""
        defaults = {"organization": org, **self._when()}
        if model in (Receipt, SupplierReturn):
            defaults["supplier"] = self.supplier
        if model in (Shipment, CustomerReturn):
            defaults["customer"] = self.customer
        if model is not Transfer:
            defaults["warehouse"] = self.w
        doc = model.objects.create(**{**defaults, **header})
        for quantity, numbers, price in rows:
            line = {"product": self.product, "quantity": quantity, "serial_numbers": numbers}
            if model not in (Transfer,):
                line["price"] = price
            if model in (Receipt, SupplierReturn, Shipment, CustomerReturn):
                line["vat_rate"] = "none"
            doc.lines.create(**line)
        return doc

    def posted(self, kind, org=None):
        """Проведённый документ вида kind организации А (с нужной подготовкой)."""
        org = org or self.a
        self.product = Product.objects.create(name=f"Товар {kind}", unit=self.unit, purchase_price=70,
                                              track_serials=True)
        if kind == "receipt":
            doc = self.doc(Receipt, org, [(1, "R-1", 100)])
        elif kind == "adjustment_income":
            doc = self.doc(StockAdjustment, org, [(1, "I-1", 100)], kind=StockAdjustment.KIND_INCOME)
        else:
            number = f"{kind}-1"                      # номера — свои у каждого вида
            basis = self.doc(Receipt, org, [(2, f"{number}\n{kind}-2", 100)],
                             **({"warehouse": self.w1} if kind == "transfer" else {}))
            basis.post()
            if kind == "shipment":
                doc = self.doc(Shipment, org, [(1, number, 300)])
            elif kind == "customer_return":
                sale = self.doc(Shipment, org, [(1, number, 300)])
                sale.post()
                doc = self.doc(CustomerReturn, org, [(1, number, 300)], shipment=sale)
            elif kind == "supplier_return":
                doc = self.doc(SupplierReturn, org, [(1, number, 100)], receipt=basis)
            elif kind == "transfer":
                doc = self.doc(Transfer, org, [(1, number, None)], warehouse_from=self.w1, warehouse_to=self.w2)
            else:
                doc = self.doc(StockAdjustment, org, [(1, number, 0)], kind=StockAdjustment.KIND_EXPENSE)
        doc.post()
        return type(doc).objects.get(pk=doc.pk)

    # ---------- чтение ----------

    def full_state(self):
        """Всё, что должно остаться прежним при отказе: организации и статусы
        документов, строки, движения, номера, покрытия, себестоимость, остатки,
        взаиморасчёты и нумерация."""
        docs = sorted((model.__name__, *row) for model in DOCS for row in model._base_manager.values_list(
            "pk", "organization_id", "status", "comment"))
        lines = sorted((model.__name__, *row) for model in LINES for row in model.objects.values_list(
            "pk", "product_id", "quantity", "serial_numbers"))
        settlements = sorted((p.organization.pk, p.counterparty.pk, p.shipped, p.customer_returned, p.received,
                              p.supplier_returned) for p in settlement_pairs())
        numbers = sorted(DocumentNumber.objects.values_list("organization_id", "doc_type", "year", "last_number"))
        return self.state(), docs, lines, settlements, numbers

    def owners(self, doc):
        """Кому принадлежит документ: сам документ, его складские движения и
        движения номеров, ненулевые остатки, взаиморасчёты по документам."""
        stored = type(doc)._base_manager.get(pk=doc.pk)
        names = {self.a.pk: "А", self.b.pk: "Б"}
        return {
            "документ": names[stored.organization_id],
            "движения": sorted({names[org] for org in StockMovement.objects.filter(
                doc_type=stored.DOC_TYPE, doc_id=stored.pk).values_list("organization_id", flat=True)}),
            "номера": sorted({names[org] for org in SerialMovement.objects.filter(
                doc_type=stored._meta.model_name, doc_id=stored.pk).values_list("organization_id", flat=True)}),
            "остаток": sorted({names[b.organization_id] for b in StockBalance.objects.exclude(quantity=0)}),
            "взаиморасчёты": sorted({names[p.organization.pk] for p in settlement_pairs()
                                     if p.received or p.shipped or p.customer_returned or p.supplier_returned}),
        }


# ---------------------------------------------------------------------------
# Исходный сценарий и все способы записи в обход перепроведения
# ---------------------------------------------------------------------------

class PlainSaveBypassTests(ChangeWorld):
    def test_plain_save_of_posted_receipt_keeps_document_and_stock_together(self):
        """Исходный сценарий: А принимает 1 шт. по 100, у приёмки меняют
        организацию обычным сохранением."""
        receipt = self.receipt(self.a, 1, 100)
        before = self.full_state()
        receipt.organization = self.b
        rejected = None
        try:
            receipt.save(update_fields=["organization"])
        except StockError as exc:
            rejected = exc
        # было: {"документ": "Б", "движения": ["А"], "остаток": ["А"], "взаиморасчёты": ["Б"]}
        self.assertEqual(self.owners(receipt), {"документ": "А", "движения": ["А"], "номера": [], "остаток": ["А"],
                                                "взаиморасчёты": ["А"]})
        self.assertIsInstance(rejected, OrganizationChangeError, "сохранение с новой организацией должно быть отклонено")
        self.assertIn("простым сохранением", str(rejected))
        self.assertEqual(self.full_state(), before)
        self.assertEqual(receipt.organization_id, self.b.pk)        # объект не тронут, база — прежняя

    def test_every_document_kind_rejects_every_bypass_before_writing(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                doc = self.posted(kind)
                model = type(doc)
                owners, before = self.owners(doc), self.full_state()
                self.assertEqual(owners["документ"], "А")
                self.assertEqual(owners["движения"], ["А"])
                self.assertEqual(owners["номера"], ["А"])

                def plain(fields=None, attr="organization"):
                    obj = model.objects.get(pk=doc.pk)
                    setattr(obj, attr, self.b if attr == "organization" else self.b.pk)
                    return lambda: obj.save(**({"update_fields": fields} if fields is not None else {}))

                def bulk(field):
                    obj = model.objects.get(pk=doc.pk)
                    obj.organization = self.b
                    return lambda: model.objects.bulk_update([obj], [field])

                def upsert():
                    obj = model.objects.get(pk=doc.pk)
                    obj.organization = self.b
                    return lambda: model.objects.bulk_create([obj], update_conflicts=True,
                                                             update_fields=["organization"], unique_fields=["id"])

                attempts = {
                    "save()": plain(),
                    "save(update_fields=[organization])": plain(["organization"]),
                    "save(update_fields=[organization_id])": plain(["organization_id"]),
                    "organization_id = …; save()": plain(attr="organization_id"),
                    "QuerySet.update(organization=…)": lambda: model.objects.filter(pk=doc.pk).update(
                        organization=self.b),
                    "QuerySet.update(organization_id=…)": lambda: model.objects.filter(pk=doc.pk).update(
                        organization_id=self.b.pk),
                    "QuerySet.update(выражение)": lambda: model.objects.filter(pk=doc.pk).update(
                        organization_id=Value(self.b.pk)),
                    "связанный менеджер организации": lambda: getattr(self.a, f"{model._meta.model_name}_set")
                    .filter(pk=doc.pk).update(organization=self.b),
                    "bulk_update([organization])": bulk("organization"),
                    "bulk_update([organization_id])": bulk("organization_id"),
                    "bulk_create(update_conflicts)": upsert(),
                }
                for name, attempt in attempts.items():
                    with self.subTest(kind=kind, attempt=name):
                        with self.assertRaises(OrganizationChangeError):
                            attempt()
                        self.assertEqual(self.full_state(), before)
                        self.assertEqual(self.owners(doc), owners)

    def test_unsaved_change_in_memory_with_comment_only_save(self):
        """Смена организации в памяти, сохраняется только комментарий: база не
        меняется, а проведение такого объекта отклоняется — иначе движения
        записались бы с организацией из памяти."""
        for kind in KINDS:
            with self.subTest(kind=kind):
                doc = self.posted(kind)
                owners, before = self.owners(doc), self.full_state()
                doc.organization = self.b
                doc.comment = "проверено"
                doc.save(update_fields=["comment"])
                stored = type(doc).objects.get(pk=doc.pk)
                self.assertEqual((stored.organization_id, stored.comment), (self.a.pk, "проверено"))
                with self.assertRaisesMessage(OrganizationChangeError, "изменена, но не сохранена"):
                    doc.post()
                type(doc).objects.filter(pk=doc.pk).update(comment="")
                self.assertEqual(self.full_state(), before)
                self.assertEqual(self.owners(doc), owners)

    def test_unsaved_change_is_not_posted_from_draft_either(self):
        draft = self.receipt(self.a, 1, 100, post=False)
        draft.organization = self.b
        with self.assertRaisesMessage(OrganizationChangeError, "изменена, но не сохранена"):
            draft.post()
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(Receipt.objects.get(pk=draft.pk).status, DOC_DRAFT)

    def test_stale_object_cannot_write_back_old_organization(self):
        """Объект загружен до смены организации другим пользователем: его
        полное сохранение вернуло бы документу А при движениях Б."""
        receipt = self.receipt(self.a, 1, 100)
        stale = Receipt.objects.get(pk=receipt.pk)
        Receipt.objects.get(pk=receipt.pk).change_organization(self.b)
        after_change = self.full_state()
        stale.comment = "правка из старой вкладки"
        with self.assertRaisesMessage(OrganizationChangeError, "загрузите его заново"):
            stale.save()
        self.assertEqual(self.full_state(), after_change)
        self.assertEqual(self.owners(receipt)["движения"], ["Б"])

    def test_stale_draft_object_after_posting_by_other_user(self):
        draft = self.receipt(self.a, 1, 100, post=False)
        stale = Receipt.objects.get(pk=draft.pk)            # открыт черновиком
        Receipt.objects.get(pk=draft.pk).post()             # другой пользователь провёл
        before = self.full_state()
        stale.organization = self.b
        with self.assertRaises(OrganizationChangeError):
            stale.save()
        self.assertEqual(self.full_state(), before)

    def test_draft_with_movements_after_status_rewrite_is_still_protected(self):
        """Статус переписан мимо отмены проведения (движения остались): смена
        организации такого «черновика» тоже отклоняется — защита смотрит и на
        движения, а не только на статус."""
        receipt = self.receipt(self.a, 1, 100)
        Receipt.objects.filter(pk=receipt.pk).update(status=DOC_DRAFT)
        before = self.full_state()
        doc = Receipt.objects.get(pk=receipt.pk)
        doc.organization = self.b
        with self.assertRaises(OrganizationChangeError):
            doc.save()
        with self.assertRaises(OrganizationChangeError):
            Receipt.objects.filter(pk=receipt.pk).update(organization=self.b)
        with self.assertRaises(StockError):                  # механизм тоже: перепроводить нечего
            Receipt.objects.get(pk=receipt.pk).change_organization(self.b)
        self.assertEqual(self.full_state(), before)

    def test_rejection_inside_outer_transaction_keeps_it_usable(self):
        receipt = self.receipt(self.a, 1, 100)
        before = self.full_state()
        with transaction.atomic():
            doc = Receipt.objects.get(pk=receipt.pk)
            doc.organization = self.b
            with self.assertRaises(OrganizationChangeError):
                doc.save()
            with self.assertRaises(OrganizationChangeError):
                Receipt.objects.filter(pk=receipt.pk).update(organization=self.b)
            Receipt.objects.filter(pk=receipt.pk).update(comment="после отказа")   # транзакция пригодна
        Receipt.objects.filter(pk=receipt.pk).update(comment="")
        self.assertEqual(self.full_state(), before)


# ---------------------------------------------------------------------------
# Допустимые изменения: черновики и механизм перепроведения
# ---------------------------------------------------------------------------

class AllowedChangeTests(ChangeWorld):
    def test_draft_organization_changes_as_usual(self):
        draft = self.receipt(self.a, 1, 100, post=False)
        draft.organization = self.b
        draft.save()
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.b.pk)
        Receipt.objects.filter(pk=draft.pk).update(organization=self.a)
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.a.pk)
        draft = Receipt.objects.get(pk=draft.pk)
        draft.organization = self.b
        Receipt.objects.bulk_update([draft], ["organization"])
        draft = Receipt.objects.get(pk=draft.pk)
        self.assertEqual(draft.organization_id, self.b.pk)
        draft.post()
        self.assertEqual(self.owners(draft), {"документ": "Б", "движения": ["Б"], "номера": [], "остаток": ["Б"],
                                              "взаиморасчёты": ["Б"]})

    def test_mass_update_touches_drafts_and_unchanged_posted_documents(self):
        posted = self.receipt(self.a, 1, 100)
        draft = self.receipt(self.a, 1, 100, post=False)
        self.assertEqual(Receipt.objects.filter(pk__in=[posted.pk, draft.pk]).update(organization=self.a), 2)
        posted, draft = Receipt.objects.get(pk=posted.pk), Receipt.objects.get(pk=draft.pk)
        draft.organization = self.b
        self.assertEqual(Receipt.objects.bulk_update([posted, draft], ["organization", "comment"]), 2)
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.b.pk)
        self.assertEqual(self.owners(posted)["движения"], ["А"])
        posted.organization = self.b                          # один недопустимый — отказ всего пакета
        draft.organization = self.a
        with self.assertRaises(OrganizationChangeError):
            Receipt.objects.bulk_update([posted, draft], ["organization"])
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.b.pk)

    def compatible(self, kind):
        """Проведённый документ А, который можно перевести на Б. Приход —
        серийный (номер переходит к Б); расход и перемещение — обычный товар,
        у Б на складе своя единица."""
        if kind in ("receipt", "adjustment_income"):
            return self.posted(kind)
        self.product = Product.objects.create(name=f"Простой {kind}", unit=self.unit, purchase_price=70)
        place = {"warehouse": self.w1} if kind == "transfer" else {}
        for org in (self.a, self.b):
            self.doc(Receipt, org, [(1, "", 100)], **place).post()
        model, header = {
            "shipment": (Shipment, {}), "customer_return": (CustomerReturn, {}),
            "supplier_return": (SupplierReturn, {}),
            "transfer": (Transfer, {"warehouse_from": self.w1, "warehouse_to": self.w2}),
            "adjustment_expense": (StockAdjustment, {"kind": StockAdjustment.KIND_EXPENSE}),
        }[kind]
        doc = self.doc(model, self.a, [(1, "", 100)], **header)
        doc.post()
        return model.objects.get(pk=doc.pk)

    def test_change_organization_moves_document_and_stock_together(self):
        """Совместимая смена — для всех семи видов: документ, складские
        движения и движения номеров переходят к Б вместе, документ остаётся
        проведённым, расхождений нет."""
        for kind in KINDS:
            with self.subTest(kind=kind):
                doc = self.compatible(kind)
                serial = ["Б"] if kind in ("receipt", "adjustment_income") else []
                self.assertEqual(self.owners(doc)["движения"], ["А"])
                doc.change_organization(self.b)
                owners = self.owners(doc)
                self.assertEqual((owners["документ"], owners["движения"], owners["номера"]), ("Б", ["Б"], serial))
                self.assertEqual(type(doc).objects.get(pk=doc.pk).status, DOC_POSTED)
                self.assertEqual(transition.organization_mismatches(), [])

    def test_change_through_form_reposts_with_both_histories(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        receipt = self.receipt(self.a, 2, 100)
        line = receipt.lines.get()
        data = {
            "date": receipt.date.isoformat(), "time": receipt.time.strftime("%H:%M:%S"), "number": receipt.number,
            "organization": self.b.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
            "supplier_invoice": "", "comment": "", "action": "save",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-product": line.product_id,
            "lines-0-quantity": "2", "lines-0-price": "100", "lines-0-discount": "0", "lines-0-vat_rate": "none",
            "lines-0-serial_numbers": "",
        }
        response = self.client.post(reverse("receipt_edit", args=[receipt.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.owners(receipt), {"документ": "Б", "движения": ["Б"], "номера": [], "остаток": ["Б"],
                                                "взаиморасчёты": ["Б"]})
        self.assertEqual(self.balances(), {("Б", "W"): (2, 200)})

    def test_failed_change_restores_object_and_keeps_everything(self):
        self.receipt(self.a, 1, 100)
        shipment = self.ship(self.a, 1)
        before = self.full_state()
        doc = Shipment.objects.get(pk=shipment.pk)
        with self.assertRaisesMessage(StockError, "Недостаточно товара"):     # у Б товара нет
            doc.change_organization(self.b)
        self.assertEqual((doc.organization_id, doc.status), (self.a.pk, DOC_POSTED))
        self.assertEqual(self.full_state(), before)

    def test_editing_without_changes_reposts_once_and_keeps_everything(self):
        self.negative(self.a, True)
        self.receipt(self.a, 3, 100)
        shipment = self.ship(self.a, 5)
        self.receipt(self.a, 2, 120)
        before = self.full_state()
        doc = Shipment.objects.get(pk=shipment.pk)
        with mock.patch.object(Shipment, "post", autospec=True, side_effect=Shipment.post) as post:
            with doc.editing() as posted:
                self.assertTrue(posted)
                doc.save()
        self.assertEqual(post.call_count, 1)                 # перепроведение — один раз, при выходе
        self.assertEqual(self.full_state(), before)


# ---------------------------------------------------------------------------
# Связанные основания и возвраты не переносятся вслед за документом
# ---------------------------------------------------------------------------

class LinkedDocumentsTests(ChangeWorld):
    def test_basis_with_posted_returns_keeps_its_organization(self):
        receipt = self.receipt(self.a, 2, 100)
        supplier_back = self.supplier_return(self.a, 1, receipt)
        shipment = self.ship(self.a, 1)
        customer_back = self.customer_return(self.a, 1, basis=shipment)
        before = self.full_state()
        for doc in (receipt, shipment):
            with self.subTest(basis=type(doc).__name__):
                with self.assertRaisesMessage(StockError, "должны совпадать с возвратом"):
                    type(doc).objects.get(pk=doc.pk).change_organization(self.b)
                self.assertEqual(self.full_state(), before)
        for doc in (supplier_back, customer_back):
            with self.subTest(ret=type(doc).__name__):
                with self.assertRaisesMessage(StockError, "не совпадает с организацией документа-основания"):
                    type(doc).objects.get(pk=doc.pk).change_organization(self.b)
                self.assertEqual(self.full_state(), before)

    def test_return_is_not_carried_over_after_basis_changes(self):
        receipt = self.receipt(self.a, 2, 100)
        back = self.supplier_return(self.a, 1, receipt)
        SupplierReturn.objects.get(pk=back.pk).unpost()
        Receipt.objects.get(pk=receipt.pk).change_organization(self.b)
        back = SupplierReturn.objects.get(pk=back.pk)
        self.assertEqual((back.organization_id, back.status, back.receipt_id), (self.a.pk, DOC_DRAFT, receipt.pk))
        with self.assertRaisesMessage(StockError, "не совпадает с организацией документа-основания"):
            back.post()
        back.change_organization(self.b)                      # черновик — обычная смена
        SupplierReturn.objects.get(pk=back.pk).post()
        self.assertEqual(self.balances(), {("Б", "W"): (1, 100)})


# ---------------------------------------------------------------------------
# Административная панель — тот же механизм (только черновики)
# ---------------------------------------------------------------------------

class AdminTests(ChangeWorld):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="pass12345"))

    def admin_data(self, receipt, organization):
        line = receipt.lines.get()
        return {
            "number": receipt.number, "date": receipt.date.isoformat(), "time": receipt.time.strftime("%H:%M:%S"),
            "organization": organization.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
            "supplier_invoice": "", "comment": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-receipt": receipt.pk,
            "lines-0-product": line.product_id, "lines-0-quantity": "1", "lines-0-serial_numbers": "",
            "lines-0-price": "100", "lines-0-discount": "0", "lines-0-vat_rate": "none",
        }

    def test_admin_changes_draft_organization(self):
        draft = self.receipt(self.a, 1, 100, post=False)
        url = reverse("admin:purchases_receipt_change", args=[draft.pk])
        response = self.client.post(url, self.admin_data(draft, self.b))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.b.pk)

    def test_admin_does_not_change_posted_document(self):
        receipt = self.receipt(self.a, 1, 100)
        url = reverse("admin:purchases_receipt_change", args=[receipt.pk])
        before = self.full_state()
        self.assertEqual(self.client.post(url, self.admin_data(receipt, self.b)).status_code, 403)
        # документ провели, пока страница была открыта черновиком: проверка прав
        # пройдена — сохранение всё равно отклоняется механизмом, откат целиком;
        # и смена организации, и правка без неё (строки проведённого документа
        # не меняются мимо перепроведения)
        for organization, comment in ((self.b, ""), (self.a, "правка проведённого")):
            with self.subTest(organization=organization.name):
                data = {**self.admin_data(receipt, organization), "comment": comment}
                with mock.patch.object(ReceiptAdmin, "has_change_permission", return_value=True):
                    response = self.client.post(url, data, follow=True)
                self.assertContains(response, "Изменения не сохранены")
                self.assertEqual(self.full_state(), before)
                self.assertEqual(self.owners(receipt)["документ"], "А")


# ---------------------------------------------------------------------------
# Старые рассогласованные данные: диагностика и явное исправление
# ---------------------------------------------------------------------------

class OldMismatchTests(ChangeWorld):
    def split(self, doc, organization):
        """Расхождение из старых данных (прямая правка базы, мимо программы)."""
        type(doc)._base_manager.filter(pk=doc.pk).update(organization=organization)

    def command(self, *args):
        out = StringIO()
        call_command("stock_transition", *args, stdout=out)
        return out.getvalue()

    def test_reports_do_not_rewrite_movements_and_diagnostics_show_both_fixes(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        receipt = self.receipt(self.a, 1, 100)
        self.split(receipt, self.b)
        before = self.full_state()
        for name in ("balance_list", "report_sales", "settlements"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.full_state(), before)          # чтение отчёта ничего не переписывает
        self.assertEqual(transition.organization_mismatches(), [("receipt", receipt.pk, self.b.pk, [self.a.pk])])
        output = self.command("--limit", "0")
        self.assertIn("организация документа и его движений расходится (требует решения): 1", output)
        self.assertIn(f"--fix-organization receipt:{receipt.pk}=document", output)
        self.assertIn(f"receipt:{receipt.pk}=movements", output)
        costs = StringIO()
        call_command("check_costs", "--limit", "0", stdout=costs)
        self.assertIn(f"Организация документа и его движений расходится: 1 (документы: receipt:{receipt.pk})",
                      costs.getvalue())
        self.assertEqual(self.full_state(), before)          # диагностика только читает

    def test_fix_by_document_moves_movements_to_document_organization(self):
        receipt = self.receipt(self.a, 1, 100)
        self.split(receipt, self.b)
        with self.assertRaises(CommandError):                 # запись — только с резервной копией
            self.command("--fix-organization", f"receipt:{receipt.pk}=document")
        self.command("--fix-organization", f"receipt:{receipt.pk}=document", "--i-have-a-backup")
        self.assertEqual(self.owners(receipt), {"документ": "Б", "движения": ["Б"], "номера": [], "остаток": ["Б"],
                                                "взаиморасчёты": ["Б"]})
        self.assertEqual(transition.organization_mismatches(), [])

    def test_fix_by_movements_returns_their_organization_to_document(self):
        receipt = self.receipt(self.a, 1, 100)
        self.ship(self.a, 1)                                  # А продала эту единицу
        self.split(receipt, self.b)
        before = self.full_state()
        with self.assertRaisesMessage(CommandError, "Недостаточно товара"):   # у А продажа без поступления
            self.command("--fix-organization", f"receipt:{receipt.pk}=document", "--i-have-a-backup")
        self.assertEqual(self.full_state(), before)
        self.command("--fix-organization", f"receipt:{receipt.pk}=movements", "--i-have-a-backup")
        self.assertEqual(self.owners(receipt)["документ"], "А")
        self.assertEqual(self.owners(receipt)["движения"], ["А"])
        self.assertEqual(transition.organization_mismatches(), [])

    def test_serial_movements_are_checked_too(self):
        doc = self.posted("receipt")
        SerialMovement.objects.filter(doc_type="receipt", doc_id=doc.pk).update(organization=self.b)
        self.assertEqual(transition.organization_mismatches(), [("receipt", doc.pk, self.a.pk, [self.a.pk, self.b.pk])])
        with self.assertRaises(transition.TransitionError):   # движения у двух организаций
            transition.fix_organization_mismatch("receipt", doc.pk, transition.FIX_MOVEMENTS)
        transition.fix_organization_mismatch("receipt", doc.pk, transition.FIX_DOCUMENT)
        self.assertEqual(self.owners(doc)["номера"], ["А"])
        self.assertEqual(transition.organization_mismatches(), [])

    def test_regular_repost_also_fixes_by_document_and_bypass_cannot_recreate_it(self):
        receipt = self.receipt(self.a, 1, 100)
        self.split(receipt, self.b)
        Receipt.objects.get(pk=receipt.pk).post()             # перепроведение — с организацией документа
        self.assertEqual(self.owners(receipt)["движения"], ["Б"])
        doc = Receipt.objects.get(pk=receipt.pk)
        doc.organization = self.a
        with self.assertRaises(OrganizationChangeError):
            doc.save()
        self.assertEqual(transition.organization_mismatches(), [])


class FExpressionTests(ChangeWorld):
    def test_expression_on_posted_documents_is_rejected_on_drafts_allowed(self):
        posted = self.receipt(self.a, 1, 100)
        draft = self.receipt(self.a, 1, 100, post=False)
        with self.assertRaises(OrganizationChangeError):
            Receipt.objects.filter(pk__in=[posted.pk, draft.pk]).update(organization_id=F("organization_id"))
        self.assertEqual(Receipt.objects.filter(pk=draft.pk).update(organization_id=Value(self.b.pk)), 1)
        self.assertEqual(Receipt.objects.get(pk=draft.pk).organization_id, self.b.pk)
