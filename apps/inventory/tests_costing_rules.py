"""Три подтверждённые ошибки расчёта себестоимости и их исправление.

1. Проверка наличия смотрела только итог дня: отгрузку от 11:00 можно было
   провести при приёмке от 12:00 того же дня — с себестоимостью 0.
2. Скидка поставщика не входила в стоимость поступления: 10 по 100 со скидкой
   10 % давали себестоимость 100 вместо 90.
3. Возврат без основания и без истории пересчитывался по сегодняшней цене
   карточки: цену меняли с 77 на 999 — старый возврат становился 999.
"""
import datetime
import importlib
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db import transaction
from django.urls import reverse

from apps.purchases.models import Receipt
from apps.sales.models import CustomerReturn, CustomerReturnLine, Shipment, ShipmentLine

from . import costing, services
from .models import DeficitCover, StockBalance, StockMovement, Transfer
from .services import StockError
from .tests_consistency import ConsistencyBase
from .tests_costing import JAN, NOON, CostingBase, legacy, migration_apps

D = Decimal


def at(hour, minute=0, second=0):
    return datetime.time(hour, minute, second)


class RulesBase(CostingBase):
    def assert_rejected(self, operation, message):
        """Операция отклонена целиком: в учёте ничего не изменилось."""
        before = self.full_state()
        with self.assertRaisesMessage(StockError, message):
            with transaction.atomic():
                operation()
        self.assertEqual(self.full_state(), before)

    def full_state(self):
        return (
            list(StockMovement.objects.order_by("pk").values_list(
                "pk", "doc_type", "doc_id", "warehouse_id", "quantity", "cost", "amount", "open_qty", "date", "time")),
            list(ShipmentLine.objects.order_by("pk").values_list("pk", "quantity", "cost_price", "cost_amount",
                                                                 "cost_open_qty")),
            list(DeficitCover.objects.order_by("pk").values_list("issue_id", "cover_id", "quantity", "amount")),
            list(StockBalance.objects.order_by("product_id", "warehouse_id")
                 .values_list("product_id", "warehouse_id", "quantity", "avg_cost", "value")),
            sorted((model.__name__, pk, status)
                   for model in (Receipt, Shipment, CustomerReturn, Transfer)
                   for pk, status in model.objects.values_list("pk", "status")),
        )

    def draft_shipment(self, day, qty, time=NOON, warehouse=None):
        doc = Shipment.objects.create(organization=self.org, warehouse=warehouse or self.wh,
                                      customer=self.customer, date=JAN[day], time=time)
        doc.lines.create(product=self.product, quantity=qty, price=500)
        return doc

    def run_command(self, *args):
        out = StringIO()
        call_command("check_costs", *args, stdout=out)
        return out.getvalue()


# ---------------------------------------------------------------------------
# 1. Единая хронология проверки наличия и себестоимости
# ---------------------------------------------------------------------------

