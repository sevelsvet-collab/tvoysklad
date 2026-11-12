"""Операции с задачами — единственный путь их изменения.

Каждая операция:
- в одной транзакции блокирует строку задачи и перечитывает её из базы;
- проверяет право по ролям, прочитанным в этой же транзакции, и текущий статус;
- меняет задачу, пишет журнал и уведомления — всё или ничего;
- увеличивает версию задачи, если меняет её саму или чек-лист.

Правка содержания (update_task, reassign) сверяет версию, с которой была
открыта форма: если задачу успели изменить, сохранение отклоняется с
конфликтом, а не затирает чужие изменения. Повтор того же сохранения, когда
менять уже нечего, — не конфликт, а «изменений нет». Переходы состояний
версию не требуют: их защищает проверка текущего статуса, поэтому повторное
нажатие («Принять» дважды) не создаёт второго события и уведомления.

Склад, деньги, взаиморасчёты и проведение документов здесь не затрагиваются.
"""
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .access import Viewer
from .links import BY_CODE
from .models import ChecklistItem, Notification, Task, TaskComment, TaskEvent

TITLE_MAX = 200
TEXT_MAX = 10000
REASON_MAX = 2000
COMMENT_MAX = 5000
ITEM_MAX = 300
ITEMS_MAX = 50


