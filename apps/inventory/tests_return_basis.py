"""Основание проведённого возврата (покупателя и поставщику).

Дефект (21.10.2026, воспроизведён независимо на временной базе): приёмка А —
2 шт. по 100, приёмка Б — 1 шт. по 100; по А проведён возврат поставщику на
2 шт.; return.receipt = Б; return.save(update_fields=["receipt"]) проходит —
документ остаётся проведённым, по Б возвращено 2 при принятом 1. check_returns
превышение показывает, но сохранение его не предотвращало.

Правило (posting, правило 12): основание проведённого возврата (и возврата с
движениями) меняют, ставят и снимают только в editing() — с проверкой нового
основания и перепроведением; прежнее основание освобождает возвращённое.
Обычное сохранение и массовые операции отклоняются до записи.
"""
import datetime
from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.models import F
from django.test import TransactionTestCase
from django.urls import reverse

from apps.catalog.models import Product
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.constants import DOC_DRAFT
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment

from . import return_limits, services
from .services import PostedChangeError, StockError
from .tests_org_document_change import ChangeWorld
from .tests_org_stock import OrgWorld

D = Decimal


class BasisReproductionTests(ChangeWorld):
    maxDiff = None

    def test_basis_of_posted_supplier_return_saved_without_check(self):
        receipt_a = self.receipt(self.a, 2, 100)
        receipt_b = self.receipt(self.a, 1, 100)
        back = self.supplier_return(self.a, 2, receipt_a)
        before = self.full_state()
        doc = SupplierReturn.objects.get(pk=back.pk)
        doc.receipt = receipt_b
        rejected = None
        try:
            doc.save(update_fields=["receipt"])
        except StockError as exc:
            rejected = exc
        names = {receipt_a.pk: "А", receipt_b.pk: "Б"}
        picture = {
            "основание": names[SupplierReturn.objects.get(pk=back.pk).receipt_id],
            **{f"возвращено по {name}": sum(return_limits.returned_quantities(Receipt.objects.get(pk=pk)).values(),
                                           D("0")) for pk, name in names.items()},
            "нарушений check_returns": len(return_limits.violations()),
        }
        # было: {"основание": "Б", "возвращено по А": 0, "возвращено по Б": 2, "нарушений check_returns": 1}
        self.assertEqual(picture, {"основание": "А", "возвращено по А": D("2"), "возвращено по Б": D("0"),
                                   "нарушений check_returns": 0})
        self.assertIsNotNone(rejected, "смена основания проведённого возврата без проверки должна быть отклонена")
        self.assertEqual(self.full_state(), before)


class BasisWorld(ChangeWorld):
    def snapshot(self):
        """Полное состояние учёта, основания возвратов, связи и резервная стоимость строк возвратов."""
        bases = (sorted(SupplierReturn._base_manager.values_list("pk", "receipt_id", "status")),
                 sorted(CustomerReturn._base_manager.values_list("pk", "shipment_id", "status")))
        links = sorted(CustomerReturnLine.objects.values_list("pk", "shipment_line_id", "fallback_cost",
                                                              "fallback_source"))
        return self.full_state(), bases, links

    def supplier_back(self, qty, basis, serial="", post=True, **header):
        doc = SupplierReturn.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier,
                                            receipt=basis, **{**self._when(), **header})
        doc.lines.create(product=self.product, quantity=qty, price=100, vat_rate="none", serial_numbers=serial)
        if post:
            doc.post()
        return SupplierReturn.objects.get(pk=doc.pk)

    def sale(self, *quantities, customer=None):
        """Отгрузка из нескольких строк одного товара."""
        doc = Shipment.objects.create(organization=self.a, warehouse=self.w, customer=customer or self.customer,
                                      **self._when())
        for quantity in quantities:
            doc.lines.create(product=self.product, quantity=quantity, price=300, vat_rate="none")
        doc.post()
        return Shipment.objects.get(pk=doc.pk)

    def returned(self, basis):
        return sum(return_limits.returned_quantities(type(basis).objects.get(pk=basis.pk)).values(), D("0"))

    def edit(self, doc, **changes):
        editable = type(doc).objects.get(pk=doc.pk)
        with editable.editing():
            for name, value in changes.items():
                setattr(editable, name, value)
            editable.save()

    def assertRejectedWholly(self, doc, message, **changes):
        before = self.snapshot()
        with self.assertRaisesMessage(StockError, message):
            self.edit(doc, **changes)
        self.assertEqual(self.snapshot(), before)


