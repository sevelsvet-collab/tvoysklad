"""Исправление складской истории несколькими документами сразу.

Пока у товара есть проблема, требующая решения (check_costs: «ТРЕБУЕТ
РЕШЕНИЯ»), операции с ним отклоняются. Обычно её устраняет одна операция в
программе — снять с проведения или перепровести названный документ. Если
мешающих документов несколько и по одному их не снять (каждая промежуточная
история тоже некорректна), команда выполняет все операции в одной транзакции
и проверяет историю один раз — в конце:

    python manage.py fix_stock_history --unpost transfer:12 --unpost customer_return:40
    python manage.py fix_stock_history --repost shipment:7 --apply --i-have-a-backup

Уточнение строки отгрузки в старых возвратах покупателей (check_costs:
«по отгрузке есть возвраты и по строкам, и без строки»): --link
СТРОКА_ВОЗВРАТА:СТРОКА_ОТГРУЗКИ (id строк из сообщения диагностики) — в
строке возврата указывается эта строка отгрузки, проведённый возврат
перепроводится. Строку выбирает человек; команда её не угадывает и
проверяет только, что это строка той же отгрузки с тем же товаром:

    python manage.py fix_stock_history --link 15:42 --link 16:43 --apply --i-have-a-backup

Без --apply операции выполняются и откатываются: команда только показывает,
останутся ли проблемы. С --apply результат записывается, только если после
всех операций у затронутых товаров не осталось проблем; иначе не меняется
ничего. Остальные проверки документов (запрет минуса, серийные номера,
количество возвратов) действуют как обычно. Даты и суммы документов команда
не меняет.
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.inventory import costing, services

DOCUMENTS = {
    "shipment": ("sales", "Shipment"),
    "customer_return": ("sales", "CustomerReturn"),
    "receipt": ("purchases", "Receipt"),
    "supplier_return": ("purchases", "SupplierReturn"),
    "transfer": ("inventory", "Transfer"),
    "adjustment": ("inventory", "StockAdjustment"),
    "adjustment_income": ("inventory", "StockAdjustment"),
    "adjustment_expense": ("inventory", "StockAdjustment"),
}


class _DryRun(Exception):
    """Откат пробного выполнения."""


class Command(BaseCommand):
    help = ("Снимает с проведения или перепроводит несколько документов в одной транзакции и проверяет "
            "складскую историю один раз в конце (без --apply ничего не записывает).")

    def add_arguments(self, parser):
        parser.add_argument("--unpost", action="append", default=[], metavar="ТИП:ID",
                            help="Снять документ с проведения (можно несколько раз), например transfer:12")
        parser.add_argument("--repost", action="append", default=[], metavar="ТИП:ID",
                            help="Перепровести документ по его текущим строкам (можно несколько раз)")
        parser.add_argument("--link", action="append", default=[], metavar="СТРОКА_ВОЗВРАТА:СТРОКА_ОТГРУЗКИ",
                            help="Указать в строке возврата покупателя строку отгрузки-основания (id строк); "
                                 "проведённый возврат перепроводится")
        parser.add_argument("--apply", action="store_true", help="Записать результат")
        parser.add_argument("--i-have-a-backup", action="store_true", dest="backup",
                            help="Подтверждение, что резервная копия базы сделана (обязательно с --apply)")

    def handle(self, *args, **options):
        operations = [("unpost", self._document(value)) for value in options["unpost"]]
        operations += [("repost", self._document(value)) for value in options["repost"]]
        links = [self._link(value) for value in options["link"]]
        if not operations and not links:
            raise CommandError("Укажите документы: --unpost ТИП:ID, --repost ТИП:ID или --link.")
        if options["apply"] and not options["backup"]:
            raise CommandError("Сделайте резервную копию базы и повторите с ключом --i-have-a-backup.")

        mode = "ИСПРАВЛЕНИЕ" if options["apply"] else "пробное выполнение, запись выключена"
        self.stdout.write(f"Исправление складской истории ({mode}).")
        try:
            with transaction.atomic():
                product_ids = sorted(set(self._products(doc for _, doc in operations))
                                     | {line.product_id for line, _ in links})
                services.lock_products(product_ids)
                with costing.defer_history_check():
                    for line, source in links:
                        # строка проведённого возврата меняется только вместе с его
                        # перепроведением — тем же механизмом, что форма (правило 10)
                        doc = type(line.document).objects.get(pk=line.document_id)
                        with doc.editing() as posted:
                            type(line).objects.filter(pk=line.pk).update(shipment_line=source)
                        self.stdout.write(f"  строка возврата id {line.pk} ({_label(doc)}): строка отгрузки "
                                          f"id {source.pk}" + ("; возврат перепроведён" if posted else ""))
                    for action, doc in operations:
                        doc = type(doc).objects.get(pk=doc.pk)
                        if not doc.is_posted:
                            raise CommandError(f"{_label(doc)} не проведён — снимать или перепроводить нечего.")
                        try:
                            if action == "unpost":
                                doc.unpost()
                            else:
                                doc.post()
                        except services.StockError as error:
                            raise CommandError(f"{_label(doc)}: операция отклонена — {error}") from error
                        word = "снят с проведения" if action == "unpost" else "перепроведён"
                        self.stdout.write(f"  {_label(doc)}: {word}")
                remaining = costing.remaining_problems(product_ids)
                if remaining:
                    self.stdout.write(self.style.ERROR(
                        "После этих операций история товаров всё ещё некорректна — ничего не записано:"
                    ))
                    for problem in remaining:
                        self.stdout.write(f"  товар {problem.product_id}: {problem.description}")
                    raise CommandError("Исправление отменено целиком: добавьте в команду документы, названные выше.")
                if not options["apply"]:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING(
                "Проблем после этих операций не останется. Ничего не записано: повторите с "
                "--apply --i-have-a-backup."
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Готово: операций {len(operations)}, история товаров (id: {', '.join(map(str, product_ids))}) корректна."
        ))

    def _document(self, value):
        kind, _, pk = value.partition(":")
        if kind not in DOCUMENTS or not pk.isdigit():
            raise CommandError(f"Неверный документ «{value}». Формат ТИП:ID, типы: {', '.join(DOCUMENTS)}.")
        model = apps.get_model(*DOCUMENTS[kind])
        doc = model.objects.filter(pk=int(pk)).first()
        if doc is None or (kind.startswith("adjustment_") and doc.DOC_TYPE != kind):
            raise CommandError(f"Документ «{value}» не найден.")
        return doc

    def _link(self, value):
        line_id, _, source_id = value.partition(":")
        if not (line_id.isdigit() and source_id.isdigit()):
            raise CommandError(f"Неверная связь «{value}». Формат СТРОКА_ВОЗВРАТА:СТРОКА_ОТГРУЗКИ (id).")
        line = apps.get_model("sales", "CustomerReturnLine").objects.select_related("document").filter(
            pk=int(line_id)).first()
        source = apps.get_model("sales", "ShipmentLine").objects.filter(pk=int(source_id)).first()
        if line is None or source is None:
            raise CommandError(f"Связь «{value}»: строка возврата или строка отгрузки не найдена.")
        if line.document.shipment_id != source.shipment_id or line.product_id != source.product_id:
            raise CommandError(
                f"Связь «{value}»: строка отгрузки должна быть из отгрузки-основания возврата и с тем же товаром."
            )
        return line, source

    def _products(self, docs):
        product_ids = set()
        for doc in docs:
            product_ids.update(doc.lines.values_list("product_id", flat=True))
            product_ids.update(pair[0] for pair in services.document_pairs(doc.DOC_TYPE, doc.pk))
        return sorted(product_ids)


def _label(doc):
    title = costing.DOC_TITLES.get(doc.DOC_TYPE, doc.DOC_TYPE)
    return f"{title} № {doc.number} (id {doc.pk})"
