"""Контроль количества возвратов по документу-основанию.

Подтверждённая ошибка: принято 10, отгружена 1, по этой отгрузке созданы и
проведены два возврата по 1 — остаток становился 11.
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import transaction
from django.urls import reverse

from apps.catalog.models import Product
from apps.core import roles
from apps.core.models import Organization
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt, SupplierReturn
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment

from .models import StockBalance, StockMovement
from .services import StockError
from .tests_costing import JAN, NOON, legacy
from .tests_costing_rules import RulesBase

D = Decimal


class ReturnsBase(RulesBase):
    def setUp(self):
        super().setUp()
        self.phone = Product.objects.create(name="Телефон", unit=self.unit, track_serials=True)
        self.other_customer = Counterparty.objects.create(name="Другой покупатель",
                                                          partner_type=Counterparty.TYPE_CUSTOMER)
        self.other_supplier = Counterparty.objects.create(name="Другой поставщик",
                                                          partner_type=Counterparty.TYPE_SUPPLIER)
        self.other_org = Organization.objects.create(name="Вторая организация", allow_negative_stock=False)

    def supplier_return(self, day, qty, receipt=None, post=True, product=None, serials="", supplier=None):
        doc = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh,
                                            supplier=supplier or self.supplier, receipt=receipt,
                                            date=JAN[day], time=NOON)
        doc.lines.create(product=product or self.product, quantity=qty, price=100, serial_numbers=serials)
        if post:
            doc.post()
        return doc

    def serial_receipt(self, day, numbers):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                     date=JAN[day], time=NOON)
        doc.lines.create(product=self.phone, quantity=len(numbers), price=1000, serial_numbers="\n".join(numbers))
        doc.post()
        return doc

    def serial_shipment(self, day, numbers):
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                      date=JAN[day], time=NOON)
        doc.lines.create(product=self.phone, quantity=len(numbers), price=2000, serial_numbers="\n".join(numbers))
        doc.post()
        return doc

    def serial_return(self, day, numbers, shipment, warehouse=None, post=True):
        doc = CustomerReturn.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                            customer=self.customer, shipment=shipment, date=JAN[day], time=NOON)
        doc.lines.create(product=self.phone, quantity=len(numbers), price=2000, serial_numbers="\n".join(numbers))
        if post:
            doc.post()
        return doc

    def stock(self, product=None, warehouse=None):
        bal = StockBalance.objects.filter(product=product or self.product, warehouse=warehouse or self.wh).first()
        return bal.quantity if bal else D("0")

    def login(self):
        user = get_user_model().objects.create_user("adm", password="pass12345")
        user.groups.add(Group.objects.get(name=roles.ROLE_ADMIN))
        self.client.login(username="adm", password="pass12345")

    def check_returns(self):
        out = StringIO()
        call_command("check_returns", stdout=out)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Возвраты покупателей по отгрузке
# ---------------------------------------------------------------------------

class CustomerReturnLimitTests(ReturnsBase):
    def setUp(self):
        super().setUp()
        self.receipt(1, 10, 100)

    def test_second_return_of_the_only_sold_unit_is_rejected(self):
        """Воспроизведение: продана 1, первый возврат проходит, второй — нет."""
        shipment = self.shipment(2, 1)
        first = self.customer_return(3, 1, shipment=shipment, post=False)
        second = self.customer_return(3, 1, shipment=shipment, post=False)
        first.post()
        self.assertEqual(self.stock(), D("10"))
        self.assert_rejected(second.post, "доступно к возврату — 0")
        self.assertEqual(self.stock(), D("10"))            # было 11
        self.assertEqual(CustomerReturn.objects.get(pk=second.pk).status, "draft")
        self.assertFalse(StockMovement.objects.filter(doc_type="customer_return", doc_id=second.pk).exists())

    def test_message_names_product_and_quantities(self):
        shipment = self.shipment(2, 5)
        self.customer_return(3, 3, shipment=shipment)
        extra = self.customer_return(4, 4, shipment=shipment, post=False)
        with self.assertRaises(StockError) as caught:
            extra.post()
        message = str(caught.exception)
        self.assertIn(f"возврат покупателя № {extra.number}", message)
        self.assertIn("товар «Кабель»", message)
        self.assertIn(f"в отгрузке № {shipment.number} от 02.01.2026 — 5", message)
        self.assertIn("уже возвращено другими документами — 3", message)
        self.assertIn("доступно к возврату — 2", message)
        self.assertIn("в этом возврате — 4", message)

    def test_partial_returns_up_to_exact_quantity(self):
        shipment = self.shipment(2, 5)
        for day, qty in ((3, 2), (4, 2), (5, 1)):
            self.customer_return(day, qty, shipment=shipment)
        self.assertEqual(self.stock(), D("10"))
        extra = self.customer_return(6, 1, shipment=shipment, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")

    def test_fractional_quantities_and_several_lines_of_one_product(self):
        shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                           date=JAN[2], time=NOON)
        shipment.lines.create(product=self.product, quantity=D("1.500"), price=500)
        shipment.lines.create(product=self.product, quantity=D("2.250"), price=400)
        shipment.post()                                    # всего 3.75
        back = self.customer_return(3, D("1.000"), shipment=shipment, post=False)
        back.lines.create(product=self.product, quantity=D("1.500"), price=500)
        back.post()                                        # 2.5 двумя строками
        over = self.customer_return(4, D("1.251"), shipment=shipment, post=False)
        self.assert_rejected(over.post, "доступно к возврату — 1.25, в этом возврате — 1.251")
        self.customer_return(4, D("1.250"), shipment=shipment)
        self.assertEqual(self.stock(), D("10"))
        tiny = self.customer_return(5, D("0.001"), shipment=shipment, post=False)
        self.assert_rejected(tiny.post, "доступно к возврату — 0")

    def test_drafts_do_not_reserve_and_are_checked_in_turn(self):
        shipment = self.shipment(2, 5)
        drafts = [self.customer_return(3 + i, 3, shipment=shipment, post=False) for i in range(3)]
        drafts[0].post()                                   # черновики остальных не мешали
        self.assert_rejected(drafts[1].post, "доступно к возврату — 2, в этом возврате — 3")
        drafts[1].lines.update(quantity=2)
        drafts[1].post()
        self.assert_rejected(drafts[2].post, "доступно к возврату — 0")
        self.assertEqual(self.stock(), D("10"))

    def test_repost_without_changes_is_not_rejected(self):
        shipment = self.shipment(2, 1)
        back = self.customer_return(3, 1, shipment=shipment)
        before = self.snapshot()
        back.post()
        CustomerReturn.objects.get(pk=back.pk).post()
        self.assertEqual([row[1:] for row in self.snapshot()[0]], [row[1:] for row in before[0]])
        self.assertEqual(self.stock(), D("10"))

    def test_increasing_posted_return_beyond_limit_is_rolled_back(self):
        shipment = self.shipment(2, 5)
        back = self.customer_return(3, 2, shipment=shipment)
        self.customer_return(4, 2, shipment=shipment)
        self.assert_rejected(lambda: self.edit_and_repost(back, quantity=D("4")),
                             "доступно к возврату — 3, в этом возврате — 4")
        self.assertEqual(back.lines.get().quantity, D("2"))
        self.edit_and_repost(back, quantity=D("3"))        # до предела — можно
        self.assertEqual(self.stock(), D("10"))

    def test_decreasing_posted_return_frees_quantity(self):
        shipment = self.shipment(2, 5)
        back = self.customer_return(3, 5, shipment=shipment)
        extra = self.customer_return(4, 2, shipment=shipment, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")
        self.edit_and_repost(back, quantity=D("3"))
        extra.post()
        self.assertEqual(self.stock(), D("10"))

    def test_unposting_and_deleting_return_free_quantity(self):
        shipment = self.shipment(2, 1)
        first = self.customer_return(3, 1, shipment=shipment)
        second = self.customer_return(4, 1, shipment=shipment, post=False)
        self.assert_rejected(second.post, "доступно к возврату — 0")
        first.unpost()
        second.post()
        third = self.customer_return(5, 1, shipment=shipment, post=False)
        self.assert_rejected(third.post, "доступно к возврату — 0")
        second.delete()
        third.post()
        self.assertEqual(self.stock(), D("10"))

    def test_basis_cannot_drop_below_returned(self):
        shipment = self.shipment(2, 5)
        self.customer_return(3, 3, shipment=shipment)
        prefix = "На документ ссылается проведённый возврат"
        self.assert_rejected(lambda: self.edit_and_repost(shipment, quantity=D("2")), "уже возвращено 3")
        self.assert_rejected(shipment.unpost, prefix)
        self.assert_rejected(shipment.delete, prefix)
        self.assertEqual(Shipment.objects.get(pk=shipment.pk).lines.get().quantity, D("5"))
        self.edit_and_repost(shipment, quantity=D("3"))    # ровно возвращённое — можно
        self.edit_and_repost(shipment, quantity=D("7"))    # увеличение — можно
        self.assertEqual(self.stock(), D("6"))             # 10 − 7 + 3

    def test_removing_returned_product_from_basis_is_rejected(self):
        self.receipt(1, 10, 50, product=self.other)
        shipment = self.shipment(2, 5)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with shipment.editing(products=[self.other.pk]):
            shipment.lines.create(product=self.other, quantity=1, price=500)
        self.customer_return(3, 1, shipment=shipment)

        def remove():
            with shipment.editing():
                shipment.lines.filter(product=self.product).delete()

        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): на удаляемую строку ссылается строка проведённого
        # возврата — отказ теперь раньше проверки количества (связь возврата пропала бы мимо его
        # перепроведения). Отказ и откат — как прежде; было «после операции в документе не останется».
        self.assert_rejected(remove, "На документ ссылается проведённый возврат покупателя")
        self.assertEqual(shipment.lines.count(), 2)

    def test_other_product_is_rejected(self):
        self.receipt(1, 10, 50, product=self.other)
        shipment = self.shipment(2, 5)
        wrong = self.customer_return(3, 1, shipment=shipment, post=False, product=self.other)
        self.assert_rejected(wrong.post, "не содержит товар «Провод»")

    def test_other_customer_is_rejected(self):
        shipment = self.shipment(2, 5)
        wrong = self.customer_return(3, 1, shipment=shipment, post=False)
        wrong.customer = self.other_customer
        wrong.save()
        self.assert_rejected(CustomerReturn.objects.get(pk=wrong.pk).post,
                             "покупатель возврата («Другой покупатель») не совпадает")

    def test_other_organization_is_rejected(self):
        shipment = self.shipment(2, 5)
        wrong = self.customer_return(3, 1, shipment=shipment, post=False)
        wrong.organization = self.other_org
        wrong.save()
        self.assert_rejected(CustomerReturn.objects.get(pk=wrong.pk).post, "организация возврата")

    def test_changing_customer_of_basis_with_returns_is_rejected(self):
        shipment = self.shipment(2, 5)
        self.customer_return(3, 1, shipment=shipment)
        self.assert_rejected(lambda: self.edit_and_repost(shipment, customer=self.other_customer),
                             "организация и покупатель должны совпадать")

    def test_unposted_basis_is_rejected(self):
        draft = self.draft_shipment(2, 5)
        back = self.customer_return(3, 1, shipment=draft, post=False)
        self.assert_rejected(back.post, "не проведена")

    def test_return_to_another_warehouse_is_allowed_and_counted(self):
        shipment = self.shipment(2, 2)
        self.customer_return(3, 2, shipment=shipment, warehouse=self.wh2)
        self.assertEqual((self.stock(), self.stock(warehouse=self.wh2)), (D("8"), D("2")))
        extra = self.customer_return(4, 1, shipment=shipment, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")   # склад другой, основание то же

    def test_return_without_basis_is_not_limited(self):
        """Возврат без основания — отдельный прежний сценарий, ни к чему не привязывается."""
        self.shipment(2, 1)
        first, second = self.customer_return(3, 1), self.customer_return(4, 1)
        self.assertEqual(self.stock(), D("11"))
        self.assertIsNone(first.shipment_id)
        self.assertIsNone(CustomerReturn.objects.get(pk=second.pk).shipment_id)

    def test_switching_posted_return_to_another_basis_is_checked_in_full(self):
        """Послабление «не больше прежнего» не переносится на другое основание."""
        big = self.shipment(2, 3)
        small = self.shipment(2, 1, time=datetime.time(13, 0))
        back = self.customer_return(3, 3, shipment=big)
        loaded = CustomerReturn.objects.get(pk=back.pk)

        def switch():
            # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
            # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
            # отдельно. Учётные ожидания прежние.
            with loaded.editing():                 # сохранение очищает связи строк с прежней отгрузкой
                loaded.shipment = small
                loaded.save()

        self.assert_rejected(switch, "доступно к возврату — 1, в этом возврате — 3")
        self.assertEqual(CustomerReturn.objects.get(pk=back.pk).shipment_id, big.pk)

    def test_returns_of_one_product_do_not_use_up_another(self):
        self.receipt(1, 10, 50, product=self.other)
        shipment = self.shipment(2, 2)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with shipment.editing(products=[self.other.pk]):
            shipment.lines.create(product=self.other, quantity=3, price=500)
        self.customer_return(3, 2, shipment=shipment)
        self.customer_return(4, 3, shipment=shipment, product=self.other)
        over = self.customer_return(5, 1, shipment=shipment, post=False, product=self.other)
        self.assert_rejected(over.post, "товар «Провод»")


class CustomerReturnCostTests(ReturnsBase):
    """Точный расчёт стоимости предыдущего этапа сохраняется."""

    def setUp(self):
        super().setUp()
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                     date=JAN[1], time=NOON)
        doc.lines.create(product=self.product, quantity=3, price=50, discount=D("33.333"))   # три единицы на 100.00
        doc.post()
        self.shipment_doc = self.shipment(2, 3)
        self.assertEqual(self.shipment_doc.lines.get().cost_amount, D("100.00"))

    def amounts(self, *docs):
        return [StockMovement.objects.get(doc_type="customer_return", doc_id=doc.pk).amount for doc in docs]

    def value(self):
        bal = StockBalance.objects.get(product=self.product, warehouse=self.wh)
        return bal.quantity, bal.value

    def test_three_units_for_hundred_returned_in_parts(self):
        returns = [self.customer_return(day, 1, shipment=self.shipment_doc) for day in (3, 4, 5)]
        self.assertEqual(self.amounts(*returns), [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.value(), (D("3"), D("100.00")))          # ровно стоимость отгрузки

    def test_rejected_extra_return_does_not_change_cost(self):
        returns = [self.customer_return(day, 1, shipment=self.shipment_doc) for day in (3, 4, 5)]
        extra = self.customer_return(6, 1, shipment=self.shipment_doc, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")
        self.assertEqual(self.amounts(*returns), [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.value(), (D("3"), D("100.00")))

    def test_unposting_one_return_recalculates_the_following(self):
        first, second, third = [self.customer_return(day, 1, shipment=self.shipment_doc) for day in (3, 4, 5)]
        first.unpost()
        self.assertEqual(self.amounts(second, third), [D("33.33"), D("33.34")])   # итог 33.33 → 66.67
        self.assertEqual(self.value(), (D("2"), D("66.67")))
        first.post()                                                    # вернулся на своё место по дате
        self.assertEqual(self.amounts(first, second, third), [D("33.33"), D("33.34"), D("33.33")])
        self.assertEqual(self.value(), (D("3"), D("100.00")))

    def test_reposting_a_return_keeps_amounts(self):
        returns = [self.customer_return(day, 1, shipment=self.shipment_doc) for day in (3, 4, 5)]
        for doc in returns:
            doc.post()
        self.assertEqual(self.amounts(*returns), [D("33.33"), D("33.34"), D("33.33")])

    def test_full_return_in_two_documents(self):
        first = self.customer_return(3, 2, shipment=self.shipment_doc)
        second = self.customer_return(4, 1, shipment=self.shipment_doc)
        self.assertEqual(self.amounts(first, second), [D("66.67"), D("33.33")])
        self.assertEqual(self.value(), (D("3"), D("100.00")))


class SerialReturnTests(ReturnsBase):
    def setUp(self):
        super().setUp()
        self.serial_receipt(1, ["A", "B", "C", "D"])
        self.sold = self.serial_shipment(2, ["A", "B"])
        self.other_sale = self.serial_shipment(2, ["D"])

    def test_only_numbers_of_the_basis_can_be_returned(self):
        wrong = self.serial_return(3, ["D"], self.sold, post=False)    # продан, но другой отгрузкой
        self.assert_rejected(wrong.post, "номера D нет в отгрузке")
        self.serial_return(3, ["A"], self.sold)
        self.assertEqual(self.stock(self.phone), D("2"))               # C и вернувшийся A

    def test_returned_number_cannot_be_returned_again(self):
        first = self.serial_return(3, ["A"], self.sold)
        again = self.serial_return(4, ["A"], self.sold, post=False)
        self.assert_rejected(again.post, f"номер A уже возвращён документом № {first.number}")
        self.serial_return(4, ["B"], self.sold)
        self.assertEqual(self.stock(self.phone), D("3"))

    def test_quantity_limit_does_not_replace_serial_check(self):
        """Количество ещё доступно (продано 2, возвращён 1), но номер — уже нет."""
        self.serial_return(3, ["A"], self.sold)
        again = self.serial_return(4, ["A"], self.sold, post=False)
        with self.assertRaisesMessage(StockError, "Серийные номера"):
            again.post()

    def test_serial_return_to_another_warehouse(self):
        self.serial_return(3, ["B"], self.sold, warehouse=self.wh2)
        self.assertEqual(self.stock(self.phone, self.wh2), D("1"))

    def test_draft_from_basis_takes_only_numbers_not_yet_returned(self):
        self.serial_return(3, ["A"], self.sold)
        self.login()
        self.client.post(reverse("shipment_to_return", args=[self.sold.pk]))
        draft = CustomerReturn.objects.filter(shipment=self.sold, status="draft").get()
        line = draft.lines.get()
        self.assertEqual((line.quantity, line.serial_numbers), (D("1"), "B"))
        draft.date, draft.time = JAN[4], NOON
        draft.save()
        draft.post()
        self.assertEqual(self.stock(self.phone), D("3"))


class CreateReturnFromBasisTests(ReturnsBase):
    def setUp(self):
        super().setUp()
        self.receipt(1, 10, 100)
        self.sale = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                            date=JAN[2], time=NOON)
        self.sale.lines.create(product=self.product, quantity=2, price=100, discount=10, vat_rate="none")
        self.sale.lines.create(product=self.product, quantity=3, price=200, discount=0, vat_rate="20")
        self.sale.post()
        self.login()

    def create(self):
        return self.client.post(reverse("shipment_to_return", args=[self.sale.pk]), follow=True)

    def lines(self, doc):
        return list(doc.lines.order_by("pk").values_list("quantity", "price", "discount", "vat_rate"))

    def test_first_return_copies_everything(self):
        self.create()
        draft = CustomerReturn.objects.get()
        self.assertEqual(self.lines(draft), [
            (D("2"), D("100"), D("10"), "none"), (D("3"), D("200"), D("0"), "20"),
        ])
        self.assertEqual(draft.total, self.sale.total)

    def test_next_return_gets_only_remaining_quantity(self):
        self.customer_return(3, 3, shipment=self.sale)                 # 3 из 5 уже возвращено
        self.create()
        draft = CustomerReturn.objects.filter(status="draft").get()
        # первая строка (2 шт.) закрыта целиком, от второй осталось 2 — цена, скидка и НДС те же
        self.assertEqual(self.lines(draft), [(D("2"), D("200"), D("0"), "20")])

    def test_drafts_do_not_reduce_prefill(self):
        self.create()
        self.create()
        first, second = CustomerReturn.objects.order_by("pk")
        self.assertEqual(self.lines(first), self.lines(second))
        for doc in (first, second):
            CustomerReturn.objects.filter(pk=doc.pk).update(date=JAN[3])
        CustomerReturn.objects.get(pk=first.pk).post()
        with self.assertRaisesMessage(StockError, "доступно к возврату — 0"):   # окончательная проверка
            CustomerReturn.objects.get(pk=second.pk).post()

    def test_fully_returned_basis_gives_message_instead_of_document(self):
        self.customer_return(3, 5, shipment=self.sale)
        response = self.create()
        self.assertContains(response, "уже возвращено всё количество — новый возврат не создан")
        self.assertEqual(CustomerReturn.objects.count(), 1)

    def test_editing_posted_return_through_form_is_atomic(self):
        back = self.customer_return(3, 3, shipment=self.sale)
        self.customer_return(4, 2, shipment=self.sale)
        line = back.lines.get()
        before = self.full_state()
        response = self.client.post(reverse("customer_return_edit", args=[back.pk]), {
            "date": back.date.isoformat(), "number": back.number, "time": "12:00",
            "organization": self.org.pk, "warehouse": self.wh.pk, "customer": self.customer.pk,
            "comment": "", "action": "save",
            "lines-TOTAL_FORMS": 1, "lines-INITIAL_FORMS": 1, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
            "lines-0-id": line.pk, "lines-0-product": self.product.pk, "lines-0-quantity": "4",
            "lines-0-price": "500", "lines-0-discount": "0", "lines-0-vat_rate": "none", "lines-0-serial_numbers": "",
        })
        self.assertContains(response, "Изменения не сохранены")
        self.assertContains(response, "доступно к возврату — 3, в этом возврате — 4")
        self.assertEqual(self.full_state(), before)
        self.assertEqual(back.lines.get().quantity, D("3"))


# ---------------------------------------------------------------------------
# Возвраты поставщику по приёмке
# ---------------------------------------------------------------------------

class SupplierReturnLimitTests(ReturnsBase):
    def test_second_return_of_the_only_received_unit_is_rejected(self):
        """Принята 1 этой приёмкой (на складе есть ещё товар), два возврата по 1."""
        self.receipt(1, 10, 100)
        basis = self.receipt(2, 1, 100)
        first = self.supplier_return(3, 1, receipt=basis, post=False)
        second = self.supplier_return(3, 1, receipt=basis, post=False)
        first.post()
        self.assert_rejected(second.post, "доступно к возврату — 0")
        self.assertEqual(self.stock(), D("10"))
        self.assertEqual(SupplierReturn.objects.get(pk=second.pk).status, "draft")

    def test_message_names_receipt_and_quantities(self):
        basis = self.receipt(1, 5, 100)
        self.supplier_return(2, 2, receipt=basis)
        extra = self.supplier_return(3, 4, receipt=basis, post=False)
        with self.assertRaises(StockError) as caught:
            extra.post()
        message = str(caught.exception)
        self.assertIn(f"возврат поставщику № {extra.number}", message)
        self.assertIn(f"в приёмке № {basis.number} от 01.01.2026 — 5", message)
        self.assertIn("уже возвращено другими документами — 2, доступно к возврату — 3, в этом возврате — 4", message)

    def test_partial_fractional_and_several_lines(self):
        basis = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                       date=JAN[1], time=NOON)
        basis.lines.create(product=self.product, quantity=D("1.500"), price=100)
        basis.lines.create(product=self.product, quantity=D("2.250"), price=90)
        basis.post()
        self.supplier_return(2, D("2.500"), receipt=basis)
        over = self.supplier_return(3, D("1.251"), receipt=basis, post=False)
        self.assert_rejected(over.post, "доступно к возврату — 1.25")
        self.supplier_return(3, D("1.250"), receipt=basis)
        self.assertEqual(self.stock(), D("0"))

    def test_repost_increase_decrease_unpost_delete(self):
        self.receipt(1, 10, 100)
        basis = self.receipt(2, 5, 100)
        back = self.supplier_return(3, 2, receipt=basis)
        back.post()                                                     # повторно — без отказа
        self.assert_rejected(lambda: self.edit_and_repost(back, quantity=D("6")),
                             "доступно к возврату — 5, в этом возврате — 6")
        self.edit_and_repost(back, quantity=D("5"))
        extra = self.supplier_return(4, 1, receipt=basis, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")
        self.edit_and_repost(back, quantity=D("4"))
        extra.post()
        back.unpost()
        third = self.supplier_return(5, 4, receipt=basis)
        third.delete()
        self.supplier_return(6, 4, receipt=basis)
        self.assertEqual(self.stock(), D("10"))                         # 15 − 1 − 4

    def test_basis_cannot_drop_below_returned(self):
        self.receipt(1, 10, 100)
        basis = self.receipt(2, 5, 100)
        self.supplier_return(3, 3, receipt=basis)
        prefix = "На документ ссылается проведённый возврат"
        self.assert_rejected(lambda: self.edit_and_repost(basis, quantity=D("2")), "уже возвращено 3")
        self.assert_rejected(basis.unpost, prefix)
        self.assert_rejected(basis.delete, prefix)
        self.edit_and_repost(basis, quantity=D("3"))
        self.assertEqual(self.stock(), D("10"))

    def test_other_product_supplier_organization_and_draft_basis(self):
        self.receipt(1, 10, 100)
        self.receipt(1, 10, 100, product=self.other)
        basis = self.receipt(2, 5, 100)
        wrong_product = self.supplier_return(3, 1, receipt=basis, post=False, product=self.other)
        self.assert_rejected(wrong_product.post, "не содержит товар «Провод»")
        wrong_supplier = self.supplier_return(3, 1, receipt=basis, post=False, supplier=self.other_supplier)
        self.assert_rejected(wrong_supplier.post, "поставщик возврата («Другой поставщик») не совпадает")
        wrong_org = self.supplier_return(3, 1, receipt=basis, post=False)
        wrong_org.organization = self.other_org
        wrong_org.save()
        self.assert_rejected(SupplierReturn.objects.get(pk=wrong_org.pk).post, "организация возврата")
        draft_basis = self.receipt(2, 5, 100, post=False)
        from_draft = self.supplier_return(3, 1, receipt=draft_basis, post=False)
        self.assert_rejected(from_draft.post, "не проведена")

    def test_return_without_basis_is_not_limited(self):
        self.receipt(1, 10, 100)
        self.supplier_return(2, 4)
        self.supplier_return(3, 6)
        self.assertEqual(self.stock(), D("0"))

    def test_serial_numbers_must_come_from_the_receipt(self):
        basis = self.serial_receipt(1, ["A", "B"])
        self.serial_receipt(1, ["X"])
        wrong = self.supplier_return(2, 1, receipt=basis, post=False, product=self.phone, serials="X")
        self.assert_rejected(wrong.post, "номера X нет в приёмке")
        first = self.supplier_return(2, 1, receipt=basis, product=self.phone, serials="A")
        again = self.supplier_return(3, 1, receipt=basis, post=False, product=self.phone, serials="A")
        self.assert_rejected(again.post, f"номер A уже возвращён документом № {first.number}")
        self.supplier_return(3, 1, receipt=basis, product=self.phone, serials="B")

    def test_draft_from_receipt_takes_remaining_quantity_and_numbers(self):
        basis = self.serial_receipt(1, ["A", "B", "C"])
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with basis.editing(products=[self.product.pk]):
            basis.lines.create(product=self.product, quantity=10, price=80, discount=5, vat_rate="20")
        self.supplier_return(2, 1, receipt=basis, product=self.phone, serials="B")
        self.supplier_return(2, 4, receipt=basis)
        self.login()
        self.client.post(reverse("receipt_to_return", args=[basis.pk]))
        draft = SupplierReturn.objects.filter(status="draft").get()
        self.assertEqual(
            list(draft.lines.order_by("pk").values_list("product_id", "quantity", "price", "discount",
                                                        "vat_rate", "serial_numbers")),
            [(self.phone.pk, D("2"), D("1000"), D("0"), "20", "A\nC"),
             (self.product.pk, D("6"), D("80"), D("5"), "20", "")],
        )

    def test_fully_returned_receipt_gives_message(self):
        basis = self.receipt(1, 2, 100)
        self.supplier_return(2, 2, receipt=basis)
        self.login()
        response = self.client.post(reverse("receipt_to_return", args=[basis.pk]), follow=True)
        self.assertContains(response, "уже возвращено всё количество — новый возврат не создан")
        self.assertEqual(SupplierReturn.objects.count(), 1)


# ---------------------------------------------------------------------------
# Старые превышения
# ---------------------------------------------------------------------------

class LegacyExcessTests(ReturnsBase):
    """В базе уже есть возврат сверх основания (появился до проверки)."""

    def setUp(self):
        super().setUp()
        self.receipt(1, 10, 100)
        self.sale = self.shipment(2, 1)
        other_sale = self.shipment(2, 1, time=datetime.time(13, 0))
        self.first = self.customer_return(3, 1, shipment=self.sale)
        self.second = self.customer_return(4, 1, shipment=other_sale)
        # ПРАВИЛО ДОПОЛНЕНО (21.10.2026): основание проведённого возврата массово не меняют
        # (posting, правило 12) — старые данные имитируются прямой записью мимо программы
        CustomerReturn._base_manager.filter(pk=self.second.pk).update(shipment=self.sale)   # как будто так и было
        # в старых данных строка возврата не связана со строкой отгрузки (связь
        # ставит проведение и миграция sales 0008 — только по текущему основанию)
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).filter(document=self.second).update(shipment_line=None)
        self.second = CustomerReturn.objects.get(pk=self.second.pk)

    def test_diagnostics_lists_excess_and_writes_nothing(self):
        before = self.full_state()
        output = self.check_returns()
        self.assertIn("Возвращено больше, чем было в основании: 1", output)
        self.assertIn(f"отгрузка № {self.sale.number} от 02.01.2026 (id {self.sale.pk}), товар «Кабель»", output)
        self.assertIn("в основании 1, возвращено 2, превышение 1", output)
        self.assertIn(f"возврат покупателя № {self.first.number} от 03.01.2026 (id {self.first.pk}) — 1", output)
        self.assertIn(f"возврат покупателя № {self.second.number} от 04.01.2026 (id {self.second.pk}) — 1", output)
        self.assertIn("Ничего не изменено", output)
        self.assertEqual(self.full_state(), before)
        self.assertEqual(CustomerReturn.objects.filter(status="posted").count(), 2)   # документы на месте

    def test_clean_database_has_no_violations(self):
        self.second.unpost()
        self.assertIn("Нарушений нет", self.check_returns())

    def test_operations_that_do_not_increase_excess_are_allowed(self):
        self.second.post()                                             # перепроведение без изменений
        self.first.post()
        self.sale.post()                                               # основание без уменьшения
        self.edit_and_repost(self.sale, quantity=D("1.5"))             # основание увеличили
        self.edit_and_repost(self.second, quantity=D("0.5"))           # возврат уменьшили — превышения нет
        self.assertIn("Нарушений нет", self.check_returns())

    def test_operations_that_increase_excess_are_rejected(self):
        self.assert_rejected(lambda: self.edit_and_repost(self.second, quantity=D("2")), "в этом возврате — 2")
        self.assert_rejected(lambda: self.edit_and_repost(self.sale, quantity=D("0.5")), "уже возвращено 2")
        third = self.customer_return(5, 1, shipment=self.sale, post=False)
        self.assert_rejected(third.post, "доступно к возврату — 0")

    def test_excess_can_be_removed_by_unposting_or_deleting(self):
        self.second.unpost()
        self.assertEqual(self.stock(), D("9"))                         # 10 − 2 + 1
        self.second.delete()
        self.assertIn("Нарушений нет", self.check_returns())

    def test_party_mismatch_is_listed(self):
        CustomerReturn.objects.filter(pk=self.first.pk).update(customer=self.other_customer)
        output = self.check_returns()
        self.assertIn("Организация или контрагент возврата не совпадают с основанием: 1", output)
        self.assertIn("Другой покупатель", output)

    def test_supplier_side_excess_is_listed(self):
        basis = self.receipt(5, 1, 100)
        other = self.receipt(5, 1, 100, time=datetime.time(13, 0))
        self.supplier_return(6, 1, receipt=basis)
        extra = self.supplier_return(7, 1, receipt=other)
        # ПРАВИЛО ДОПОЛНЕНО (21.10.2026): основание проведённого возврата массово не меняют
        # (posting, правило 12) — старые данные имитируются прямой записью мимо программы
        SupplierReturn._base_manager.filter(pk=extra.pk).update(receipt=basis)
        output = self.check_returns()
        self.assertIn("Возвращено больше, чем было в основании: 2", output)
        self.assertIn(f"приёмка № {basis.number}", output)


# ---------------------------------------------------------------------------
# Заполнение серийного возврата при нескольких строках одного товара
# ---------------------------------------------------------------------------

class SerialPrefillTests(ReturnsBase):
    """Подтверждённая ошибка: отгрузка двумя строками (A и B), возвращён B —
    следующий возврат «на основании» получался пустым вместо строки с A."""

    def setUp(self):
        super().setUp()
        self.login()

    def sale(self, lines, day=2):
        """lines: [(номера, цена, скидка)] — строки одного серийного товара."""
        doc = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                      date=JAN[day], time=NOON)
        for numbers, price, discount in lines:
            doc.lines.create(product=self.phone, quantity=len(numbers), price=price, discount=discount,
                             vat_rate="20", serial_numbers="\n".join(numbers))
        doc.post()
        return doc

    def purchase(self, lines, day=1):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                     date=JAN[day], time=NOON)
        for numbers, price, discount in lines:
            doc.lines.create(product=self.phone, quantity=len(numbers), price=price, discount=discount,
                             vat_rate="20", serial_numbers="\n".join(numbers))
        doc.post()
        return doc

    def draft_from_shipment(self, shipment):
        """Создание возврата через HTTP-обработчик; возвращает (ответ, новый черновик или None)."""
        known = set(CustomerReturn.objects.values_list("pk", flat=True))
        response = self.client.post(reverse("shipment_to_return", args=[shipment.pk]), follow=True)
        return response, CustomerReturn.objects.exclude(pk__in=known).first()

    def draft_from_receipt(self, receipt):
        known = set(SupplierReturn.objects.values_list("pk", flat=True))
        response = self.client.post(reverse("receipt_to_return", args=[receipt.pk]), follow=True)
        return response, SupplierReturn.objects.exclude(pk__in=known).first()

    def rows(self, doc):
        return list(doc.lines.order_by("pk").values_list("quantity", "serial_numbers", "price", "discount", "vat_rate"))

    def post_on(self, doc, day):
        type(doc).objects.filter(pk=doc.pk).update(date=JAN[day], time=NOON)
        doc = type(doc).objects.get(pk=doc.pk)
        doc.post()
        return doc

    # ----- отгрузка → возврат покупателя -----

    def test_two_lines_a_and_b_with_b_returned_first(self):
        """Воспроизведение: раньше черновик не создавался вовсе."""
        self.purchase([(["A", "B"], 1000, 0)])
        shipment = self.sale([(["A"], 2000, 0), (["B"], 1800, 10)])
        self.serial_return(3, ["B"], shipment)

        response, draft = self.draft_from_shipment(shipment)
        self.assertIsNotNone(draft)
        self.assertNotContains(response, "уже возвращено всё количество")
        # осталась строка с A — со своей ценой и скидкой, а не строки B
        self.assertEqual(self.rows(draft), [(D("1"), "A", D("2000"), D("0"), "20")])

        self.post_on(draft, 4)                                         # черновик проводится
        self.assertEqual(self.stock(self.phone), D("2"))

        response, again = self.draft_from_shipment(shipment)           # возвращено всё
        self.assertIsNone(again)
        self.assertContains(response, "уже возвращено всё количество — новый возврат не создан")
        self.assertEqual(CustomerReturn.objects.count(), 2)

    def test_a_returned_first_leaves_b_with_its_own_price(self):
        self.purchase([(["A", "B"], 1000, 0)])
        shipment = self.sale([(["A"], 2000, 0), (["B"], 1800, 10)])
        self.serial_return(3, ["A"], shipment)
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual(self.rows(draft), [(D("1"), "B", D("1800"), D("10"), "20")])
        self.assertEqual(draft.total, D("1620.00"))                    # 1800 − 10 %

    def test_several_numbers_per_line_returned_in_mixed_order(self):
        self.purchase([(["A", "B", "C", "D", "E"], 1000, 0)])
        shipment = self.sale([(["A", "B", "C"], 2000, 0), (["D", "E"], 1500, 5)])
        self.serial_return(3, ["D"], shipment)                         # сначала из второй строки
        self.serial_return(4, ["B"], shipment)                         # потом из середины первой
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual(self.rows(draft), [
            (D("2"), "A\nC", D("2000"), D("0"), "20"),
            (D("1"), "E", D("1500"), D("5"), "20"),
        ])
        draft.delete()

        self.serial_return(5, ["E", "C"], shipment)                    # одним документом из обеих строк
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual(self.rows(draft), [(D("1"), "A", D("2000"), D("0"), "20")])
        self.post_on(draft, 6)
        self.assertEqual(self.stock(self.phone), D("5"))
        _, nothing = self.draft_from_shipment(shipment)
        self.assertIsNone(nothing)

    def test_nothing_returned_yet_copies_all_lines(self):
        self.purchase([(["A", "B"], 1000, 0)])
        shipment = self.sale([(["A"], 2000, 0), (["B"], 1800, 10)])
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual(self.rows(draft), [
            (D("1"), "A", D("2000"), D("0"), "20"), (D("1"), "B", D("1800"), D("10"), "20"),
        ])

    def test_drafts_of_returns_do_not_hide_numbers(self):
        self.purchase([(["A", "B"], 1000, 0)])
        shipment = self.sale([(["A"], 2000, 0), (["B"], 1800, 10)])
        self.serial_return(3, ["B"], shipment, post=False)             # черновик — номер ещё доступен
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual([row[1] for row in self.rows(draft)], ["A", "B"])

    def test_serial_and_plain_products_in_one_shipment(self):
        """Серийный товар считается по номерам, обычный — по количеству; друг другу не мешают."""
        self.purchase([(["A", "B"], 1000, 0)])
        self.receipt(1, 10, 100)
        shipment = self.sale([(["A"], 2000, 0), (["B"], 1800, 10)])
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with shipment.editing(products=[self.product.pk]):
            shipment.lines.create(product=self.product, quantity=2, price=300, discount=0, vat_rate="none")
            shipment.lines.create(product=self.product, quantity=3, price=250, discount=4, vat_rate="none")
        self.serial_return(3, ["B"], shipment)
        self.customer_return(3, 3, shipment=shipment)                  # 3 из 5 обычного товара
        _, draft = self.draft_from_shipment(shipment)
        self.assertEqual(self.rows(draft), [
            (D("1"), "A", D("2000"), D("0"), "20"),
            (D("2"), "", D("250"), D("4"), "none"),                    # первая строка (2 шт.) закрыта, от второй — 2
        ])

    def test_serial_product_with_a_line_without_numbers(self):
        """Старые данные: учёт по номерам включили позже, в одной строке номера
        есть, в другой нет. Возврат с номером считается по номеру и из общего
        количества исключается; возврат без номера — по количеству."""
        from . import return_limits
        from apps.core.based_on import LINE_FIELDS

        gadget = Product.objects.create(name="Гаджет", unit=self.unit)       # пока без учёта номеров
        self.receipt(1, 5, 100, product=gadget)
        shipment = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                           date=JAN[2], time=NOON)
        plain = shipment.lines.create(product=gadget, quantity=2, price=300)
        numbered = shipment.lines.create(product=gadget, quantity=1, price=500)
        shipment.post()
        without_number = self.customer_return(3, 1, shipment=shipment, product=gadget)
        with_number = self.customer_return(4, 1, shipment=shipment, product=gadget)
        Product.objects.filter(pk=gadget.pk).update(track_serials=True)
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(type(numbered)).filter(pk=numbered.pk).update(serial_numbers="A")
        legacy(CustomerReturnLine).filter(document=with_number).update(serial_numbers="A")

        rows = return_limits.remaining_rows(Shipment.objects.get(pk=shipment.pk), LINE_FIELDS)
        # строка без номеров: 2 − 1 (возврат без номера); строка с A возвращена целиком
        self.assertEqual([(r["quantity"], r["serial_numbers"], r["price"]) for r in rows],
                         [(D("1"), "", D("300"))])
        self.assertEqual(plain.quantity, D("2"))
        self.assertIsNotNone(without_number.pk)

    # ----- приёмка → возврат поставщику -----

    def test_receipt_with_two_lines_a_and_b_with_b_returned_first(self):
        receipt = self.purchase([(["A"], 1000, 0), (["B"], 900, 10)])
        self.supplier_return(2, 1, receipt=receipt, product=self.phone, serials="B")

        response, draft = self.draft_from_receipt(receipt)
        self.assertIsNotNone(draft)
        self.assertEqual(self.rows(draft), [(D("1"), "A", D("1000"), D("0"), "20")])

        self.post_on(draft, 3)
        self.assertEqual(self.stock(self.phone), D("0"))
        response, again = self.draft_from_receipt(receipt)
        self.assertIsNone(again)
        self.assertContains(response, "уже возвращено всё количество — новый возврат не создан")
        self.assertEqual(SupplierReturn.objects.count(), 2)

    def test_receipt_with_several_numbers_per_line(self):
        receipt = self.purchase([(["A", "B", "C"], 1000, 0), (["D", "E"], 900, 10)])
        self.supplier_return(2, 1, receipt=receipt, product=self.phone, serials="E")
        self.supplier_return(3, 2, receipt=receipt, product=self.phone, serials="C\nA")
        _, draft = self.draft_from_receipt(receipt)
        self.assertEqual(self.rows(draft), [
            (D("1"), "B", D("1000"), D("0"), "20"), (D("1"), "D", D("900"), D("10"), "20"),
        ])
        self.post_on(draft, 4)
        self.assertEqual(self.stock(self.phone), D("0"))


# ---------------------------------------------------------------------------
# Возврат поставщику стоит после своей приёмки
# ---------------------------------------------------------------------------

class SupplierReturnChronologyTests(ReturnsBase):
    def setUp(self):
        super().setUp()
        self.early = self.receipt(1, 10, 100)              # обеспечивает остаток
        self.basis = self.receipt(5, 5, 120)               # основание возврата — позже

    def dated_return(self, day, time=NOON, qty=1, post=True):
        doc = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                            receipt=self.basis, date=JAN[day], time=time)
        doc.lines.create(product=self.product, quantity=qty, price=120)
        if post:
            doc.post()
        return doc

    def save_receipt_form(self, receipt, **header):
        line = receipt.lines.get()
        data = {
            "date": receipt.date.isoformat(), "number": receipt.number, "time": receipt.time.strftime("%H:%M"),
            "organization": receipt.organization_id, "warehouse": receipt.warehouse_id,
            "supplier": receipt.supplier_id, "comment": "", "action": "save",
            "lines-TOTAL_FORMS": 1, "lines-INITIAL_FORMS": 1, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
            "lines-0-id": line.pk, "lines-0-product": line.product_id, "lines-0-quantity": line.quantity,
            "lines-0-price": line.price, "lines-0-discount": "0", "lines-0-vat_rate": "none",
            "lines-0-serial_numbers": "",
        }
        data.update(header)
        return self.client.post(reverse("receipt_edit", args=[receipt.pk]), data)

    def test_return_dated_before_its_receipt_is_rejected_despite_enough_stock(self):
        """На складе 10 шт. из ранней приёмки, но основание возврата — приёмка
        от 5 января, а возврат датирован 3 января."""
        back = self.dated_return(3, post=False)
        self.assertEqual(self.stock(), D("15"))            # товара достаточно и сейчас, и на 3 января
        self.assert_rejected(back.post, "стоит в хронологии не раньше возврата")
        self.assertEqual(SupplierReturn.objects.get(pk=back.pk).status, "draft")
        self.assertEqual(self.stock(), D("15"))

    def test_message_names_both_moments(self):
        back = self.dated_return(3, post=False)
        with self.assertRaises(StockError) as caught:
            back.post()
        message = str(caught.exception)
        self.assertIn(f"возврат поставщику № {back.number}", message)
        self.assertIn(f"приёмка-основание № {self.basis.number}", message)
        self.assertIn("приёмка от 05.01.2026 12:00, возврат от 03.01.2026 12:00", message)

    def test_return_after_its_receipt_is_posted(self):
        self.dated_return(6)
        self.assertEqual(self.stock(), D("14"))

    def test_same_date_different_time(self):
        before = self.dated_return(5, time=datetime.time(11, 0), post=False)
        self.assert_rejected(before.post, "приёмка от 05.01.2026 12:00, возврат от 05.01.2026 11:00")
        self.dated_return(5, time=datetime.time(13, 0))
        self.assertEqual(self.stock(), D("14"))

    def test_same_date_and_time_follow_creation_order(self):
        """Дата и время совпали — возврат создан позже приёмки, значит стоит после неё.
        Перепроведение обоих документов порядок не меняет."""
        back = self.dated_return(5, time=NOON)
        self.basis.post()
        back.post()
        self.assertEqual(self.stock(), D("14"))

    def test_return_without_basis_is_not_tied_to_any_receipt(self):
        doc = SupplierReturn.objects.create(organization=self.org, warehouse=self.wh, supplier=self.supplier,
                                            date=JAN[3], time=NOON)
        doc.lines.create(product=self.product, quantity=1, price=100)
        doc.post()                                         # товар ранней приёмки, основания нет
        self.assertIsNone(SupplierReturn.objects.get(pk=doc.pk).receipt_id)
        self.assertEqual(self.stock(), D("14"))

    def test_moving_posted_return_before_its_receipt_is_rejected(self):
        back = self.dated_return(6)
        self.assert_rejected(lambda: self.edit_and_repost(back, date=JAN[4]), "стоит в хронологии не раньше возврата")
        self.assertEqual(SupplierReturn.objects.get(pk=back.pk).date, JAN[6])

    def test_moving_receipt_after_posted_return_is_rejected(self):
        back = self.dated_return(6)
        prefix = "На документ ссылается проведённый возврат"
        self.assert_rejected(lambda: self.edit_and_repost(self.basis, date=JAN[8]), prefix)
        self.assert_rejected(lambda: self.edit_and_repost(self.basis, date=JAN[6], time=datetime.time(18, 0)),
                             "документ нельзя поставить позже возврата по нему")
        self.assertEqual(Receipt.objects.get(pk=self.basis.pk).date, JAN[5])
        self.edit_and_repost(self.basis, date=JAN[4])      # раньше — можно
        self.edit_and_repost(self.basis, date=JAN[6], time=datetime.time(9, 0))   # тот же день, но раньше возврата
        back.post()
        self.assertEqual(self.stock(), D("14"))

    def test_moving_receipt_through_form_is_rolled_back(self):
        self.dated_return(6)
        self.login()
        before = self.full_state()
        response = self.save_receipt_form(self.basis, date=JAN[8].isoformat())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Изменения не сохранены")
        self.assertContains(response, "документ нельзя поставить позже возврата по нему")
        self.assertEqual(self.full_state(), before)
        self.assertEqual(Receipt.objects.get(pk=self.basis.pk).date, JAN[5])
        self.assertEqual(
            list(StockMovement.objects.filter(doc_type="receipt", doc_id=self.basis.pk).values_list("date", flat=True)),
            [JAN[5]],
        )

    def test_allowed_move_through_form_is_saved(self):
        self.dated_return(6)
        self.login()
        response = self.save_receipt_form(self.basis, date=JAN[4].isoformat())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Receipt.objects.get(pk=self.basis.pk).date, JAN[4])

    def test_quantity_limit_still_applies(self):
        self.dated_return(6, qty=5)
        extra = self.dated_return(7, qty=1, post=False)
        self.assert_rejected(extra.post, "доступно к возврату — 0")

    # ----- старые нарушения -----

    def make_legacy(self):
        """Возврат проведён, но в базе стоит раньше своей приёмки (так было возможно)."""
        back = self.dated_return(6)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): дату проведённого документа массово не меняют
        # (posting, правило 9) — старые данные имитируются прямой записью мимо программы
        SupplierReturn._base_manager.filter(pk=back.pk).update(date=JAN[3])
        StockMovement.objects.filter(doc_type="supplier_return", doc_id=back.pk).update(date=JAN[3])
        return SupplierReturn.objects.get(pk=back.pk)

    def test_diagnostics_lists_old_violation_and_writes_nothing(self):
        back = self.make_legacy()
        before = self.full_state()
        output = self.check_returns()
        self.assertIn("Возврат стоит не позже своего основания: 1", output)
        self.assertIn(f"возврат поставщику № {back.number} от 03.01.2026 (id {back.pk}) — 03.01.2026 12:00", output)
        self.assertIn(f"приёмка № {self.basis.number} от 05.01.2026 (id {self.basis.pk}) — 05.01.2026 12:00", output)
        self.assertIn("Ничего не изменено", output)
        self.assertEqual(self.full_state(), before)

    def test_old_violation_does_not_block_unchanged_documents_and_can_be_fixed(self):
        back = self.make_legacy()
        back.post()                                        # перепроведение без изменений
        self.basis.post()
        self.assert_rejected(lambda: self.edit_and_repost(self.basis, date=JAN[7]),
                             "документ нельзя поставить позже возврата по нему")
        self.edit_and_repost(back, date=JAN[6])            # исправление: возврат после приёмки
        self.assertIn("Нарушений нет", self.check_returns())

    def test_customer_side_violation_is_listed_too(self):
        sale = self.shipment(6, 1)
        back = self.customer_return(7, 1, shipment=sale)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): дату проведённого документа массово не меняют
        # (posting, правило 9) — старые данные имитируются прямой записью мимо программы
        CustomerReturn._base_manager.filter(pk=back.pk).update(date=JAN[2])
        output = self.check_returns()
        self.assertIn("Возврат стоит не позже своего основания: 1", output)
        self.assertIn(f"возврат покупателя № {back.number}", output)