# ---------------------------------------------------------------------------
# Обычная запись основания проведённого возврата — все пути отклоняются
# ---------------------------------------------------------------------------

class BasisBypassTests(BasisWorld):
    def cases(self):
        """(возврат, другое основание): возврат поставщику и покупателя, с основанием и без."""
        receipt_a, receipt_b = self.receipt(self.a, 3, 100), self.receipt(self.a, 3, 100)
        sale_1, sale_2 = self.sale(2), self.sale(1)             # по sale_1 — два возврата по 1
        return {
            "поставщику: смена": (self.supplier_back(1, receipt_a), receipt_b),
            "поставщику: снятие": (self.supplier_back(1, receipt_a), None),
            "поставщику: установка": (self.supplier_back(1, None), receipt_b),
            "покупателя: смена": (self.customer_return(self.a, 1, basis=sale_1), sale_2),
            "покупателя: снятие": (self.customer_return(self.a, 1, basis=sale_1), None),
            "покупателя: установка": (self.customer_return(self.a, 1), sale_2),
        }

    def attempts(self, doc, value):
        model = type(doc)
        name = model.BASIS_FIELDS[0]
        attname = model._meta.get_field(name).attname
        raw = value.pk if value is not None else None

        def plain(fields=None, attr=name):
            obj = model.objects.get(pk=doc.pk)
            setattr(obj, attr, value if attr == name else raw)
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

        return {
            "save()": plain(), f"save(update_fields=[{name}])": plain([name]),
            f"save(update_fields=[{attname}])": plain([attname]), f"{attname} = …; save()": plain(attr=attname),
            f"QuerySet.update({name}=…)": lambda: model.objects.filter(pk=doc.pk).update(**{name: value}),
            f"QuerySet.update({attname}=…)": lambda: model.objects.filter(pk=doc.pk).update(**{attname: raw}),
            "QuerySet.update(выражение)": lambda: model.objects.filter(pk=doc.pk).update(**{name: F(name)}),
            f"bulk_update([{name}])": bulk(name), f"bulk_update([{attname}])": bulk(attname),
            "bulk_create(update_conflicts)": upsert(),
        }

    def test_every_return_and_every_path_is_rejected(self):
        for title, (doc, value) in self.cases().items():
            for attempt_title, attempt in self.attempts(doc, value).items():
                before = self.snapshot()
                with self.subTest(case=title, attempt=attempt_title):
                    with self.assertRaises(PostedChangeError):
                        attempt()
                    self.assertEqual(self.snapshot(), before)

    def test_related_manager_of_basis_and_mass_change_of_drafts(self):
        receipt_a, receipt_b = self.receipt(self.a, 3, 100), self.receipt(self.a, 3, 100)
        posted = self.supplier_back(1, receipt_a)
        draft = self.supplier_back(1, receipt_a, post=False)
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "Массовая смена реквизита «приёмка-основание» не поддерживается"):
            receipt_a.returns.update(receipt=receipt_b)
        with self.assertRaisesMessage(PostedChangeError, "Массовая смена реквизита «приёмка-основание»"):
            SupplierReturn.objects.filter(pk=draft.pk).update(receipt=receipt_b)      # и у черновика
        self.assertEqual(self.snapshot(), before)
        draft.receipt = receipt_b
        draft.save()                                            # по одному — черновик меняется как обычно
        self.assertEqual(SupplierReturn.objects.get(pk=draft.pk).receipt_id, receipt_b.pk)
        self.assertEqual(SupplierReturn.objects.get(pk=posted.pk).receipt_id, receipt_a.pk)

    def test_stale_object_and_document_with_movements(self):
        receipt_a, receipt_b = self.receipt(self.a, 3, 100), self.receipt(self.a, 3, 100)
        draft = self.supplier_back(1, receipt_a, post=False)
        stale = SupplierReturn.objects.get(pk=draft.pk)         # открыт черновиком
        SupplierReturn.objects.get(pk=draft.pk).post()          # другой пользователь провёл
        before = self.snapshot()
        stale.receipt = receipt_b
        with self.assertRaisesMessage(PostedChangeError, "загрузите его заново"):
            stale.save()
        SupplierReturn.objects.filter(pk=draft.pk).update(status=DOC_DRAFT)   # статус переписан мимо отмены
        with_moves = SupplierReturn.objects.get(pk=draft.pk)
        with_moves.receipt = receipt_b
        with self.assertRaises(PostedChangeError):
            with_moves.save(update_fields=["receipt"])
        SupplierReturn.objects.filter(pk=draft.pk).update(status="posted")
        self.assertEqual(self.snapshot(), before)


