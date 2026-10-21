"""Согласованность сохранения, проведения, отмены и удаления складских документов.

Подтверждённые ошибки:
А. Проведённую отгрузку на 2 шт. меняли на 5 и сохраняли: строка 5, документ
   проведён, а остаток оставался 8 вместо 5.
Б. Приняли 10, отгрузили 4, сняли приёмку с проведения: остаток −4 при
   запрещённых отрицательных остатках.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.constants import DOC_DRAFT, DOC_POSTED
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, Shipment

from . import services
from .models import SerialNumber, StockAdjustment, StockBalance, StockMovement, Transfer
from .services import StockError

User = get_user_model()
TODAY = timezone.localdate()
DAY = timedelta(days=1)


class ConsistencyBase(TestCase):
    def setUp(self):
        self.unit = Unit.objects.create(name="шт")
        self.org = Organization.objects.create(name="Орг", is_default=True, allow_negative_stock=False)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.wh2 = Warehouse.objects.create(name="Магазин")
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.product = Product.objects.create(name="Кабель", unit=self.unit)
        self.other = Product.objects.create(name="Провод", unit=self.unit)
        self.phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        self.user = User.objects.create_user("adm", password="pass12345")
        self.user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")

    # ---------- документы ----------

    def receipt(self, qty, product=None, warehouse=None, date=TODAY, serials="", post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                     supplier=self.supplier, date=date)
        doc.lines.create(product=product or self.product, quantity=qty, price=100, serial_numbers=serials)
        if post:
            doc.post()
        return doc

    def shipment(self, qty, product=None, warehouse=None, date=TODAY, serials="", post=True):
        doc = Shipment.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                      customer=self.customer, date=date)
        doc.lines.create(product=product or self.product, quantity=qty, price=150, serial_numbers=serials)
        if post:
            doc.post()
        return doc

    def transfer(self, qty, post=True):
        doc = Transfer.objects.create(organization=self.org, warehouse_from=self.wh, warehouse_to=self.wh2)
        doc.lines.create(product=self.product, quantity=qty)
        if post:
            doc.post()
        return doc

    # ---------- состояние ----------

    def stock(self, product=None, warehouse=None):
        return services.current_qty((product or self.product).pk, (warehouse or self.wh).pk)

    def moves(self, doc):
        return sorted(StockMovement.objects.filter(doc_type=doc.DOC_TYPE, doc_id=doc.pk)
                      .values_list("product_id", "warehouse_id", "quantity"))

    def state(self):
        """Всё, что должно остаться прежним после отказа."""
        return (
            list(StockMovement.objects.order_by("pk").values_list(
                "pk", "doc_type", "doc_id", "product_id", "warehouse_id", "quantity", "date")),
            list(StockBalance.objects.order_by("pk").values_list("product_id", "warehouse_id", "quantity")),
            list(SerialNumber.objects.order_by("pk").values_list("number", "warehouse_id", "status")),
        )

    # ---------- форма документа ----------

    def form_data(self, doc, partner_field, lines=None, **header):
        """POST формы документа с его текущими строками; lines — их замена."""
        data = {
            "date": doc.date.isoformat(), "number": doc.number, "time": doc.time.strftime("%H:%M"),
            "organization": doc.organization_id, "comment": doc.comment, "action": "save",
        }
        if hasattr(doc, "warehouse_id"):
            data["warehouse"] = doc.warehouse_id
        if partner_field:
            data[partner_field] = getattr(doc, f"{partner_field}_id")
        data.update(header)
        rows = lines if lines is not None else [
            {"id": line.pk, "product": line.product_id, "quantity": line.quantity,
             "serial_numbers": line.serial_numbers} for line in doc.lines.order_by("pk")
        ]
        existing = sum(1 for row in rows if row.get("id"))
        data.update({"lines-TOTAL_FORMS": len(rows), "lines-INITIAL_FORMS": existing,
                     "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000})
        for index, row in enumerate(rows):
            defaults = {"id": "", "price": "100", "discount": "0", "vat_rate": "none", "serial_numbers": ""}
            for key, value in {**defaults, **row}.items():
                if key == "DELETE":
                    if value:
                        data[f"lines-{index}-DELETE"] = "on"
                else:
                    data[f"lines-{index}-{key}"] = value
        return data

    def save_shipment(self, doc, **changes):
        return self.client.post(reverse("shipment_edit", args=[doc.pk]), self.form_data(doc, "customer", **changes))

    def save_receipt(self, doc, **changes):
        return self.client.post(reverse("receipt_edit", args=[doc.pk]), self.form_data(doc, "supplier", **changes))

    def line(self, doc, **changes):
        line = doc.lines.get()
        return [{"id": line.pk, "product": line.product_id, "quantity": line.quantity,
                 "serial_numbers": line.serial_numbers, **changes}]

    def assert_rejected(self, response, before):
        """Форма вернулась с ошибкой, в базе ничего не изменилось."""
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Изменения не сохранены")
        self.assertNotContains(response, "сохранён и перепроведён")
        self.assertEqual(self.state(), before)


class ConfirmedScenariosTests(ConsistencyBase):
    def test_a_saving_posted_shipment_updates_stock(self):
        self.receipt(10)
        shipment = self.shipment(2)
        self.assertEqual(self.stock(), 8)
        response = self.save_shipment(shipment, lines=self.line(shipment, quantity="5"))
        self.assertEqual(response.status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, DOC_POSTED)                 # статус остался «проведён»
        self.assertEqual(shipment.lines.get().quantity, 5)
        self.assertEqual(self.stock(), 5)                             # раньше оставалось 8
        self.assertEqual(self.moves(shipment), [(self.product.pk, self.wh.pk, Decimal("-5"))])

    def test_b_unposting_receipt_after_shipment_is_blocked(self):
        receipt = self.receipt(10)
        self.shipment(4)
        before = self.state()
        with self.assertRaisesMessage(StockError, "Недостаточно товара «Кабель» на складе «Основной»"):
            receipt.unpost()
        self.assertEqual(self.state(), before)

        response = self.client.post(reverse("receipt_unpost", args=[receipt.pk]), follow=True)
        self.assertContains(response, "Не удалось снять с проведения")
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, DOC_POSTED)
        self.assertEqual(self.stock(), 6)                             # раньше становилось −4


class EditPostedDocumentTests(ConsistencyBase):
    def test_failed_increase_rolls_back_everything(self):
        self.receipt(10)
        shipment = self.shipment(2)
        before = self.state()
        response = self.save_shipment(shipment, comment="новый комментарий",
                                      lines=self.line(shipment, quantity="50"))
        self.assert_rejected(response, before)
        shipment.refresh_from_db()
        self.assertEqual(shipment.comment, "")                        # шапка не сохранилась
        self.assertEqual(shipment.lines.get().quantity, 2)            # строка — тоже
        self.assertEqual(shipment.status, DOC_POSTED)
        self.assertEqual(self.stock(), 8)
        # введённые значения остались в форме для исправления
        self.assertContains(response, 'value="50"')
        self.assertContains(response, "новый комментарий")

    def test_change_product(self):
        self.receipt(10)
        self.receipt(10, product=self.other)
        shipment = self.shipment(2)
        self.save_shipment(shipment, lines=self.line(shipment, product=self.other.pk))
        self.assertEqual(self.stock(), 10)                            # прежний товар вернулся
        self.assertEqual(self.stock(self.other), 8)
        self.assertEqual(self.moves(shipment), [(self.other.pk, self.wh.pk, Decimal("-2"))])

    def test_change_product_without_stock_is_rejected(self):
        self.receipt(10)
        shipment = self.shipment(2)
        before = self.state()
        response = self.save_shipment(shipment, lines=self.line(shipment, product=self.other.pk))
        self.assert_rejected(response, before)
        self.assertEqual(shipment.lines.get().product, self.product)

    def test_change_warehouse(self):
        self.receipt(10)
        self.receipt(10, warehouse=self.wh2)
        shipment = self.shipment(2)
        self.save_shipment(shipment, warehouse=self.wh2.pk)
        self.assertEqual((self.stock(), self.stock(warehouse=self.wh2)), (10, 8))

    def test_change_warehouse_without_stock_is_rejected(self):
        self.receipt(10)
        shipment = self.shipment(2)
        before = self.state()
        self.assert_rejected(self.save_shipment(shipment, warehouse=self.wh2.pk), before)
        shipment.refresh_from_db()
        self.assertEqual(shipment.warehouse, self.wh)

    def test_shipment_date_before_receipt_is_rejected(self):
        """Конечный остаток тот же, но на новую дату товара ещё не было."""
        self.receipt(10)
        shipment = self.shipment(2)
        before = self.state()
        response = self.save_shipment(shipment, date=(TODAY - DAY).isoformat())
        self.assert_rejected(response, before)

    def test_receipt_date_after_shipment_is_rejected(self):
        receipt = self.receipt(10)
        self.shipment(2)
        before = self.state()
        response = self.save_receipt(receipt, date=(TODAY + DAY).isoformat())
        self.assert_rejected(response, before)
        receipt.refresh_from_db()
        self.assertEqual(receipt.date, TODAY)

    def test_date_change_moves_movements(self):
        self.receipt(10, date=TODAY - 2 * DAY)
        shipment = self.shipment(2)
        self.save_shipment(shipment, date=(TODAY - DAY).isoformat())
        movement = StockMovement.objects.get(doc_type="shipment", doc_id=shipment.pk)
        self.assertEqual(movement.date, TODAY - DAY)

    def test_delete_line(self):
        self.receipt(10)
        self.receipt(10, product=self.other)
        shipment = self.shipment(2)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with shipment.editing(products=[self.other.pk]):
            extra = shipment.lines.create(product=self.other, quantity=3, price=150)
        self.assertEqual(self.stock(self.other), 7)
        first = shipment.lines.exclude(pk=extra.pk).get()
        rows = [{"id": first.pk, "product": first.product_id, "quantity": first.quantity},
                {"id": extra.pk, "product": extra.product_id, "quantity": extra.quantity, "DELETE": True}]
        self.save_shipment(shipment, lines=rows)
        self.assertEqual(shipment.lines.count(), 1)
        self.assertEqual((self.stock(), self.stock(self.other)), (8, 10))
        self.assertEqual(len(self.moves(shipment)), 1)

    def test_add_line(self):
        self.receipt(10)
        self.receipt(10, product=self.other)
        shipment = self.shipment(2)
        first = shipment.lines.get()
        rows = [{"id": first.pk, "product": first.product_id, "quantity": first.quantity},
                {"product": self.other.pk, "quantity": "4"}]
        self.save_shipment(shipment, lines=rows)
        self.assertEqual((self.stock(), self.stock(self.other)), (8, 6))

    def test_reducing_receipt_below_shipped_is_rejected(self):
        receipt = self.receipt(10)
        self.shipment(4)
        before = self.state()
        response = self.save_receipt(receipt, lines=self.line(receipt, quantity="3"))
        self.assert_rejected(response, before)
        self.assertEqual(receipt.lines.get().quantity, 10)

    def test_reducing_receipt_within_shipped_is_saved(self):
        receipt = self.receipt(10)
        self.shipment(4)
        self.save_receipt(receipt, lines=self.line(receipt, quantity="6"))
        self.assertEqual(self.stock(), 2)

    def test_draft_still_saves_without_posting(self):
        shipment = self.shipment(50, post=False)                      # товара нет вовсе
        response = self.save_shipment(shipment, lines=self.line(shipment, quantity="70"))
        self.assertEqual(response.status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual((shipment.status, shipment.lines.get().quantity), (DOC_DRAFT, 70))
        self.assertFalse(StockMovement.objects.exists())

    def test_save_and_post_failure_keeps_draft_without_movements(self):
        shipment = self.shipment(50, post=False)
        response = self.client.post(reverse("shipment_edit", args=[shipment.pk]),
                                    {**self.form_data(shipment, "customer"), "action": "save_post"}, follow=True)
        self.assertContains(response, "Сохранено, но не проведено")
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, DOC_DRAFT)
        self.assertFalse(StockMovement.objects.exists())

    def test_stale_form_does_not_resurrect_posted_status(self):
        """Форму открыли у проведённого документа, тем временем его сняли с проведения."""
        self.receipt(10)
        shipment = self.shipment(2)
        stale = self.form_data(shipment, "customer")
        Shipment.objects.get(pk=shipment.pk).unpost()
        self.client.post(reverse("shipment_edit", args=[shipment.pk]), stale)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, DOC_DRAFT)
        self.assertEqual(self.stock(), 10)


class RepostTests(ConsistencyBase):
    def test_repeated_posting_makes_no_double_movements(self):
        self.receipt(10)
        shipment = self.shipment(2)
        shipment.post()
        Shipment.objects.get(pk=shipment.pk).post()
        self.client.post(reverse("shipment_post", args=[shipment.pk]))
        self.assertEqual(self.moves(shipment), [(self.product.pk, self.wh.pk, Decimal("-2"))])
        self.assertEqual(self.stock(), 8)

    def test_repeated_posting_of_receipt(self):
        receipt = self.receipt(10)
        receipt.post()
        self.client.post(reverse("receipt_post", args=[receipt.pk]))
        self.assertEqual(self.stock(), 10)
        self.assertEqual(len(self.moves(receipt)), 1)

    def test_repeated_unposting(self):
        receipt = self.receipt(10)
        receipt.unpost()
        receipt.unpost()
        self.assertEqual(self.stock(), 0)
        self.assertEqual(receipt.status, DOC_DRAFT)


class UnpostAndDeleteTests(ConsistencyBase):
    def test_history_is_checked_not_only_final_balance(self):
        """Позже был новый приход, итог положительный — но на дату отгрузки товара не было бы."""
        first = self.receipt(10, date=TODAY - 3 * DAY)
        self.shipment(4, date=TODAY - 2 * DAY)
        self.receipt(10, date=TODAY - DAY)
        before = self.state()
        # в сообщении теперь дата и время операции: проверка идёт по хронологии, а не по дням
        with self.assertRaisesRegex(StockError, rf"на {TODAY - 2 * DAY:%d\.%m\.%Y} \d\d:\d\d остаток стал бы -4"):
            first.unpost()
        self.assertEqual(self.state(), before)

    def test_unpost_allowed_when_earlier_receipt_covers(self):
        self.receipt(10, date=TODAY - 3 * DAY)
        self.shipment(4, date=TODAY - 2 * DAY)
        later = self.receipt(10, date=TODAY - DAY)
        later.unpost()
        self.assertEqual(self.stock(), 6)

    def test_single_delete_of_receipt_after_shipment_is_blocked(self):
        receipt = self.receipt(10)
        self.shipment(4)
        before = self.state()
        response = self.client.post(reverse("receipt_delete", args=[receipt.pk]), follow=True)
        self.assertContains(response, "Не удалось удалить")
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk, status=DOC_POSTED).exists())
        self.assertEqual(self.state(), before)

    def test_model_delete_is_guarded(self):
        receipt = self.receipt(10)
        self.shipment(4)
        with self.assertRaises(StockError):
            receipt.delete()
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk, status=DOC_POSTED).exists())
        self.assertEqual(self.stock(), 6)

    def test_delete_of_free_receipt_removes_stock(self):
        receipt = self.receipt(10)
        self.client.post(reverse("receipt_delete", args=[receipt.pk]))
        self.assertFalse(Receipt.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(self.stock(), 0)

    def test_bulk_delete_follows_same_rules(self):
        blocked = self.receipt(10)
        free = self.receipt(5, product=self.other)
        self.shipment(4)
        response = self.client.post(reverse("receipt_bulk_delete"), {"ids": [blocked.pk, free.pk]}, follow=True)
        self.assertEqual(list(Receipt.objects.values_list("pk", "status")), [(blocked.pk, DOC_POSTED)])
        self.assertContains(response, "Удалено приёмок: 1")
        self.assertContains(response, "Не удалён")
        self.assertContains(response, "Недостаточно товара")
        self.assertEqual((self.stock(), self.stock(self.other)), (6, 0))

    def test_unposting_outgoing_document_is_always_possible(self):
        self.receipt(10)
        shipment = self.shipment(4)
        shipment.unpost()
        self.assertEqual(self.stock(), 10)

    def test_existing_negative_does_not_block_improvement(self):
        """Минус, накопленный раньше, не мешает операциям, которые его не углубляют."""
        self.org.allow_negative_stock = True
        self.org.save()
        shipment = self.shipment(4)                                   # остаток −4
        self.org.allow_negative_stock = False
        self.org.save()
        receipt = self.receipt(3)                                     # остаток −1: лучше, чем было
        self.assertEqual(self.stock(), -1)
        with self.assertRaises(StockError):
            Receipt.objects.get(pk=receipt.pk).unpost()               # вернуть −4 уже нельзя
        Shipment.objects.get(pk=shipment.pk).unpost()
        self.assertEqual(self.stock(), 3)


class NegativeStockAllowedTests(ConsistencyBase):
    def setUp(self):
        super().setUp()
        self.org.allow_negative_stock = True
        self.org.save()

    def test_sales_may_go_negative_as_before(self):
        self.receipt(1)
        self.shipment(4)
        self.assertEqual(self.stock(), -3)

    def test_unposting_receipt_is_allowed(self):
        receipt = self.receipt(10)
        self.shipment(4)
        receipt.unpost()
        self.assertEqual(self.stock(), -4)

    def test_editing_posted_shipment_above_stock(self):
        self.receipt(1)
        shipment = self.shipment(1)
        self.save_shipment(shipment, lines=self.line(shipment, quantity="9"))
        self.assertEqual(self.stock(), -8)

    def test_transfer_is_still_strict(self):
        """Флаг организации — про продажи; перемещение без остатка не проходит и с ним."""
        self.receipt(2)
        with self.assertRaises(StockError):
            self.transfer(5)

    def test_serial_rules_are_not_bypassed(self):
        receipt = self.receipt(1, product=self.phone, serials="SN-1")
        self.shipment(1, product=self.phone, serials="SN-1")
        before = self.state()
        with self.assertRaisesMessage(StockError, "Серийные номера: номер SN-1"):
            receipt.unpost()
        self.assertEqual(self.state(), before)
        with self.assertRaises(StockError):
            receipt.delete()
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk, status=DOC_POSTED).exists())

    def test_serial_cannot_be_sold_without_being_in_stock(self):
        with self.assertRaisesMessage(StockError, "Серийные номера"):
            self.shipment(1, product=self.phone, serials="SN-404")


class TransferTests(ConsistencyBase):
    def test_unposting_transfer_after_shipment_from_target(self):
        self.receipt(10)
        transfer = self.transfer(6)
        self.shipment(5, warehouse=self.wh2)
        before = self.state()
        with self.assertRaisesMessage(StockError, "на складе «Магазин»"):
            transfer.unpost()
        self.assertEqual(self.state(), before)

    def test_editing_posted_transfer(self):
        self.receipt(10)
        transfer = self.transfer(6)
        self.shipment(5, warehouse=self.wh2)
        url = reverse("transfer_edit", args=[transfer.pk])
        header = {"warehouse_from": self.wh.pk, "warehouse_to": self.wh2.pk}

        before = self.state()
        response = self.client.post(url, self.form_data(
            transfer, None, lines=self.line(transfer, quantity="4"), **header))
        self.assert_rejected(response, before)                        # в магазине не хватило бы

        self.client.post(url, self.form_data(transfer, None, lines=self.line(transfer, quantity="8"), **header))
        self.assertEqual((self.stock(), self.stock(warehouse=self.wh2)), (2, 3))

    def test_unposting_receipt_after_transfer(self):
        receipt = self.receipt(10)
        self.transfer(6)
        with self.assertRaises(StockError):
            receipt.unpost()

    def test_transfer_bulk_delete_blocked(self):
        self.receipt(10)
        transfer = self.transfer(6)
        self.shipment(5, warehouse=self.wh2)
        self.client.post(reverse("transfer_bulk_delete"), {"ids": [transfer.pk]})
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk, status=DOC_POSTED).exists())
        self.assertEqual(self.stock(warehouse=self.wh2), 1)


class ReturnTests(ConsistencyBase):
    def test_unposting_customer_return_after_resale(self):
        self.receipt(10)
        self.shipment(10)
        returned = CustomerReturn.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        returned.lines.create(product=self.product, quantity=3, price=150)
        returned.post()
        self.shipment(3)
        with self.assertRaises(StockError):
            returned.unpost()
        self.client.post(reverse("customer_return_delete", args=[returned.pk]))
        self.assertTrue(CustomerReturn.objects.filter(pk=returned.pk, status=DOC_POSTED).exists())
        self.assertEqual(self.stock(), 0)

    def test_supplier_return_cannot_exceed_stock_on_edit(self):
        self.receipt(10)
        doc = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier)
        doc.lines.create(product=self.product, quantity=4, price=100)
        doc.post()
        before = self.state()
        response = self.client.post(reverse("supplier_return_edit", args=[doc.pk]),
                                    self.form_data(doc, "supplier", lines=self.line(doc, quantity="11")))
        self.assert_rejected(response, before)
        self.client.post(reverse("supplier_return_edit", args=[doc.pk]),
                         self.form_data(doc, "supplier", lines=self.line(doc, quantity="7")))
        self.assertEqual(self.stock(), 3)

    def test_expense_adjustment_edit(self):
        self.receipt(10)
        doc = StockAdjustment.objects.create(kind=StockAdjustment.KIND_EXPENSE, organization=self.org,
                                             warehouse=self.wh)
        doc.lines.create(product=self.product, quantity=2)
        doc.post()
        url = reverse("adjustment_edit", args=[doc.pk])
        before = self.state()
        self.assert_rejected(
            self.client.post(url, self.form_data(doc, None, reason="", lines=self.line(doc, quantity="20"))), before)
        self.client.post(url, self.form_data(doc, None, reason="", lines=self.line(doc, quantity="9")))
        self.assertEqual(self.stock(), 1)


class SerialConsistencyTests(ConsistencyBase):
    def serial(self, number):
        return SerialNumber.objects.get(product=self.phone, number=number)

    def test_editing_posted_shipment_swaps_serial(self):
        self.receipt(3, product=self.phone, serials="SN-1\nSN-2\nSN-3")
        shipment = self.shipment(1, product=self.phone, serials="SN-1")
        self.save_shipment(shipment, lines=self.line(shipment, serial_numbers="SN-2"))
        self.assertEqual(self.serial("SN-1").status, SerialNumber.STATUS_IN_STOCK)
        self.assertEqual(self.serial("SN-2").status, SerialNumber.STATUS_SOLD)
        self.assertEqual(self.stock(self.phone), 2)

    def test_editing_posted_shipment_to_missing_serial_is_rejected(self):
        self.receipt(3, product=self.phone, serials="SN-1\nSN-2\nSN-3")
        shipment = self.shipment(1, product=self.phone, serials="SN-1")
        before = self.state()
        response = self.save_shipment(shipment, lines=self.line(shipment, serial_numbers="SN-9"))
        self.assert_rejected(response, before)
        self.assertEqual(self.serial("SN-1").status, SerialNumber.STATUS_SOLD)
        self.assertEqual(shipment.lines.get().serial_numbers, "SN-1")

    def test_unposting_receipt_with_sold_serial_is_blocked(self):
        receipt = self.receipt(2, product=self.phone, serials="SN-1\nSN-2")
        self.shipment(1, product=self.phone, serials="SN-1")
        before = self.state()
        with self.assertRaises(StockError):
            receipt.unpost()
        self.assertEqual(self.state(), before)

    def test_removing_sold_serial_from_posted_receipt_is_blocked(self):
        """Остатка хватает (другой номер на месте), но проданный номер из приёмки убрать нельзя."""
        receipt = self.receipt(2, product=self.phone, serials="SN-1\nSN-2")
        self.receipt(1, product=self.phone, serials="SN-3")
        self.shipment(1, product=self.phone, serials="SN-1")
        before = self.state()
        response = self.save_receipt(receipt, lines=self.line(receipt, quantity="1", serial_numbers="SN-2"))
        self.assert_rejected(response, before)

    def test_transfer_of_serial_and_blocked_unpost(self):
        self.receipt(1, product=self.phone, serials="SN-1")
        transfer = Transfer.objects.create(organization=self.org, warehouse_from=self.wh, warehouse_to=self.wh2)
        transfer.lines.create(product=self.phone, quantity=1, serial_numbers="SN-1")
        transfer.post()
        self.shipment(1, product=self.phone, warehouse=self.wh2, serials="SN-1")
        before = self.state()
        with self.assertRaises(StockError):
            transfer.unpost()
        self.assertEqual(self.state(), before)

    def test_bulk_delete_respects_serials(self):
        receipt = self.receipt(1, product=self.phone, serials="SN-1")
        self.shipment(1, product=self.phone, serials="SN-1")
        self.client.post(reverse("receipt_bulk_delete"), {"ids": [receipt.pk]})
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk, status=DOC_POSTED).exists())
        self.assertEqual(self.serial("SN-1").status, SerialNumber.STATUS_SOLD)


class LockingTests(ConsistencyBase):
    """Порядок действий под блокировками. Сами блокировки строк есть только
    в PostgreSQL — их проверяет tests_concurrency.py."""

    def test_products_are_locked_before_stock_is_read(self):
        self.receipt(10)
        self.receipt(10, product=self.other)
        shipment = self.shipment(2, post=False)
        shipment.lines.create(product=self.other, quantity=1, price=150)
        events = []
        real_specs = Shipment.build_specs

        def specs(doc):
            events.append("read")
            return real_specs(doc)

        with mock.patch.object(services, "lock_products", side_effect=lambda ids: events.append(sorted(ids))), \
                mock.patch.object(Shipment, "build_specs", specs):
            shipment.post()
        self.assertEqual(events, [sorted([self.product.pk, self.other.pk]), "read"])

    def test_old_and_new_products_are_locked_on_edit(self):
        """При смене товара блокируются оба: и прежний (из движений), и новый (из строк)."""
        self.receipt(10)
        self.receipt(10, product=self.other)
        shipment = self.shipment(2)
        line = shipment.lines.get()
        locked = []
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with mock.patch.object(services, "lock_products", side_effect=lambda ids: locked.append(set(ids))):
            with shipment.editing(products=[self.other.pk]):
                line.product = self.other
                line.save()
        # заранее — в editing() (прежний из движений и строк, новый из products), и в проведении при выходе
        self.assertEqual(locked[0], {self.product.pk, self.other.pk})
        self.assertEqual(locked[-1], {self.product.pk, self.other.pk})

    def test_lock_sql_for_postgresql(self):
        """SQL блокировки, каким его соберёт PostgreSQL-бэкенд Django (без сервера):
        товары по возрастанию id и FOR NO KEY UPDATE. Саму работу блокировок
        это не доказывает — только то, что запрос собирается и имеет нужный вид."""
        from django.db.utils import ConnectionHandler

        try:
            postgres = ConnectionHandler(
                {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "x"}})["default"]
        except Exception as exc:  # noqa: BLE001 — нет драйвера psycopg
            self.skipTest(f"PostgreSQL-бэкенд недоступен: {exc}")
        query = services.product_lock_query([7, 3, 5], postgres.features).query
        with mock.patch.object(postgres, "get_autocommit", return_value=False):   # «внутри транзакции»
            sql, params = query.get_compiler(connection=postgres).as_sql()
        # ORDER BY 1 — по единственной выбранной колонке, то есть по id товара
        self.assertIn('SELECT "catalog_product"."id"', sql)
        self.assertTrue(sql.endswith("ORDER BY 1 ASC FOR NO KEY UPDATE"), sql)
        self.assertEqual(sorted(params), [3, 5, 7])

        # блокировка строки документа (PostableMixin._lock) — тот же запрос, что делает .first()
        document = Shipment.objects.select_for_update().filter(pk=1).values_list("status", flat=True)
        with mock.patch.object(postgres, "get_autocommit", return_value=False):
            sql, _ = document.order_by("pk")[:1].query.get_compiler(connection=postgres).as_sql()
        self.assertTrue(sql.endswith("LIMIT 1 FOR UPDATE"), sql)

    def test_lock_query_matches_backend(self):
        """В SQLite блокировок строк нет — запрос не выполняется вовсе.
        В PostgreSQL — ровно один запрос FOR NO KEY UPDATE по возрастанию id."""
        from django.test.utils import CaptureQueriesContext

        if not connection.features.has_select_for_update:
            with self.assertNumQueries(0):
                services.lock_products([self.product.pk, self.other.pk])
            return
        with CaptureQueriesContext(connection) as queries:
            services.lock_products([self.other.pk, self.product.pk])
        self.assertEqual(len(queries), 1)
        sql = queries[0]["sql"]
        self.assertIn("FOR NO KEY UPDATE", sql)
        self.assertIn("ORDER BY", sql)


class AdminGuardTests(ConsistencyBase):
    def setUp(self):
        super().setUp()
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()

    def test_derived_records_are_read_only(self):
        self.receipt(10)
        movement = StockMovement.objects.get()
        balance = StockBalance.objects.get()
        for url in [reverse("admin:inventory_stockmovement_add"), reverse("admin:inventory_stockbalance_add"),
                    reverse("admin:inventory_stockmovement_delete", args=[movement.pk]),
                    reverse("admin:inventory_stockbalance_delete", args=[balance.pk])]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        change = reverse("admin:inventory_stockbalance_change", args=[balance.pk])
        self.assertEqual(self.client.get(change).status_code, 200)            # просмотр есть
        self.assertEqual(self.client.post(change, {"quantity": "999"}).status_code, 403)
        balance.refresh_from_db()
        self.assertEqual(balance.quantity, 10)

    def test_posted_document_cannot_be_changed_or_deleted(self):
        receipt = self.receipt(10)
        change = reverse("admin:purchases_receipt_change", args=[receipt.pk])
        self.assertEqual(self.client.post(change, {"status": DOC_DRAFT}).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("admin:purchases_receipt_delete", args=[receipt.pk]), {"post": "yes"}).status_code,
            403)
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk, status=DOC_POSTED).exists())

    def test_admin_bulk_delete_is_disabled(self):
        receipt = self.receipt(10)
        self.client.post(reverse("admin:purchases_receipt_changelist"),
                         {"action": "delete_selected", "_selected_action": [receipt.pk], "post": "yes"})
        self.assertTrue(Receipt.objects.filter(pk=receipt.pk).exists())
        self.assertEqual(self.stock(), 10)

    def test_status_is_not_editable_for_draft(self):
        draft = self.receipt(10, post=False)
        response = self.client.get(reverse("admin:purchases_receipt_change", args=[draft.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="status"')
