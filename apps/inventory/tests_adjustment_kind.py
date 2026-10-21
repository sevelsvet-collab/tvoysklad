"""Вид оприходования / списания (StockAdjustment.kind) проведённого документа.

Дефект (19.10.2026, подтверждён независимо на временной базе): у проведённого
оприходования 1 шт. по 100 выполнили kind = "expense";
save(update_fields=["kind"]). Документ стал списанием, а движение осталось
adjustment_income: +1 шт. на 100.

Контракт (posting, правило 11): вид проведённого документа и документа с
движениями не меняется — в том числе через editing(). Исправление —
отменить проведение (все проверки истории), изменить вид черновика,
провести заново. Номер сохраняется и переходит в серию нового вида.
"""
from decimal import Decimal
from io import StringIO
from unittest import skipUnless

from django.core.management import call_command
from django.db import connection
from django.db.models import F, Value
from django.test import TransactionTestCase

from apps.catalog.models import Product
from apps.core.concurrency_testing import ConcurrencyMixin
from apps.core.constants import DOC_DRAFT
from apps.core.models import DocumentNumber

from . import services, transition
from .models import StockAdjustment, StockMovement
from .services import PostedChangeError, StockError
from .tests_org_document_change import ChangeWorld
from .tests_org_stock import OrgWorld

D = Decimal


class KindReproductionTests(ChangeWorld):
    maxDiff = None

    def picture(self, doc):
        stored = StockAdjustment.objects.get(pk=doc.pk)
        moves = StockMovement.objects.filter(doc_id=doc.pk, doc_type__in=StockAdjustment.DOC_TYPES)
        return {"документ": stored.kind, "движения": sorted((m.doc_type, m.quantity, m.amount) for m in moves)}

    def test_kind_of_posted_income_saved_without_reposting(self):
        doc = self.doc(StockAdjustment, self.a, [(1, "", 100)], kind=StockAdjustment.KIND_INCOME)
        doc.post()
        before = self.full_state()
        doc = StockAdjustment.objects.get(pk=doc.pk)
        doc.kind = StockAdjustment.KIND_EXPENSE
        rejected = None
        try:
            doc.save(update_fields=["kind"])
        except StockError as exc:
            rejected = exc
        # было: {"документ": "expense", "движения": [("adjustment_income", 1, 100)]}
        self.assertEqual(self.picture(doc), {"документ": "income",
                                             "движения": [("adjustment_income", D("1.000"), D("100.00"))]})
        self.assertIsNotNone(rejected, "смена вида проведённого документа должна быть отклонена")
        self.assertEqual(self.full_state(), before)


INCOME, EXPENSE = StockAdjustment.KIND_INCOME, StockAdjustment.KIND_EXPENSE
OTHER = {INCOME: EXPENSE, EXPENSE: INCOME}


class KindWorld(ChangeWorld):
    def setUp(self):
        super().setUp()
        self.receipt(self.a, 2, 100)                 # товар для списаний

    def adjustment(self, kind, qty=1, price=100, post=True, **header):
        doc = self.doc(StockAdjustment, self.a, [(qty, "", price)], kind=kind, **header)
        if post:
            doc.post()
        return StockAdjustment.objects.get(pk=doc.pk)

    def snapshot(self):
        """Полное состояние учёта, виды и номера документов, счётчики нумерации."""
        kinds = sorted(StockAdjustment._base_manager.values_list("pk", "kind", "number", "status"))
        return self.full_state(), kinds

    def moves(self, doc):
        return sorted(StockMovement.objects.filter(doc_id=doc.pk, doc_type__in=StockAdjustment.DOC_TYPES)
                      .values_list("doc_type", "quantity", "amount"))


# ---------------------------------------------------------------------------
# Обе стороны × все пути записи вида
# ---------------------------------------------------------------------------

