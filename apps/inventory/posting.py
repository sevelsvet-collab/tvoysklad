"""Миксин проведения документов: черновик ⇄ проведён.

Проведение создаёт движения по складу, отмена — удаляет их. Документ обязан
задать DOC_TYPE и метод build_specs().

Правила согласованности (общие для формы документа, кнопок, массовых операций
и административной панели — все они приходят в post / unpost / delete):

1. Каждая операция — одна транзакция. Сначала блокируется строка документа,
   затем товары, которые она затрагивает (в порядке возрастания id). Проверка
   остатка и его изменение идут под этими блокировками, поэтому два
   одновременных расхода последнего товара не пройдут оба.
2. Повторное проведение сначала убирает прежние движения документа, поэтому
   двойных движений не бывает.
3. Если минус запрещён, операция не может создать или углубить отрицательный
   остаток ни в одной точке хронологии (services.check_history): остаток
   проверяется после каждой операции в порядке «дата, время, момент создания
   документа» — том же, по которому считается себестоимость. Это касается и
   отмены или удаления приёмки, товар из которой уже отгружен, и отгрузки,
   поставленной раньше прихода в тот же день. Минус, существовавший до
   операции, её не блокирует, пока она его не углубляет.
4. История серийного номера проверяется всегда, даже когда минус разрешён.
5. При любой ошибке бросается StockError, и вся операция откатывается.
6. Проведение, перепроведение, отмена и удаление пересчитывают себестоимость
   товаров документа по всей хронологии (costing.recalculate): обновляются
   движения, строки отгрузок и остатки — и у последующих документов тоже.
8. Возвраты по основанию ограничены количеством в основании, сверяются по
   организации, контрагенту и серийным номерам; основание нельзя уменьшить
   ниже уже возвращённого (apps/inventory/return_limits.py).
7. Возврат покупателя с основанием требует, чтобы отгрузка-основание была
   проведена, стояла раньше возврата и содержала товар. Операция, которая это
   нарушает (проведение такого возврата, отмена, удаление или перенос
   отгрузки), отклоняется — возврат не превращается молча в «без основания».
9. Учётные реквизиты документа — организация, склад (у перемещения — оба
   склада), дата и время (STOCK_FIELDS) — всегда совпадают с реквизитами его
   движений, складских и серийных номеров. У проведённого документа (и у
   документа, у которого есть движения) их меняет только editing() /
   change_organization(): изменение и перепроведение — одна транзакция,
   проверяются прежняя и новая история, возвраты и основания. Отклоняются до
   записи (PostedChangeError, смена организации — OrganizationChangeError):
   обычное сохранение с другими значениями, массовые update() /
   bulk_update() / bulk_create(update_conflicts) и проведение объекта с
   несохранёнными реквизитами. Сравниваются фактически записываемые
   значения со свежим состоянием базы под блокировкой строки документа;
   выражение (F(), Case(), подзапрос) вместо значения считается изменением.
   Черновик меняется как обычно.
10. Строки проведённого документа (и документа с движениями) — товар,
   количество, цена, скидка, НДС, серийные номера, связь строки возврата с
   исходной строкой, сам документ строки — добавляются, меняются и удаляются
   только внутри editing() этого документа (или самим post()): после всех
   изменений документ перепроводится один раз. Разрешение выдаётся
   конкретному документу в текущей транзакции (_document_scope); вместе с
   ним записаны товары, уже заблокированные операцией, — новый товар строки
   должен быть среди них (editing(products=…)), блокировки не добираются в
   обратном порядке. Остальные пути — save(), delete(), создание строки,
   связанный менеджер (create, add, update, удаление выборки), QuerySet
   update()/delete(), bulk_create()/bulk_update() — проверяют актуальный
   статус документа под блокировкой его строки и отклоняются до записи
   (PostedChangeError). Перенос строки между документами, если хотя бы один
   проведён, не поддерживается. Строку нельзя удалить, если на неё ссылается
   строка проведённого документа (возврат → строка отгрузки). Расчётные поля
   (себестоимость строки отгрузки) пишет пересчёт, они не защищаются.
11. Вид документа (IDENTITY_FIELDS: оприходование или списание) задаёт тип
   его движений. У проведённого документа и у документа, у которого есть
   движения (даже со статусом «черновик»), вид не меняется — в том числе
   через editing(): исправление — отменить проведение (со всеми проверками
   истории), изменить вид черновика и провести заново. Единственная
   допустимая смена вида у документа с движениями — вернуть ему вид его же
   движений (исправление старых данных). У черновика без движений вид
   меняется сохранением: номер сохраняется, регистрируется в серии нового
   вида и не должен быть там занят. Массовая смена вида (update(),
   bulk_update(), bulk_create(update_conflicts)) не поддерживается. Проведение,
   отмена и удаление объекта с несохранённым или устаревшим видом, а также
   документа, чьи движения записаны другим видом, отклоняются.
12. Основание возврата (BASIS_FIELDS: приёмка у возврата поставщику, отгрузка
   у возврата покупателя) — учётный реквизит (правило 9): у проведённого
   возврата и возврата с движениями его меняют, ставят и снимают только в
   editing() — с проверкой нового основания (return_limits.check_return:
   количество, организация, контрагент, хронология, товар, номера, строки
   отгрузки) и перепроведением; прежнее основание освобождает возвращённое.
   Массовая смена основания не поддерживается ни у каких возвратов: связи
   строк возврата покупателя с прежней отгрузкой снимает только save().
"""
import contextvars
import datetime
from contextlib import contextmanager, nullcontext
from typing import NamedTuple

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import connection, models, transaction

from apps.core.constants import DOC_DRAFT, DOC_POSTED

EDITING = "_stock_editing"   # объект внутри editing() проведённого документа

