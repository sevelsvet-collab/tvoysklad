"""Создание документа на основании другого: счёт → отгрузка, отгрузка → возврат,
приёмка → возврат поставщику.

Шапка и все строки создаются одной транзакцией: при ошибке на любой строке
не остаётся ни пустого документа, ни сдвинутого счётчика номеров.
"""
from django.db import transaction

# Поля строки, которые ввёл пользователь. Вычисляемые при проведении
# (себестоимость и т.п.) и статус проведения не переносятся.
LINE_FIELDS = ("product", "quantity", "price", "discount", "vat_rate", "serial_numbers")


@transaction.atomic
def create_based_on(source, document_model, line_model, line_fk, header, fields=LINE_FIELDS, rows=None):
    """Создаёт документ document_model с шапкой header и копией строк source.

    line_fk — имя поля строки, которое ссылается на новый документ.
    При полном переносе итог нового документа равен итогу исходного.
    rows — готовые значения строк вместо полной копии (например, только
    ещё не возвращённое количество для возврата).
    """
    document = document_model.objects.create(**header)
    if rows is None:
        rows = [{name: getattr(line, name) for name in fields} for line in source.lines.all().order_by("pk")]
    for values in rows:
        line_model.objects.create(**{line_fk: document}, **values)
    return document


def create_return(source, document_model, line_model, header, fields=LINE_FIELDS):
    """Возврат на основании отгрузки или приёмки: строки заполняются тем, что
    ещё не возвращено проведёнными возвратами. Возвращает (документ, None)
    или (None, сообщение), если возвращать больше нечего.

    Это только заполнение черновика: окончательно количество проверяется при
    проведении — черновиков по одному основанию может быть несколько.
    """
    from apps.inventory import return_limits

    rows = return_limits.remaining_rows(source, fields)
    if not rows:
        return None, return_limits.all_returned_message(source)
    return create_based_on(source, document_model, line_model, "document", header, fields, rows=rows), None
