"""Задачи сотрудникам (CRM-1).

Задача не участвует в учёте: её создание, изменение и завершение не пишут
складских движений, денег, взаиморасчётов и не проводят документы.

Записи меняются только операциями apps/tasks/services.py: в одной транзакции
с блокировкой строки задачи меняется задача, пишется журнал и уведомления.
Журнал, комментарии и сама задача не удаляются; постановщик и связь задачи
не меняются после создания.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .links import LINK_CODES, LINK_TYPES


class TaskIntegrityError(Exception):
    """Запись мимо правил задач (удаление, смена постановщика или связи, правка журнала)."""


IMMUTABLE_FIELDS = ("creator", *LINK_CODES, "link_type")


def _one_link_condition():
    """Ровно одна связь из списка — или никакой; link_type совпадает с заполненным полем."""
    empty = {f"{code}__isnull": True for code in LINK_CODES}
    condition = Q(link_type="", **empty)
    for code in LINK_CODES:
        condition |= Q(link_type=code, **{**empty, f"{code}__isnull": False})
    return condition


STATUS_CODES = ["new", "in_progress", "waiting", "review", "done", "cancelled"]


class TaskQuerySet(models.QuerySet):
    def update(self, **kwargs):
        fixed = [name for name in IMMUTABLE_FIELDS if name in kwargs or f"{name}_id" in kwargs]
        if fixed:
            raise TaskIntegrityError(f"Постановщика и связь задачи менять нельзя ({', '.join(fixed)})")
        return super().update(**kwargs)

    def delete(self):
        raise TaskIntegrityError("Задачи не удаляются: отмените задачу — история сохранится")


class Task(models.Model):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (NEW, "Новая"),
        (IN_PROGRESS, "В работе"),
        (WAITING, "Ожидание"),
        (REVIEW, "На проверке"),
        (DONE, "Завершена"),
        (CANCELLED, "Отменена"),
    ]
    ACTIVE = (NEW, IN_PROGRESS, WAITING, REVIEW)
    FINAL = (DONE, CANCELLED)
    BEFORE_REVIEW = (NEW, IN_PROGRESS, WAITING)     # чек-лист и проверка ещё меняются

    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_CHOICES = [(PRIORITY_NORMAL, "Обычный"), (PRIORITY_HIGH, "Высокий")]

    title = models.CharField("Заголовок", max_length=200)
    description = models.TextField("Описание", blank=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks_created",
                                verbose_name="Постановщик")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks_assigned",
                                 verbose_name="Исполнитель")
    due_at = models.DateTimeField("Срок", null=True, blank=True)
    priority = models.CharField("Приоритет", max_length=8, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=NEW)
    requires_review = models.BooleanField("Требуется проверка результата", default=False)
    result = models.TextField("Результат", blank=True)
    status_reason = models.TextField("Причина ожидания или отмены", blank=True)

    link_type = models.CharField("Связь", max_length=32, blank=True,
                                 choices=[("", "Без связи")] + [(link.code, link.label) for link in LINK_TYPES])
    counterparty = models.ForeignKey("partners.Counterparty", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="tasks", verbose_name="Контрагент")
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="tasks", verbose_name="Счёт покупателю")
    shipment = models.ForeignKey("sales.Shipment", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="tasks", verbose_name="Отгрузка")
    customer_return = models.ForeignKey("sales.CustomerReturn", on_delete=models.PROTECT, null=True, blank=True,
                                        related_name="tasks", verbose_name="Возврат покупателя")
    receipt = models.ForeignKey("purchases.Receipt", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="tasks", verbose_name="Приёмка")
    supplier_return = models.ForeignKey("purchases.SupplierReturn", on_delete=models.PROTECT, null=True,
                                        blank=True, related_name="tasks", verbose_name="Возврат поставщику")
    payment = models.ForeignKey("finance.Payment", on_delete=models.PROTECT, null=True, blank=True,
                                related_name="tasks", verbose_name="Платёж")

    created_at = models.DateTimeField("Создана", default=timezone.now)
    updated_at = models.DateTimeField("Изменена", default=timezone.now)
    submitted_at = models.DateTimeField("Результат сдан", null=True, blank=True)
    completed_at = models.DateTimeField("Завершена", null=True, blank=True)
    cancelled_at = models.DateTimeField("Отменена", null=True, blank=True)
    version = models.PositiveIntegerField("Версия", default=1)

    objects = TaskQuerySet.as_manager()

    class Meta:
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"
        ordering = ["-pk"]
        indexes = [
            models.Index(fields=["assignee", "status"], name="tasks_assignee_status"),
            models.Index(fields=["creator", "status"], name="tasks_creator_status"),
            models.Index(fields=["status", "due_at"], name="tasks_status_due"),
        ]
        constraints = [
            models.CheckConstraint(condition=_one_link_condition(), name="tasks_task_one_link"),
            models.CheckConstraint(condition=Q(status__in=STATUS_CODES), name="tasks_task_status_known"),
        ]

    def __str__(self):
        return f"Задача № {self.number}"

    @property
    def number(self):
        """Номер задачи — её id: постоянный и не зависит от организации и года."""
        return str(self.pk) if self.pk else ""

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    @property
    def is_overdue(self):
        return bool(self.due_at and self.status in self.ACTIVE and self.due_at < timezone.now())

    @property
    def is_self_task(self):
        return self.creator_id == self.assignee_id

    @property
    def link(self):
        """(тип связи, объект) или None."""
        if not self.link_type:
            return None
        from .links import BY_CODE

        return BY_CODE[self.link_type], getattr(self, self.link_type)

    def link_id(self):
        return getattr(self, f"{self.link_type}_id") if self.link_type else None

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            stored = type(self)._base_manager.filter(pk=self.pk).values(
                "creator_id", "link_type", *[f"{code}_id" for code in LINK_CODES]).first()
            if stored:
                changed = [name for name, value in stored.items() if getattr(self, name) != value]
                if changed:
                    raise TaskIntegrityError("Постановщика и связь задачи менять нельзя")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TaskIntegrityError("Задачи не удаляются: отмените задачу — история сохранится")


class ChecklistItem(models.Model):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="checklist")
    text = models.CharField("Пункт", max_length=300)
    position = models.PositiveIntegerField(default=0)
    is_done = models.BooleanField("Выполнен", default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="+")
    is_removed = models.BooleanField("Удалён из чек-листа", default=False)

    class Meta:
        verbose_name = "Пункт чек-листа"
        verbose_name_plural = "Пункты чек-листа"
        ordering = ["position", "pk"]

    def __str__(self):
        return self.text


class AppendOnlyQuerySet(models.QuerySet):
    """История не правится и не удаляется — ни формами, ни массово."""

    def update(self, **kwargs):
        raise TaskIntegrityError("Историю задачи изменить нельзя")

    def delete(self):
        raise TaskIntegrityError("Историю задачи удалить нельзя")


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise TaskIntegrityError("Историю задачи изменить нельзя")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TaskIntegrityError("Историю задачи удалить нельзя")


class TaskComment(AppendOnly):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                               verbose_name="Автор")
    text = models.TextField("Комментарий")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Комментарий к задаче"
        verbose_name_plural = "Комментарии к задачам"
        ordering = ["created_at", "pk"]


class TaskEvent(AppendOnly):
    """Запись журнала задачи: кто, когда, что сделал, было → стало."""

    CREATED = "created"
    STARTED = "started"
    WAITING = "waiting"
    UNWAITED = "unwaited"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    ACCEPTED = "accepted"
    RETURNED = "returned"
    CANCELLED = "cancelled"
    REOPENED = "reopened"
    REASSIGNED = "reassigned"
    TITLE = "title"
    DESCRIPTION = "description"
    DUE = "due"
    PRIORITY = "priority"
    REVIEW = "review"
    CHECK_ADDED = "check_added"
    CHECK_RENAMED = "check_renamed"
    CHECK_REMOVED = "check_removed"
    CHECK_DONE = "check_done"
    CHECK_UNDONE = "check_undone"
    COMMENTED = "commented"
    ACTION_CHOICES = [
        (CREATED, "Задача поставлена"),
        (STARTED, "Взята в работу"),
        (WAITING, "Переведена в ожидание"),
        (UNWAITED, "Возвращена из ожидания в работу"),
        (SUBMITTED, "Результат сдан на проверку"),
        (COMPLETED, "Завершена исполнителем"),
        (ACCEPTED, "Результат принят"),
        (RETURNED, "Возвращена на доработку"),
        (CANCELLED, "Отменена"),
        (REOPENED, "Возобновлена"),
        (REASSIGNED, "Сменён исполнитель"),
        (TITLE, "Изменён заголовок"),
        (DESCRIPTION, "Изменено описание"),
        (DUE, "Изменён срок"),
        (PRIORITY, "Изменён приоритет"),
        (REVIEW, "Изменена проверка результата"),
        (CHECK_ADDED, "Добавлен пункт чек-листа"),
        (CHECK_RENAMED, "Изменён пункт чек-листа"),
        (CHECK_REMOVED, "Удалён пункт чек-листа"),
        (CHECK_DONE, "Отмечен пункт чек-листа"),
        (CHECK_UNDONE, "Снята отметка пункта чек-листа"),
        (COMMENTED, "Комментарий"),
    ]

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="events")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
                               verbose_name="Автор")
    created_at = models.DateTimeField(default=timezone.now)
    action = models.CharField("Действие", max_length=32, choices=ACTION_CHOICES)
    old_value = models.TextField("Было", blank=True)
    new_value = models.TextField("Стало", blank=True)
    text = models.TextField("Причина, результат или пункт", blank=True)
    old_status = models.CharField(max_length=16, blank=True, choices=Task.STATUS_CHOICES)
    new_status = models.CharField(max_length=16, blank=True, choices=Task.STATUS_CHOICES)
    old_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="+")
    new_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="+")
    comment = models.OneToOneField(TaskComment, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="event")

    class Meta:
        verbose_name = "Событие задачи"
        verbose_name_plural = "События задач"
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["task", "created_at"], name="tasks_event_task_time")]

    def __str__(self):
        return f"{self.get_action_display()} ({self.task_id})"


class Notification(models.Model):
    """Внутреннее уведомление: ссылка на задачу и событие журнала.

    Текст задачи здесь не хранится: подробности показываются по текущему
    праву на задачу, после его потери уведомление не раскрывает заголовок."""

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  related_name="task_notifications")
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="+")
    event = models.ForeignKey(TaskEvent, on_delete=models.PROTECT, related_name="notifications")
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ["-pk"]
        constraints = [models.UniqueConstraint(fields=["recipient", "event"], name="tasks_one_notice_per_event")]
        indexes = [models.Index(fields=["recipient", "read_at"], name="tasks_notice_unread")]


assert [code for code, _ in Task.STATUS_CHOICES] == STATUS_CODES