class StockCheckChronologyTests(RulesBase):
    def test_shipment_before_receipt_on_same_day_is_rejected(self):
        """Воспроизведение: приёмка 10 шт. 1 января в 12:00, отгрузка 1 шт. в 11:00."""
        self.receipt(1, 10, 100, time=at(12))
        shipment = self.draft_shipment(1, 1, time=at(11))
        self.assert_rejected(shipment.post, "на 01.01.2026 11:00 остаток стал бы -1")
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, "draft")
        self.assertFalse(StockMovement.objects.filter(doc_type="shipment").exists())
        self.assertEqual(self.balance(), (D("10"), D("100.00")))

    def test_same_shipment_after_receipt_is_posted_with_cost(self):
        self.receipt(1, 10, 100, time=at(12))
        shipment = self.shipment(1, 1, time=at(12, 0, 1))
        self.assertEqual(self.line_cost(shipment), D("100.00"))

    def test_negative_allowed_covers_sale_before_receipt(self):
        """Минус разрешён: отгрузку можно внести раньше поступления. ПРАВИЛО
        ИЗМЕНЕНО: раньше она получала 0, а остаток — завышенную среднюю 111,11;
        теперь приход от 12:00 покрывает её дефицит по своей цене."""
        self.org.allow_negative_stock = True
        self.org.save()
        self.receipt(1, 10, 100, time=at(12))
        shipment = self.shipment(1, 1, time=at(11))
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(shipment.lines.get().cost_open_qty, D("0"))
        self.assertEqual(self.balance(), (D("9"), D("100.00")))

    def test_moving_posted_shipment_before_receipt_is_rejected(self):
        self.receipt(1, 10, 100, time=at(12))
        shipment = self.shipment(1, 1, time=at(13))
        self.assert_rejected(lambda: self.edit_and_repost(shipment, time=at(11)), "11:00 остаток стал бы -1")
        self.assertEqual(Shipment.objects.get(pk=shipment.pk).time, at(13))

    def test_moving_receipt_after_shipment_is_rejected(self):
        receipt = self.receipt(1, 10, 100, time=at(10))
        self.shipment(1, 4, time=at(11))
        self.assert_rejected(lambda: self.edit_and_repost(receipt, time=at(12)), "11:00 остаток стал бы -4")

    def test_unposting_receipt_consumed_later_the_same_day(self):
        """Итог дня положительный (+10 −10 +10), но в 11:00 товара не было бы."""
        first = self.receipt(1, 10, 100, time=at(10))
        self.shipment(1, 10, time=at(11))
        self.receipt(1, 10, 100, time=at(12))
        self.assert_rejected(first.unpost, "11:00 остаток стал бы -10")

    def test_deleting_receipt_consumed_later_the_same_day(self):
        first = self.receipt(1, 10, 100, time=at(10))
        self.shipment(1, 10, time=at(11))
        self.receipt(1, 10, 100, time=at(12))
        self.assert_rejected(first.delete, "11:00 остаток стал бы -10")
        self.assertTrue(Receipt.objects.filter(pk=first.pk, status="posted").exists())

    def test_unposting_later_receipt_is_allowed(self):
        self.receipt(1, 10, 100, time=at(10))
        self.shipment(1, 10, time=at(11))
        later = self.receipt(1, 10, 100, time=at(12))
        later.unpost()
        self.assertEqual(self.balance(), (D("0"), D("0")))

    def test_several_lines_of_one_product_are_one_operation(self):
        """Две строки одного товара проверяются вместе: 3 + 3 при остатке 5 в 12:00."""
        self.receipt(1, 5, 100, time=at(10))
        self.receipt(1, 5, 100, time=at(14))            # итог дня хватил бы
        doc = self.draft_shipment(1, 3, time=at(12))
        doc.lines.create(product=self.product, quantity=3, price=500)
        self.assert_rejected(doc.post, "12:00 остаток стал бы -1")

        doc.lines.order_by("-pk").first().delete()
        doc.lines.create(product=self.product, quantity=2, price=500)
        doc.post()                                       # 3 + 2 = 5 — ровно остаток
        self.assertEqual(self.balance(), (D("5"), D("100.00")))

    def test_transfer_chain_out_of_order_is_rejected(self):
        """Со склада 2 нельзя переместить в 11:00 то, что придёт туда в 12:00."""
        self.receipt(1, 10, 100, time=at(10))
        self.transfer(1, 5, self.wh, self.wh2, time=at(12))
        second = Transfer.objects.create(organization=self.org, warehouse_from=self.wh2,
                                         warehouse_to=self.wh3, date=JAN[1], time=at(11))
        second.lines.create(product=self.product, quantity=5)
        self.assert_rejected(second.post, "«Магазин»: на 01.01.2026 11:00 остаток стал бы -5")

        second.time = at(13)
        second.save()
        second.post()
        self.assertEqual(self.balance(self.wh3), (D("5"), D("100.00")))

    def test_moving_first_transfer_after_second_is_rejected(self):
        self.receipt(1, 10, 100, time=at(10))
        first = self.transfer(1, 5, self.wh, self.wh2, time=at(12))
        self.transfer(1, 5, self.wh2, self.wh3, time=at(13))
        self.assert_rejected(lambda: self.edit_and_repost(first, time=at(14)), "13:00 остаток стал бы -5")
        self.assert_rejected(first.unpost, "13:00 остаток стал бы -5")

    def test_transfer_between_same_times_uses_creation_order(self):
        """Дата и время совпали — порядок по моменту создания; перепроведение его не меняет."""
        receipt = self.receipt(1, 10, 100)
        transfer = self.transfer(1, 10)
        shipment = self.shipment(1, 10, warehouse=self.wh2)
        for doc in (receipt, transfer, shipment, receipt, transfer):
            doc.post()                                   # движения пересоздаются с новыми id
        self.assertEqual(self.line_cost(shipment), D("100.00"))
        self.assertEqual(self.balance(self.wh2), (D("0"), D("0")))
        self.assertConsistent()

    def test_existing_negative_does_not_block_operations_that_do_not_deepen_it(self):
        """Политика прежнего минуса: операция отклоняется, только если создаёт
        минус или делает существующий глубже в какой-либо точке хронологии."""
        self.org.allow_negative_stock = True
        self.org.save()
        old = self.shipment(1, 2, time=at(9))            # остаток −2, когда минус был разрешён
        self.org.allow_negative_stock = False
        self.org.save()

        partial = self.receipt(1, 1, 100, time=at(10))   # −2 → −1: минус стал меньше — можно
        self.assertEqual(self.balance()[0], D("-1"))
        old.post()                                       # перепроведение без изменений — можно
        self.receipt(1, 10, 100, time=at(11))            # остаток 9
        self.shipment(1, 3, time=at(12))                 # расход из положительного остатка — можно
        self.assertEqual(self.balance()[0], D("6"))

        self.assert_rejected(partial.unpost, "10:00 остаток стал бы -2")              # углубляет
        self.assert_rejected(lambda: self.edit_and_repost(old, quantity=D("3")), "09:00 остаток стал бы -3")
        late = self.draft_shipment(1, 1, time=at(10, 30))                             # в точке, где −1
        self.assert_rejected(late.post, "10:30 остаток стал бы -2")

    def test_diagnostics_after_sale_before_receipt(self):
        """Расход раньше прихода покрыт этим приходом — расхождений нет, а покрытие
        записано для объяснения расчёта. ПРАВИЛО ИЗМЕНЕНО: раньше диагностика
        предупреждала о заниженной себестоимости; теперь она определена."""
        from .models import DeficitCover

        self.org.allow_negative_stock = True
        self.org.save()
        receipt = self.receipt(1, 10, 100, time=at(12))
        shipment = self.shipment(1, 1, time=at(11))
        output = self.run_command()
        self.assertIn("Расхождений нет", output)
        self.assertNotIn("Непокрытый дефицит", output)
        cover = DeficitCover.objects.get()
        self.assertEqual((cover.issue.doc_id, cover.cover.doc_id, cover.quantity, cover.amount),
                         (shipment.pk, receipt.pk, D("1"), D("100.00")))

    def test_backdated_receipt_is_allowed_and_recalculates(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        self.receipt(2, 10, 200)
        self.assertEqual(self.line_cost(shipment), D("150.00"))


class DocumentFormTimeTests(ConsistencyBase):
    """Форма показывает время без секунд — сохранение не должно их обрезать."""

    def setUp(self):
        super().setUp()
        self.incoming = self.receipt(10)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): время проведённого документа меняется только
        # вместе с перепроведением (posting, правило 9) — раньше update() и отдельный post()
        with self.incoming.editing():
            self.incoming.time = at(10, 0, 30)
            self.incoming.save(update_fields=["time"])
        self.outgoing = self.shipment(2, post=False)
        Shipment.objects.filter(pk=self.outgoing.pk).update(time=at(10, 0, 45))
        self.outgoing.refresh_from_db()
        self.outgoing.post()

    def test_saving_keeps_seconds_and_order(self):
        """Раньше 10:00:45 превращалось в 10:00:00 — раньше приёмки от 10:00:30."""
        response = self.save_shipment(self.outgoing, lines=self.line(self.outgoing, quantity="3"))
        self.assertEqual(response.status_code, 302)
        self.outgoing.refresh_from_db()
        self.assertEqual(self.outgoing.time, at(10, 0, 45))
        self.assertEqual(self.stock(), 7)

    def test_changed_time_is_saved(self):
        response = self.save_shipment(self.outgoing, time="10:01")
        self.assertEqual(response.status_code, 302)
        self.outgoing.refresh_from_db()
        self.assertEqual(self.outgoing.time, at(10, 1))

    def test_time_before_receipt_is_rejected_by_form(self):
        before = self.state()
        response = self.save_shipment(self.outgoing, time="09:59")
        self.assert_rejected(response, before)
        self.assertContains(response, "09:59 остаток стал бы -2")

    def test_new_shipment_before_receipt_is_saved_but_not_posted(self):
        """Воспроизведение через форму: черновик сохранён, проведение отклонено."""
        draft = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer,
                                        date=self.incoming.date, time=at(9))
        draft.lines.create(product=self.product, quantity=1, price=150)
        data = self.form_data(draft, "customer")
        data["action"] = "save_post"
        response = self.client.post(reverse("shipment_edit", args=[draft.pk]), data, follow=True)
        self.assertContains(response, "Сохранено, но не проведено")
        self.assertContains(response, "09:00 остаток стал бы -1")
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")
        self.assertEqual(self.stock(), 8)


