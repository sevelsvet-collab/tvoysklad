"""Согласованность платежей и оплаты счетов покупателям.

Оплата счёта (Invoice.paid_amount) — сумма связанных с ним ПРОВЕДЁННЫХ
ВХОДЯЩИХ платежей. Черновики и исходящие платежи в неё не входят. Переплата
не обрезается: хранится фактическая сумма платежей.

Единый механизм — сама модель Payment: её save(), delete(), post(), unpost()
и её QuerySet (массовые update() и delete()). Каждая операция — одна
транзакция: изменился платёж — в той же транзакции пересчитаны все счета,
которых он касался (и прежний, и новый). Поэтому формы, кнопки, массовые
действия, административная панель и прямые вызовы модели дают один результат.

Порядок блокировок (PostgreSQL) один для всех денежных операций — по уровням:
    1) строки платежей                          — по возрастанию id;
    2) денежные счета и кассы                   — по возрастанию id; все
       затронутые сразу: при переносе документа — прежняя и новая касса;
    3) строки корректировок остатков этих касс  — все корректировки
       затронутых касс (и строка самой корректировки), по возрастанию id;
    4) счета покупателям                        — по возрастанию id;
и только после этого — проверка связей по свежим данным, запись, пересчёт
истории касс (recalculate_accounts: пишет только в строки уровня 3) и
оплаты счетов (строки уровня 4). Операция может пропустить уровень, но
никогда не берёт блокировку уровня выше уже взятой: после блокировки кассы
новые строки платежей не блокируются, а строки корректировок блокируются
только целым набором по кассе. Корректировка свою строку раньше кассы не
блокирует: читает свою кассу без блокировки, блокирует кассы (2), затем
строки корректировок (3) и перечитывает себя; если её касса тем временем
сменилась (одновременный перенос), операция отклоняется — повторить.
Пересчёт читает платежи кассы без блокировок: изменить, добавить или
удалить платёж кассы нельзя, не взяв блокировку этой же кассы.

Смена организации кассы блокирует только её строку (уровень 2) и затем
проверяет документы чтением; смена сторон счёта покупателю — только его
строку (уровень 4). Поэтому при гонке «платёж против смены сторон» вторая
операция ждёт первую и видит её результат: либо платёж отклоняется
(стороны уже сменились), либо отклоняется смена сторон (платёж уже есть).
В SQLite блокировок строк нет — запись там идёт по одной транзакции.

Совместимость связей (проверяется на сервере при сохранении платежа — по
состоянию записей в базе под блокировкой, а не по связанным объектам в памяти,
которые могли устареть):
* счёт/касса принадлежит организации платежа;
* счёт покупателю связывается только с входящим платежом;
* организация и контрагент платежа совпадают с организацией и покупателем счёта;
* назначение для взаиморасчётов подходит к направлению денег (входящий —
  оплата от покупателя, возврат от поставщика, не распределено; исходящий —
  оплата поставщику, возврат покупателю, не распределено), а платёж по счёту
  покупателю — только «оплата от покупателя» (purpose_errors).
Снять платёж с проведения и удалить его можно всегда — так исправляются
старые несовместимые связи.

Корректировка остатка (AccountCorrection) — по тем же правилам: касса или
счёт принадлежит организации корректировки; проверка по базе под
блокировками при save() и в массовых операциях. Снять с проведения и удалить
можно всегда.

История денежного счёта (правило «остаток на момент», account_history):
начальный остаток → проведённые платежи и корректировки по порядку
money_key. Проведённая корректировка фиксирует фактический остаток:
расчётный остаток перед ней = остаток по предшествующим документам,
разница = факт − расчётный, после неё остаток равен факту. Черновики на
деньги не влияют; для черновика корректировки расчётный остаток и разница —
предварительные (по документам до её момента).
"""
from decimal import Decimal

from django.apps import apps
from django.db import connection, transaction
from django.db.models import Q, Sum

from apps.core.constants import DOC_POSTED

ZERO = Decimal("0")
CENTS = Decimal("0.01")


class PaymentError(Exception):
    """Платёж нельзя сохранить: связи несовместимы. errors — {поле: текст}."""

    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join(self.errors.values()))


class CorrectionError(PaymentError):
    """Корректировку остатка нельзя сохранить: касса или счёт другой организации."""


CONCURRENT_CORRECTION_CHANGE = (
    "Корректировку одновременно перенёс в другую кассу другой пользователь. Откройте её заново и повторите"
)