# ---------------------------------------------------------------------------
# Несохранённое и устаревшее основание
# ---------------------------------------------------------------------------

class BasisInMemoryTests(BasisWorld):
    def test_comment_only_save_keeps_basis_and_line_links(self):
        self.receipt(self.a, 2, 100)
        sale_1, sale_2 = self.sale(1), self.sale(1)
        back = self.customer_return(self.a, 1, basis=sale_1)
        self.assertEqual(back.lines.get().shipment_line_id, sale_1.lines.get().pk)
        before = self.snapshot()
        doc = CustomerReturn.objects.get(pk=back.pk)
        doc.shipment = sale_2
        doc.comment = "проверено"
        doc.save(update_fields=["comment"])
        stored = CustomerReturn.objects.get(pk=back.pk)
        self.assertEqual((stored.shipment_id, stored.comment, stored.lines.get().shipment_line_id),
                         (sale_1.pk, "проверено", sale_1.lines.get().pk))
        with self.assertRaisesMessage(PostedChangeError, "отгрузка-основание изменена, но не сохранена"):
            doc.post()
        CustomerReturn.objects.filter(pk=back.pk).update(comment="")
        self.assertEqual(self.snapshot(), before)

    def test_stale_object_after_basis_change_by_other_user(self):
        receipt_a, receipt_b = self.receipt(self.a, 3, 100), self.receipt(self.a, 3, 100)
        back = self.supplier_back(1, receipt_a)
        stale = SupplierReturn.objects.get(pk=back.pk)
        self.edit(back, receipt=receipt_b)                      # другой пользователь — через editing()
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "приёмка-основание изменена, но не сохранена"):
            stale.post()
        stale.comment = "старая вкладка"
        with self.assertRaisesMessage(PostedChangeError, "загрузите его заново"):
            stale.save()
        self.assertEqual(self.snapshot(), before)


# ---------------------------------------------------------------------------
# Безопасная смена через editing(): проверка нового основания, освобождение прежнего
# ---------------------------------------------------------------------------