class KindBypassTests(KindWorld):
    def attempts(self, doc):
        new = OTHER[doc.kind]

        def plain(fields=None):
            obj = StockAdjustment.objects.get(pk=doc.pk)
            obj.kind = new
            return lambda: obj.save(**({"update_fields": fields} if fields is not None else {}))

        def bulk():
            obj = StockAdjustment.objects.get(pk=doc.pk)
            obj.kind = new
            return lambda: StockAdjustment.objects.bulk_update([obj], ["kind"])

        def upsert():
            obj = StockAdjustment.objects.get(pk=doc.pk)
            obj.kind = new
            return lambda: StockAdjustment.objects.bulk_create([obj], update_conflicts=True, update_fields=["kind"],
                                                               unique_fields=["id"])

        def through_editing():
            obj = StockAdjustment.objects.get(pk=doc.pk)
            with obj.editing():
                obj.kind = new
                obj.save()

        return {
            "save()": plain(), "save(update_fields=[kind])": plain(["kind"]),
            "QuerySet.update(kind=…)": lambda: StockAdjustment.objects.filter(pk=doc.pk).update(kind=new),
            "QuerySet.update(выражение)": lambda: StockAdjustment.objects.filter(pk=doc.pk).update(kind=Value(new)),
            "QuerySet.update(F)": lambda: StockAdjustment.objects.filter(pk=doc.pk).update(kind=F("reason")),
            "bulk_update([kind])": bulk(), "bulk_create(update_conflicts)": upsert(),
            "editing() с перепроведением": through_editing,
        }

    def test_both_directions_reject_every_path(self):
        for kind in (INCOME, EXPENSE):
            doc = self.adjustment(kind)
            for title, attempt in self.attempts(doc).items():
                before = self.snapshot()
                with self.subTest(kind=kind, attempt=title):
                    with self.assertRaises(StockError):
                        attempt()
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(StockAdjustment.objects.get(pk=doc.pk).kind, kind)

    def test_message_explains_the_correction_path(self):
        doc = self.adjustment(INCOME)
        doc.kind = EXPENSE
        with self.assertRaises(PostedChangeError) as caught:
            doc.save(update_fields=["kind"])
        text = str(caught.exception)
        self.assertIn("вид «Оприходование» → «Списание»", text)
        self.assertIn("Отмените проведение (со всеми проверками истории), измените вид черновика и проведите его "
                      "заново", text)

    def test_editing_rejects_kind_change_at_save_not_only_at_repost(self):
        """Внутри editing() отказ — уже при записи вида, а не только при
        перепроведении на выходе (там сработала бы проверка вида движений)."""
        doc = self.adjustment(INCOME)
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "Вид проведённого документа менять нельзя — и при "
                                                         "перепроведении тоже"):
            with doc.editing():
                doc.kind = EXPENSE
                doc.save(update_fields=["kind"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(doc.kind, INCOME)                        # объект вернулся к сохранённому виду

    def test_stale_object_cannot_change_kind_of_posted_document(self):
        draft = self.adjustment(INCOME, post=False)
        stale = StockAdjustment.objects.get(pk=draft.pk)          # открыт черновиком
        StockAdjustment.objects.get(pk=draft.pk).post()           # другой пользователь провёл
        before = self.snapshot()
        stale.kind = EXPENSE
        with self.assertRaises(PostedChangeError):
            stale.save()
        self.assertEqual(self.snapshot(), before)

    def test_document_with_movements_is_not_a_plain_draft(self):
        doc = self.adjustment(INCOME)
        StockAdjustment.objects.filter(pk=doc.pk).update(status=DOC_DRAFT)   # статус переписан мимо отмены
        before = self.snapshot()
        doc = StockAdjustment.objects.get(pk=doc.pk)
        doc.kind = EXPENSE
        with self.assertRaises(PostedChangeError):
            doc.save(update_fields=["kind"])
        self.assertEqual(self.snapshot(), before)

    def test_mass_kind_change_of_drafts_is_rejected_too(self):
        draft = self.adjustment(INCOME, post=False)
        before = self.snapshot()
        with self.assertRaisesMessage(PostedChangeError, "Массовая смена реквизита «вид» не поддерживается"):
            StockAdjustment.objects.filter(pk=draft.pk).update(kind=EXPENSE)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(StockAdjustment.objects.filter(pk=draft.pk).update(kind=INCOME, reason="без смены"), 1)


# ---------------------------------------------------------------------------
# Несохранённый и устаревший вид: проведение, отмена, удаление
# ---------------------------------------------------------------------------

class KindInMemoryTests(KindWorld):
    def test_comment_only_save_keeps_kind_and_numbering(self):
        for kind in (INCOME, EXPENSE):
            with self.subTest(kind=kind):
                doc = self.adjustment(kind)
                before = self.snapshot()
                doc.kind = OTHER[kind]
                doc.comment = "проверено"
                doc.save(update_fields=["comment"])
                stored = StockAdjustment.objects.get(pk=doc.pk)
                self.assertEqual((stored.kind, stored.comment), (kind, "проверено"))
                for operation in (doc.post, doc.unpost, doc.delete):
                    with self.assertRaisesMessage(PostedChangeError, "Загрузите документ заново"):
                        operation()
                StockAdjustment.objects.filter(pk=doc.pk).update(comment="")
                self.assertEqual(self.snapshot(), before)

    def test_stale_kind_after_draft_change_by_other_user(self):
        draft = self.adjustment(INCOME, post=False)
        stale = StockAdjustment.objects.get(pk=draft.pk)
        other = StockAdjustment.objects.get(pk=draft.pk)
        other.kind = EXPENSE
        other.save(update_fields=["kind"])                       # черновик — можно
        before = self.snapshot()
        for operation in (stale.post, stale.delete):
            with self.assertRaisesMessage(PostedChangeError, "в документе «Оприходование», в базе «Списание»"):
                operation()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(StockMovement.objects.filter(doc_id=draft.pk, doc_type__in=StockAdjustment.DOC_TYPES))


# ---------------------------------------------------------------------------
# Штатная последовательность «отменить → изменить черновик → провести»
# ---------------------------------------------------------------------------

class KindWorkflowTests(KindWorld):
    def correct(self, doc, kind):
        StockAdjustment.objects.get(pk=doc.pk).unpost()
        draft = StockAdjustment.objects.get(pk=doc.pk)
        draft.kind = kind
        draft.save(update_fields=["kind"])
        StockAdjustment.objects.get(pk=draft.pk).post()
        return StockAdjustment.objects.get(pk=doc.pk)

    def test_income_to_expense(self):
        doc = self.adjustment(INCOME)                            # 2 (приёмка) + 1 = 3 шт.
        self.assertEqual(self.moves(doc), [("adjustment_income", D("1.000"), D("100.00"))])
        doc = self.correct(doc, EXPENSE)
        self.assertEqual((doc.kind, doc.number, doc.is_posted), (EXPENSE, "00001", True))
        self.assertEqual(self.moves(doc), [("adjustment_expense", D("-1.000"), D("100.00"))])   # прежнего нет
        self.assertEqual(self.balances(), {("А", "W"): (D("1"), D("100.00"))})        # 2 − 1
        self.assertEqual(transition.kind_mismatches(), [])

    def test_expense_to_income(self):
        doc = self.adjustment(EXPENSE)                           # 2 − 1 = 1 шт.
        self.assertEqual(self.moves(doc), [("adjustment_expense", D("-1.000"), D("100.00"))])
        doc = self.correct(doc, INCOME)
        self.assertEqual(self.moves(doc), [("adjustment_income", D("1.000"), D("100.00"))])
        self.assertEqual(self.balances(), {("А", "W"): (D("3"), D("300.00"))})        # 2 + 1
        self.assertEqual(transition.kind_mismatches(), [])

    def test_unpost_checks_dependent_history(self):
        income = self.adjustment(INCOME)
        self.ship(self.a, 3)                                     # расходует и приёмку, и оприходование
        before = self.snapshot()
        with self.assertRaisesMessage(StockError, "остаток стал бы -1"):
            StockAdjustment.objects.get(pk=income.pk).unpost()
        self.assertEqual(self.snapshot(), before)

    def numbers(self):
        return sorted(DocumentNumber.objects.filter(doc_type__startswith="adjustment_").values_list(
            "doc_type", "last_number"))

    def test_number_moves_to_series_of_new_kind_without_damage(self):
        """Номер сохраняется и регистрируется в серии нового вида: счётчик
        прежней серии не уменьшается (пропуск), новой — не меньше номера,
        повторов в сериях нет."""
        doc = self.adjustment(INCOME)                            # оприходование № 00001
        self.correct(doc, EXPENSE)                               # списание № 00001
        self.assertEqual(self.numbers(), [("adjustment_expense", 1), ("adjustment_income", 1)])
        income, expense = self.adjustment(INCOME), self.adjustment(EXPENSE)
        self.assertEqual((income.number, expense.number), ("00002", "00002"))
        for kind in (INCOME, EXPENSE):
            numbers = list(StockAdjustment.objects.filter(kind=kind).values_list("number", flat=True))
            self.assertEqual(len(numbers), len(set(numbers)), kind)

    def test_number_taken_in_new_series_rejects_kind_change_wholly(self):
        self.adjustment(EXPENSE)                                 # списание № 00001
        draft = self.adjustment(INCOME, post=False)              # оприходование № 00001
        before = self.snapshot()
        draft.kind = EXPENSE
        with self.assertRaisesMessage(StockError, "Номер 00001 в 2026 году уже занят"):
            draft.save(update_fields=["kind"])
        self.assertEqual(self.snapshot(), before)                # вид, номер и счётчики прежние
        draft = StockAdjustment.objects.get(pk=draft.pk)
        draft.kind, draft.number = EXPENSE, ""                   # пустой номер — следующий в серии
        draft.save()
        self.assertEqual(StockAdjustment.objects.get(pk=draft.pk).number, "00002")


# ---------------------------------------------------------------------------
# Диагностика: вид документа ≠ вид движений (без исправления)
# ---------------------------------------------------------------------------

class KindDiagnosticsTests(KindWorld):
    def command(self, name, *args):
        out = StringIO()
        call_command(name, *args, stdout=out)
        return out.getvalue()

    def test_both_directions_are_found_and_blocked_until_kind_is_restored(self):
        # номера разные: после обмена видами каждый вернётся в свою серию без столкновения номеров
        to_expense, to_income = self.adjustment(INCOME), self.adjustment(EXPENSE, number="00002")
        # старые данные: вид переписан прямой записью мимо программы
        StockAdjustment._base_manager.filter(pk=to_expense.pk).update(kind=EXPENSE)
        StockAdjustment._base_manager.filter(pk=to_income.pk).update(kind=INCOME)
        before = self.snapshot()
        self.assertEqual(transition.kind_mismatches(), [
            ("adjustment_expense", to_expense.pk, ["adjustment_income"]),
            ("adjustment_income", to_income.pk, ["adjustment_expense"]),
        ])
        self.assertEqual(transition.line_mismatches(), [])        # не выдаётся за расхождение строк
        output = self.command("stock_transition", "--limit", "0")
        self.assertIn("вид документа не совпадает с видом его движений (требует решения, автоматически не "
                      "исправляется): 2", output)
        self.assertIn(f"списание id {to_expense.pk}: движения записаны как «оприходование»", output)
        self.assertIn("вида с движениями — 2", output)
        self.assertIn("Вид документа не совпадает с видом его движений: 2", self.command("check_costs", "--limit", "0"))
        for doc in (to_expense, to_income):
            for operation in ("post", "unpost", "delete"):
                with self.subTest(doc=doc.pk, operation=operation):
                    with self.assertRaisesMessage(PostedChangeError, "движения прежнего вида не убрались бы"):
                        getattr(StockAdjustment.objects.get(pk=doc.pk), operation)()
            with self.assertRaises(PostedChangeError):           # другой вид — нельзя
                fresh = StockAdjustment.objects.get(pk=doc.pk)
                fresh.kind = "other"
                fresh.save(update_fields=["kind"])
        self.assertEqual(self.snapshot(), before)                # диагностика и отказы ничего не меняют
        for doc, kind in ((to_expense, INCOME), (to_income, EXPENSE)):   # решение: вернуть вид движений
            fresh = StockAdjustment.objects.get(pk=doc.pk)
            fresh.kind = kind
            fresh.save(update_fields=["kind"])
        self.assertEqual(transition.kind_mismatches(), [])
        self.assertIn("Расхождений нет", self.command("check_costs", "--limit", "0"))


# ---------------------------------------------------------------------------
# Одновременно: смена вида черновика против его проведения (только PostgreSQL)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == "postgresql", "блокировки строк проверяются только на PostgreSQL")
class ConcurrentKindTests(OrgWorld, ConcurrencyMixin, TransactionTestCase):
    """Смена вида черновика (save) против его проведения — в обоих порядках,
    с подтверждённым ожиданием блокировки строки документа."""

    def setUp(self):
        self.make_world()

    def test_draft_kind_change_against_posting_in_any_order(self):
        from . import posting

        for first in ("post", "change"):
            with self.subTest(first=first):
                self.product = Product.objects.create(name=f"Вид {first}", unit=self.unit)
                draft = StockAdjustment.objects.create(kind=INCOME, organization=self.a, warehouse=self.w,
                                                       **self._when())
                draft.lines.create(product=self.product, quantity=1, price=100)

                def change():
                    obj = StockAdjustment.objects.get(pk=draft.pk)
                    obj.kind = EXPENSE
                    obj.save(update_fields=["kind"])

                operations = {"post": lambda: StockAdjustment.objects.get(pk=draft.pk).post(), "change": change}
                second = "change" if first == "post" else "post"
                pause = (services, "document_outflow") if first == "post" else (posting, "_stock_bound")
                results = dict(zip((first, second), self.run_overlapping(operations[first], operations[second],
                                                                         pause_on=pause)))
                self.assertIsNone(results[first])
                self.assertIsInstance(results[second], PostedChangeError)
                doc = StockAdjustment.objects.get(pk=draft.pk)
                moves = sorted(StockMovement.objects.filter(doc_id=draft.pk, doc_type__in=StockAdjustment.DOC_TYPES)
                               .values_list("doc_type", "quantity"))
                if first == "post":     # смена увидела проведённый документ
                    self.assertEqual((doc.kind, doc.is_posted, moves), (INCOME, True, [("adjustment_income", D("1"))]))
                else:                   # проведение увидело другой вид, чем у своего объекта
                    self.assertEqual((doc.kind, doc.is_posted, moves), (EXPENSE, False, []))
                self.assertEqual(transition.kind_mismatches(), [])
