"""Тексты журнала и уведомлений. Всё выводится в шаблонах с экранированием:
HTML в заголовке, описании или комментарии показывается как текст."""
from django.conf import settings
from django.utils import timezone

from .models import Task, TaskEvent
from .services import status_label

TEXT_LABELS = {
    TaskEvent.WAITING: "Причина",
    TaskEvent.SUBMITTED: "Результат",
    TaskEvent.COMPLETED: "Результат",
    TaskEvent.RETURNED: "Причина",
    TaskEvent.CANCELLED: "Причина",
    TaskEvent.REOPENED: "Причина",
    TaskEvent.REASSIGNED: "Причина",
    TaskEvent.CHECK_ADDED: "Пункт",
    TaskEvent.CHECK_REMOVED: "Пункт",
    TaskEvent.CHECK_DONE: "Пункт",
    TaskEvent.CHECK_UNDONE: "Пункт",
    TaskEvent.REVIEW: "Пояснение",
}

STATUS_EVENTS = {TaskEvent.STARTED, TaskEvent.WAITING, TaskEvent.UNWAITED, TaskEvent.SUBMITTED,
                 TaskEvent.COMPLETED, TaskEvent.ACCEPTED, TaskEvent.RETURNED, TaskEvent.CANCELLED,
                 TaskEvent.REOPENED}


def describe(event):
    """Запись журнала для карточки: заголовок, было → стало, текст."""
    change = None
    if event.action in (TaskEvent.TITLE, TaskEvent.DUE, TaskEvent.PRIORITY, TaskEvent.REVIEW,
                        TaskEvent.CHECK_RENAMED, TaskEvent.REASSIGNED):
        change = (event.old_value or "—", event.new_value or "—")
    elif event.action in STATUS_EVENTS:
        change = (status_label(event.old_status), status_label(event.new_status))
    elif event.action == TaskEvent.CREATED:
        change = None
    details = []
    if event.action == TaskEvent.CREATED:
        details.append(("Исполнитель", event.new_value))
    if event.action == TaskEvent.REASSIGNED and event.old_status and event.old_status != Task.NEW:
        details.append(("Статус", f"{status_label(event.old_status)} → {status_label(Task.NEW)}"))
    if event.action == TaskEvent.DESCRIPTION:
        details.append(("Было", event.old_value or "—"))
        details.append(("Стало", event.new_value or "—"))
    if event.action == TaskEvent.REOPENED and event.old_value:
        details.append(("Прежний результат", event.old_value))
    if event.text and event.action != TaskEvent.COMMENTED:
        details.append((TEXT_LABELS.get(event.action, "Текст"), event.text))
    return {
        "title": event.get_action_display(),
        "change": change,
        "details": details,
        "comment": event.comment.text if event.action == TaskEvent.COMMENTED and event.comment_id else None,
    }


NOTICE_TEXT = {
    TaskEvent.CREATED: "Вам поставлена задача",
    TaskEvent.DUE: "Изменён срок",
    TaskEvent.SUBMITTED: "Результат сдан на проверку",
    TaskEvent.COMPLETED: "Задача выполнена исполнителем",
    TaskEvent.ACCEPTED: "Результат принят",
    TaskEvent.RETURNED: "Задача возвращена на доработку",
    TaskEvent.CANCELLED: "Задача отменена",
    TaskEvent.REOPENED: "Задача возобновлена",
    TaskEvent.COMMENTED: "Новый комментарий",
}


def notice_text(notification):
    event = notification.event
    if event.action == TaskEvent.REASSIGNED:
        if event.new_user_id == notification.recipient_id:
            return "Вам передана задача"
        if event.old_user_id == notification.recipient_id:
            return "Задача передана другому исполнителю"
        return "Сменился исполнитель"
    return NOTICE_TEXT.get(event.action, event.get_action_display())


def notice_change(notification):
    event = notification.event
    if event.action == TaskEvent.DUE:
        return f"{event.old_value} → {event.new_value}"
    if event.action == TaskEvent.REASSIGNED and event.old_user_id != notification.recipient_id:
        return f"{event.old_value} → {event.new_value}"
    return ""


def timezone_label():
    """Часовой пояс сроков — явно в форме и карточке."""
    offset = timezone.localtime().strftime("%z")          # +0300
    sign, hours, minutes = offset[0], int(offset[1:3]), offset[3:5]
    utc = f"UTC{sign}{hours}" + (f":{minutes}" if minutes != "00" else "")
    if settings.TIME_ZONE == "Europe/Moscow":
        return f"московское время ({utc})"
    return f"часовой пояс {settings.TIME_ZONE} ({utc})"