TITLES = {
    "organization": "организация", "warehouse": "склад", "warehouse_from": "склад-отправитель",
    "warehouse_to": "склад-получатель", "date": "дата", "time": "время", "kind": "вид",
    "receipt": "приёмка-основание", "shipment": "отгрузка-основание",
}
UNSAVED = {
    "organization": "организация изменена, но не сохранена", "warehouse": "склад изменён, но не сохранён",
    "warehouse_from": "склад-отправитель изменён, но не сохранён",
    "warehouse_to": "склад-получатель изменён, но не сохранён", "date": "дата изменена, но не сохранена",
    "time": "время изменено, но не сохранено",
    "receipt": "приёмка-основание изменена, но не сохранена",
    "shipment": "отгрузка-основание изменена, но не сохранена",
}
# Массовая смена этих реквизитов не поддерживается ни у каких документов — как менять по одному
SINGLE_ONLY_ADVICE = {
    "kind": "у черновика — сохранением (номер проверяется в серии нового вида), у проведённого — только "
            "отменой проведения, правкой черновика и новым проведением",
    "receipt": "у черновика — сохранением, у проведённого — в editing() с проверкой нового основания и "
               "перепроведением",
    "shipment": "у черновика — сохранением (связи строк с прежней отгрузкой снимаются), у проведённого — в "
                "editing() с проверкой нового основания и перепроведением",
}


def stock_fields(model, names=None):
    """Учётные реквизиты модели (поля). names — только те, что среди этих
    имён полей или столбцов («warehouse» и «warehouse_id» — одно поле)."""
    fields = [model._meta.get_field(name) for name in model.STOCK_FIELDS]
    if names is None:
        return fields
    wanted = set()
    for name in names:
        try:
            wanted.add(model._meta.get_field(name).name)
        except FieldDoesNotExist:
            continue          # неизвестное имя — ошибку покажет сам Django
    return [field for field in fields if field.name in wanted]


def identity_fields(model, names=None):
    """Поля вида документа (правило 11) — как stock_fields() для IDENTITY_FIELDS."""
    fields = [model._meta.get_field(name) for name in model.IDENTITY_FIELDS]
    if names is None:
        return fields
    wanted = set()
    for name in names:
        try:
            wanted.add(model._meta.get_field(name).name)
        except FieldDoesNotExist:
            continue
    return [field for field in fields if field.name in wanted]


def single_only_fields(model, names):
    """Реквизиты среди names, массовая смена которых не поддерживается: вид
    (правило 11) и основание возврата (правило 12)."""
    singles = {*model.IDENTITY_FIELDS, *model.BASIS_FIELDS}
    found = []
    for name in names:
        try:
            field = model._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        if field.name in singles and field not in found:
            found.append(field)
    return found


def _is_expression(value):
    return hasattr(value, "resolve_expression")


def written_value(field, value):
    """Значение так, как его запишет база: объект → id, строка даты → дата."""
    if isinstance(value, models.Model):
        value = value.pk
    return field.to_python(value)


def _differs(field, value, stored):
    return _is_expression(value) or written_value(field, value) != stored


def show_value(field, value):
    """Значение реквизита для сообщения: «Склад», 02.01.2026, 12:00."""
    if _is_expression(value):
        return "выражение"
    value = written_value(field, value)
    if value is None:
        return "не указано"
    if field.is_relation:
        obj = field.related_model._base_manager.filter(pk=value).first()
        return f"«{obj}»" if obj else f"id {value}"
    if isinstance(value, datetime.date):
        return f"{value:%d.%m.%Y}"
    if isinstance(value, datetime.time):
        return f"{value:%H:%M:%S}" if value.second else f"{value:%H:%M}"
    if field.choices:
        return f"«{dict(field.flatchoices).get(value, value)}»"
    return str(value)


def _locked_rows(queryset, fields):
    """Блокирует документы выборки по возрастанию id (её фильтр сохраняется);
    возвращает {id: {столбец: значение в базе}} для полей fields."""
    rows = queryset.order_by("pk")
    if connection.features.has_select_for_update:
        if connection.features.has_select_for_update_of:
            rows = rows.select_for_update(of=("self",))
        else:
            rows = rows.select_for_update()
    return {row["pk"]: row for row in rows.values("pk", *[field.attname for field in fields])}


def _stock_bound(model, ids):
    """Документы, чьи реквизиты уже записаны в складской учёт: проведённые
    или имеющие движения (складские или серийных номеров)."""
    from apps.inventory.models import SerialMovement, StockMovement

    ids = list(ids)
    if not ids:
        return set()
    bound = set(model._base_manager.filter(pk__in=ids, status=DOC_POSTED).values_list("pk", flat=True))
    bound.update(StockMovement.objects.filter(doc_type__in=model.stock_doc_types(), doc_id__in=ids)
                 .values_list("doc_id", flat=True))
    bound.update(SerialMovement.objects.filter(doc_type=model._meta.model_name, doc_id__in=ids)
                 .values_list("doc_id", flat=True))
    return bound


def _error_class(fields):
    from apps.inventory import services

    if any(field.name == "organization" for field in fields):
        return services.OrganizationChangeError
    return services.PostedChangeError


def _reject_mass_change(model, ids, fields):
    bound = _stock_bound(model, ids)
    if bound:
        doc = model._base_manager.get(pk=min(bound))
        more = f" (и ещё {len(bound) - 1})" if len(bound) > 1 else ""
        titles = ", ".join(TITLES[field.name] for field in fields)
        raise _error_class(fields)(
            f"Массовое изменение учётных реквизитов ({titles}) проведённых документов не поддерживается: "
            f"{doc}{more} проведён или имеет складские движения. Меняйте документы по одному — в форме "
            "документа или через editing(): документ перепроводится, проверяются прежняя и новая история"
        )


def _reject_identity_change(model, ids, fields):
    """Массовая смена вида или основания не поддерживается ни у каких
    документов: у проведённых она требует перепроведения (основание) или
    запрещена (вид), у черновиков save() проверяет номер в серии нового вида и
    снимает связи строк с прежней отгрузкой."""
    if ids:
        from apps.inventory import services

        doc = model._base_manager.get(pk=min(ids))
        more = f" (и ещё {len(ids) - 1})" if len(ids) > 1 else ""
        titles = ", ".join(TITLES[field.name] for field in fields)
        advice = "; ".join(SINGLE_ONLY_ADVICE[field.name] for field in fields)
        raise services.PostedChangeError(
            f"Массовая смена реквизита «{titles}» не поддерживается: {doc}{more}. Меняйте по одному: {advice}"
        )