class TaskError(Exception):
    """Отказ операции; message — текст для пользователя, field — поле формы."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class TaskNotFound(TaskError):
    """Задачи нет или она недоступна — для пользователя одно и то же (404)."""


class TaskForbidden(TaskError):
    """Задачу видно, но это действие пользователю не разрешено (403)."""


class TaskConflict(TaskError):
    """Задачу изменили после открытия формы."""


class TaskStateError(TaskError):
    """Действие не подходит к текущему состоянию задачи."""


# ---------- оформление значений журнала

def user_label(user):
    return user.display_name if user is not None else ""


def due_label(value):
    if value is None:
        return "без срока"
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def status_label(code):
    return dict(Task.STATUS_CHOICES).get(code, code)


def priority_label(code):
    return dict(Task.PRIORITY_CHOICES).get(code, code)


def review_label(value):
    return "требуется" if value else "не требуется"


def _text(value, limit, what, field, required=False):
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if required and not value:
        raise TaskError(f"Укажите {what}", field)
    if len(value) > limit:
        raise TaskError(f"Слишком длинный текст: {what} — не более {limit} знаков", field)
    return value


def _title(value):
    title = " ".join((value or "").split())
    if not title:
        raise TaskError("Укажите заголовок задачи", "title")
    if len(title) > TITLE_MAX:
        raise TaskError(f"Заголовок — не более {TITLE_MAX} знаков", "title")
    return title


def _items(texts):
    items = [" ".join(text.split()) for text in texts]
    items = [text for text in items if text]
    for text in items:
        if len(text) > ITEM_MAX:
            raise TaskError(f"Пункт чек-листа — не более {ITEM_MAX} знаков", "checklist")
    return items


# ---------- общие шаги

def _locked(viewer, task_id):
    """Строка задачи под блокировкой. Недоступная задача — как несуществующая."""
    task = Task.objects.select_for_update().filter(pk=task_id).first()
    if task is None or not viewer.can_view(task):
        raise TaskNotFound("Задача не найдена")
    return task


def _require_manager(viewer, task, what):
    if not viewer.can_manage(task):
        raise TaskForbidden(f"{what} может постановщик задачи или администратор")


def _require_executor(viewer, task, what):
    if not viewer.is_executor(task):
        raise TaskForbidden(f"{what} может только исполнитель задачи")


def _event(task, actor, action, **values):
    return TaskEvent.objects.create(task=task, author=actor, action=action, **values)


def _notify(event, recipients):
    """Не более одного уведомления на событие каждому; автор себе не пишет;
    отключённым сотрудникам не пишем."""
    users = {user.pk: user for user in recipients if user is not None}
    users.pop(event.author_id, None)
    active = get_user_model().objects.filter(pk__in=users, is_active=True).values_list("pk", flat=True)
    Notification.objects.bulk_create(
        [Notification(recipient_id=pk, task_id=event.task_id, event=event) for pk in sorted(active)],
        ignore_conflicts=True,
    )


def _touch(task):
    task.version += 1
    task.updated_at = timezone.now()
    task.save()


def _check_assignee(user, link_code, link_object=None):
    """Исполнитель — активный сотрудник; для связанной задачи — с правом на карточку объекта."""
    if user is None:
        raise TaskError("Выберите исполнителя", "assignee")
    fresh = get_user_model().objects.filter(pk=user.pk).first()
    viewer = Viewer(fresh) if fresh is not None else None
    if viewer is None or not viewer.is_staff_member:
        raise TaskError("Исполнителем может быть только действующий сотрудник с ролью в программе", "assignee")
    if link_code and not viewer.link_allowed(link_code):
        link = BY_CODE[link_code]
        what = f"«{link_object}»" if link_object is not None else link.label_genitive
        raise TaskError(
            f"Сотрудник {fresh.display_name} не может открыть {link.label_genitive} {what}: у его ролей нет "
            "доступа к таким документам. Связанную задачу ему поставить нельзя — поставьте отдельную "
            "задачу без связи с документом и опишите в ней только то, что нужно для работы",
            "assignee",
        )
    return fresh


def _open_items(task):
    return task.checklist.filter(is_removed=False)


# ---------- создание

@dataclass
class NewTask:
    title: str
    assignee: object
    description: str = ""
    due_at: object = None
    priority: str = Task.PRIORITY_NORMAL
    requires_review: bool = False
    checklist: list = field(default_factory=list)
    link_code: str = ""
    link_id: object = None


def create_task(actor, data):
    """Новая задача. Постановщик — всегда actor."""
    viewer = Viewer(actor)
    if not viewer.is_staff_member:
        raise TaskForbidden("Ставить задачи могут только сотрудники с ролью в программе")
    title = _title(data.title)
    description = _text(data.description, TEXT_MAX, "описание", "description")
    if data.priority not in dict(Task.PRIORITY_CHOICES):
        raise TaskError("Неизвестный приоритет", "priority")
    items = _items(data.checklist)
    if len(items) > ITEMS_MAX:
        raise TaskError(f"В чек-листе не более {ITEMS_MAX} пунктов", "checklist")
    if data.assignee is not None and data.assignee.pk != actor.pk and not viewer.can_delegate:
        raise TaskForbidden("Поручать задачи другим сотрудникам могут администратор и менеджер. "
                            "Себе задачу поставить можно", "assignee")
    with transaction.atomic():
        link_object = None
        if data.link_code:
            link = BY_CODE.get(data.link_code)
            if link is None:
                raise TaskError("Неизвестный вид связанного объекта", "link")
            if not viewer.link_allowed(link.code):
                raise TaskForbidden(f"У вас нет доступа к карточке: {link.label.lower()}", "link")
            try:
                pk = int(data.link_id)
            except (TypeError, ValueError):
                raise TaskError("Связанный объект не найден", "link")
            # блокировка строки: одновременное удаление дождётся задачи и откажет
            link_object = link.model_class.objects.select_for_update(no_key=True).filter(pk=pk).first()
            if link_object is None:
                raise TaskError(f"Связанный объект ({link.label.lower()}) не найден — возможно, его удалили",
                                "link")
        assignee = _check_assignee(data.assignee, data.link_code, link_object)
        requires_review = bool(data.requires_review) and assignee.pk != actor.pk
        now = timezone.now()
        task = Task(title=title, description=description, creator=actor, assignee=assignee,
                    due_at=data.due_at, priority=data.priority, requires_review=requires_review,
                    created_at=now, updated_at=now)
        if link_object is not None:
            task.link_type = data.link_code
            setattr(task, data.link_code, link_object)
        task.save()
        ChecklistItem.objects.bulk_create(
            [ChecklistItem(task=task, text=text, position=index) for index, text in enumerate(items, 1)])
        event = _event(task, actor, TaskEvent.CREATED, new_status=Task.NEW, new_user=assignee,
                       new_value=user_label(assignee))
        _notify(event, [assignee])
    return task


# ---------- правка содержания

@dataclass
class TaskChanges:
    title: str
    description: str
    due_at: object
    priority: str
    requires_review: bool
    renamed: dict = field(default_factory=dict)      # {id пункта: новый текст}
    removed: set = field(default_factory=set)        # id пунктов
    added: list = field(default_factory=list)        # тексты новых пунктов


def update_task(actor, task_id, version, changes):
    """Заголовок, описание, срок, приоритет, проверка результата, чек-лист.

    Возвращает (задача, изменилось ли что-нибудь)."""
    viewer = Viewer(actor)
    with transaction.atomic():
        task = _locked(viewer, task_id)
        _require_manager(viewer, task, "Менять задачу")
        if task.status in Task.FINAL:
            raise TaskStateError("Завершённую или отменённую задачу менять нельзя — сначала возобновите её")
        title = _title(changes.title)
        description = _text(changes.description, TEXT_MAX, "описание", "description")
        if changes.priority not in dict(Task.PRIORITY_CHOICES):
            raise TaskError("Неизвестный приоритет", "priority")
        events = []
        if title != task.title:
            events.append(dict(action=TaskEvent.TITLE, old_value=task.title, new_value=title))
        if description != task.description:
            events.append(dict(action=TaskEvent.DESCRIPTION, old_value=task.description, new_value=description))
        if changes.due_at != task.due_at:
            events.append(dict(action=TaskEvent.DUE, old_value=due_label(task.due_at),
                               new_value=due_label(changes.due_at)))
        if changes.priority != task.priority:
            events.append(dict(action=TaskEvent.PRIORITY, old_value=priority_label(task.priority),
                               new_value=priority_label(changes.priority)))
        review = bool(changes.requires_review)
        if review != task.requires_review:
            if task.status not in Task.BEFORE_REVIEW:
                raise TaskStateError("Проверку результата нельзя менять, когда результат уже сдан", "requires_review")
            if review and task.is_self_task:
                raise TaskError("Вы и постановщик, и исполнитель: проверка своего же результата не нужна",
                                "requires_review")
            events.append(dict(action=TaskEvent.REVIEW, old_value=review_label(task.requires_review),
                               new_value=review_label(review)))

        items = {item.pk: item for item in _open_items(task)}
        renamed = {pk: " ".join(text.split()) for pk, text in changes.renamed.items()
                   if pk in items and pk not in changes.removed}
        renamed = {pk: text for pk, text in renamed.items() if text and text != items[pk].text}
        removed = {pk for pk in changes.removed if pk in items}
        added = _items(changes.added)
        for text in renamed.values():
            if len(text) > ITEM_MAX:
                raise TaskError(f"Пункт чек-листа — не более {ITEM_MAX} знаков", "checklist")
        if (renamed or removed or added) and task.status not in Task.BEFORE_REVIEW:
            raise TaskStateError("Чек-лист меняется до сдачи результата", "checklist")
        if len(items) - len(removed) + len(added) > ITEMS_MAX:
            raise TaskError(f"В чек-листе не более {ITEMS_MAX} пунктов", "checklist")

        if not (events or renamed or removed or added):
            return task, False
        if version != task.version:
            raise TaskConflict("Задачу изменили, пока страница была открыта. Ваши изменения не сохранены: "
                               "обновите страницу и внесите их заново")

        task.title, task.description, task.due_at = title, description, changes.due_at
        task.priority, task.requires_review = changes.priority, review
        _touch(task)
        for values in events:
            event = _event(task, actor, **values)
            if values["action"] == TaskEvent.DUE:
                _notify(event, [task.assignee, task.creator])
        for pk, text in renamed.items():
            _event(task, actor, TaskEvent.CHECK_RENAMED, old_value=items[pk].text, new_value=text)
            ChecklistItem.objects.filter(pk=pk).update(text=text)
        for pk in sorted(removed):
            _event(task, actor, TaskEvent.CHECK_REMOVED, text=items[pk].text)
            ChecklistItem.objects.filter(pk=pk).update(is_removed=True)
        if added:
            start = max((item.position for item in task.checklist.all()), default=0)
            ChecklistItem.objects.bulk_create(
                [ChecklistItem(task=task, text=text, position=start + index) for index, text in enumerate(added, 1)])
            for text in added:
                _event(task, actor, TaskEvent.CHECK_ADDED, text=text)
    return task, True


def reassign(actor, task_id, version, new_assignee, reason):
    """Смена исполнителя: задача становится «Новой», прежний исполнитель теряет
    доступ (если он не постановщик), оба исполнителя получают уведомление.

    Возвращает (задача, изменилось ли что-нибудь)."""
    viewer = Viewer(actor)
    with transaction.atomic():
        task = _locked(viewer, task_id)
        _require_manager(viewer, task, "Менять исполнителя")
        if task.status in Task.FINAL:
            raise TaskStateError("Задача завершена или отменена: чтобы сменить исполнителя, сначала возобновите её")
        if task.status == Task.REVIEW:
            raise TaskStateError("Задача на проверке: сначала верните её на доработку, затем меняйте исполнителя")
        if new_assignee is not None and new_assignee.pk == task.assignee_id:
            return task, False
        if version != task.version:
            raise TaskConflict("Задачу изменили, пока страница была открыта. Исполнитель не изменён: "
                               "обновите страницу")
        reason = _text(reason, REASON_MAX, "причину смены исполнителя", "reason", required=True)
        if new_assignee is not None and new_assignee.pk != actor.pk and not viewer.can_delegate:
            raise TaskForbidden("Поручать задачи другим сотрудникам могут администратор и менеджер", "assignee")
        link = task.link
        assignee = _check_assignee(new_assignee, task.link_type, link[1] if link else None)
        old = task.assignee
        event = _event(task, actor, TaskEvent.REASSIGNED, old_user=old, new_user=assignee,
                       old_value=user_label(old), new_value=user_label(assignee), text=reason,
                       old_status=task.status, new_status=Task.NEW)
        task.assignee = assignee
        task.status = Task.NEW
        task.status_reason = ""
        if task.requires_review and assignee.pk == task.creator_id:
            task.requires_review = False
            _event(task, actor, TaskEvent.REVIEW, old_value=review_label(True), new_value=review_label(False),
                   text="Исполнителем стал постановщик — проверять свой результат не нужно")
        _touch(task)
        _notify(event, [old, assignee, task.creator])
    return task, True


# ---------- переходы состояний

@dataclass(frozen=True)
class Transition:
    action: str              # TaskEvent.*
    sources: tuple
    target: str
    who: str                 # "executor" | "manager"
    text: str = ""           # что спросить: "" — ничего
    notify: tuple = ()       # кому: "creator", "assignee"
    title: str = ""          # для сообщений


TRANSITIONS = {
    "start": Transition(TaskEvent.STARTED, (Task.NEW,), Task.IN_PROGRESS, "executor", title="Взять в работу"),
    "wait": Transition(TaskEvent.WAITING, (Task.NEW, Task.IN_PROGRESS), Task.WAITING, "executor",
                       text="причину ожидания", title="Перевести в ожидание"),
    "unwait": Transition(TaskEvent.UNWAITED, (Task.WAITING,), Task.IN_PROGRESS, "executor",
                         title="Вернуть в работу"),
    "submit": Transition(TaskEvent.SUBMITTED, (Task.IN_PROGRESS,), Task.REVIEW, "executor", text="результат",
                         notify=("creator",), title="Сдать результат"),
    "complete": Transition(TaskEvent.COMPLETED, (Task.IN_PROGRESS,), Task.DONE, "executor", text="результат",
                           notify=("creator",), title="Завершить"),
    "accept": Transition(TaskEvent.ACCEPTED, (Task.REVIEW,), Task.DONE, "manager", notify=("assignee",),
                         title="Принять результат"),
    "return": Transition(TaskEvent.RETURNED, (Task.REVIEW,), Task.IN_PROGRESS, "manager",
                         text="причину возврата", notify=("assignee",), title="Вернуть на доработку"),
    "cancel": Transition(TaskEvent.CANCELLED, Task.ACTIVE, Task.CANCELLED, "manager", text="причину отмены",
                         notify=("assignee", "creator"), title="Отменить"),
    "reopen": Transition(TaskEvent.REOPENED, Task.FINAL, Task.IN_PROGRESS, "manager",
                         text="причину возобновления", notify=("assignee", "creator"), title="Возобновить"),
}


def allowed_transitions(viewer, task):
    """Действия, которые показать на карточке (сервер всё равно проверит заново)."""
    names = []
    for name, rule in TRANSITIONS.items():
        if task.status not in rule.sources:
            continue
        if rule.who == "executor" and not viewer.is_executor(task):
            continue
        if rule.who == "manager" and not viewer.can_manage(task):
            continue
        if name == "submit" and not task.requires_review:
            continue
        if name == "complete" and task.requires_review:
            continue
        names.append(name)
    return names


def transition(actor, task_id, name, text=""):
    """Возвращает (задача, изменилось ли что-нибудь). Повтор уже выполненного
    перехода («Принять» второй раз) изменений не делает."""
    rule = TRANSITIONS.get(name)
    if rule is None:
        raise TaskError("Неизвестное действие")
    viewer = Viewer(actor)
    with transaction.atomic():
        task = _locked(viewer, task_id)
        if rule.who == "executor":
            _require_executor(viewer, task, f"Действие «{rule.title}» выполнить")
        else:
            _require_manager(viewer, task, f"Действие «{rule.title}» выполнить")
        if task.status not in rule.sources:
            if task.status == rule.target:
                return task, False          # уже сделано — повторная отправка
            raise TaskStateError(f"Действие «{rule.title}» недоступно: задача сейчас в статусе "
                                 f"«{status_label(task.status)}». Обновите страницу")
        if name == "submit" and not task.requires_review:
            raise TaskStateError("Проверка результата не требуется: завершите задачу")
        if name == "complete" and task.requires_review:
            raise TaskStateError("Постановщик ждёт проверку результата: сдайте результат на проверку")
        limit = TEXT_MAX if rule.text == "результат" else REASON_MAX
        value = _text(text, limit, rule.text, "text", required=True) if rule.text else ""
        if name in ("submit", "complete") and _open_items(task).filter(is_done=False).exists():
            raise TaskStateError("Сначала отметьте все пункты чек-листа")

        now = timezone.now()
        old_status = task.status
        old_result = task.result
        task.status = rule.target
        if name in ("submit", "complete"):
            task.result = value
            task.submitted_at = now
        if name in ("complete", "accept"):
            task.completed_at = now
        if name == "return":
            task.submitted_at = None
        if name == "wait":
            task.status_reason = value
        elif name == "cancel":
            task.status_reason = value
            task.cancelled_at = now
        else:
            task.status_reason = ""
        if name == "reopen":
            task.result = ""
            task.submitted_at = task.completed_at = task.cancelled_at = None
        _touch(task)
        event = _event(task, actor, rule.action, old_status=old_status, new_status=rule.target, text=value,
                       old_value=old_result if name == "reopen" else "")
        if rule.notify:
            people = {"creator": task.creator, "assignee": task.assignee}
            _notify(event, [people[who] for who in rule.notify])
    return task, True


# ---------- чек-лист и комментарии

def set_item_done(actor, task_id, item_id, done):
    """Отметка пункта исполнителем. Повтор той же отметки ничего не меняет."""
    viewer = Viewer(actor)
    with transaction.atomic():
        task = _locked(viewer, task_id)
        _require_executor(viewer, task, "Отмечать пункты чек-листа")
        if task.status not in Task.BEFORE_REVIEW:
            raise TaskStateError("Пункты отмечаются, пока результат не сдан")
        item = _open_items(task).filter(pk=item_id).first()
        if item is None:
            raise TaskStateError("Этого пункта уже нет в чек-листе. Обновите страницу")
        done = bool(done)
        if item.is_done == done:
            return task, False
        item.is_done = done
        item.done_at = timezone.now() if done else None
        item.done_by = actor if done else None
        item.save(update_fields=["is_done", "done_at", "done_by"])
        _touch(task)
        _event(task, actor, TaskEvent.CHECK_DONE if done else TaskEvent.CHECK_UNDONE, text=item.text)
    return task, True


def add_comment(actor, task_id, text):
    viewer = Viewer(actor)
    with transaction.atomic():
        task = _locked(viewer, task_id)
        text = _text(text, COMMENT_MAX, "текст комментария", "text", required=True)
        comment = TaskComment.objects.create(task=task, author=actor, text=text)
        event = _event(task, actor, TaskEvent.COMMENTED, comment=comment)
        _notify(event, [task.creator, task.assignee])
    return comment


# ---------- уведомления

def visible_notifications(viewer):
    """Уведомления пользователя, которые можно показать сейчас.

    Видно, если задача доступна сейчас. Прежнему исполнителю после смены
    исполнителя остаётся уведомление «задача передана другому» — без
    заголовка и ссылки, и только если связанный документ ему по-прежнему
    доступен: потеря права на документ закрывает и уведомления."""
    from django.db.models import Q

    if not viewer.is_staff_member:
        return Notification.objects.none()
    removed_notice = (Q(event__action=TaskEvent.REASSIGNED, event__old_user_id=viewer.pk)
                      & viewer.link_q("task__"))
    return (Notification.objects.filter(recipient_id=viewer.pk)
            .filter(viewer.visible_q("task__") | removed_notice))


def unread_count(viewer):
    return visible_notifications(viewer).filter(read_at__isnull=True).count()


def mark_read(viewer, notification_ids=None, task_id=None):
    rows = visible_notifications(viewer).filter(read_at__isnull=True)
    if notification_ids is not None:
        rows = rows.filter(pk__in=notification_ids)
    if task_id is not None:
        rows = rows.filter(task_id=task_id)
    return Notification.objects.filter(pk__in=list(rows.values_list("pk", flat=True))).update(read_at=timezone.now())