class BasisEditingTests(BasisWorld):
    def test_reproduction_through_editing_is_rejected_wholly(self):
        receipt_a, receipt_b = self.receipt(self.a, 2, 100), self.receipt(self.a, 1, 100)
        back = self.supplier_back(2, receipt_a)
        self.assertRejectedWholly(back, "доступно к возврату — 1, в этом возврате — 2", receipt=receipt_b)
        self.assertEqual((self.returned(receipt_a), self.returned(receipt_b)), (D("2"), D("0")))
        self.assertEqual(return_limits.violations(), [])

    def test_supplier_return_moves_and_releases_old_basis(self):
        receipt_a, receipt_b = self.receipt(self.a, 2, 100), self.receipt(self.a, 1, 100)
        back = self.supplier_back(1, receipt_a)
        self.edit(back, receipt=receipt_b)
        self.assertEqual((self.returned(receipt_a), self.returned(receipt_b)), (D("0"), D("1")))
        self.supplier_back(2, receipt_a)                        # прежнее основание освободилось целиком
        with self.assertRaisesMessage(StockError, "доступно к возврату — 0"):
            self.supplier_back(1, receipt_b)                    # новое — занято
        self.assertEqual(return_limits.violations(), [])

    def test_supplier_return_checks_party_chronology_and_serials(self):
        receipt_a = self.receipt(self.a, 2, 100)
        other_org = self.receipt(self.b, 1, 100)
        back = self.supplier_back(1, receipt_a)
        later = self.receipt(self.a, 1, 100)                    # приёмка позже возврата
        self.assertRejectedWholly(back, "не совпадает с организацией документа-основания", receipt=other_org)
        self.assertRejectedWholly(back, "стоит в хронологии не раньше возврата", receipt=later)
        self.product = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        numbered = self.receipt(self.a, 2, 100, serial="N-1\nN-2")
        without_n1 = self.receipt(self.a, 1, 100, serial="M-1")
        back = self.supplier_back(1, numbered, serial="N-1")
        self.assertRejectedWholly(back, "номера N-1 нет в приёмке", receipt=without_n1)

    def test_customer_return_moves_link_to_single_line_of_new_basis(self):
        self.receipt(self.a, 3, 100)
        sale_1, sale_2 = self.sale(1), self.sale(1)
        back = self.customer_return(self.a, 1, basis=sale_1)
        self.edit(back, shipment=sale_2)
        line = CustomerReturn.objects.get(pk=back.pk).lines.get()
        self.assertEqual(line.shipment_line_id, sale_2.lines.get().pk)      # ссылки на прежнюю отгрузку нет
        self.assertEqual((self.returned(sale_1), self.returned(sale_2)), (D("0"), D("1")))
        self.assertEqual(self.amount(back), D("100.00"))
        self.assertEqual(return_limits.violations(), [])

    def test_ambiguous_line_is_not_picked_automatically(self):
        self.receipt(self.a, 4, 100)
        sale_1, two_lines = self.sale(1), self.sale(1, 1)
        back = self.customer_return(self.a, 1, basis=sale_1)
        self.edit(back, shipment=two_lines)
        self.assertIsNone(CustomerReturn.objects.get(pk=back.pk).lines.get().shipment_line_id)   # «в целом»
        self.assertEqual(self.returned(two_lines), D("1"))

    def test_ambiguous_line_with_deficit_requires_choice_and_rolls_back(self):
        self.negative(self.a, True)
        self.receipt(self.a, 2, 100)
        sale_1 = self.sale(1)
        deficit = self.sale(1, 1)                                # вторая строка — в дефиците
        back = self.customer_return(self.a, 1, basis=sale_1)
        self.assertRejectedWholly(back, "Укажите в возврате, из какой строки отгрузки возвращается товар",
                                  shipment=deficit)

    def test_removing_and_setting_basis_follow_existing_rules(self):
        self.receipt(self.a, 3, 100)
        sale_1 = self.sale(1)
        stranger = Counterparty.objects.create(name="Другой покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        foreign = self.sale(1, customer=stranger)
        back = self.customer_return(self.a, 1, basis=sale_1)
        self.edit(back, shipment=None)                           # снятие: оценка возврата без основания
        doc = CustomerReturn.objects.get(pk=back.pk)
        self.assertEqual((doc.shipment_id, doc.lines.get().shipment_line_id), (None, None))
        self.assertEqual(self.returned(sale_1), D("0"))
        self.assertEqual(self.amount(back), D("100.00"))         # средняя перед возвратом
        self.assertRejectedWholly(back, "покупатель возврата", shipment=foreign)   # чужой покупатель
        self.edit(back, shipment=sale_1)                         # установка: снова по отгрузке
        self.assertEqual(CustomerReturn.objects.get(pk=back.pk).lines.get().shipment_line_id, sale_1.lines.get().pk)
        self.assertEqual(self.returned(sale_1), D("1"))

    def test_stale_object_does_not_get_leniency_of_basis_it_was_loaded_with(self):
        """Послабление «не больше прежнего количества» — только для основания,
        с которым возврат проведён сейчас (по базе на входе в editing()), а не с
        которым объект когда-то загрузили."""
        receipt_a, receipt_b = self.receipt(self.a, 2, 100), self.receipt(self.a, 2, 100)
        back = self.supplier_back(2, receipt_a)
        stale = SupplierReturn.objects.get(pk=back.pk)            # загружен с основанием А
        self.edit(back, receipt=receipt_b)                         # другой пользователь перенёс на Б
        self.supplier_back(2, receipt_a)                           # А снова занята целиком
        before = self.snapshot()
        with self.assertRaisesMessage(StockError, "доступно к возврату — 0, в этом возврате — 2"):
            with stale.editing():
                stale.receipt = receipt_a                          # вернуть на А «по старой памяти»
                stale.save()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(return_limits.violations(), [])

    def test_draft_basis_change_by_column_name_clears_line_links(self):
        self.receipt(self.a, 3, 100)
        sale_1, sale_2 = self.sale(1), self.sale(1)
        draft = self.customer_return(self.a, 1, basis=sale_1, post=False)
        CustomerReturnLine.objects.filter(document=draft).update(shipment_line=sale_1.lines.get())
        draft = CustomerReturn.objects.get(pk=draft.pk)
        draft.shipment_id = sale_2.pk
        draft.save(update_fields=["shipment_id"])                  # «shipment_id» — то же поле, что «shipment»
        self.assertIsNone(draft.lines.get().shipment_line_id)

    def test_form_keeps_basis_and_links(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        self.receipt(self.a, 3, 100)
        sale_1 = self.sale(2)
        back = self.customer_return(self.a, 1, basis=sale_1)
        line = back.lines.get()
        data = {
            "date": back.date.isoformat(), "time": back.time.strftime("%H:%M:%S"), "number": back.number,
            "organization": self.a.pk, "warehouse": self.w.pk, "customer": self.customer.pk, "comment": "",
            "action": "save", "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000", "lines-0-id": line.pk, "lines-0-product": line.product_id,
            "lines-0-quantity": "2", "lines-0-price": "300", "lines-0-discount": "0", "lines-0-vat_rate": "none",
            "lines-0-serial_numbers": "", "lines-0-shipment_line": line.shipment_line_id,
        }
        response = self.client.post(reverse("customer_return_edit", args=[back.pk]), data)
        self.assertEqual(response.status_code, 302)
        doc = CustomerReturn.objects.get(pk=back.pk)
        self.assertEqual((doc.shipment_id, doc.lines.get().shipment_line_id, doc.lines.get().quantity),
                         (sale_1.pk, sale_1.lines.get().pk, D("2")))
        self.assertEqual(self.returned(sale_1), D("2"))


# ---------------------------------------------------------------------------
# Диагностика: нарушения основания видны, данные не меняются
# ---------------------------------------------------------------------------

class BasisDiagnosticsTests(BasisWorld):
    def check_returns(self):
        out = StringIO()
        call_command("check_returns", stdout=out)
        return out.getvalue()

    def test_old_violations_are_listed_and_nothing_is_rewritten(self):
        receipt_a, receipt_b = self.receipt(self.a, 2, 100), self.receipt(self.a, 1, 100)
        self.receipt(self.a, 5, 100)                             # товар для отгрузок
        excess = self.supplier_back(2, receipt_a)
        sale_1, sale_2, third = self.sale(1), self.sale(1), self.sale(1)
        moved = self.customer_return(self.a, 1, basis=sale_1)
        dropped = self.customer_return(self.a, 1, basis=sale_2)
        # старые данные: основание переписано прямой записью мимо программы, связи строк остались
        SupplierReturn._base_manager.filter(pk=excess.pk).update(receipt=receipt_b)
        CustomerReturn._base_manager.filter(pk=moved.pk).update(shipment=third)
        CustomerReturn._base_manager.filter(pk=dropped.pk).update(shipment=None)
        before = self.snapshot()
        output = self.check_returns()
        self.assertIn("Возвращено больше, чем было в основании: 1", output)
        self.assertIn("Строка возврата ссылается на строку не своей отгрузки-основания: 2", output)
        self.assertIn(f"(id {moved.pk}) (отгрузка № {third.number}", output)
        self.assertIn(f"(id {dropped.pk}) (основания нет)", output)
        self.assertEqual(self.snapshot(), before)                 # только чтение
        with self.assertRaisesMessage(StockError, "без основания, а строка «Товар» ссылается на строку отгрузки"):
            CustomerReturn.objects.get(pk=dropped.pk).post()       # перепровести как есть — нельзя
        for doc in (moved, dropped):                               # решение человека: убрать связь строки
            with CustomerReturn.objects.get(pk=doc.pk).editing():
                CustomerReturnLine.objects.filter(document=doc).update(shipment_line=None)
        output = self.check_returns()
        self.assertNotIn("Строка возврата ссылается", output)
        self.assertEqual(CustomerReturn.objects.get(pk=moved.pk).lines.get().shipment_line_id,
                         third.lines.get().pk)                      # при проведении — строка нового основания


# ---------------------------------------------------------------------------
# Одновременно: смена основания против другого возврата по новому основанию (PostgreSQL)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == "postgresql", "блокировки строк проверяются только на PostgreSQL")
class ConcurrentBasisTests(OrgWorld, ConcurrencyMixin, TransactionTestCase):
    """Перенос возврата на новое основание (editing) и проведение другого
    возврата по тому же основанию — в обоих порядках. Проходит одна
    операция, по основанию не возвращается больше принятого; вторая ждала
    блокировку первой (документ → товары)."""

    def setUp(self):
        self.make_world()

    def back(self, basis, qty, post):
        doc = SupplierReturn.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier,
                                            receipt=basis, **self._when())
        doc.lines.create(product=self.product, quantity=qty, price=100, vat_rate="none")
        if post:
            doc.post()
        return doc

    def test_basis_change_against_other_return_in_any_order(self):
        for first in ("move", "other"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Основание {first}", unit=self.unit)
                receipt_a, receipt_b = self.receipt(self.a, 2, 100), self.receipt(self.a, 2, 100)
                moving = self.back(receipt_a, 1, post=True)
                other = self.back(receipt_b, 2, post=False)

                def move():
                    doc = SupplierReturn.objects.get(pk=moving.pk)
                    with doc.editing():
                        doc.receipt = receipt_b
                        doc.save(update_fields=["receipt"])

                operations = {"move": move, "other": lambda: SupplierReturn.objects.get(pk=other.pk).post()}
                second = "other" if first == "move" else "move"
                results = dict(zip((first, second), self.run_overlapping(
                    operations[first], operations[second], pause_on=(services, "document_outflow"))))
                self.assertIsNone(results[first])
                self.assertIsInstance(results[second], StockError)
                self.assertIn("доступно к возврату", str(results[second]))
                self.assertEqual(return_limits.violations(), [])
                taken = sum(return_limits.returned_quantities(Receipt.objects.get(pk=receipt_b.pk)).values(), D("0"))
                self.assertEqual(taken, D("1") if first == "move" else D("2"))