class UnsupportedBulkChange(PaymentError):
    """Массовая операция с платежами в неподдерживаемом виде — отказ до записи."""

    def __init__(self, message):
        super().__init__({"__all__": message})


def _invoice_model():
    return apps.get_model("sales", "Invoice")


def _lock(model, ids):
    """Блокирует строки модели на время транзакции — по возрастанию id."""
    ids = sorted({int(pk) for pk in ids if pk})
    if ids and connection.features.has_select_for_update:
        list(model.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk"))
    return ids


def lock_accounts(account_ids):
    """Уровень 2: денежные счета и кассы."""
    return _lock(apps.get_model("finance", "Account"), account_ids)


def lock_invoices(invoice_ids):
    """Уровень 3: счета покупателям."""
    return _lock(_invoice_model(), invoice_ids)


def paid_total(invoice_id):
    """Фактическая оплата счёта: сумма проведённых входящих платежей по нему."""
    payment_model = apps.get_model("finance", "Payment")
    total = payment_model.objects.filter(
        invoice_id=invoice_id, kind=payment_model.KIND_IN, status=DOC_POSTED,
    ).aggregate(total=Sum("amount"))["total"]
    return (total or ZERO).quantize(CENTS)


@transaction.atomic
def recompute_invoices(invoice_ids):
    """Пересчитывает «Оплачено» у счетов. Счета блокируются здесь же
    (повторная блокировка в той же транзакции безвредна), сумма считается
    после блокировки. Своя транзакция (или точка сохранения внутри внешней):
    вызов вне транзакции на PostgreSQL иначе падал — FOR UPDATE требует
    транзакции. Возвращает {id счёта: оплачено}."""
    result = {}
    for invoice_id in lock_invoices(invoice_ids):
        paid = paid_total(invoice_id)
        # update(), а не save(): пишем только это поле, ничего не затирая
        _invoice_model().objects.filter(pk=invoice_id).exclude(paid_amount=paid).update(paid_amount=paid)
        result[invoice_id] = paid
    return result


# Поля платежа, которые проверяются при записи (имена столбцов): связи,
# вид, статус, сумма и назначение для взаиморасчётов.
LINK_FIELDS = ("kind", "status", "organization_id", "account_id", "counterparty_id", "invoice_id", "amount",
               "settlement_purpose")

NEGATIVE_AMOUNT = (
    "Сумма платежа не может быть отрицательной: направление денег задаёт вид платежа "
    "(входящий или исходящий), а не знак суммы"
)
ZERO_POSTED = "Нельзя провести платёж с нулевой суммой — укажите сумму больше нуля"


def link_values(payment):
    """Значения связей объекта платежа — только идентификаторы, без
    закэшированных связанных объектов."""
    return {name: getattr(payment, name) for name in LINK_FIELDS}


def written_values(payment, stored, update_fields):
    """Что окажется в базе после save(update_fields=...): изменяемые поля —
    из объекта, остальные — как сохранены (stored — строка из базы)."""
    values = link_values(payment)
    if stored is None or update_fields is None:
        return values
    written = set()
    for name in update_fields:
        field = payment._meta.get_field(name)
        written.add(field.attname)
    return {name: (values[name] if name in written else stored[name]) for name in LINK_FIELDS}


def consistency_errors(payment):
    """Несовместимые связи и недопустимая сумма объекта платежа (для форм
    и админки). Связанные записи и прежнее состояние платежа читаются из базы
    заново: объект в памяти мог устареть."""
    values = link_values(payment)
    stored = None
    if payment.pk and not payment._state.adding:
        stored = apps.get_model("finance", "Payment").objects.filter(pk=payment.pk).values(*LINK_FIELDS).first()
    return {**link_errors(values), **amount_errors(values, stored)}


def amount_errors(values, stored=None):
    """Допустимость суммы для того, что окажется в базе: {"amount": текст}.

    * Сумма проведённого платежа (входящего и исходящего) строго больше нуля.
    * Черновик может содержать ноль — так готовят документ.
    * Отрицательная сумма недопустима, кроме одного случая: старый платёж,
      у которого она уже записана, остаётся черновиком с той же суммой
      (его снимают с проведения, чтобы исправить или удалить).
    stored — строка платежа из базы до изменения (None — новый платёж).
    """
    amount = values["amount"]
    if amount is None:
        return {"amount": "Укажите сумму платежа"}
    if amount < 0:
        kept_old = stored is not None and stored["amount"] == amount and values["status"] != DOC_POSTED
        return {} if kept_old else {"amount": NEGATIVE_AMOUNT}
    if amount == 0 and values["status"] == DOC_POSTED:
        return {"amount": ZERO_POSTED}
    return {}


def auto_purpose(values):
    """Назначение для взаиморасчётов, если оно не указано: входящий платёж
    со счётом-основанием — «оплата от покупателя» (счёт — продажа), остальные
    — «не распределено». Ничего не угадывается по типу контрагента, тексту
    назначения или наличию закупок."""
    payment_model = apps.get_model("finance", "Payment")
    if values["invoice_id"] and values["kind"] == payment_model.KIND_IN:
        return payment_model.PURPOSE_CUSTOMER_PAYMENT
    return payment_model.PURPOSE_UNALLOCATED


def purpose_errors(values):
    """Назначение для взаиморасчётов против направления денег и
    счёта-основания: {"settlement_purpose": текст}. Пустое («не указано»)
    проверяется так, как его определит auto_purpose."""
    payment_model = apps.get_model("finance", "Payment")
    labels = dict(payment_model.PURPOSE_CHOICES)
    purpose = values["settlement_purpose"] or auto_purpose(values)
    if purpose not in labels:
        return {"settlement_purpose": f"Неизвестное назначение для взаиморасчётов: «{purpose}»"}
    if purpose not in payment_model.PURPOSES_BY_KIND.get(values["kind"], ()):
        if values["kind"] == payment_model.KIND_IN:
            text = "это деньги, которые мы отдаём: такое назначение бывает только у исходящего платежа"
        else:
            text = "это деньги, которые мы получаем: такое назначение бывает только у входящего платежа"
        return {"settlement_purpose": f"«{labels[purpose]}» — {text}"}
    if (values["invoice_id"] and values["kind"] == payment_model.KIND_IN
            and purpose != payment_model.PURPOSE_CUSTOMER_PAYMENT):
        return {"settlement_purpose": (
            f"Платёж по счёту покупателю — это «{labels[payment_model.PURPOSE_CUSTOMER_PAYMENT]}», "
            f"а выбрано «{labels[purpose]}». Выберите это назначение или уберите счёт-основание"
        )}
    return {}


def link_errors(values):
    """Несовместимые связи для набора значений LINK_FIELDS: {поле: текст}.

    Счёт, касса, счёт покупателю, организация и контрагент читаются из базы
    при каждом вызове. При сохранении платежа вызывается после блокировки
    этих строк — тогда прочитанное не изменится до конца транзакции.
    """
    from apps.core.models import Organization

    payment_model = apps.get_model("finance", "Payment")
    account_model = apps.get_model("finance", "Account")
    counterparty_model = apps.get_model("partners", "Counterparty")
    errors = {}
    organization = Organization.objects.filter(pk=values["organization_id"]).first()
    org_name = f"«{organization}»" if organization else "—"

    account = (
        account_model.objects.select_related("organization").filter(pk=values["account_id"]).first()
        if values["account_id"] else None
    )
    if account is not None and values["organization_id"] and account.organization_id != values["organization_id"]:
        errors["account"] = (
            f"«{account}» принадлежит организации «{account.organization}», а платёж оформлен "
            f"на {org_name}. Выберите счёт или кассу этой организации"
        )

    invoice = (
        _invoice_model().objects.select_related("organization", "customer").filter(pk=values["invoice_id"]).first()
        if values["invoice_id"] else None
    )
    if invoice is not None:
        if values["kind"] != payment_model.KIND_IN:
            errors["invoice"] = "Со счётом покупателю связывается только входящий платёж"
        elif values["organization_id"] and invoice.organization_id != values["organization_id"]:
            errors["invoice"] = (
                f"{invoice} выставлен организацией «{invoice.organization}», а платёж оформлен на {org_name}"
            )
        elif values["counterparty_id"] and invoice.customer_id != values["counterparty_id"]:
            counterparty = counterparty_model.objects.filter(pk=values["counterparty_id"]).first()
            errors["invoice"] = (
                f"{invoice} выставлен покупателю «{invoice.customer}», а в платеже указан «{counterparty}»"
            )
    errors.update(purpose_errors(values))
    return errors


# ---------------------------------------------------------------------------
# Диагностика существующих данных (только чтение)
# ---------------------------------------------------------------------------

def paid_amount_differences():
    """Счета, у которых сохранённая оплата не равна сумме проведённых платежей."""
    payment_model = apps.get_model("finance", "Payment")
    actual = {
        row["invoice_id"]: row["total"]
        for row in payment_model.objects
        .filter(invoice__isnull=False, kind=payment_model.KIND_IN, status=DOC_POSTED)
        .values("invoice_id").annotate(total=Sum("amount"))
    }
    found = []
    for invoice in _invoice_model().objects.order_by("pk"):
        paid = (actual.get(invoice.pk) or ZERO).quantize(CENTS)
        if invoice.paid_amount != paid:
            found.append((invoice, invoice.paid_amount, paid))
    return found


def invalid_amounts():
    """Платежи с недопустимой суммой: отрицательные (любой статус) и
    проведённые нулевые. Нулевой черновик ошибкой не считается."""
    payment_model = apps.get_model("finance", "Payment")
    return list(
        payment_model.objects.filter(Q(amount__lt=0) | Q(amount=0, status=DOC_POSTED)).order_by("pk")
    )


def incompatible_payments():
    """Платежи с несовместимыми связями: [(платёж, {поле: текст})]."""
    payment_model = apps.get_model("finance", "Payment")
    found = []
    for payment in payment_model.objects.order_by("pk"):
        errors = link_errors(link_values(payment))
        if errors:
            found.append((payment, errors))
    return found


# ---------------------------------------------------------------------------
# Корректировки остатков: касса или счёт — своей организации
# ---------------------------------------------------------------------------

# Поля корректировки, которые проверяются при записи (имена столбцов).
CORRECTION_FIELDS = ("organization_id", "account_id", "status")
_CORRECTION_NAMES = {"organization": "organization_id", "organization_id": "organization_id",
                     "account": "account_id", "account_id": "account_id", "status": "status"}


def correction_values(correction):
    return {name: getattr(correction, name) for name in CORRECTION_FIELDS}


def written_correction_values(correction, stored, update_fields):
    """Что окажется в базе после save(update_fields=...): изменяемые поля —
    из объекта, остальные — как сохранены (stored — строка из базы)."""
    values = correction_values(correction)
    if stored is None or update_fields is None:
        return values
    written = {correction._meta.get_field(name).attname for name in update_fields}
    return {name: (values[name] if name in written else stored[name]) for name in CORRECTION_FIELDS}


def only_unposting(values, update_fields):
    """Запись только статуса «не проведена» — снятие с проведения. Его не
    запрещают: им исправляют старые несовместимые документы."""
    return update_fields is not None and set(update_fields) == {"status"} and values["status"] != DOC_POSTED


def correction_link_errors(values):
    """{"account": текст}, если касса или счёт принадлежит не организации
    корректировки. Касса и организация читаются из базы при каждом вызове —
    при сохранении после блокировки строки кассы."""
    from apps.core.models import Organization

    if not values["account_id"] or not values["organization_id"]:
        return {}
    account = apps.get_model("finance", "Account").objects.select_related("organization").filter(
        pk=values["account_id"]).first()
    if account is None or account.organization_id == values["organization_id"]:
        return {}
    organization = Organization.objects.filter(pk=values["organization_id"]).first()
    return {"account": (
        f"«{account}» принадлежит организации «{account.organization}», а корректировка оформлена на "
        f"«{organization}». Выберите счёт или кассу этой организации"
    )}


def _plain(value, field, subject):
    """Значение для проверяемого поля массовой операции: объект → id.
    Выражения пользователя (F(), Case(), подзапросы) не поддерживаются.
    subject — чьё поле, для сообщения: «корректировок остатков», «касс и счетов»."""
    if hasattr(value, "resolve_expression"):
        raise UnsupportedBulkChange(
            f"Массовое изменение поля «{field}» {subject} выражением не поддерживается: "
            "укажите конкретное значение или сохраняйте записи по одной"
        )
    return getattr(value, "pk", value)


def bulk_update_fields(model, fields):
    """Поля для bulk_update() с теми же условиями, что у Django: заданы,
    хранятся в таблице, не первичный ключ и не «многие ко многим».
    Принимаются и имя поля, и имя столбца (organization / organization_id)."""
    if not fields:
        raise ValueError("Field names must be given to bulk_update().")
    resolved = [model._meta.get_field(name) for name in fields]
    if any(not field.concrete or field.many_to_many for field in resolved):
        raise ValueError("bulk_update() can only be used with concrete fields.")
    if any(field.primary_key for field in resolved):
        raise ValueError("bulk_update() cannot be used with primary key fields.")
    return resolved


def locked_pks(queryset):
    """Блокирует строки выборки по возрастанию id (её фильтр сохраняется)
    и возвращает их id."""
    rows = queryset.order_by("pk")
    if connection.features.has_select_for_update:
        if connection.features.has_select_for_update_of:
            rows = rows.select_for_update(of=("self",))
        else:
            rows = rows.select_for_update()
    return list(rows.values_list("pk", flat=True))


def write_rows(model, objs_by_pk, fields):
    """Прямая запись пакета bulk_update() после проверок и блокировок — по
    строке, по возрастанию id, мимо переопределённого QuerySet.update().
    Стандартный bulk_update() подставляет в update() свои выражения
    Case/When; их нельзя путать с выражениями пользователя, поэтому он здесь
    не используется. Возвращает число действительно обновлённых строк."""
    count = 0
    for pk in sorted(objs_by_pk):
        obj = objs_by_pk[pk]
        count += model._base_manager.filter(pk=pk).update(
            **{field.attname: getattr(obj, field.attname) for field in fields})
    return count


def check_correction_links(rows, update_fields):
    """Проверка набора записей корректировок перед записью. rows —
    [(значения, которые будут записаны, подпись или None)]; блокировки
    (кассы, затем корректировки) уже взяты вызывающим. Одна несовместимая
    строка — отказ всего пакета."""
    for values, label in rows:
        if only_unposting(values, update_fields):
            continue
        errors = correction_link_errors(values)
        if errors:
            raise CorrectionError({key: f"{label}: {text}" if label else text for key, text in errors.items()})


def incompatible_corrections():
    """Корректировки остатков с кассой или счётом другой организации:
    [(корректировка, {поле: текст})] — для диагностики, ничего не меняет."""
    model = apps.get_model("finance", "AccountCorrection")
    found = []
    for correction in model._base_manager.order_by("pk"):
        errors = correction_link_errors(correction_values(correction))
        if errors:
            found.append((correction, errors))
    return found


# ---------------------------------------------------------------------------
# История денежного счёта: правило «остаток на момент»
# ---------------------------------------------------------------------------

PAYMENT_RANK = 0          # при полном совпадении даты, времени и момента
CORRECTION_RANK = 1       # создания платёж стоит раньше корректировки
_LAST_ID = 10 ** 18       # ещё не сохранённый документ — после всех с тем же временем


def money_key(date, time, created_at, rank, pk):
    """Место денежного документа в истории счёта: дата → время → момент
    создания → вид документа → id. Платежи и корректировки лежат в разных
    таблицах и их id могут совпадать, поэтому вид входит в ключ раньше id.
    Время последнего редактирования в порядок не входит: правка документа
    его не переставляет. Ещё не созданный документ считается созданным сейчас."""
    from django.utils import timezone

    return (date, time, created_at or timezone.now(), rank, pk or _LAST_ID)


def _history_events(account_id):
    payment_model = apps.get_model("finance", "Payment")
    correction_model = apps.get_model("finance", "AccountCorrection")
    events = []
    for pk, kind, amount, date, time, created in payment_model.objects.filter(
            account_id=account_id, status=DOC_POSTED).values_list(
            "pk", "kind", "amount", "date", "time", "created_at"):
        signed = amount if kind == payment_model.KIND_IN else -amount
        events.append((money_key(date, time, created, PAYMENT_RANK, pk), "payment", pk, signed, True))
    for pk, status, actual, date, time, created in correction_model._base_manager.filter(
            account_id=account_id).values_list("pk", "status", "actual_balance", "date", "time", "created_at"):
        events.append((money_key(date, time, created, CORRECTION_RANK, pk), "correction", pk, actual,
                       status == DOC_POSTED))
    return sorted(events, key=lambda event: event[0])


def account_history(account_id):
    """Расчёт истории счёта по данным базы — только чтение.

    {"opening": начальный остаток, "balance": остаток после всех проведённых
    документов, "corrections": {id корректировки: (расчётный остаток перед
    ней, разница)}}. Для черновика корректировки значения предварительные:
    по проведённым документам до её момента; на остаток черновик не влияет.
    """
    opening = apps.get_model("finance", "Account")._base_manager.filter(pk=account_id).values_list(
        "opening_balance", flat=True).first() or ZERO
    balance, corrections = opening, {}
    for _, kind, pk, value, posted in _history_events(account_id):
        if kind == "payment":
            balance += value
            continue
        corrections[pk] = (balance, value - balance)
        if posted:
            balance = value
    return {"opening": opening, "balance": balance, "corrections": corrections}


def balance_before(account_id, key, exclude_correction_pk=None):
    """Расчётный остаток счёта непосредственно перед моментом key (money_key)
    по проведённым документам — для предварительного расчёта в форме."""
    balance = apps.get_model("finance", "Account")._base_manager.filter(pk=account_id).values_list(
        "opening_balance", flat=True).first() or ZERO
    for event_key, kind, pk, value, posted in _history_events(account_id):
        if event_key >= key:
            break
        if kind == "payment":
            balance += value
        elif posted and pk != exclude_correction_pk:
            balance = value
    return balance


def lock_account_corrections(account_ids, extra_ids=()):
    """Уровень 3: строки корректировок указанных касс и строки extra_ids —
    одним запросом, по возрастанию id. Возвращает {id корректировки: id кассы}."""
    model = apps.get_model("finance", "AccountCorrection")
    account_ids = {int(pk) for pk in account_ids if pk}
    extra_ids = {int(pk) for pk in extra_ids if pk}
    if not account_ids and not extra_ids:
        return {}
    rows = model._base_manager.filter(Q(account_id__in=account_ids) | Q(pk__in=extra_ids)).order_by("pk")
    if connection.features.has_select_for_update:
        rows = rows.select_for_update()
    return dict(rows.values_list("pk", "account_id"))


def lock_account_history(account_ids, extra_correction_ids=()):
    """Уровни 2 и 3: кассы (по возрастанию id), затем все их корректировки."""
    lock_accounts(account_ids)
    return lock_account_corrections(account_ids, extra_correction_ids)


def recalculate_accounts(account_ids):
    """Пересчитывает историю касс: расчётный остаток и разницу каждой
    корректировки (проведённой — окончательно, черновика — предварительно).
    Пишет только изменившееся и только в строки корректировок. Вызывать под
    блокировками lock_account_history в той же транзакции.
    Возвращает {id кассы: account_history}."""
    model = apps.get_model("finance", "AccountCorrection")
    histories = {}
    for account_id in sorted({int(pk) for pk in account_ids if pk}):
        history = account_history(account_id)
        histories[account_id] = history
        stored = model._base_manager.filter(account_id=account_id).values_list("pk", "balance_before", "amount")
        for pk, stored_before, stored_amount in stored:
            before, amount = history["corrections"][pk]
            if (stored_before, stored_amount) != (before, amount):
                model._base_manager.filter(pk=pk).update(balance_before=before, amount=amount)
    return histories


def correction_history_differences():
    """Диагностика: записанные значения против правила «остаток на момент» —
    только чтение. [(касса, записанный остаток, остаток по правилу,
    [(корректировка, (записано: расчётный, разница), (по правилу: расчётный, разница))])]."""
    account_model = apps.get_model("finance", "Account")
    correction_model = apps.get_model("finance", "AccountCorrection")
    found = []
    ids = sorted(set(correction_model._base_manager.values_list("account_id", flat=True)))
    for account in account_model._base_manager.filter(pk__in=ids).order_by("pk"):
        history = account_history(account.pk)
        rows = []
        for correction in correction_model._base_manager.filter(account_id=account.pk).order_by("pk"):
            expected = history["corrections"][correction.pk]
            if (correction.balance_before, correction.amount) != expected:
                rows.append((correction, (correction.balance_before, correction.amount), expected))
        stored_balance = account.balance
        if rows or stored_balance != history["balance"]:
            found.append((account, stored_balance, history["balance"], rows))
    return found


def fix_correction_history(account_ids):
    """Исправление: пересчёт истории касс — каждая касса в своей транзакции,
    целиком или никак. Фактические остатки, даты и платежи не меняются."""
    fixed = []
    for account_id in sorted(account_ids):
        with transaction.atomic():
            lock_account_history([account_id])
            recalculate_accounts([account_id])
        fixed.append(account_id)
    return fixed