class PostableQuerySet(models.QuerySet):
    """Массовые изменения складских документов — по правилу 9.

    * update(реквизит=…) — документы выборки блокируются по возрастанию id;
      если среди тех, у кого реквизит изменится, есть проведённый или
      имеющий движения, — отказ всего изменения до записи. Выражение
      (F(), Case(), подзапрос) вместо значения считается изменением у всех
      документов выборки.
    * bulk_update(objs, [..., реквизит]) — то же для пакета: одна
      недопустимая строка — отказ всего пакета до записи.
    * bulk_create(update_conflicts=True, update_fields=[..., реквизит]) —
      не поддерживается: реквизит перезаписался бы мимо проверок.
    Черновики и остальные поля — как в Django.
    """

    def update(self, **kwargs):
        fields = stock_fields(self.model, kwargs)
        identity = single_only_fields(self.model, kwargs)
        if not fields and not identity:
            return super().update(**kwargs)
        with transaction.atomic():
            stored = _locked_rows(self, [*fields, *identity])
            _reject_identity_change(self.model, {
                pk for pk, row in stored.items() for field in identity
                if _differs(field, kwargs[field.name] if field.name in kwargs else kwargs[field.attname],
                            row[field.attname])}, identity)
            moved = set()
            for field in fields:
                value = kwargs[field.name] if field.name in kwargs else kwargs[field.attname]
                moved |= {pk for pk, row in stored.items() if _differs(field, value, row[field.attname])}
            _reject_mass_change(self.model, moved, fields)
            return self.model._base_manager.filter(pk__in=list(stored)).update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        checked = stock_fields(self.model, fields)
        identity = single_only_fields(self.model, fields)
        if not checked and not identity:
            return super().bulk_update(objs, fields, batch_size=batch_size)
        objs = list(objs)
        if any(obj.pk is None for obj in objs):
            raise ValueError("All bulk_update() objects must have a primary key set.")
        if not objs:
            return 0
        with transaction.atomic():
            stored = _locked_rows(self.filter(pk__in=[obj.pk for obj in objs]), [*checked, *identity])
            _reject_identity_change(self.model, {
                obj.pk for obj in objs if obj.pk in stored and any(
                    _differs(field, getattr(obj, field.attname), stored[obj.pk][field.attname])
                    for field in identity)}, identity)
            moved = {obj.pk for obj in objs if obj.pk in stored and any(
                _differs(field, getattr(obj, field.attname), stored[obj.pk][field.attname]) for field in checked)}
            _reject_mass_change(self.model, moved, checked)
            # запись — обычным bulk_update по той же выборке, мимо update() выше:
            # его выражения Case/When — не новые значения пользователя
            return models.QuerySet.bulk_update(self.model._base_manager.filter(pk__in=list(stored)),
                                               objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
                    update_fields=None, unique_fields=None):
        checked = stock_fields(self.model, update_fields or ()) + identity_fields(self.model, update_fields or ())
        if update_conflicts and checked:
            titles = ", ".join(TITLES[field.name] for field in checked)
            raise _error_class(checked)(
                f"Массовое создание документов с перезаписью учётных реквизитов ({titles}) при конфликте "
                "(update_conflicts) не поддерживается: реквизиты существующего документа перезаписались бы "
                "мимо перепроведения"
            )
        return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                                   update_conflicts=update_conflicts, update_fields=update_fields,
                                   unique_fields=unique_fields)

    def delete(self):
        """Массовое удаление проведённых документов (или документов с
        движениями) не поддерживается: каскад удалил бы строки, а движения
        остались бы. Удаляйте по одному — delete() документа сначала снимает
        его с проведения со всеми проверками. Черновики — как в Django."""
        with transaction.atomic():
            stored = _locked_rows(self, [])
            bound = _stock_bound(self.model, stored)
            if bound:
                from apps.inventory import services

                doc = self.model._base_manager.get(pk=min(bound))
                more = f" (и ещё {len(bound) - 1})" if len(bound) > 1 else ""
                raise services.PostedChangeError(
                    f"Массовое удаление проведённых документов не поддерживается: {doc}{more} проведён или имеет "
                    "складские движения — его строки исчезли бы, а движения остались. Удаляйте документы по "
                    "одному: удаление сначала снимает документ с проведения"
                )
            return super().delete()


# ---------------------------------------------------------------------------
# Строки складских документов (правило 10)
# ---------------------------------------------------------------------------

class _Scope(NamedTuple):
    label: str              # модель документа
    pk: int
    products: frozenset     # товары, уже заблокированные операцией
    writes: bool            # можно менять строки этого документа


_SCOPES = contextvars.ContextVar("stock_document_scopes", default=())


@contextmanager
def _document_scope(doc, products, writes):
    """Операция над документом doc идёт в текущей транзакции под блокировкой
    его строки и товаров products. writes — разрешено менять строки этого
    документа (editing(), post()). Разрешение действует только внутри блока и
    только внутри транзакции."""
    token = _SCOPES.set(_SCOPES.get() + (_Scope(doc._meta.label, doc.pk, frozenset(products), writes),))
    try:
        yield
    finally:
        _SCOPES.reset(token)


def _active_scopes():
    return _SCOPES.get() if connection.in_atomic_block else ()


def _plain(queryset):
    """Та же выборка как обычный QuerySet Django — запись после проверки, мимо защиты."""
    return models.QuerySet(queryset.model, query=queryset.query.chain(), using=queryset._db, hints=queryset._hints)


def _lock_documents(model, ids):
    """Строки документов — по возрастанию id (уровень «документы», до товаров)."""
    rows = model._base_manager.filter(pk__in=list(ids)).order_by("pk")
    if connection.features.has_select_for_update:
        rows = rows.select_for_update(of=("self",)) if connection.features.has_select_for_update_of \
            else rows.select_for_update()
    list(rows.values_list("pk", flat=True))


def _line_document(model, pk):
    doc = model._base_manager.filter(pk=pk).first()
    return f"«{doc}»" if doc else f"id {pk}"


