"""Переход складского учёта на ключ «организация + склад + товар».

    python manage.py stock_transition                    # статус и диагностика, только чтение
    python manage.py stock_transition --apply --i-have-a-backup   # пересчёт, затем «готово»
    python manage.py stock_transition --fix-organization receipt:5=document --i-have-a-backup

Диагностика (без ключей ничего не записывает):

1. статус перехода и ход пересчёта;
2. складские движения без организации — документ-источник не найден
   (организация по умолчанию не ставится; решение — ключ --assign);
   документы, у которых организация документа и его движений (складских или
   номеров) расходится — решение ключом --fix-organization: «document»
   (движения переходят к организации документа) или «movements» (документу
   возвращается организация движений); в обоих случаях документ
   перепроводится через тот же механизм, что форма, с проверкой обеих историй;
   документы, у которых склад, дата или время расходятся с движениями (у
   перемещения расход сверяется со складом-отправителем, приход — со
   складом-получателем) — только перечень, решение человека в форме документа;
   документы, у которых строки расходятся с движениями (количество товара по
   направлению, серийные номера) — только перечень;
   документы, вид которых (оприходование / списание) не совпадает с видом их
   движений — только перечень, решение человека;
3. серийные номера без определённой организации (ввод старого остатка без
   документа): подсказка — организации, у которых на этом складе в день ввода
   был товар; решение — ключ --assign-serial;
4. нарушения, которые создаёт разделение: организация с запретом минуса
   расходовала чужой товар (остаток уходит в минус там, где по складу в
   целом минуса не было);
5. изменения себестоимости отгрузок «было → станет» и прибыль по месяцам,
   новые остатки по организациям, другие проблемы, требующие решения
   (перемещения чужого товара и т. п.).

Пересчёт (--apply): каждый товар — в своей транзакции под блокировкой
товара; остатки, стоимость, покрытия и себестоимость строятся из истории
документов по новым ключам; нарушения из п. 4 записываются и блокируют
операции с товаром, пока документы не исправлены целиком (или организации не
разрешили минус). Прерванный пересчёт оставляет статус «не готово» —
запустите снова: всё пересчитывается с начала, результат тот же. Статус
«готово» ставится только после полного прохода. Пока он не «готово»,
складские операции отклоняются, а отчёты показывают предупреждение.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Organization
from apps.inventory import costing, transition
from apps.inventory.models import StockMovement, StockTransitionIssue
from apps.inventory.services import StockError
from apps.sales.models import Shipment


class Command(BaseCommand):
    help = "Переход склада на учёт по организациям: диагностика (по умолчанию) и пересчёт (--apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Пересчитать по новым ключам и завершить переход")
        parser.add_argument("--i-have-a-backup", action="store_true", dest="backup",
                            help="Подтверждение, что резервная копия базы сделана (обязательно для записи)")
        parser.add_argument("--assign", action="append", default=[], metavar="ВИД:ID=ОРГАНИЗАЦИЯ",
                            help="Решение человека: движения документа без источника относятся к организации (id)")
        parser.add_argument("--assign-serial", action="append", default=[], metavar="ТОВАР:НОМЕР=ОРГАНИЗАЦИЯ",
                            help="Решение человека: чей серийный номер, введённый остатком без документа")
        parser.add_argument("--fix-organization", action="append", default=[], dest="fix_organization",
                            metavar="ВИД:ID=document|movements",
                            help="Решение человека по расхождению организации документа и его движений")
        parser.add_argument("--limit", type=int, default=50, help="Сколько строк каждого раздела печатать (0 — все)")

    def handle(self, *args, **options):
        writes = options["apply"] or options["assign"] or options["assign_serial"] or options["fix_organization"]
        if writes and not options["backup"]:
            raise CommandError("Команда изменит данные склада. Сделайте резервную копию базы и повторите с ключом "
                               "--i-have-a-backup.")
        for value in options["assign"]:
            doc, _, org = value.partition("=")
            doc_type, _, doc_id = doc.partition(":")
            if not (doc_id.isdigit() and org.isdigit() and Organization.objects.filter(pk=org).exists()):
                raise CommandError(f"Неверно: {value}. Формат: ВИД:ID=ID_ОРГАНИЗАЦИИ")
            count = transition.assign_organization(doc_type, int(doc_id), int(org))
            self.stdout.write(f"Движения {doc_type} id {doc_id} → организация {org}: {count}")
        for value in options["assign_serial"]:
            place, _, org = value.partition("=")
            product, _, number = place.partition(":")
            if not (product.isdigit() and number and org.isdigit() and Organization.objects.filter(pk=org).exists()):
                raise CommandError(f"Неверно: {value}. Формат: ID_ТОВАРА:НОМЕР=ID_ОРГАНИЗАЦИИ")
            count = transition.assign_serial(int(product), number, int(org))
            self.stdout.write(f"Номер {number} товара {product} → организация {org}: движений {count}")
        for value in options["fix_organization"]:
            doc, _, choice = value.partition("=")
            doc_type, _, doc_id = doc.partition(":")
            if not doc_id.isdigit():
                raise CommandError(f"Неверно: {value}. Формат: ВИД:ID=document|movements")
            try:
                organization = transition.fix_organization_mismatch(doc_type, int(doc_id), choice)
            except (transition.TransitionError, StockError) as exc:
                raise CommandError(f"{value}: {exc}") from exc
            self.stdout.write(f"{doc_type} id {doc_id}: документ и движения — организация "
                              f"«{Organization.objects.get(pk=organization)}», документ перепроведён")
        if options["apply"]:
            self._apply()
        else:
            self._diagnose(options["limit"])

    # ---------- пересчёт ----------

    def _apply(self):
        def progress(index, total, product_id):
            if index == total or index % 50 == 0:
                self.stdout.write(f"  пересчитано товаров: {index} из {total}")

        self.stdout.write("Пересчёт склада по организациям (запись).")
        try:
            found = transition.apply(progress)
        except transition.TransitionError as exc:
            raise CommandError(str(exc))
        blocking = [d for items in found.values() for d in costing.blocking(items)]
        state = transition.state()
        self.stdout.write(self.style.SUCCESS(f"Готово: {state.get_status_display()}. {state.message}"))
        if blocking:
            self.stdout.write(self.style.ERROR(
                f"Требуют решения: {len(blocking)} (товары id: "
                f"{', '.join(str(pk) for pk in sorted({d.product_id for d in blocking}))}). Операции с этими "
                "товарами отклоняются, пока документы не исправлены целиком (fix_stock_history или вручную)."))
            for d in blocking:
                self.stdout.write(f"  товар {d.product_id}: {d.description}")

    # ---------- диагностика ----------

    def _diagnose(self, limit):
        def cut(items):
            return items if not limit else items[:limit]

        out = self.stdout.write
        state = transition.state()
        out(f"Переход склада на учёт по организациям — только проверка, запись выключена.")
        out(f"Статус: {state.get_status_display()}"
            + (f" (пересчитано {state.products_done} из {state.products_total})" if state.products_total else ""))

        unresolved = transition.unresolved_movements()
        out("")
        out(f"1. Движения без организации (источник не найден, требует решения): {len(unresolved)}")
        for (doc_type, doc_id), count in cut(list(unresolved.items())):
            out(f"  {costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: движений {count} — "
                f"--assign {doc_type}:{doc_id}=ID_ОРГАНИЗАЦИИ")
        mismatches = transition.organization_mismatches()
        if mismatches:
            names = dict(Organization.objects.values_list("pk", "name"))
            out(f"  организация документа и его движений расходится (требует решения): {len(mismatches)}")
            for doc_type, doc_id, organization_id, movement_orgs in cut(mismatches):
                theirs = ", ".join(f"«{names.get(pk, pk)}»" for pk in movement_orgs)
                out(f"   — {costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: документ — "
                    f"«{names.get(organization_id, organization_id)}», движения — {theirs}: "
                    f"--fix-organization {doc_type}:{doc_id}=document (движения → организации документа) или "
                    f"{doc_type}:{doc_id}=movements (документ → организации движений)")
        requisites = transition.requisite_mismatches()
        if requisites:
            out(f"  склад, дата или время документа и его движений расходятся (требует решения, "
                f"автоматически не исправляется): {len(requisites)}")
            for doc_type, doc_id, texts in cut(requisites):
                out(f"   — {costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: {'; '.join(texts)}")
            out("     решение: перепровести документ в форме (движения примут его реквизиты) или вернуть "
                "документу реквизиты движений — тоже в форме, с проверкой истории")
        kinds = transition.kind_mismatches()
        if kinds:
            out(f"  вид документа не совпадает с видом его движений (требует решения, автоматически не "
                f"исправляется): {len(kinds)}")
            for doc_type, doc_id, movement_types in cut(kinds):
                theirs = ", ".join(f"«{costing.DOC_TITLES.get(kind, kind)}»" for kind in movement_types)
                out(f"   — {costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: движения записаны как {theirs}")
            out("     решение: не перепроводить (движения прежнего вида не убрались бы); вернуть документу вид его "
                "движений — единственная смена вида, допустимая у документа с движениями, — затем при "
                "необходимости отменить проведение и исправить черновик")
        lines = transition.line_mismatches()
        if lines:
            out(f"  строки документа и его движения расходятся (количество, серийные номера; требует решения, "
                f"автоматически не исправляется): {len(lines)}")
            for doc_type, doc_id, texts in cut(lines):
                out(f"   — {costing.DOC_TITLES.get(doc_type, doc_type)} id {doc_id}: {'; '.join(texts)}")
            out("     решение: перепровести документ в форме (движения примут строки) или вернуть строки "
                "к проведённым — тоже в форме")
        drafts = self._draft_sources()
        if drafts:
            out(f"  документы не проведены, а движения есть (организация взята из документа): {len(drafts)}")
            for line in cut(drafts):
                out(f"   — {line}")

        serial_rows = transition.unresolved_serials()
        out("")
        out(f"2. Серийные номера без определённой организации (требует решения): {len(serial_rows)}")
        for movement in cut(serial_rows):
            hint = self._serial_hint(movement)
            out(f"  товар {movement.serial.product_id} «{movement.serial.product}», номер {movement.serial.number}: "
                f"{movement.doc_number or movement.doc_type}, склад «{movement.warehouse}», {movement.date:%d.%m.%Y}; "
                f"{hint}")

        products = transition.products_to_recalculate()
        violations = []
        for product_id in products:
            for organization_id, warehouse_id, _, point, mine, whole in transition.split_violations(product_id):
                violations.append(transition.violation_text(product_id, organization_id, warehouse_id, point, mine,
                                                            whole))
        out("")
        out(f"3. Нарушения, которые создаёт разделение (минус у организации с запретом за счёт чужого товара): "
            f"{len(violations)}")
        for line in cut(violations):
            out(f"  {line}")

        changes, blocking, months = [], [], defaultdict(lambda: Decimal("0"))
        new_balances = []
        for product_id in products:
            plan = costing.build_plan(product_id)
            found = costing.differences(plan)
            blocking.extend(d for d in costing.blocking(found)
                            if d.kind not in ("organization_missing", "transition_negative"))
            for d in found:
                if d.kind == "shipment_line" and d.stored[1] != d.expected[1] or (
                        d.kind == "shipment_line" and len(d.expected) > 2 and d.stored[-1] != d.expected[-1]):
                    changes.append((product_id, d))
            for (organization_id, warehouse_id), (qty, value, _) in sorted(plan.balances.items(), key=str):
                if qty:
                    new_balances.append((product_id, organization_id, warehouse_id, qty, value))
        dates = dict(Shipment.objects.filter(pk__in={d.docs[0][1] for _, d in changes}).values_list("pk", "date"))
        for _, d in changes:
            months[dates[d.docs[0][1]].strftime("%m.%Y")] -= d.expected[1] - d.stored[1]
        out("")
        out(f"4. Строки отгрузок, у которых изменится себестоимость или непокрытое количество: {len(changes)}")
        for product_id, d in cut(changes):
            out(f"  товар {product_id}: {d.description} — было {_show(d.stored)}, станет {_show(d.expected)}")
        out("  Прибыль «Продажи и прибыль» по месяцам продажи изменится на: "
            + (", ".join(f"{month}: {_plain(value)}" for month, value in sorted(
                months.items(), key=lambda item: (item[0][3:], item[0][:2]))) or "без изменений"))
        names = dict(Organization.objects.values_list("pk", "name"))
        out("")
        out(f"5. Остатки после пересчёта по организациям (ненулевые): {len(new_balances)}")
        for product_id, organization_id, warehouse_id, qty, value in cut(new_balances):
            owner = names.get(organization_id, "не определена")
            out(f"  товар {product_id}, склад {warehouse_id}, «{owner}»: {_plain(qty)} шт. на {_plain(value)}"
                + (" — дефицит, стоимость не определена" if qty < 0 else ""))
        out("")
        out(f"6. Другие проблемы, требующие решения (перемещения чужого товара и т. п.): {len(blocking)}")
        for d in cut(blocking):
            out(f"  товар {d.product_id}: {d.description}")
        out("")
        out(f"Итого: без организации — {len(unresolved)}, номеров без организации — {len(serial_rows)}, "
            f"нарушений разделения — {len(violations)}, изменённых строк отгрузок — {len(changes)}, "
            f"других проблем — {len(blocking)}, расхождений организации с движениями — {len(mismatches)}, "
            f"склада, даты или времени с движениями — {len(requisites)}, вида с движениями — {len(kinds)}, "
            f"строк с движениями — {len(lines)}. "
            "Ничего не изменено.")
        if StockTransitionIssue.objects.exists():
            out(f"Записанных нарушений после перехода: {StockTransitionIssue.objects.count()}")

    @staticmethod
    def _draft_sources():
        from django.apps import apps

        lines = []
        sources = {"receipt": "purchases.Receipt", "supplier_return": "purchases.SupplierReturn",
                   "shipment": "sales.Shipment", "customer_return": "sales.CustomerReturn",
                   "transfer": "inventory.Transfer", "adjustment_income": "inventory.StockAdjustment",
                   "adjustment_expense": "inventory.StockAdjustment"}
        keys = set(StockMovement.objects.values_list("doc_type", "doc_id"))
        for doc_type, model_name in sources.items():
            ids = {doc_id for kind, doc_id in keys if kind == doc_type}
            if not ids:
                continue
            for pk in apps.get_model(model_name).objects.filter(pk__in=ids).exclude(status="posted").values_list(
                    "pk", flat=True):
                lines.append(f"{costing.DOC_TITLES.get(doc_type, doc_type)} id {pk}")
        return lines

    @staticmethod
    def _serial_hint(movement):
        """Организации, у которых на складе в день ввода был товар (только
        подсказка, назначение — решение человека)."""
        state = costing.state_on(movement.serial.product_id, movement.date)
        owners = sorted({org for (org, wh), (qty, _, _) in state.items() if wh == movement.warehouse_id and qty > 0
                         and org})
        names = dict(Organization.objects.filter(pk__in=owners).values_list("pk", "name"))
        if not owners:
            return "товара на складе в этот день не было ни у одной организации — решает человек"
        listed = ", ".join(f"«{names[pk]}» (id {pk})" for pk in owners)
        return (f"товар был у: {listed} — подсказка, не решение; "
                f"--assign-serial {movement.serial.product_id}:{movement.serial.number}=ID_ОРГАНИЗАЦИИ")


def _plain(value):
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _show(value):
    if isinstance(value, tuple):
        return "(" + ", ".join(_plain(v) for v in value) + ")"
    return _plain(value)