# ---------------------------------------------------------------------------
# 2. Скидка поставщика входит в стоимость поступления
# ---------------------------------------------------------------------------

class ReceiptDiscountTests(RulesBase):
    def discounted_receipt(self, day, lines, time=NOON, post=True):
        doc = Receipt.objects.create(organization=self.org, warehouse=self.wh,
                                     supplier=self.supplier, date=JAN[day], time=time)
        for qty, price, discount in lines:
            doc.lines.create(product=self.product, quantity=qty, price=price, discount=discount)
        if post:
            doc.post()
        return doc

    def incoming(self, doc):
        return list(StockMovement.objects.filter(doc_type="receipt", doc_id=doc.pk)
                    .order_by("pk").values_list("cost", "amount"))

    def test_discount_reduces_cost(self):
        """Воспроизведение: 10 по 100 со скидкой 10 % — себестоимость 90, а не 100."""
        receipt = self.discounted_receipt(1, [(10, 100, 10)])
        shipment = self.shipment(2, 1)
        self.assertEqual(self.line_cost(shipment), D("90.00"))
        self.assertEqual(self.incoming(receipt), [(D("90.00"), D("900.00"))])
        self.assertEqual(self.balance(), (D("9"), D("90.00")))
        self.assertEqual(receipt.lines.get().total, D("900.00"))       # совпадает с итогом строки
        self.assertConsistent()

    def test_without_discount_nothing_changes(self):
        receipt = self.discounted_receipt(1, [(10, 100, 0)])
        self.assertEqual(self.incoming(receipt), [(D("100.00"), D("1000.00"))])

    def test_fractional_quantity_and_discount(self):
        # 2.5 × 99.99 = 249.975; −12.5 % → 218.728125 → строка 218.73; за единицу 87.492 → 87.49
        receipt = self.discounted_receipt(1, [(D("2.500"), D("99.99"), D("12.5"))])
        self.assertEqual(receipt.lines.get().total, D("218.73"))
        self.assertEqual(self.incoming(receipt), [(D("87.49"), D("218.73"))])
        shipment = self.shipment(2, D("0.500"))
        # списана доля стоимости: 218.73 × 0.5 / 2.5 = 43.746 → 43.75. Цена единицы
        # для показа — 43.75 / 0.5 = 87.50 (раньше показывалось 87.49: итог тогда
        # считался от округлённой цены, теперь цена выводится из суммы)
        self.assertEqual(shipment.lines.get().cost_amount, D("43.75"))
        self.assertEqual(self.line_cost(shipment), D("87.50"))
        self.assertEqual(StockBalance.objects.get().value, D("174.98"))    # 218.73 − 43.75
        self.assertConsistent()

    def test_full_discount_gives_zero_cost(self):
        free = self.discounted_receipt(1, [(10, 100, 100)])
        self.assertEqual(self.incoming(free), [(D("0.00"), D("0.00"))])
        first = self.shipment(2, 1)
        self.assertEqual(self.line_cost(first), D("0.00"))
        self.discounted_receipt(3, [(9, 100, 0)])        # 9 бесплатных + 9 по 100 → 50
        second = self.shipment(4, 1)
        self.assertEqual(self.line_cost(second), D("50.00"))
        self.assertConsistent()

    def test_several_lines_with_different_discounts(self):
        receipt = self.discounted_receipt(1, [(10, 100, 10), (10, 100, 0), (5, 200, D("33.333"))])
        # 900 + 1000 + 666.67 = 2566.67 на 25 → 102.67
        self.assertEqual(self.incoming(receipt), [
            (D("90.00"), D("900.00")), (D("100.00"), D("1000.00")), (D("133.33"), D("666.67")),
        ])
        self.assertEqual(self.balance(), (D("25"), D("102.67")))
        self.assertConsistent()

    def test_line_total_is_not_lost_to_unit_price_rounding(self):
        """В остаток входит сумма строки, а не «количество × округлённая цена»."""
        # 3 × 50 − 33.333 % = 100.0005 → 100.00; за единицу 33.33 (33.33 × 3 = 99.99)
        receipt = self.discounted_receipt(1, [(3, 50, D("33.333"))])
        self.assertEqual(self.incoming(receipt), [(D("33.33"), D("100.00"))])
        self.receipt(2, 1, D("0.02"))
        # (100.00 + 0.02) / 4 = 25.005 → 25.01; по округлённой цене было бы 100.01 / 4 → 25.00
        self.assertEqual(self.balance(), (D("4"), D("25.01")))

    def test_large_quantity(self):
        # 100 000 × 0.99 − 33.333 % = 66 000.33; «100 000 × 0.66» потеряло бы 33 копейки
        receipt = self.discounted_receipt(1, [(100000, D("0.99"), D("33.333"))])
        self.assertEqual(receipt.lines.get().total, D("66000.33"))
        self.assertEqual(self.incoming(receipt), [(D("0.66"), D("66000.33"))])
        self.discounted_receipt(2, [(1, D("0.67"), 0)])
        plan = costing.build_plan(self.product.pk)
        self.assertEqual(costing.differences(plan), [])

    def test_changing_discount_recalculates_later_shipments(self):
        receipt = self.discounted_receipt(1, [(10, 100, 10)])
        shipment = self.shipment(2, 4)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with receipt.editing():
            receipt.lines.update(discount=D("25"))
        self.assertEqual(self.line_cost(shipment), D("75.00"))
        self.assertEqual(self.balance(), (D("6"), D("75.00")))

    def test_repost_and_repeated_recalculation_change_nothing(self):
        receipt = self.discounted_receipt(1, [(10, 100, 10), (3, 50, D("33.333"))])
        self.shipment(2, 4)
        before = self.snapshot()
        self.assertEqual(costing.recalculate([self.product.pk]), [])
        receipt.post()
        # id движений приёмки новые, значения — те же
        self.assertEqual(sorted(row[1:] for row in self.snapshot()[0]), sorted(row[1:] for row in before[0]))
        self.assertEqual(self.snapshot()[1:], before[1:])

    def test_transfer_chain_carries_discounted_cost(self):
        self.discounted_receipt(1, [(10, 100, 10)])
        self.transfer(2, 6, self.wh, self.wh2)
        self.transfer(3, 4, self.wh2, self.wh3)
        shipment = self.shipment(4, 3, warehouse=self.wh3)
        self.assertEqual(self.line_cost(shipment), D("90.00"))
        self.assertEqual(self.balance(self.wh3), (D("1"), D("90.00")))
        self.assertConsistent()

    # ----- старые движения: скидка была проигнорирована -----

    def make_legacy(self, lines):
        """Состояние «как до исправления»: приход по цене без скидки, движения без ссылок."""
        receipt = self.discounted_receipt(1, lines)
        shipment = self.shipment(2, 1)
        for movement in StockMovement.objects.filter(doc_type="receipt"):
            price = receipt.lines.get(pk=movement.line_id).price
            StockMovement.objects.filter(pk=movement.pk).update(cost=price, amount=None)
        StockMovement.objects.update(line_id=None)
        StockMovement.objects.filter(doc_type="shipment").update(cost=D("100.00"), amount=None)
        ShipmentLine.objects.update(cost_price=D("100.00"), cost_amount=None)
        StockBalance.objects.update(avg_cost=D("100.00"), value=None)
        return receipt, shipment

    def test_diagnostics_finds_ignored_discount_in_old_receipt(self):
        receipt, shipment = self.make_legacy([(10, 100, 10)])
        before = self.full_state()
        output = self.run_command()
        self.assertIn("Стоимость поступлений не совпадает со строкой приёмки (скидка поставщика): 1", output)
        self.assertIn("цена 100.00, скидка 10.000 %: (цена единицы, сумма) — "
                      "в базе (100.00, 1000.00), по расчёту (90.00, 900.00)", output)
        self.assertIn("Ничего не записано", output)
        self.assertEqual(self.full_state(), before)                    # dry-run ничего не пишет

        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertNotIn("НЕ исправлены", output)
        self.assertEqual(self.line_cost(shipment), D("90.00"))
        self.assertEqual(self.incoming(receipt), [(D("90.00"), D("900.00"))])
        self.assertEqual(self.balance(), (D("9"), D("90.00")))
        self.assertIn("Расхождений нет", self.run_command())

    def test_old_receipt_with_distinguishable_lines_is_matched(self):
        """Две строки товара с разным количеством — соответствие однозначно."""
        receipt, shipment = self.make_legacy([(10, 100, 10), (5, 100, 20)])
        self.run_command("--apply", "--i-have-a-backup")
        self.assertEqual(self.incoming(receipt), [(D("90.00"), D("900.00")), (D("80.00"), D("400.00"))])
        self.assertEqual(self.line_cost(shipment), D("86.67"))         # 1300 / 15
        self.assertIn("Расхождений нет", self.run_command())

    def test_ambiguous_old_receipt_is_reported_and_not_guessed(self):
        """Две строки одного товара с одинаковым количеством и разной скидкой:
        какое движение к какой строке относится — неизвестно."""
        receipt, shipment = self.make_legacy([(10, 100, 10), (10, 100, 50)])
        before = self.full_state()
        dry = self.run_command()
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — движения не сопоставляются со строками документа: 1", dry)
        self.assertIn("Такие товары исправление пропустит целиком", dry)

        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("Исправлено расхождений: 0", output)
        self.assertIn(f"Товары НЕ исправлены и не изменены (id: {self.product.pk})", output)
        self.assertEqual(self.full_state(), before)                    # товар не тронут вовсе
        self.assertEqual(self.line_cost(shipment), D("100.00"))

        receipt.post()                                                 # решение: перепровести документ
        self.assertEqual(self.incoming(receipt), [(D("90.00"), D("900.00")), (D("50.00"), D("500.00"))])
        self.assertEqual(self.line_cost(shipment), D("70.00"))         # 1400 / 20
        self.assertIn("Расхождений нет", self.run_command())

    def test_unmatched_movement_blocks_other_operations_until_fixed(self):
        """Старое нарушение: движение отгрузки не сопоставляется со строкой.
        ПРАВИЛО ИЗМЕНЕНО (согласовано владельцем): раньше другие операции с
        товаром проходили, а пересчёт обходил проблему. Теперь операции с
        проблемным товаром отклоняются целиком с указанием документа, а
        исправляющая операция (перепроведение отгрузки) проходит."""
        self.receipt(1, 10, 100)
        shipment = self.shipment(3, 5)
        StockMovement.objects.filter(doc_type="shipment").update(line_id=None)
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(ShipmentLine).filter(shipment=shipment).update(quantity=D("4"))   # в обход проведения
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())

        late = self.receipt(2, 10, 200, post=False)
        self.assert_rejected(late.post, f"отгрузка № {shipment.number}")
        self.assertEqual(Receipt.objects.get(pk=late.pk).status, "draft")

        shipment.post()                # исправление: перепроведение по текущим строкам
        late.post()                    # теперь история корректна
        self.assertEqual(self.line_cost(shipment), D("150.00"))
        self.assertIn("Расхождений нет", self.run_command())