def _guard_lines(model, writes):
    """Проверка записи строк до записи (вызывается внутри транзакции).

    writes — [(id строки или None, {столбец: новое значение} или None —
    удаление, записываемые поля)]. Значения, которых нет в записи, берутся из
    базы. Нарушение правила 10 — PostedChangeError, ничего не записано."""
    from apps.inventory import services

    doc_field = model._meta.get_field(model.DOCUMENT_FIELD)
    doc_model, parent = doc_field.related_model, doc_field.attname
    product_field = model._meta.get_field("product")
    product = product_field.attname
    columns = [field.attname for field in model.guarded_fields()]
    pks = [pk for pk, _, _ in writes if pk is not None]

    def read():
        return {row["pk"]: row for row in models.QuerySet(model).filter(pk__in=pks).values("pk", *columns)}

    def target(pk, values, attname, field, stored):
        """Новое значение столбца: из записи, иначе — из базы."""
        if values is not None and attname in values:
            if _is_expression(values[attname]):
                return _UNKNOWN
            return written_value(field, values[attname])
        return stored[attname] if stored else None

    stored = read()
    for pk, values, _ in writes:
        if values is not None and parent in values and _is_expression(values[parent]):
            raise services.PostedChangeError(
                "Перенос строк складских документов выражением не поддерживается: укажите документ значением")
    involved = {row[parent] for row in stored.values()}
    involved |= {target(pk, values, parent, doc_field, stored.get(pk)) for pk, values, _ in writes
                 if values is not None} - {None}
    scopes = _active_scopes()
    permitted = {scope.pk: scope for scope in scopes if scope.label == doc_model._meta.label and scope.writes}
    locked_products = frozenset().union(*(scope.products for scope in scopes))
    others = involved - set(permitted)
    if others and scopes:
        # Внутри операции с другим документом его строку нельзя блокировать
        # после товаров (порядок «документы → товары»). Такая запись
        # допустима, только если все её товары уже заблокированы этой
        # операцией: любое проведение того документа ждёт тех же товаров.
        for pk, values, _ in writes:
            old = stored.get(pk)
            old_parent = old[parent] if old else None
            new_parent = target(pk, values, parent, doc_field, old) if values is not None else old_parent
            if not ({old_parent, new_parent} & others):
                continue
            products = {old[product]} if old else set()
            if values is not None:
                products.add(target(pk, values, product, product_field, old))
            if (old and values is not None and new_parent != old_parent) or not products <= locked_products:
                raise services.PostedChangeError(
                    f"Внутри операции с другим документом строки документа "
                    f"{_line_document(doc_model, new_parent or old_parent)} меняются только по уже "
                    "заблокированным товарам и без переноса между документами — измените их отдельно"
                )
    elif others:
        _lock_documents(doc_model, others)      # актуальный статус — под блокировкой
        stored = read()

    bound = _stock_bound(doc_model, involved)
    removed = []
    for pk, values, fields in writes:
        old = stored.get(pk) if pk is not None else None
        if values is None:
            if old is None:
                continue
            removed.append(pk)
            action, old_parent, new_parent, product_changed = "удалить", old[parent], None, False
        else:
            if old is not None and not any(
                    _differs(field, values[field.attname], old[field.attname])
                    for field in fields if field.attname in values):
                continue                                         # ничего не меняется
            action = "добавить" if old is None else "изменить"
            old_parent = old[parent] if old else None
            new_parent = target(pk, values, parent, doc_field, old)
            new_product = target(pk, values, product, product_field, old)
            product_changed = old is None or new_product is _UNKNOWN or new_product != old[product]
        if old is not None and values is not None and new_parent != old_parent and (
                old_parent in bound or new_parent in bound):
            raise services.PostedChangeError(
                f"Перенос строки между документами {_line_document(doc_model, old_parent)} и "
                f"{_line_document(doc_model, new_parent)} не поддерживается, когда один из них проведён: "
                "удалите строку в одном документе и добавьте в другом (в проведённом — через editing())"
            )
        for doc_id in {old_parent, new_parent} - {None}:
            if doc_id in bound and doc_id not in permitted:
                raise services.PostedChangeError(
                    f"Строки проведённого документа {_line_document(doc_model, doc_id)} нельзя {action} отдельно "
                    "от его перепроведения: движения, остатки и себестоимость остались бы прежними. Измените "
                    "строки в форме документа (сохранение перепроводит его) или внутри editing() этого "
                    "документа. Если объект строки устарел, загрузите его заново"
                )
        if new_parent in permitted and new_parent in bound and product_changed and (
                new_product is _UNKNOWN or new_product not in permitted[new_parent].products):
            name = apps_product_name(new_product)
            raise services.PostedChangeError(
                f"Товар {name} не заблокирован заранее: передайте его в editing(products=[…]) документа "
                f"{_line_document(doc_model, new_parent)} — блокировки берутся до изменения строк, в порядке "
                "«документы → товары»"
            )
    if removed:
        _check_references(model, removed)


class _Unknown:
    """Значение задано выражением — заранее неизвестно."""

    def __repr__(self):
        return "выражение"


_UNKNOWN = _Unknown()


def apps_product_name(product_id):
    from django.apps import apps

    if product_id is _UNKNOWN:
        return "(задан выражением)"
    product = apps.get_model("catalog", "Product")._base_manager.filter(pk=product_id).first()
    return f"«{product}»" if product else f"id {product_id}"


def _check_references(model, removed):
    """Строку, на которую ссылается строка проведённого документа (возврат →
    строка отгрузки), удалить нельзя: связь пропала бы мимо его перепроведения."""
    from apps.inventory import services

    for relation in model._meta.related_objects:
        other = relation.related_model
        if not (isinstance(other, type) and issubclass(other, DocumentLineMixin)):
            continue
        other_doc = other._meta.get_field(other.DOCUMENT_FIELD)
        docs = set(models.QuerySet(other).filter(**{f"{relation.field.name}__in": removed})
                   .values_list(other_doc.attname, flat=True))
        bound = _stock_bound(other_doc.related_model, docs)
        if bound:
            doc = other_doc.related_model._base_manager.get(pk=min(bound))
            title = other_doc.related_model._meta.verbose_name.lower()
            raise services.PostedChangeError(
                f"На документ ссылается проведённый {title} № {doc.number} от {doc.date:%d.%m.%Y}: его строка "
                "связана с удаляемой строкой, и связь пропала бы мимо его перепроведения. Сначала отмените "
                "этот документ или уберите в нём связь со строкой"
            )


