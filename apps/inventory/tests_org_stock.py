"""Складской учёт по организациям: ключ «организация + склад + товар».

Ожидания перенесены из независимой модели docs/proposals/org_stock_model.py
(27 проверок; числа посчитаны вручную) на настоящие документы. «Было» в
комментариях — поведение до 18.10.2026 (ключ «склад + товар»), «стало» —
ожидание модели в режиме «организация + склад + товар».
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.reports.services import sales_summary
from apps.sales.models import CustomerReturn, Shipment, ShipmentLine

from . import costing, serials, transition
from .models import (
    DeficitCover, SerialMovement, SerialNumber, StockBalance, StockLedgerState, StockMovement, Transfer,
)
from .services import StockError

D = Decimal
ZERO = D("0")


class OrgWorld:
    """Две организации, общий склад W и склады W1, W2; документы по порядку времени."""

    def make_world(self):
        self.a = Organization.objects.create(name="А", is_default=True)
        self.b = Organization.objects.create(name="Б")
        self.w = Warehouse.objects.create(name="W", is_default=True)
        self.w1 = Warehouse.objects.create(name="W1")
        self.w2 = Warehouse.objects.create(name="W2")
        self.unit = Unit.objects.create(name="шт")
        self.product = Product.objects.create(name="Товар", unit=self.unit, purchase_price=70)
        self.supplier = Counterparty.objects.create(name="Поставщик", partner_type=Counterparty.TYPE_SUPPLIER)
        self.customer = Counterparty.objects.create(name="Покупатель", partner_type=Counterparty.TYPE_CUSTOMER)
        self.clock = datetime.datetime(2026, 10, 1, 8, 0)

    def negative(self, org, allowed):
        org.allow_negative_stock = allowed
        org.save()

    def _when(self):
        self.clock += datetime.timedelta(minutes=10)
        return {"date": self.clock.date(), "time": self.clock.time()}

    def receipt(self, org, qty, price, wh=None, product=None, serial="", post=True):
        doc = Receipt.objects.create(organization=org, warehouse=wh or self.w, supplier=self.supplier, **self._when())
        doc.lines.create(product=product or self.product, quantity=qty, price=price, vat_rate="none",
                         serial_numbers=serial)
        if post:
            doc.post()
        return doc

    def ship(self, org, qty, wh=None, product=None, serial="", post=True):
        doc = Shipment.objects.create(organization=org, warehouse=wh or self.w, customer=self.customer, **self._when())
        doc.lines.create(product=product or self.product, quantity=qty, price=300, vat_rate="none",
                         serial_numbers=serial)
        if post:
            doc.post()
        return doc

    def transfer(self, org, wh_from, wh_to, qty, serial="", post=True):
        doc = Transfer.objects.create(organization=org, warehouse_from=wh_from, warehouse_to=wh_to, **self._when())
        doc.lines.create(product=self.product, quantity=qty, serial_numbers=serial)
        if post:
            doc.post()
        return doc

    def customer_return(self, org, qty, basis=None, wh=None, post=True):
        doc = CustomerReturn.objects.create(organization=org, warehouse=wh or self.w, customer=self.customer,
                                            shipment=basis, **self._when())
        doc.lines.create(product=self.product, quantity=qty, price=300, vat_rate="none")
        if post:
            doc.post()
        return doc

    def supplier_return(self, org, qty, basis):
        doc = SupplierReturn.objects.create(organization=org, warehouse=self.w, supplier=self.supplier,
                                            receipt=basis, **self._when())
        doc.lines.create(product=self.product, quantity=qty, price=200, vat_rate="none")
        doc.post()
        return doc

    # ---------- чтение ----------

    def balances(self, product=None):
        return {(b.organization.name, b.warehouse.name): (b.quantity, b.value)
                for b in StockBalance.objects.filter(product=product or self.product).select_related(
                    "organization", "warehouse")}

    def line(self, shipment):
        line = ShipmentLine.objects.get(shipment=shipment)
        return line.cost_amount, line.cost_open_qty

    def amount(self, doc):
        return StockMovement.objects.get(doc_type=doc.DOC_TYPE, doc_id=doc.pk).amount

    def assertRejected(self, action, text="Недостаточно товара"):
        with self.assertRaisesMessage(StockError, text):
            action()

    def state(self):
        """Всё, что откатывается при отказе: документы, движения, номера,
        покрытия, себестоимость строк, остатки."""
        return (
            sorted(Receipt.objects.values_list("pk", "organization_id", "status")),
            sorted(Shipment.objects.values_list("pk", "organization_id", "status")),
            sorted(Transfer.objects.values_list("pk", "organization_id", "status")),
            sorted(StockMovement.objects.values_list("doc_type", "doc_id", "organization_id", "warehouse_id",
                                                     "quantity", "amount", "open_qty")),
            sorted(ShipmentLine.objects.values_list("pk", "cost_amount", "cost_open_qty")),
            sorted(StockBalance.objects.values_list("organization_id", "product_id", "warehouse_id", "quantity",
                                                    "value")),
            sorted(DeficitCover.objects.values_list("issue__doc_id", "cover__doc_id", "quantity", "amount")),
            sorted(SerialNumber.objects.values_list("number", "organization_id", "warehouse_id", "status")),
            sorted(SerialMovement.objects.values_list("serial__number", "doc_type", "doc_id", "organization_id",
                                                      "warehouse_id", "quantity")),
        )


class OrgStockBase(OrgWorld, TestCase):
    def setUp(self):
        self.make_world()


# ---------------------------------------------------------------------------
# 27 ожиданий независимой модели на настоящих документах
# ---------------------------------------------------------------------------

class ModelScenarioTests(OrgStockBase):
    def test_1_strict_b_cannot_use_stock_of_a(self):
        """А приняла 1 × 100 на W; у Б поступлений нет, минус запрещён."""
        self.receipt(self.a, 1, 100)
        ship_b = self.ship(self.b, 1, post=False)
        # было: Б проводит по 100, общий остаток 0, А продать не может; стало: Б — отказ
        self.assertRejected(ship_b.post)
        self.assertEqual(Shipment.objects.get(pk=ship_b.pk).status, "draft")
        ship_a = self.ship(self.a, 1)                       # стало: А продаёт свою единицу по 100
        self.assertEqual(self.line(ship_a), (D("100.00"), ZERO))
        self.assertEqual(self.balances(), {("А", "W"): (ZERO, D("0.00"))})

    def test_2_deficit_of_b_is_not_covered_by_a(self):
        self.negative(self.b, True)
        self.receipt(self.a, 1, 100)
        ship_b = self.ship(self.b, 1)
        # было: себестоимость 100 из остатка А; стало: дефицит Б 1, себестоимость предварительная 0
        self.assertEqual(self.line(ship_b), (D("0.00"), D("1")))
        self.assertEqual(self.balances(), {("А", "W"): (D("1"), D("100.00")), ("Б", "W"): (D("-1"), D("0.00"))})
        self.receipt(self.b, 1, 80)
        # было: остаток W 1 / 80 (у А товара нет); стало: поступление Б покрыло её дефицит по 80
        self.assertEqual(self.line(ship_b), (D("80.00"), ZERO))
        self.assertEqual(self.balances(), {("А", "W"): (D("1"), D("100.00")), ("Б", "W"): (ZERO, D("0.00"))})
        cover = DeficitCover.objects.get()
        self.assertEqual((cover.issue.organization_id, cover.cover.organization_id), (self.b.pk, self.b.pk))

    def test_3_each_organization_has_its_own_average(self):
        self.receipt(self.a, 10, 100)
        self.receipt(self.b, 10, 200)
        ship_a, ship_b = self.ship(self.a, 5), self.ship(self.b, 2)
        # было: общая средняя 150 — 750 и 300, остаток 13 / 1 950; стало: 500 и 400
        self.assertEqual((self.line(ship_a)[0], self.line(ship_b)[0]), (D("500.00"), D("400.00")))
        self.assertEqual(self.balances(), {("А", "W"): (D("5"), D("500.00")), ("Б", "W"): (D("8"), D("1600.00"))})

    def test_4_receipt_covers_only_own_deficit(self):
        self.negative(self.a, True)
        self.negative(self.b, True)
        ship_a, ship_b = self.ship(self.a, 2), self.ship(self.b, 3)
        self.receipt(self.b, 3, 100)
        # было: приёмка Б покрыла дефицит А (200) и 1 шт. своего (100, не покр. 2)
        self.assertEqual((self.line(ship_a), self.line(ship_b)), ((D("0.00"), D("2")), (D("300.00"), ZERO)))
        self.receipt(self.a, 2, 120)                          # 2 × 120 = 240
        # было: приёмка А покрыла остаток дефицита Б (340); стало: свой (240)
        self.assertEqual((self.line(ship_a), self.line(ship_b)), ((D("240.00"), ZERO), (D("300.00"), ZERO)))
        self.assertEqual(self.balances(), {("А", "W"): (ZERO, D("0.00")), ("Б", "W"): (ZERO, D("0.00"))})

    def test_5_returns_use_own_organization(self):
        self.receipt(self.a, 10, 100)
        receipt_b = self.receipt(self.b, 10, 200)
        ship_a = self.ship(self.a, 5)
        back_a = self.customer_return(self.a, 2, basis=ship_a)
        back_b = self.customer_return(self.b, 1)
        to_supplier = self.supplier_return(self.b, 2, receipt_b)
        # было: 750; 300; 150 (общая средняя); 300; стало: 500; 200; 200 (средняя Б); 400
        self.assertEqual((self.line(ship_a)[0], self.amount(back_a), self.amount(back_b), self.amount(to_supplier)),
                         (D("500.00"), D("200.00"), D("200.00"), D("400.00")))
        cross = self.customer_return(self.b, 1, basis=ship_a, post=False)
        with self.assertRaisesMessage(StockError, "не совпадает с организацией"):
            cross.post()                                     # и было, и стало — отказ
        self.assertEqual(self.balances(), {("А", "W"): (D("7"), D("700.00")), ("Б", "W"): (D("9"), D("1800.00"))})

    def test_6_transfer_keeps_organization(self):
        self.receipt(self.a, 5, 100, wh=self.w1)
        move_b = self.transfer(self.b, self.w1, self.w2, 5, post=False)
        # было: Б перемещает товар А и продаёт его на W2 (500); стало: отказ
        self.assertRejected(move_b.post)
        ship_b = self.ship(self.b, 5, wh=self.w2, post=False)
        self.assertRejected(ship_b.post)
        self.transfer(self.a, self.w1, self.w2, 3)          # было: отказ (W1 пуст); стало: проведено
        self.assertEqual(self.balances(), {("А", "W1"): (D("2"), D("200.00")), ("А", "W2"): (D("3"), D("300.00"))})
        self.assertEqual(set(StockMovement.objects.filter(doc_type="transfer").values_list("organization_id",
                                                                                            flat=True)), {self.a.pk})

    def test_7_cancelling_own_receipt(self):
        for allowed in (False, True):
            with self.subTest(minus_b_allowed=allowed):
                self.negative(self.b, allowed)
                product = Product.objects.create(name=f"Товар 7 {allowed}", unit=self.unit)
                receipt = self.receipt(self.a, 1, 100, product=product)
                ship_b = self.ship(self.b, 1, product=product, post=False)
                if allowed:
                    ship_b.post()                            # стало: у Б свой дефицит
                    self.assertEqual(self.line(ship_b), (D("0.00"), D("1")))
                else:
                    self.assertRejected(ship_b.post)
                Receipt.objects.get(pk=receipt.pk).unpost()  # было: отказ — Б продала товар А
                expected = {("Б", "W"): (D("-1"), D("0.00"))} if allowed else {}
                self.assertEqual(self.balances(product), expected)

    def test_8_change_of_organization_checks_both_histories(self):
        receipt = self.receipt(self.a, 1, 100)
        ship_a = self.ship(self.a, 1)
        before = self.state()
        doc = Receipt.objects.get(pk=receipt.pk)
        # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): обычное сохранение проведённого документа с новой
        # организацией отклоняется (posting, правило 9) — меняют через change_organization():
        # то же сохранение и перепроведение одной транзакцией. Ожидания прежние.
        # было: проходит незаметно — у А продажа без поступления; стало: отказ, всё откатывается
        with self.assertRaisesMessage(StockError, "Недостаточно товара"):
            doc.change_organization(self.b)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.line(ship_a), (D("100.00"), ZERO))

    def test_9_serial_number_belongs_to_organization(self):
        self.product.track_serials = True
        self.product.save()
        self.receipt(self.a, 1, 100, wh=self.w1, serial="SN-1")
        move_b = self.transfer(self.b, self.w1, self.w2, 1, serial="SN-1", post=False)
        with self.assertRaises(StockError):
            move_b.post()                                   # было: проходит
        self.receipt(self.b, 1, 100, wh=self.w1, serial="SN-2")   # у Б на W1 своя единица — количество есть
        ship_b = self.ship(self.b, 1, wh=self.w1, serial="SN-1", post=False)
        with self.assertRaisesMessage(StockError, "принадлежит не организации документа"):
            ship_b.post()                                   # было: продан Б
        serial = SerialNumber.objects.get(number="SN-1")
        self.assertEqual((serial.organization_id, serial.warehouse_id, serial.status),
                         (self.a.pk, self.w1.pk, SerialNumber.STATUS_IN_STOCK))


# ---------------------------------------------------------------------------
# Смена организации, повторное проведение, несколько товаров
# ---------------------------------------------------------------------------

class OrganizationChangeTests(OrgStockBase):
    def test_compatible_change_moves_stock_to_new_organization(self):
        receipt = self.receipt(self.a, 3, 100)
        # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): обычное сохранение проведённого документа с новой
        # организацией отклоняется (posting, правило 9) — меняют через change_organization():
        # то же сохранение и перепроведение одной транзакцией. Ожидания прежние.
        Receipt.objects.get(pk=receipt.pk).change_organization(self.b)
        self.assertEqual(self.balances(), {("Б", "W"): (D("3"), D("300.00"))})
        self.assertEqual(set(StockMovement.objects.values_list("organization_id", flat=True)), {self.b.pk})

    def test_change_rejected_by_new_history_rolls_back_everything(self):
        """Отгрузка А переводится на Б, у которой товара нет."""
        self.receipt(self.a, 2, 100)
        shipment = self.ship(self.a, 1)
        before = self.state()
        # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): обычное сохранение проведённого документа с новой
        # организацией отклоняется (posting, правило 9) — меняют через change_organization():
        # то же сохранение и перепроведение одной транзакцией. Ожидания прежние.
        with self.assertRaisesMessage(StockError, "Недостаточно товара"):
            Shipment.objects.get(pk=shipment.pk).change_organization(self.b)
        self.assertEqual(self.state(), before)

    def test_change_incompatible_with_returns_is_rejected(self):
        self.receipt(self.a, 2, 100)
        shipment = self.ship(self.a, 2)
        self.customer_return(self.a, 1, basis=shipment)
        before = self.state()
        # ПРАВИЛО ИЗМЕНЕНО (19.10.2026): обычное сохранение проведённого документа с новой
        # организацией отклоняется (posting, правило 9) — меняют через change_organization():
        # то же сохранение и перепроведение одной транзакцией. Ожидания прежние.
        with self.assertRaisesMessage(StockError, "организация и покупатель должны совпадать"):
            Shipment.objects.get(pk=shipment.pk).change_organization(self.b)
        self.assertEqual(self.state(), before)

    def test_change_through_form_with_two_products_rolls_back_all(self):
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        second = Product.objects.create(name="Второй", unit=self.unit)
        receipt = Receipt.objects.create(organization=self.a, warehouse=self.w, supplier=self.supplier, **self._when())
        first_line = receipt.lines.create(product=self.product, quantity=2, price=100, vat_rate="none")
        second_line = receipt.lines.create(product=second, quantity=2, price=50, vat_rate="none")
        receipt.post()
        self.ship(self.a, 1, product=second)                 # А продала второй товар из этой приёмки
        before = self.state()
        data = {
            "date": receipt.date.isoformat(), "time": receipt.time.strftime("%H:%M:%S"), "number": receipt.number,
            "organization": self.b.pk, "warehouse": self.w.pk, "supplier": self.supplier.pk, "contract": "",
            "supplier_invoice": "", "comment": "", "action": "save",
            "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "2", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
        }
        for index, line in enumerate((first_line, second_line)):
            data.update({f"lines-{index}-id": line.pk, f"lines-{index}-product": line.product_id,
                         f"lines-{index}-quantity": "2", f"lines-{index}-price": str(line.price),
                         f"lines-{index}-discount": "0", f"lines-{index}-vat_rate": "none",
                         f"lines-{index}-serial_numbers": ""})
        response = self.client.post(reverse("receipt_edit", args=[receipt.pk]), data)
        self.assertEqual(response.status_code, 200)          # форма с ошибкой, ничего не сохранено
        self.assertEqual(self.state(), before)
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).organization_id, self.a.pk)

    def test_repost_without_changes_keeps_everything(self):
        self.negative(self.a, True)
        self.receipt(self.a, 3, 100)
        self.receipt(self.b, 2, 200)
        shipment = self.ship(self.a, 5)                      # 3 из остатка А, 2 — дефицит А
        self.receipt(self.a, 2, 120)
        before = self.state()
        Shipment.objects.get(pk=shipment.pk).post()
        for doc in Receipt.objects.all():
            doc.post()
        self.assertEqual(self.state(), before)
        self.assertEqual(self.line(shipment), (D("540.00"), ZERO))


# ---------------------------------------------------------------------------
# Возвраты без основания, остатки на дату, сводка, фильтры
# ---------------------------------------------------------------------------

class ViewsAndReportsTests(OrgStockBase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("acc", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.force_login(user)

    def test_return_without_basis_never_borrows_average_of_other_organization(self):
        self.receipt(self.a, 10, 100)
        back_b = self.customer_return(self.b, 1)             # у Б истории товара нет
        # не средняя А (100), а цена, зафиксированная в строке возврата (закупочная карточки 70)
        self.assertEqual(self.amount(back_b), D("70.00"))
        self.assertEqual(self.balances()[("Б", "W")], (D("1"), D("70.00")))

    def test_all_organizations_summary_shows_stock_and_deficit_separately(self):
        """А +1 на 100, Б −1 без определённой стоимости."""
        self.negative(self.b, True)
        self.receipt(self.a, 1, 100)
        self.ship(self.b, 1)
        response = self.client.get(reverse("balance_list"))
        rows = list(response.context["balances"])
        self.assertEqual([(r.organization.name, r.quantity) for r in rows], [("А", D("1")), ("Б", D("-1"))])
        summary = rows[-1].summary
        self.assertEqual((summary.quantity, summary.value, summary.deficit, summary.average),
                         (D("1"), D("100.00"), D("1"), D("100.00")))
        self.assertContains(response, "в наличии 1")
        self.assertContains(response, "дефицит 1 — стоимость не определена")
        self.assertContains(response, "Справочно")
        self.assertNotContains(response, "Остатков нет")
        self.assertEqual(response.context["total_value"], D("100.00"))
        self.assertEqual(response.context["deficit_quantity"], D("1"))
        only_a = self.client.get(reverse("balance_list"), {"organization": self.a.pk})
        self.assertEqual([(r.organization.name, r.quantity) for r in only_a.context["balances"]], [("А", D("1"))])

    def test_stock_on_date_by_organization(self):
        self.receipt(self.a, 10, 100)
        self.receipt(self.b, 10, 200)
        day = self.clock.date()
        self.clock += datetime.timedelta(days=1)
        self.ship(self.a, 4)
        self.assertEqual(costing.state_on(self.product.pk, day),
                         {(self.a.pk, self.w.pk): (D("10"), D("1000.00"), ZERO),
                          (self.b.pk, self.w.pk): (D("10"), D("2000.00"), ZERO)})
        response = self.client.get(reverse("balance_list"), {"on": day.isoformat(), "organization": self.b.pk})
        self.assertEqual([(r.organization.name, r.quantity, r.total_value) for r in response.context["balances"]],
                         [("Б", D("10"), D("2000.00"))])

    def test_product_picker_uses_document_organization(self):
        self.receipt(self.a, 7, 100)
        self.receipt(self.b, 2, 100)
        url = reverse("api_product_stock")
        def stock(org):
            params = {"ids": str(self.product.pk), "warehouse": self.w.pk}
            if org:
                params["organization"] = org.pk
            return self.client.get(url, params).json()["stock"][str(self.product.pk)]["stock"]
        self.assertEqual((stock(self.a), stock(self.b), stock(None)), (7.0, 2.0, None))
        found = self.client.get(reverse("api_product_search"), {"q": "Товар", "warehouse": self.w.pk,
                                                               "organization": self.b.pk}).json()["results"][0]
        self.assertEqual(found["stock"], 2)

    def test_sales_report_filter_and_sum(self):
        self.receipt(self.a, 10, 100)
        self.receipt(self.b, 10, 200)
        self.ship(self.a, 5)                                 # выручка 1 500, себестоимость 500
        self.ship(self.b, 2)                                 # выручка 600, себестоимость 400
        a, b, both = (sales_summary(organization=self.a.pk), sales_summary(organization=self.b.pk),
                      sales_summary())
        self.assertEqual((a["revenue"], a["cost"], a["profit"]), (D("1500"), D("500"), D("1000")))
        self.assertEqual((b["revenue"], b["cost"], b["profit"]), (D("600"), D("400"), D("200")))
        self.assertEqual((both["revenue"], both["cost"]), (a["revenue"] + b["revenue"], a["cost"] + b["cost"]))
        page = self.client.get(reverse("report_sales"), {"date_from": "2026-01-01", "date_to": "2026-12-31",
                                                         "organization": self.b.pk})
        self.assertEqual(page.context["summary"]["profit"], D("200"))

    def test_inventory_surplus_belongs_to_document_organization(self):
        from .models import StockAdjustment

        surplus = StockAdjustment.objects.create(kind=StockAdjustment.KIND_INCOME, organization=self.b,
                                                 warehouse=self.w, **self._when())
        surplus.lines.create(product=self.product, quantity=2, price=50)
        surplus.post()
        self.assertEqual(self.balances(), {("Б", "W"): (D("2"), D("100.00"))})


# ---------------------------------------------------------------------------
# Серийные номера
# ---------------------------------------------------------------------------

class SerialOrganizationTests(OrgStockBase):
    def setUp(self):
        super().setUp()
        self.product.track_serials = True
        self.product.save()

    def test_same_physical_unit_cannot_be_received_by_other_organization(self):
        self.receipt(self.a, 1, 100, serial="SN-1")
        duplicate = self.receipt(self.b, 1, 100, serial="SN-1", post=False)
        with self.assertRaisesMessage(StockError, "уже числится на складе"):
            duplicate.post()
        self.assertEqual(SerialNumber.objects.get(number="SN-1").organization_id, self.a.pk)

    def test_owner_moves_and_sells_and_history_keeps_organization(self):
        self.receipt(self.a, 1, 100, wh=self.w1, serial="SN-1")
        self.transfer(self.a, self.w1, self.w2, 1, serial="SN-1")
        self.ship(self.a, 1, wh=self.w2, serial="SN-1")
        moves = list(SerialMovement.objects.order_by(*SerialMovement.ORDER).values_list(
            "doc_type", "organization_id", "warehouse_id", "quantity"))
        self.assertEqual(moves, [("receipt", self.a.pk, self.w1.pk, 1), ("transfer", self.a.pk, self.w1.pk, -1),
                                 ("transfer", self.a.pk, self.w2.pk, 1), ("shipment", self.a.pk, self.w2.pk, -1)])
        serial = SerialNumber.objects.get(number="SN-1")
        self.assertEqual((serial.organization_id, serial.status), (None, SerialNumber.STATUS_SOLD))

    def test_number_entered_as_stock_requires_organization(self):
        self.product.track_serials = False
        self.product.save()
        self.receipt(self.a, 1, 100)
        self.receipt(self.b, 1, 100)
        self.product.track_serials = True
        self.product.save()
        with self.assertRaisesMessage(StockError, "Укажите организацию"):
            serials.enter_stock(self.product, self.w, "OLD-1", None)
        serials.enter_stock(self.product, self.w, "OLD-1", self.b)
        self.assertEqual(serials.available(self.product.pk, self.w.pk, self.a.pk), [])
        self.assertEqual(serials.available(self.product.pk, self.w.pk, self.b.pk), ["OLD-1"])
        with self.assertRaisesMessage(StockError, "без номеров 0 шт."):
            serials.enter_stock(self.product, self.w, "OLD-2", self.b)

    def test_unknown_owner_blocks_consumption_until_decided(self):
        self.product.track_serials = False
        self.product.save()
        self.receipt(self.a, 1, 100)
        self.product.track_serials = True
        self.product.save()
        serials.enter_stock(self.product, self.w, "OLD-1", self.a)
        SerialMovement.objects.update(organization=None)           # как старый ввод без организации
        SerialNumber.objects.update(organization=None)
        self.clock = datetime.datetime.now() + datetime.timedelta(days=1)   # после ввода номера
        shipment = self.ship(self.a, 1, serial="OLD-1", post=False)
        with self.assertRaises(StockError):
            shipment.post()
        transition.assign_serial(self.product.pk, "OLD-1", self.a.pk)   # решение человека
        Shipment.objects.get(pk=shipment.pk).post()
        self.assertEqual(SerialNumber.objects.get(number="OLD-1").status, SerialNumber.STATUS_SOLD)


# ---------------------------------------------------------------------------
# Готовность после миграции: явный статус, прерванный и повторный пересчёт
# ---------------------------------------------------------------------------

class TransitionTests(OrgStockBase):
    def simulate_migration(self):
        """Как после миграций 0007–0009: движения с организациями, остатков нет,
        статус «ожидает пересчёта»."""
        StockBalance.objects.all().delete()
        StockLedgerState.objects.update_or_create(pk=1, defaults={"status": StockLedgerState.STATUS_PENDING})

    def test_empty_warehouse_is_ready_but_pending_status_blocks_operations(self):
        self.assertTrue(transition.is_ready())                 # пустая база после миграций — «готово»
        self.receipt(self.a, 2, 100)
        self.simulate_migration()
        self.assertFalse(transition.is_ready())
        draft = self.ship(self.a, 1, post=False)
        self.assertRejected(draft.post, "ещё не пересчитан по организациям")
        user = get_user_model().objects.create_superuser("adm", password="pass12345")
        self.client.force_login(user)
        page = self.client.get(reverse("balance_list"))
        self.assertContains(page, "ещё не пересчитан по организациям")
        self.assertNotContains(page, "Остатков нет")
        report = self.client.get(reverse("report_sales"))
        self.assertContains(report, "ещё не пересчитана по организациям")
        stock = self.client.get(reverse("api_product_stock"), {"ids": self.product.pk, "warehouse": self.w.pk,
                                                               "organization": self.a.pk}).json()
        self.assertIsNone(stock["stock"][str(self.product.pk)]["stock"])

    def test_interrupted_recalculation_and_safe_rerun(self):
        second = Product.objects.create(name="Второй", unit=self.unit)
        self.receipt(self.a, 3, 100)
        self.receipt(self.b, 2, 50, product=second)
        expected = self.balances(), self.balances(second)
        self.simulate_migration()
        original = costing.recalculate

        def broken(product_ids):
            if second.pk in product_ids:
                raise RuntimeError("сбой во время пересчёта")
            return original(product_ids)

        with mock.patch.object(costing, "recalculate", broken):
            with self.assertRaises(RuntimeError):
                transition.apply()
        state = StockLedgerState.objects.get(pk=1)
        self.assertEqual((state.status, state.products_done, state.products_total),
                         (StockLedgerState.STATUS_RUNNING, 1, 2))
        self.assertFalse(transition.is_ready())                # прерванный пересчёт — не готово
        self.assertRejected(self.ship(self.a, 1, post=False).post, "ещё не пересчитан")
        transition.apply()                                     # повтор — с начала, тот же результат
        self.assertTrue(transition.is_ready())
        self.assertEqual((self.balances(), self.balances(second)), expected)
        with self.assertRaises(transition.TransitionError):
            transition.apply()                                 # уже готово

    def test_split_violation_blocks_product_until_fixed_entirely(self):
        """Старые данные: Б (минус запрещён) продала единицу А. После перехода —
        нарушение; операции с товаром отклоняются; снятие отгрузки Б — проходит."""
        self.receipt(self.a, 1, 100)
        ship_b = self.ship(self.b, 1, post=False)
        ship_b.post(allow_negative=True)                       # как в старой программе: проведена
        self.simulate_migration()
        output = self._command()
        self.assertIn("Нарушения, которые создаёт разделение", output)
        self.assertIn("«Б», товар «Товар» на складе «W»", output)
        transition.apply()
        self.assertTrue(transition.is_ready())
        self.assertRejected(self.receipt(self.a, 1, 100, post=False).post, "расходовала чужой товар")
        Shipment.objects.get(pk=ship_b.pk).unpost()            # исправление: нарушения нет целиком
        self.receipt(self.a, 1, 100)
        self.assertEqual(self.balances(), {("А", "W"): (D("2"), D("200.00"))})

    def test_allowing_minus_resolves_split_violation_as_deficit(self):
        self.receipt(self.a, 1, 100)
        self.ship(self.b, 1, post=False).post(allow_negative=True)
        self.simulate_migration()
        transition.apply()
        self.negative(self.b, True)                            # решение человека: Б разрешён минус
        self.receipt(self.a, 1, 100)
        self.assertEqual(self.balances(), {("А", "W"): (D("2"), D("200.00")), ("Б", "W"): (D("-1"), D("0.00"))})

    def test_unresolved_source_blocks_recalculation_until_assigned(self):
        receipt = self.receipt(self.a, 1, 100)
        StockMovement.objects.filter(doc_id=receipt.pk).update(organization=None)
        self.simulate_migration()
        self.assertIn("движений 1 — --assign receipt:", self._command())
        with self.assertRaises(transition.TransitionError):
            transition.apply()
        from io import StringIO

        call_command("stock_transition", "--assign", f"receipt:{receipt.pk}={self.a.pk}", "--i-have-a-backup",
                     stdout=StringIO())
        transition.apply()
        self.assertEqual(self.balances(), {("А", "W"): (D("1"), D("100.00"))})

    def test_command_requires_backup_for_writes(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("stock_transition", "--apply")

    def _command(self):
        from io import StringIO

        out = StringIO()
        call_command("stock_transition", "--limit", "0", stdout=out)
        return out.getvalue()
