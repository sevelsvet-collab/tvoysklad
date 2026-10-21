"""Диагностика себестоимости: сверяет базу с хронологическим расчётом.

По умолчанию — только чтение (dry-run): команда показывает расхождения
и ничего не записывает.

    python manage.py check_costs                # все товары
    python manage.py check_costs --product 15   # один товар
    python manage.py check_costs --limit 0      # не обрезать список

Что показывает:

* расхождения, которые устраняет пересчёт: суммы и цены движений и строк
  отгрузок, стоимость остатков, не учтённая скидка поставщика в старых
  приёмках. Сравниваются суммы, а не только цена единицы; суммы берутся из
  исходных документов, а не восстанавливаются из округлённой цены;
* проблемы, которые пересчёт не решает и не угадывает («ТРЕБУЕТ РЕШЕНИЯ»):
  движения, которые нельзя однозначно сопоставить со строками документа;
  возвраты покупателей с неподходящим основанием; перемещения, забиравшие
  больше, чем было на складе; возвраты непоступившего товара на другой склад;
  возвраты без строки отгрузки, когда строк товара несколько и у части есть
  дефицит. Пока такая проблема есть, операции с товаром отклоняются —
  исправляет её снятие с проведения или изменение указанных документов
  (в том числе пакетно: команда fix_stock_history);
* неопределённость: возвраты, исходная стоимость которых не сохранилась;
* дефицит: продано (списано) раньше поступления — себестоимость этих единиц
  ещё не определена, прибыль по отгрузке предварительная.
* документы, у которых организация документа и его движений расходится
  (старые данные или прямая правка базы) — только перечень; решение —
  manage.py stock_transition --fix-organization;
* документы, у которых склад, дата или время расходятся с движениями (у
  перемещения — склад-отправитель для расхода, склад-получатель для
  прихода) — только перечень, без исправления;
* документы, у которых строки расходятся с движениями (количество товара
  по направлению, серийные номера) — только перечень, без исправления;
* документы, вид которых (оприходование / списание) не совпадает с видом
  их движений — только перечень, без исправления.

Исправление включается только явно, двумя ключами сразу:

    python manage.py check_costs --apply --i-have-a-backup

Товар исправляется целиком в одной транзакции или не исправляется вовсе:
если у товара есть нерешённая проблема, для него не записывается ничего.

Запускать на копии базы до того, как пользователей пустят в новую версию.
Порядок описан в docs/cost-recalculation.md.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.inventory import costing, services, transition
from apps.inventory.models import StockBalance, StockMovement
from apps.sales.models import Shipment

KIND_TITLES = {
    "receipt_cost": "Стоимость поступлений не совпадает со строкой приёмки (скидка поставщика)",
    "movement": "Себестоимость движений",
    "shipment_line": "Себестоимость строк отгрузок (отчёт «Продажи и прибыль»)",
    "balance": "Остатки: количество, стоимость и средняя",
    "amount_missing": "Точная сумма ещё не записана (данные до обновления; с прежним расчётом совпадает)",
    "balance_extra": "Остатки без движений",
    "link": "Старые движения: строка документа определена однозначно, ссылка будет записана",
    "unmatched": "ТРЕБУЕТ РЕШЕНИЯ — движения не сопоставляются со строками документа",
    "return_basis": "ТРЕБУЕТ РЕШЕНИЯ — возвраты покупателей с неподходящим основанием",
    "return_uncertain": "НЕОПРЕДЕЛЁННОСТЬ — исходная стоимость возврата не подтверждена",
    "cover": "Записи покрытия дефицита поступлениями",
    "transfer_deficit": "ТРЕБУЕТ РЕШЕНИЯ — перемещения забирали больше, чем было на складе",
    "return_elsewhere": "ТРЕБУЕТ РЕШЕНИЯ — возврат непоступившего товара на другой склад",
    "return_ambiguous": "ТРЕБУЕТ РЕШЕНИЯ — возврат без строки отгрузки: строк товара несколько, у части дефицит",
    "return_mixed": "ТРЕБУЕТ РЕШЕНИЯ — по отгрузке есть возвраты и по строкам, и без строки",
    "return_unlinked": "ЗАМЕЧАНИЕ — возврат без строки отгрузки: себестоимость восстановлена по строкам в целом",
    "deficit": "Непокрытый дефицит — продано раньше поступления, себестоимость предварительная",
    "organization_missing": "ТРЕБУЕТ РЕШЕНИЯ — движения без организации (источник не найден при переносе)",
    "transition_negative": "ТРЕБУЕТ РЕШЕНИЯ — после перехода на учёт по организациям: минус за счёт чужого товара",
}
WITHOUT_VALUES = costing.BLOCKING | costing.NOTES


class Command(BaseCommand):
    help = "Показывает расхождения себестоимости с хронологическим расчётом (без --apply ничего не меняет)."

    def add_arguments(self, parser):
        parser.add_argument("--product", type=int, action="append", help="ID товара (можно несколько раз)")
        parser.add_argument("--limit", type=int, default=50,
                            help="Сколько расхождений каждого вида печатать (0 — все)")
        parser.add_argument("--apply", action="store_true",
                            help="Записать исправления. Без этого ключа команда только читает")
        parser.add_argument("--i-have-a-backup", action="store_true", dest="backup",
                            help="Подтверждение, что резервная копия базы сделана (обязательно с --apply)")

    def handle(self, *args, **options):
        if options["apply"] and not options["backup"]:
            raise CommandError(
                "Исправление меняет себестоимость в движениях, строках отгрузок и остатках. "
                "Сделайте резервную копию базы и повторите с ключом --i-have-a-backup."
            )
        product_ids = options["product"] or sorted(
            set(StockMovement.objects.values_list("product_id", flat=True))
            | set(StockBalance.objects.values_list("product_id", flat=True))
        )
        mode = "ИСПРАВЛЕНИЕ" if options["apply"] else "только проверка, запись выключена"
        self.stdout.write(f"Проверка себестоимости ({mode}). Товаров: {len(product_ids)}")

        found, fixed, skipped = [], [], []
        for product_id in product_ids:
            if options["apply"]:
                with transaction.atomic():
                    services.lock_products([product_id])
                    result, applied = costing.repair(product_id)
                if applied:
                    fixed.extend(costing.fixable(result))
                else:
                    skipped.append(product_id)
            else:
                result = costing.differences(costing.build_plan(product_id))
            found.extend(result)

        self._report(found, options["limit"])
        self._by_month(found)
        self.stdout.write("")
        fixable, blocking, notes = costing.fixable(found), costing.blocking(found), costing.notes(found)
        if not fixable and not blocking:
            self.stdout.write(self.style.SUCCESS("Расхождений нет."))
        elif options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Исправлено расхождений: {len(fixed)}."))
            if skipped:
                self.stdout.write(self.style.ERROR(
                    f"Товары НЕ исправлены и не изменены (id: {_ids(skipped)}): есть проблемы, требующие "
                    f"решения ({len(blocking)}). Исправьте документы и запустите команду снова."
                ))
        else:
            self.stdout.write(self.style.WARNING(
                f"Найдено расхождений: {len(fixable)} (товаров: {len({d.product_id for d in fixable})}). "
                "Ничего не записано."
            ))
            if blocking:
                self.stdout.write(self.style.ERROR(
                    f"Требуют решения до исправления: {len(blocking)} "
                    f"(товары id: {_ids(sorted({d.product_id for d in blocking}))}). "
                    "Такие товары исправление пропустит целиком."
                ))
        if blocking:
            self.stdout.write(
                "Как исправить: измените или снимите с проведения документы, названные выше "
                "(по одному — в программе, несколько сразу — командой fix_stock_history). "
                "Даты и суммы автоматически не меняются."
            )
        uncertain = [d for d in notes if d.kind == "return_uncertain"]
        deficits = [d for d in notes if d.kind == "deficit"]
        if uncertain:
            self.stdout.write(self.style.WARNING(
                f"Неопределённостей: {len(uncertain)} — значения оставлены как есть, проверьте их вручную."
            ))
        if deficits:
            self.stdout.write(self.style.WARNING(
                f"Непокрытых дефицитов: {len(deficits)} (товаров: {len({d.product_id for d in deficits})}) — "
                "себестоимость и прибыль этих документов предварительные, уточнятся после поступления. "
                "Если продажа раньше поступления не задумана, проверьте дату и время документов."
            ))
        mismatches = transition.organization_mismatches()
        if mismatches:
            # программа такого не допускает (posting, правило 9) — старые данные или прямая правка базы
            self.stdout.write(self.style.ERROR(
                f"Организация документа и его движений расходится: {len(mismatches)} "
                f"(документы: {', '.join(f'{kind}:{pk}' for kind, pk, _, _ in mismatches[:10])}"
                f"{' …' if len(mismatches) > 10 else ''}). Ничего не переписано; решение и исправление — "
                "manage.py stock_transition (ключ --fix-organization)."
            ))
        requisites = transition.requisite_mismatches()
        if requisites:
            # склад, дата или время документа ≠ его движений (у перемещения — по сторонам)
            self.stdout.write(self.style.ERROR(
                f"Склад, дата или время документа и его движений расходятся: {len(requisites)} "
                f"(документы: {', '.join(f'{kind}:{pk}' for kind, pk, _ in requisites[:10])}"
                f"{' …' if len(requisites) > 10 else ''}). Ничего не переписано; подробности — "
                "manage.py stock_transition, решение — перепроведение документа в форме."
            ))
        kinds = transition.kind_mismatches()
        if kinds:
            # вид оприходования/списания ≠ виду движений — перепроводить нельзя
            self.stdout.write(self.style.ERROR(
                f"Вид документа не совпадает с видом его движений: {len(kinds)} "
                f"(документы: {', '.join(f'{kind}:{pk}' for kind, pk, _ in kinds[:10])}"
                f"{' …' if len(kinds) > 10 else ''}). Ничего не переписано; не перепроводите — подробности и "
                "решение: manage.py stock_transition."
            ))
        lines = transition.line_mismatches()
        if lines:
            # строки добавили/убрали или изменили номера мимо перепроведения
            self.stdout.write(self.style.ERROR(
                f"Строки документа и его движения расходятся: {len(lines)} "
                f"(документы: {', '.join(f'{kind}:{pk}' for kind, pk, _ in lines[:10])}"
                f"{' …' if len(lines) > 10 else ''}). Ничего не переписано; подробности — "
                "manage.py stock_transition, решение — перепроведение документа в форме."
            ))

    def _report(self, found, limit):
        blocked = {d.product_id for d in costing.blocking(found)}
        for kind, title in KIND_TITLES.items():
            items = [d for d in found if d.kind == kind]
            if not items:
                continue
            self.stdout.write("")
            self.stdout.write(f"{title}: {len(items)}")
            for d in items[:limit] if limit else items:
                line = f"  товар {d.product_id}: {d.description}"
                if kind not in WITHOUT_VALUES:
                    line += f" — в базе {_show(d.stored)}, по расчёту {_show(d.expected)}"
                    if d.product_id in blocked:
                        line += " (не будет записано, пока у товара есть проблема, требующая решения)"
                self.stdout.write(line)
            if limit and len(items) > limit:
                self.stdout.write(f"  … и ещё {len(items) - limit}")


    def _by_month(self, found):
        """Себестоимость отгрузок по месяцам продажи: в базе и по расчёту —
        прибыль каких периодов изменит исправление."""
        lines = [d for d in found if d.kind == "shipment_line" and d.docs]
        if not lines:
            return
        dates = dict(Shipment.objects.filter(pk__in={d.docs[0][1] for d in lines}).values_list("pk", "date"))
        months = defaultdict(lambda: [Decimal("0"), Decimal("0")])
        for d in lines:
            month = dates[d.docs[0][1]].strftime("%m.%Y")
            months[month][0] += d.stored[1]
            months[month][1] += d.expected[1]
        self.stdout.write("")
        self.stdout.write("Себестоимость изменённых строк отгрузок по месяцам продажи (прибыль месяца изменится на "
                          "разницу с обратным знаком):")
        for month in sorted(months, key=lambda m: (m[3:], m[:2])):
            stored, expected = months[month]
            self.stdout.write(f"  {month}: в базе {stored:f}, по расчёту {expected:f}, разница {expected - stored:f}")


def _ids(values):
    return ", ".join(str(v) for v in values)


def _show(value):
    if value is None:
        return "нет"
    if isinstance(value, tuple):
        return "(" + ", ".join(_show(v) for v in value) + ")"
    return f"{value:f}" if hasattr(value, "quantize") else str(value)