# ---------------------------------------------------------------------------
# 3. Возврат покупателя: зафиксированная исходная стоимость и основание
# ---------------------------------------------------------------------------

class ReturnFallbackTests(RulesBase):
    def test_card_price_change_does_not_touch_old_return(self):
        """Воспроизведение: цена карточки 77 → 999, стоимость старого возврата остаётся 77."""
        back = self.customer_return(3, 1)
        self.assertEqual(self.move_cost(back), D("77.00"))
        line = back.lines.get()
        self.assertEqual((line.fallback_cost, line.fallback_source), (D("77.00"), "card"))

        self.product.purchase_price = D("999")
        self.product.save()
        self.assertEqual(costing.recalculate([self.product.pk]), [])
        services.recompute_balance(self.product.pk)
        self.assertEqual(self.move_cost(back), D("77.00"))
        self.assertEqual(self.balance(), (D("1"), D("77.00")))
        self.assertIn("Расхождений нет", self.run_command())

    def test_unpost_and_repost_keep_fixed_cost(self):
        back = self.customer_return(3, 1)
        self.product.purchase_price = D("999")
        self.product.save()
        back.post()
        self.assertEqual(self.move_cost(back), D("77.00"))
        back.unpost()
        back.post()
        self.assertEqual(self.move_cost(back), D("77.00"))
        self.assertEqual(back.lines.get().fallback_cost, D("77.00"))

    def test_other_operations_keep_fixed_cost(self):
        back = self.customer_return(3, 1)
        self.product.purchase_price = D("999")
        self.product.save()
        self.receipt(4, 1, 100)
        self.shipment(5, 2)
        self.assertEqual(self.move_cost(back), D("77.00"))
        self.assertEqual(self.balance(), (D("0"), D("0")))

    def test_new_return_takes_new_card_price(self):
        self.customer_return(3, 1)
        self.product.purchase_price = D("999")
        self.product.save()
        other = self.customer_return(3, 1, warehouse=self.wh2)
        self.assertEqual(self.move_cost(other), D("999.00"))

    def test_fixed_cost_is_only_a_fallback(self):
        """Когда история остатков есть, возврат без основания идёт по средней —
        и возвращается к зафиксированной цене, если историю убрали."""
        back = self.customer_return(3, 1)
        early = self.receipt(1, 5, 120)
        self.assertEqual(self.move_cost(back), D("120.00"))
        self.product.purchase_price = D("999")
        self.product.save()
        early.unpost()
        self.assertEqual(self.move_cost(back), D("77.00"))

    def test_fixed_cost_is_stored_for_return_with_basis_too(self):
        self.receipt(1, 10, 100)
        shipment = self.shipment(2, 5)
        back = self.customer_return(3, 1, shipment=shipment)
        self.assertEqual(self.move_cost(back), D("100.00"))
        self.assertEqual(back.lines.get().fallback_cost, D("77.00"))

    def test_changing_product_in_line_drops_fixed_cost(self):
        back = self.customer_return(3, 1)
        self.other.purchase_price = D("15")
        self.other.save()
        line = back.lines.get()
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with back.editing(products=[self.other.pk]):
            line.product = self.other
            line.save()
            self.assertIsNone(CustomerReturnLine.objects.get(pk=line.pk).fallback_cost)
        self.assertEqual(StockMovement.objects.get(doc_type="customer_return").cost, D("15.00"))

    def test_draft_does_not_fix_cost(self):
        draft = self.customer_return(3, 1, post=False)
        self.assertIsNone(draft.lines.get().fallback_cost)

    # ----- старые возвраты -----

    def make_legacy_return(self, stored_cost):
        """Возврат, проведённый до появления фиксации: в строке ничего нет."""
        back = self.customer_return(3, 1)
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).update(fallback_cost=None, fallback_source="")
        StockMovement.objects.update(cost=stored_cost, amount=None, line_id=None)
        StockBalance.objects.update(avg_cost=stored_cost, value=None)
        return back

    def migrate_legacy(self):
        migration = importlib.import_module("apps.sales.migrations.0006_customerreturnline_fallback_cost")
        migration.carry_over_legacy_cost(migration_apps(), None)

    def test_old_return_without_saved_value_is_reported_as_uncertain(self):
        back = self.make_legacy_return(D("55.00"))
        self.product.purchase_price = D("999")
        self.product.save()
        before = self.full_state()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn("НЕОПРЕДЕЛЁННОСТЬ — исходная стоимость возврата не подтверждена: 1", output)
        self.assertIn("не зафиксирована; оставлена сумма движения 55.00", output)
        self.assertIn("Неопределённостей: 1", output)
        self.assertEqual(self.move_cost(back), D("55.00"))             # не 999 и не 77
        self.assertEqual([row[:6] for row in self.full_state()[0]], [row[:6] for row in before[0]])

    def test_migration_carries_over_old_movement_cost(self):
        back = self.make_legacy_return(D("55.00"))
        self.migrate_legacy()
        line = back.lines.get()
        self.assertEqual((line.fallback_cost, line.fallback_source), (D("55.00"), "legacy"))

        self.product.purchase_price = D("999")
        self.product.save()
        costing.recalculate([self.product.pk])
        self.assertEqual(self.move_cost(back), D("55.00"))
        output = self.run_command()
        self.assertIn("перенесённой из старого движения", output)       # честно: это не цена карточки
        self.assertIn("Неопределённостей: 1", output)
        back.post()                                                     # перепроведение не подменяет
        self.assertEqual(self.move_cost(back), D("55.00"))

    def test_migration_does_not_guess_between_identical_lines(self):
        back = self.customer_return(3, 1, post=False)
        back.lines.create(product=self.product, quantity=1, price=400)
        back.post()
        # «старые данные» — прямая запись мимо программы (обычные пути строку проведённого
        # документа теперь не меняют, posting, правило 10)
        legacy(CustomerReturnLine).update(fallback_cost=None, fallback_source="")
        StockMovement.objects.update(line_id=None)
        self.migrate_legacy()
        self.assertEqual(list(back.lines.values_list("fallback_cost", flat=True)), [None, None])
        self.assertIn("НЕОПРЕДЕЛЁННОСТЬ", self.run_command())

    def test_migration_skips_drafts(self):
        draft = self.customer_return(3, 1, post=False)
        self.migrate_legacy()
        self.assertIsNone(draft.lines.get().fallback_cost)