class DocumentLineMixin:
    """Строка складского документа — по правилу 10."""

    DOCUMENT_FIELD = None       # внешний ключ на документ
    DERIVED_FIELDS = ()         # пишет пересчёт себестоимости, не реквизиты строки

    @classmethod
    def guarded_fields(cls):
        return [field for field in cls._meta.concrete_fields
                if not field.primary_key and field.name not in cls.DERIVED_FIELDS]

    @classmethod
    def written_fields(cls, names):
        """Защищённые поля среди имён полей или столбцов; None — все."""
        fields = cls.guarded_fields()
        if names is None:
            return fields
        wanted = set()
        for name in names:
            try:
                wanted.add(cls._meta.get_field(name).name)
            except FieldDoesNotExist:
                continue
        return [field for field in fields if field.name in wanted]

    def save(self, *args, **kwargs):
        fields = self.written_fields(kwargs.get("update_fields"))
        if not fields:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            _guard_lines(type(self), [(self.pk, {field.attname: getattr(self, field.attname) for field in fields},
                                       fields)])
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            _guard_lines(type(self), [(self.pk, None, [])])
            return super().delete(*args, **kwargs)


class DocumentLineQuerySet(models.QuerySet):
    """Массовые операции со строками — по правилу 10: проверка всех
    затронутых строк до записи, одна недопустимая — отказ целиком. Менеджер
    строк — ещё и базовый (Meta.base_manager_name), поэтому связанный
    менеджер документа (add(), create(), удаление выборки) и обнуление связи
    при удалении строки-основания проходят через эту же проверку."""

    def update(self, **kwargs):
        fields = self.model.written_fields(kwargs)
        if not fields:
            return super().update(**kwargs)
        with transaction.atomic():
            pks = list(self.values_list("pk", flat=True))
            values = {field.attname: kwargs[field.name] if field.name in kwargs else kwargs[field.attname]
                      for field in fields}
            _guard_lines(self.model, [(pk, values, fields) for pk in pks])
            return _plain(self.filter(pk__in=pks)).update(**kwargs)

    def delete(self):
        with transaction.atomic():
            pks = list(self.values_list("pk", flat=True))
            _guard_lines(self.model, [(pk, None, []) for pk in pks])
            return _plain(self.filter(pk__in=pks)).delete()

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
                    update_fields=None, unique_fields=None):
        from apps.inventory import services

        objs = list(objs)
        if update_conflicts and self.model.written_fields(update_fields or ()):
            raise services.PostedChangeError(
                "Создание строк складских документов с перезаписью при конфликте (update_conflicts) не "
                "поддерживается: строки существующих документов изменились бы мимо проверок")
        fields = self.model.guarded_fields()
        with transaction.atomic():
            _guard_lines(self.model, [(None, {field.attname: getattr(obj, field.attname) for field in fields},
                                       fields) for obj in objs])
            return super().bulk_create(objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                                       update_conflicts=update_conflicts, update_fields=update_fields,
                                       unique_fields=unique_fields)

    def bulk_update(self, objs, fields, batch_size=None):
        checked = self.model.written_fields(fields)
        if not checked:
            return super().bulk_update(objs, fields, batch_size=batch_size)
        objs = list(objs)
        if any(obj.pk is None for obj in objs):
            raise ValueError("All bulk_update() objects must have a primary key set.")
        if not objs:
            return 0
        with transaction.atomic():
            _guard_lines(self.model, [(obj.pk, {field.attname: getattr(obj, field.attname) for field in checked},
                                       checked) for obj in objs])
            return _plain(self).bulk_update(objs, fields, batch_size=batch_size)