class ReturnBasisTests(RulesBase):
    def setUp(self):
        super().setUp()
        self.receipt(1, 10, 100)

    def test_unposted_basis_is_rejected(self):
        basis = self.draft_shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis, post=False)
        self.assert_rejected(back.post, "не проведена")
        self.assertEqual(CustomerReturn.objects.get(pk=back.pk).status, "draft")

    def test_basis_later_than_return_is_rejected(self):
        basis = self.shipment(5, 5)
        back = self.customer_return(4, 1, shipment=basis, post=False)
        self.assert_rejected(back.post, "стоит в хронологии не раньше возврата")

    def test_basis_later_the_same_day_is_rejected(self):
        basis = self.shipment(4, 5, time=at(15))
        back = self.customer_return(4, 1, shipment=basis, post=False, time=at(14))
        self.assert_rejected(back.post, "стоит в хронологии не раньше возврата")
        back.time = at(16)
        back.save()
        back.post()
        self.assertEqual(self.move_cost(back), D("100.00"))

    def test_basis_without_the_product_is_rejected(self):
        self.receipt(1, 10, 50, product=self.other)
        basis = self.shipment(2, 5, product=self.other)
        back = self.customer_return(4, 1, shipment=basis, post=False)
        self.assert_rejected(back.post, "не содержит товар «Кабель»")

    def test_error_names_documents_and_way_out(self):
        basis = self.draft_shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis, post=False)
        with self.assertRaises(StockError) as caught:
            back.post()
        message = str(caught.exception)
        self.assertIn(f"возврат покупателя № {back.number}", message)
        self.assertIn(f"отгрузка-основание № {basis.number}", message)
        self.assertIn("Укажите в возврате подходящую отгрузку или уберите основание", message)

    def test_removing_basis_explicitly_makes_it_a_return_without_basis(self):
        basis = self.draft_shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis, post=False)
        back.shipment = None
        back.save()
        back.post()
        self.assertEqual(self.move_cost(back), D("100.00"))            # средняя перед возвратом

    def test_operations_on_basis_that_break_posted_return_are_rejected(self):
        basis = self.shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis)
        prefix = "На документ ссылается проведённый возврат"
        self.assert_rejected(basis.unpost, prefix)
        self.assert_rejected(basis.delete, prefix)
        self.assert_rejected(lambda: self.edit_and_repost(basis, date=JAN[6]), prefix)
        self.assertTrue(Shipment.objects.filter(pk=basis.pk, status="posted").exists())
        self.assertEqual(self.move_cost(back), D("100.00"))

        back.unpost()                  # сначала возврат — потом можно и отгрузку
        basis.unpost()
        self.assertEqual(self.balance(), (D("10"), D("100.00")))

    def test_harmless_edit_of_basis_is_allowed(self):
        basis = self.shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis)
        self.edit_and_repost(basis, quantity=D("4"), date=JAN[3])
        self.assertEqual(self.move_cost(back), D("100.00"))
        self.assertEqual(self.balance(), (D("7"), D("100.00")))

    def test_old_bad_basis_blocks_other_operations_until_fixed(self):
        """В старых данных возврат уже ссылается на негодное основание.
        ПРАВИЛО ИЗМЕНЕНО (согласовано владельцем): раньше посторонние операции
        с товаром проходили; теперь отклоняются с указанием мешающего возврата.
        Диагностика требует решения, исправляющий режим товар не «чинит»;
        исправление — снять этот возврат с проведения."""
        basis = self.shipment(2, 5)
        back = self.customer_return(4, 1, shipment=basis)
        later = self.draft_shipment(6, 1)
        # ПРАВИЛО ДОПОЛНЕНО (21.10.2026): основание проведённого возврата массово не меняют
        # (posting, правило 12) — старые данные имитируются прямой записью мимо программы
        CustomerReturn._base_manager.filter(pk=back.pk).update(shipment=later)   # как будто так и было

        unrelated = self.receipt(7, 1, 100, post=False)
        self.assert_rejected(unrelated.post, f"возврат покупателя № {back.number}")
        dry = self.run_command()
        self.assertIn("ТРЕБУЕТ РЕШЕНИЯ — возвраты покупателей с неподходящим основанием: 1", dry)
        self.assertIn("не проведена", dry)

        before = self.full_state()
        output = self.run_command("--apply", "--i-have-a-backup")
        self.assertIn(f"Товары НЕ исправлены и не изменены (id: {self.product.pk})", output)
        self.assertEqual(self.full_state(), before)
        with self.assertRaisesMessage(StockError, "не проведена"):         # сам возврат перепровести нельзя
            CustomerReturn.objects.get(pk=back.pk).post()
        CustomerReturn.objects.get(pk=back.pk).unpost()                    # исправляющая операция
        unrelated.post()
        self.assertNotIn("ТРЕБУЕТ РЕШЕНИЯ", self.run_command())

    def test_removing_returned_product_from_basis_is_rejected(self):
        """Из отгрузки-основания убирают строку товара, по которому есть возврат."""
        self.receipt(1, 10, 50, product=self.other)
        basis = self.shipment(2, 5)
        # ПРАВИЛО ДОПОЛНЕНО (19.10.2026): строки проведённого документа меняются только вместе
        # с перепроведением — editing() (posting, правило 10); раньше строку меняли и проводили
        # отдельно. Учётные ожидания прежние.
        with basis.editing(products=[self.other.pk]):
            basis.lines.create(product=self.other, quantity=1, price=500)
        back = self.customer_return(4, 1, shipment=basis)

        def remove_line():
            with basis.editing():
                basis.lines.filter(product=self.product).delete()

        self.assert_rejected(remove_line, "На документ ссылается проведённый возврат")
        self.assertEqual(basis.lines.count(), 2)
        self.assertEqual(self.move_cost(back), D("100.00"))