class PostableMixin:
    DOC_TYPE = None  # переопределяется в модели документа
    # Учётные реквизиты (правило 9): от них зависят движения проведённого документа
    STOCK_FIELDS = ("organization", "warehouse", "date", "time")
    # Вид документа (правило 11): задаёт тип движений, у проведённого не меняется
    IDENTITY_FIELDS = ()
    # Основание возврата (правило 12): входит в STOCK_FIELDS, массово не меняется
    BASIS_FIELDS = ()
    # Поля, от которых зависит серия номера: при их записи номер регистрируется заново
    NUMBERING_FIELDS = ("number", "organization")

    def numbering_scope(self):
        """Разделение серии номеров внутри организации и года (у оприходований и списаний — вид)."""
        return {}

    @classmethod
    def stock_doc_types(cls):
        """Виды движений документов этой модели (у оприходований и списаний — два)."""
        return getattr(cls, "DOC_TYPES", None) or (cls.DOC_TYPE,)

    def assign_number(self):
        """Номер перед записью (переопределяется в модели документа)."""

    # ---------- сохранение и учётные реквизиты (правило 9) ----------

    def movement_warehouse_field(self, incoming):
        """Поле склада, на котором стоят движения документа: прихода
        (incoming) или расхода. У перемещения — склад-получатель и
        склад-отправитель, у остальных — склад документа."""
        return "warehouse"

    def _writes(self, update_fields, name):
        """Попадёт ли поле в запись. Django принимает в update_fields и имя
        поля, и имя столбца: «organization» и «organization_id»."""
        if update_fields is None:
            return True
        return name in {self._meta.get_field(field).name for field in update_fields}

    def save(self, *args, **kwargs):
        """Запись, в которую попадает учётный реквизит, сверяется с базой под
        блокировкой строки документа — до присвоения номера и до записи.
        Другое значение у проведённого документа (или документа с
        движениями) — отказ, если объект не внутри editing(). Проверяется то,
        что действительно будет записано: склад или дата, изменённые только в
        памяти, при save(update_fields=[«comment»]) не записываются и проверку
        не вызывают (их поймает проведение, см. post). Номер присваивается и
        регистрируется в серии организации, только когда записываются номер
        или организация: сохранение одного комментария не трогает нумерацию."""
        update_fields = kwargs.get("update_fields")
        numbering = any(self._writes(update_fields, name) for name in self.NUMBERING_FIELDS)
        fields = [field for field in stock_fields(type(self)) if self._writes(update_fields, field.name)]
        identity = [field for field in identity_fields(type(self)) if self._writes(update_fields, field.name)]
        if self.pk is None or not (fields or identity):
            if numbering:
                self.assign_number()
            return super().save(*args, **kwargs)
        with transaction.atomic():
            stored = self._check_stock_write(fields, identity)
            identity_changed = self._check_identity_write(identity, stored)
            if numbering:
                self.assign_number()
            if identity_changed:
                self._check_number_series()
            return super().save(*args, **kwargs)

    def _check_stock_write(self, fields, identity=()):
        """Сверка учётных реквизитов с базой под блокировкой строки документа.
        Возвращает сохранённые значения (вместе с полями вида)."""
        rows = type(self)._base_manager.filter(pk=self.pk)
        if connection.features.has_select_for_update:
            rows = rows.select_for_update()
        stored = rows.values(*[field.attname for field in (*fields, *identity)]).first()
        if stored is None or getattr(self, EDITING, False) or not fields:
            return stored
        changed = [field for field in fields if _differs(field, getattr(self, field.attname), stored[field.attname])]
        if changed and _stock_bound(type(self), [self.pk]):
            what = ", ".join(f"{TITLES[field.name]} {show_value(field, stored[field.attname])} → "
                             f"{show_value(field, getattr(self, field.attname))}" for field in changed)
            raise _error_class(changed)(
                f"Учётные реквизиты проведённого документа нельзя изменить простым сохранением: его движения, "
                f"остатки и серийные номера остались бы прежними ({what}). Измените документ в форме "
                "(сохранение перепроводит его) или через editing() / change_organization() — с "
                "перепроведением и проверкой прежней и новой истории. Если объект документа устарел, "
                "загрузите его заново"
            )
        return stored

    def _check_identity_write(self, fields, stored):
        """Правило 11: вид документа с движениями не меняется — и внутри
        editing() тоже. Возвращает, меняется ли вид (у черновика)."""
        from apps.inventory import services

        if stored is None:
            return False
        changed = [field for field in fields if _differs(field, getattr(self, field.attname), stored[field.attname])]
        if not changed:
            return False
        what = ", ".join(f"{TITLES[field.name]} {show_value(field, stored[field.attname])} → "
                         f"{show_value(field, getattr(self, field.attname))}" for field in changed)
        if any(_is_expression(getattr(self, field.attname)) for field in changed):
            raise services.PostedChangeError(f"Вид документа выражением не задаётся ({what}): укажите значение")
        if _stock_bound(type(self), [self.pk]) and not self._restores_movement_kind():
            raise services.PostedChangeError(
                f"Вид проведённого документа менять нельзя — и при перепроведении тоже ({what}): его движения "
                "записаны по прежнему виду. Отмените проведение (со всеми проверками истории), измените вид "
                "черновика и проведите его заново. Если объект документа устарел, загрузите его заново"
            )
        return True

    def _restores_movement_kind(self):
        """Новый вид совпадает с видом всех движений документа — исправление
        старых данных, где вид документа разошёлся с движениями."""
        from apps.inventory.models import StockMovement

        types = set(StockMovement.objects.filter(doc_type__in=self.stock_doc_types(), doc_id=self.pk)
                    .values_list("doc_type", flat=True))
        return types == {self.DOC_TYPE}

    def _check_number_series(self):
        """Номер черновика, сменившего вид, сохраняется и переходит в серию
        нового вида — там он не должен быть занят (организация, год, вид)."""
        from apps.inventory import services

        if not self.number:
            return
        year = self._meta.get_field("date").to_python(self.date).year
        taken = type(self)._base_manager.filter(
            organization_id=self.organization_id, number=self.number, date__year=year, **self.numbering_scope(),
        ).exclude(pk=self.pk).first()
        if taken is not None:
            raise services.StockError(
                f"Номер {self.number} в {year} году уже занят документом «{taken}» — вид не изменён: "
                "укажите другой номер или оставьте поле пустым"
            )

    def _check_loaded_identity(self, row):
        """Вид объекта в памяти совпадает с сохранённым: иначе проведение,
        отмена или удаление выбрали бы движения другого вида."""
        from apps.inventory import services

        unsaved = [field for field in identity_fields(type(self))
                   if _differs(field, getattr(self, field.attname), row[field.attname])]
        if unsaved:
            what = ", ".join(f"в документе {show_value(field, getattr(self, field.attname))}, в базе "
                             f"{show_value(field, row[field.attname])}" for field in unsaved)
            raise services.PostedChangeError(
                f"Вид документа не совпадает с сохранённым ({what}): объект устарел или вид изменён без "
                "сохранения — по нему выбрались бы движения другого вида. Загрузите документ заново"
            )

    def _check_movement_kind(self):
        """Движения документа записаны его видом. Иначе (старые данные, прямая
        правка базы) проведение не убрало бы движения прежнего вида."""
        from apps.inventory import costing, services
        from apps.inventory.models import StockMovement

        other = set(self.stock_doc_types()) - {self.DOC_TYPE}
        found = sorted(set(StockMovement.objects.filter(doc_type__in=other, doc_id=self.pk)
                           .values_list("doc_type", flat=True))) if other else []
        if found:
            theirs = ", ".join(f"«{costing.DOC_TITLES.get(kind, kind)}»" for kind in found)
            raise services.PostedChangeError(
                f"Движения документа записаны другим видом ({theirs}), чем у самого документа "
                f"(«{costing.DOC_TITLES.get(self.DOC_TYPE, self.DOC_TYPE)}»): проводить, отменять и удалять его "
                "нельзя — движения прежнего вида не убрались бы. Сначала верните документу вид его движений "
                "(manage.py stock_transition покажет какой)"
            )

    @contextmanager
    def editing(self, products=(), drafts_only=False):
        """Единственный путь изменить сохранённый документ вместе с учётными
        реквизитами (организация, склад, дата, время — STOCK_FIELDS).

            with doc.editing():
                doc.warehouse = other
                doc.save()            # и строки, если нужно

        1. Транзакция (точка сохранения внутри внешней). Строка документа
           блокируется, статус берётся из базы, а не из объекта.
        2. Проведённый документ: товары его строк, движений и products
           (новые товары строк) блокируются до изменения строк — тот же
           порядок, что у post() (документ → товары по возрастанию id).
           Только этому объекту и только внутри блока разрешено записать
           другие реквизиты, а строкам этого документа — добавляться,
           меняться и удаляться (правило 10; новый товар строки — только из
           заблокированных).
        3. После блока проведённый документ перепроводится один раз (post):
           прежняя и новая история (склад, момент, организация), возвраты и
           основания (return_limits), серийные номера, покрытия,
           себестоимость, остатки.
        4. Любая ошибка — откат всего блока; у объекта восстанавливаются
           сохранённые статус и реквизиты.
        drafts_only=True — проведённый документ не меняется (StockError):
        административная панель правит только черновики.

        Рекурсии нет: save() механизм не вызывает, post() пишет только статус.
        Промежуточное состояние (новые реквизиты при старых движениях)
        существует только внутри незавершённой транзакции под блокировками
        документа и товаров. Перепроведение выполняется один раз, при выходе
        из блока.
        """
        from apps.inventory import services

        fields = [*stock_fields(type(self)), *identity_fields(type(self))]
        with transaction.atomic():
            rows = type(self)._base_manager.filter(pk=self.pk)
            if connection.features.has_select_for_update:
                rows = rows.select_for_update()
            row = rows.values("status", *[field.attname for field in fields]).first()
            if row is None:
                raise services.StockError("Документ уже удалён другим пользователем")
            self._check_loaded_identity(row)            # правило 11: устаревший или несохранённый вид
            self.status = row["status"]
            if self.BASIS_FIELDS:
                # правило 12: послабление «не больше прежнего» — только для основания, с которым
                # возврат проведён сейчас (по базе), а не с которым объект когда-то загружен
                self._loaded_basis_id = row[self._meta.get_field(self.BASIS_FIELDS[0]).attname]
            posted = row["status"] == DOC_POSTED
            if posted and drafts_only:
                raise services.StockError(
                    "Документ уже проведён (возможно, другим пользователем) — здесь меняются только "
                    "черновики. Измените его в программе: там сохранение перепроводит документ"
                )
            locked = set()
            if posted:
                locked = self._stock_products() | {pk for pk in products if pk}
                services.lock_products(locked)
            previous = getattr(self, EDITING, False)
            setattr(self, EDITING, posted)
            try:
                # строки проведённого документа меняются только здесь (правило 10)
                with _document_scope(self, locked, writes=True) if posted else nullcontext():
                    yield posted
                    if posted:
                        self.post()
            except BaseException:
                self.status = row["status"]
                for field in fields:
                    setattr(self, field.attname, row[field.attname])
                raise
            finally:
                setattr(self, EDITING, previous)

    def change_organization(self, organization):
        """Смена организации сохранённого документа; проведённый перепроводится
        (editing). Отказ — StockError, всё остаётся прежним."""
        with self.editing():
            self.organization = organization
            self.save(update_fields=["organization"])

    def build_specs(self):
        """Возвращает список движений: dict(product_id, warehouse_id, quantity, cost)."""
        raise NotImplementedError

    def get_allow_negative(self):
        """Может ли сам документ увести остаток в минус своим расходом.

        Переопределяется в документах продаж (флаг организации).
        """
        return getattr(settings, "ALLOW_NEGATIVE_STOCK", False)

    def negative_stock_allowed(self):
        """Разрешены ли отрицательные остатки вообще — для проверки последствий
        (история после изменения, отмены или удаления документа)."""
        if getattr(settings, "ALLOW_NEGATIVE_STOCK", False):
            return True
        organization = getattr(self, "organization", None)
        return bool(organization and organization.allow_negative_stock)

    def _allowed_organizations(self, keys, own_negative=None):
        """id организаций, чьи ключи не проверяются на минус: по их настройке;
        организация документа — ещё и по own_negative (разрешение самого
        документа). Ключи прежней организации (смена организации проведённого
        документа) проверяются по её собственной настройке."""
        from apps.inventory import services

        allowed = services.negative_allowed_organizations({key[2] for key in keys})
        if own_negative or self.negative_stock_allowed():
            allowed.add(self.organization_id)
        else:
            allowed.discard(self.organization_id)
        return allowed

    @property
    def is_posted(self):
        return self.status == DOC_POSTED

    @classmethod
    def from_db(cls, db, field_names, values):
        # Запоминаем основание, с которым возврат загружен: если его сменили,
        # послабление «не больше прежнего количества» к новому основанию не относится.
        from apps.inventory import return_limits

        instance = super().from_db(db, field_names, values)
        instance._loaded_basis_id = return_limits.basis_id(instance)
        return instance

    def _check_returns(self, removing=False):
        """Количество возвратов по основанию — см. return_limits. Вызывается
        под блокировкой товаров, до изменения движений."""
        from apps.inventory import return_limits

        previous = return_limits.posted_quantities(self)
        if not removing:
            return_limits.check_return(self, previous, getattr(self, "_loaded_basis_id", None))
        return_limits.check_basis(self, previous, removing=removing)

    # ---------- блокировки ----------

    def _stock_products(self):
        """Товары строк и движений документа."""
        from apps.inventory import services

        product_ids = set(self.lines.values_list("product_id", flat=True))
        product_ids.update(pair[0] for pair in services.document_pairs(self.DOC_TYPE, self.pk))
        return product_ids

    def _lock(self):
        """Блокирует документ и его товары; статус берёт из базы, а не из памяти.
        Возвращает учётные реквизиты документа в базе {столбец: значение} и
        заблокированные товары.

        Вызывается только внутри транзакции.
        """
        from apps.inventory import services

        row = (
            type(self)._base_manager.select_for_update().filter(pk=self.pk)
            .values("status", *[field.attname for field in (*stock_fields(type(self)), *identity_fields(type(self)))])
            .first()
        )
        if row is None:
            raise services.StockError("Документ уже удалён другим пользователем")
        # правило 11: тип движений выбирается по виду — он должен быть свежим и совпадать с движениями
        self._check_loaded_identity(row)
        self._check_movement_kind()
        self.status = row["status"]
        products = self._stock_products()
        services.lock_products(products)
        return row, products

    def _check_return_basis(self, found):
        """Отклоняет операцию, после которой у возврата покупателя нет годного
        основания, если она касается этого документа — самого возврата или
        его отгрузки. Чужие старые проблемы операцию не блокируют: их
        показывает диагностика (manage.py check_costs)."""
        from apps.inventory import services

        me = (self.DOC_TYPE, self.pk)
        for problem in found:
            if problem.kind == "return_basis" and me in problem.docs:
                prefix = "" if me == problem.docs[0] else "На документ ссылается проведённый возврат. "
                raise services.StockError(prefix + problem.description)

    def _check_history_problems(self, found):
        """Итоговая история затронутых товаров должна быть корректной.

        После пересчёта у товара не должно остаться проблем, требующих решения
        человека (costing.BLOCKING): перемещение без остатка, возврат на другой
        склад непокрытых единиц, неоднозначный возврат при дефиците, возвраты по
        одной отгрузке и по строкам, и без строки, движения без строк документа,
        негодное основание возврата. Операция, после
        которой такая проблема есть — новая или старая, — отклоняется целиком:
        откатываются документ, строки, движения, стоимость и остатки. Так
        исправляющая операция (после неё история корректна) проходит, а
        остальные операции с проблемным товаром — нет, с перечнем мешающих
        документов. Несколько проблем сразу устраняет команда fix_stock_history.
        """
        from apps.inventory import costing, services

        if costing.history_check_deferred():
            return
        problems = costing.blocking(found)
        if not problems:
            return
        lines = [f"— {problem.description}" for problem in problems[:5]]
        more = f"\n…и ещё {len(problems) - 5}" if len(problems) > 5 else ""
        raise services.StockError(
            "Операция отменена: после неё складская история товара была бы некорректной.\n"
            + "\n".join(lines) + more
        )

    # ---------- операции ----------

    @transaction.atomic
    def post(self, allow_negative=None):
        """Проводит документ; уже проведённый — перепроводит по текущим строкам.

        Товар принадлежит организации документа: движения пишутся с ней, наличие
        и история проверяются по ключу «организация + склад + товар». При смене
        организации проведённого документа проверяются обе истории — прежней
        организации (документ из неё уходит) и новой; любой отказ откатывает
        всё: документ, движения, номера, покрытия, себестоимость и остатки.
        """
        from apps.inventory import transition

        transition.require_ready()
        stored, products = self._lock()
        unsaved = [field for field in stock_fields(type(self))
                   if _differs(field, getattr(self, field.attname), stored[field.attname])]
        if unsaved:
            # движения пишутся с реквизитами объекта, а документ в базе — с прежними
            what = "; ".join(f"{UNSAVED[field.name]} (в базе — {show_value(field, stored[field.attname])}, "
                             f"в документе — {show_value(field, getattr(self, field.attname))})"
                             for field in unsaved)
            raise _error_class(unsaved)(
                f"Документ не проведён: {what}. Сохраните документ (проведённый — через форму или "
                "editing()) или загрузите его заново"
            )
        # строки этого документа пишет и само проведение: возврат покупателя
        # запоминает строку отгрузки и резервную стоимость (правило 10)
        with _document_scope(self, products, writes=True):
            self._post(allow_negative)

    def _post(self, allow_negative):
        """Тело проведения — под блокировками и разрешением post()."""
        from apps.inventory import return_limits, serials, services

        self._check_returns()
        own_negative = self.get_allow_negative() if allow_negative is None else allow_negative

        before = services.history_snapshot(services.document_pairs(self.DOC_TYPE, self.pk))
        previous = services.document_outflow(self.DOC_TYPE, self.pk)
        touched_serials = serials.clear(self)
        services.clear_movements(self.DOC_TYPE, self.pk)  # чистый лист: без двойных движений

        specs = self.build_specs()
        for spec in specs:
            spec.setdefault("organization_id", self.organization_id)
        services.validate_stock(specs, own_negative, previous)
        found = services.create_movements(
            self.DOC_TYPE, self.pk, self.number, self.date, specs,
            time=getattr(self, "time", None), created_at=getattr(self, "created_at", None),
            also_products={pair[0] for pair in before},   # товары, убранные из документа
            organization_id=self.organization_id,
        )
        pairs = set(before) | {(s["product_id"], s["warehouse_id"], s["organization_id"]) for s in specs}
        services.check_history(pairs, before, exclude_doc=(self.DOC_TYPE, self.pk),
                               allowed=self._allowed_organizations(pairs, own_negative))
        self._check_return_basis(found)
        self._check_history_problems(found)

        touched_serials |= serials.apply(self)   # проверка номеров и их движение
        serials.validate_history(touched_serials)

        self.status = DOC_POSTED
        self.save(update_fields=["status"])
        self._loaded_basis_id = return_limits.basis_id(self)

    @transaction.atomic
    def unpost(self):
        """Снимает с проведения. Блокируется, если из-за этого остаток уйдёт
        в минус на какую-либо дату или сломается история серийного номера."""
        from apps.inventory import serials, services, transition

        transition.require_ready()
        self._lock()
        self._check_returns(removing=True)
        pairs = services.document_pairs(self.DOC_TYPE, self.pk)
        before = services.history_snapshot(pairs)
        touched_serials = serials.clear(self)
        found = services.clear_movements(self.DOC_TYPE, self.pk)
        # ключи — организации, с которой документ проведён (по движениям)
        allowed = services.negative_allowed_organizations({pair[2] for pair in pairs})
        services.check_history(pairs, before, allowed=allowed)
        self._check_return_basis(found)
        self._check_history_problems(found)
        serials.validate_history(touched_serials)

        self.status = DOC_DRAFT
        self.save(update_fields=["status"])

    @transaction.atomic
    def delete(self, *args, **kwargs):
        """Удаление с теми же проверками, что и отмена проведения.

        Так удаление из карточки, из списка и из административной панели
        подчиняется одним правилам; при отказе документ остаётся проведённым.
        """
        from apps.inventory import services

        if self.pk:
            _, products = self._lock()
            # каскад обнуляет связи строк-черновиков других документов с этими
            # строками — по уже заблокированным товарам, без новых блокировок
            with _document_scope(self, products, writes=False):
                if self.status == DOC_POSTED or services.document_pairs(self.DOC_TYPE, self.pk):
                    self.unpost()                 # проверяет готовность перехода
                return super().delete(*args, **kwargs)
        return super().delete(*args, **kwargs)
