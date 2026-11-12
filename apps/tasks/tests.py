"""Задачи сотрудникам (CRM-1): права, переходы, журнал, уведомления.

Проверяется серверная часть (apps/tasks/services.py, access.py) — то, что
действует независимо от экранов. Экраны и прямые адреса — tests_views.py,
удаление связанных объектов — tests_delete.py, одновременные операции на
PostgreSQL — tests_concurrency.py.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core import roles

from . import services
from .access import Viewer, staff_users
from .links import LINK_TYPES
from .models import ChecklistItem, Notification, Task, TaskComment, TaskEvent, TaskIntegrityError
from .services import TaskChanges, TaskConflict, TaskError, TaskForbidden, TaskNotFound, TaskStateError
from .testing import TaskWorld, accounting_state

ALL_USERS = ("admin", "manager", "manager2", "keeper", "accountant", "nobody", "boss")


def actions(task):
    return list(TaskEvent.objects.filter(task=task).order_by("pk").values_list("action", flat=True))


def notices(task):
    return sorted(Notification.objects.filter(task=task).values_list("recipient__username", "event__action"))


def changes_of(task, **overrides):
    values = dict(title=task.title, description=task.description, due_at=task.due_at, priority=task.priority,
                  requires_review=task.requires_review)
    values.update(overrides)
    return TaskChanges(**values)


class LinkRulesTests(TaskWorld, TestCase):
    """Право на связанный объект — то же, что у его карточки."""

    def setUp(self):
        self.make_world()

    def test_link_access_equals_card_access_for_every_role(self):
        for link in LINK_TYPES:
            obj = self.linked[link.code]
            for name in ALL_USERS:
                user = getattr(self, name)
                self.client.force_login(user)
                status = self.client.get(link.url(obj.pk)).status_code
                self.assertIn(status, (200, 403), (link.code, name))
                self.assertEqual(Viewer(user).link_allowed(link.code), status == 200, (link.code, name, status))

    def test_card_roles_are_taken_from_the_card_view(self):
        from apps.finance.views import PaymentUpdateView
        from apps.sales.views import InvoiceUpdateView

        by_code = {link.code: link for link in LINK_TYPES}
        self.assertEqual(set(by_code["invoice"].card_roles()), set(InvoiceUpdateView.allowed_roles))
        self.assertEqual(set(by_code["payment"].card_roles()), set(PaymentUpdateView.allowed_roles))
        # кладовщик видит отгрузку, но не счёт и не платёж
        keeper = Viewer(self.keeper)
        self.assertTrue(keeper.link_allowed("shipment"))
        self.assertFalse(keeper.link_allowed("invoice"))
        self.assertFalse(keeper.link_allowed("payment"))
        self.assertFalse(keeper.link_allowed("counterparty"))


class VisibilityTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.plain = self.new_task(self.manager, self.keeper, "Без связи")
        self.on_shipment = self.new_task(self.manager, self.keeper, "По отгрузке", link="shipment")
        self.on_invoice = self.new_task(self.manager, self.manager2, "По счёту", link="invoice")
        self.own = self.new_task(self.accountant, self.accountant, "Своя бухгалтера", link="payment")

    def visible(self, user):
        viewer = Viewer(user)
        by_query = set(Task.objects.filter(viewer.visible_q()).values_list("title", flat=True))
        by_object = {task.title for task in Task.objects.all() if viewer.can_view(task)}
        self.assertEqual(by_query, by_object, user.username)
        return by_query

    def test_matrix(self):
        everything = {"Без связи", "По отгрузке", "По счёту", "Своя бухгалтера"}
        self.assertEqual(self.visible(self.admin), everything)
        self.assertEqual(self.visible(self.boss), everything)
        self.assertEqual(self.visible(self.manager), {"Без связи", "По отгрузке", "По счёту"})
        self.assertEqual(self.visible(self.manager2), {"По счёту"})
        self.assertEqual(self.visible(self.keeper), {"Без связи", "По отгрузке"})
        self.assertEqual(self.visible(self.accountant), {"Своя бухгалтера"})
        self.assertEqual(self.visible(self.nobody), set())

    def test_lost_document_access_closes_task_for_participant(self):
        # Мария перестаёт быть менеджером и становится кладовщиком: счёт ей больше недоступен
        self.manager2.groups.set([self.groups[roles.ROLE_STOREKEEPER]])
        self.assertNotIn("По счёту", self.visible(self.manager2))
        with self.assertRaises(TaskNotFound):
            services.add_comment(self.manager2, self.on_invoice.pk, "есть доступ?")
        # роль вернули — задача снова видна
        self.manager2.groups.set([self.groups[roles.ROLE_MANAGER]])
        self.assertIn("По счёту", self.visible(self.manager2))

    def test_inactive_user_sees_nothing(self):
        self.keeper.is_active = False
        self.keeper.save()
        self.assertEqual(self.visible(self.keeper), set())


class CreateTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()

    def test_manager_assigns_to_employee(self):
        due = timezone.now() + timedelta(days=1)
        task = self.new_task(self.manager, self.keeper, "Собрать заказ", due_at=due, priority=Task.PRIORITY_HIGH,
                             requires_review=True, checklist=["Собрать", "  ", "Упаковать"])
        task.refresh_from_db()
        self.assertEqual((task.creator, task.assignee, task.status), (self.manager, self.keeper, Task.NEW))
        self.assertTrue(task.requires_review)
        self.assertEqual(list(task.checklist.values_list("text", "position")), [("Собрать", 1), ("Упаковать", 2)])
        self.assertEqual(actions(task), [TaskEvent.CREATED])
        self.assertEqual(notices(task), [("keeper1", TaskEvent.CREATED)])
        self.assertEqual(task.version, 1)

    def test_only_admin_and_manager_delegate(self):
        for actor in (self.keeper, self.accountant):
            with self.assertRaises(TaskForbidden):
                self.new_task(actor, self.manager)
        own = self.new_task(self.keeper, self.keeper, "Себе")
        self.assertEqual(own.creator, self.keeper)
        self.assertEqual(Task.objects.count(), 1)

    def test_self_task_has_no_review(self):
        task = self.new_task(self.manager, self.manager, requires_review=True)
        self.assertFalse(task.requires_review)

    def test_user_without_role_cannot_create_or_be_assignee(self):
        with self.assertRaises(TaskForbidden):
            self.new_task(self.nobody, self.nobody)
        with self.assertRaises(TaskError) as caught:
            self.new_task(self.manager, self.nobody)
        self.assertEqual(caught.exception.field, "assignee")
        self.assertNotIn(self.nobody, list(staff_users()))

    def test_inactive_assignee_rejected(self):
        self.keeper.is_active = False
        self.keeper.save()
        with self.assertRaises(TaskError):
            self.new_task(self.manager, self.keeper)
        self.assertFalse(Task.objects.exists())

    def test_link_requires_assignee_access(self):
        with self.assertRaises(TaskError) as caught:
            self.new_task(self.manager, self.keeper, link="invoice")
        self.assertIn("не может открыть счёт покупателю", caught.exception.message)
        self.assertIn("без связи", caught.exception.message)
        self.assertFalse(Task.objects.exists())
        self.assertFalse(TaskEvent.objects.exists())
        self.assertFalse(Notification.objects.exists())
        ok = self.new_task(self.manager, self.keeper, link="shipment")
        self.assertEqual((ok.link_type, ok.shipment), ("shipment", self.shipment))

    def test_link_requires_creator_access(self):
        with self.assertRaises(TaskForbidden):
            self.new_task(self.keeper, self.keeper, link="invoice")
        self.assertFalse(Task.objects.exists())

    def test_unknown_or_missing_link_rejected(self):
        for code, pk in (("product", self.product.pk), ("invoice", 999999), ("invoice", "1; drop"),
                         ("core.User", self.admin.pk)):
            with self.assertRaises(TaskError):
                services.create_task(self.manager, services.NewTask(title="x", assignee=self.manager,
                                                                    link_code=code, link_id=pk))
        self.assertFalse(Task.objects.exists())

    def test_title_required_and_html_kept_as_text(self):
        with self.assertRaises(TaskError):
            self.new_task(self.manager, self.keeper, "   ")
        task = self.new_task(self.manager, self.keeper, "<script>alert(1)</script>")
        self.assertEqual(task.title, "<script>alert(1)</script>")


class TransitionTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.keeper, "Пересчитать", requires_review=True,
                                  checklist=["Склад 1", "Склад 2"], link="shipment")
        self.items = list(self.task.checklist.order_by("position"))

    def go(self, actor, name, text=""):
        return services.transition(actor, self.task.pk, name, text)

    def reload(self):
        self.task.refresh_from_db()
        return self.task

    def test_full_cycle_with_return(self):
        self.go(self.keeper, "start")
        with self.assertRaises(TaskStateError):        # не все пункты отмечены
            self.go(self.keeper, "submit", "Готово")
        for item in self.items:
            services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        with self.assertRaises(TaskError):             # результат обязателен
            self.go(self.keeper, "submit", "  ")
        self.go(self.keeper, "submit", "Пересчитано")
        submitted = self.reload().submitted_at
        self.assertEqual(self.task.status, Task.REVIEW)
        with self.assertRaises(TaskError):             # причина возврата обязательна
            self.go(self.manager, "return", "")
        self.go(self.manager, "return", "Нет склада 3")
        self.assertEqual(self.reload().status, Task.IN_PROGRESS)
        self.assertIsNone(self.task.submitted_at)
        self.go(self.keeper, "submit", "Пересчитаны все склады")
        self.go(self.manager, "accept")
        task = self.reload()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.result, "Пересчитаны все склады")
        self.assertIsNotNone(task.submitted_at)
        self.assertGreater(task.completed_at, task.submitted_at)
        self.assertGreater(task.submitted_at, submitted)
        self.assertEqual(actions(task), [
            TaskEvent.CREATED, TaskEvent.STARTED, TaskEvent.CHECK_DONE, TaskEvent.CHECK_DONE, TaskEvent.SUBMITTED,
            TaskEvent.RETURNED, TaskEvent.SUBMITTED, TaskEvent.ACCEPTED,
        ])
        returned = TaskEvent.objects.get(task=task, action=TaskEvent.RETURNED)
        self.assertEqual((returned.text, returned.old_status, returned.new_status),
                         ("Нет склада 3", Task.REVIEW, Task.IN_PROGRESS))
        self.assertEqual(notices(task), sorted([
            ("keeper1", TaskEvent.CREATED), ("manager1", TaskEvent.SUBMITTED), ("keeper1", TaskEvent.RETURNED),
            ("manager1", TaskEvent.SUBMITTED), ("keeper1", TaskEvent.ACCEPTED),
        ]))

    def test_without_review_executor_completes(self):
        task = self.new_task(self.manager, self.keeper, "Простая")
        services.transition(self.keeper, task.pk, "start")
        with self.assertRaises(TaskStateError):
            services.transition(self.keeper, task.pk, "submit", "x")
        services.transition(self.keeper, task.pk, "complete", "Сделал")
        task.refresh_from_db()
        self.assertEqual((task.status, task.result), (Task.DONE, "Сделал"))
        self.assertEqual(notices(task), [("keeper1", TaskEvent.CREATED), ("manager1", TaskEvent.COMPLETED)])

    def test_executor_cannot_switch_review_or_accept(self):
        self.go(self.keeper, "start")
        with self.assertRaises(TaskForbidden):         # исполнитель не постановщик — менять нельзя
            services.update_task(self.keeper, self.task.pk, self.reload().version,
                                 changes_of(self.task, requires_review=False))
        self.assertTrue(self.reload().requires_review)
        for item in self.items:
            services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        self.go(self.keeper, "submit", "Готово")
        with self.assertRaises(TaskForbidden):
            self.go(self.keeper, "accept")
        self.assertEqual(self.reload().status, Task.REVIEW)

    def test_who_accepts(self):
        self.go(self.keeper, "start")
        for item in self.items:
            services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        self.go(self.keeper, "submit", "Готово")
        with self.assertRaises(TaskNotFound):          # другой менеджер задачу не видит
            self.go(self.manager2, "accept")
        self.go(self.admin, "accept")                  # администратор принимает любую
        self.assertEqual(self.reload().status, Task.DONE)

    def test_repeated_accept_changes_nothing(self):
        self.go(self.keeper, "start")
        for item in self.items:
            services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        self.go(self.keeper, "submit", "Готово")
        _, first = self.go(self.manager, "accept")
        version = self.reload().version
        _, second = self.go(self.manager, "accept")
        self.assertEqual((first, second), (True, False))
        self.assertEqual(actions(self.task).count(TaskEvent.ACCEPTED), 1)
        self.assertEqual(Notification.objects.filter(event__action=TaskEvent.ACCEPTED).count(), 1)
        self.assertEqual(self.reload().version, version)

    def test_waiting_keeps_due_and_overdue(self):
        past = timezone.now() - timedelta(hours=1)
        task = self.new_task(self.manager, self.keeper, "Ждёт", due_at=past)
        with self.assertRaises(TaskError):
            services.transition(self.keeper, task.pk, "wait", "")
        services.transition(self.keeper, task.pk, "wait", "Нет поставки")
        task.refresh_from_db()
        self.assertEqual((task.status, task.status_reason, task.due_at), (Task.WAITING, "Нет поставки", past))
        self.assertTrue(task.is_overdue)
        services.transition(self.keeper, task.pk, "unwait")
        task.refresh_from_db()
        self.assertEqual((task.status, task.status_reason), (Task.IN_PROGRESS, ""))

    def test_overdue_on_review_still_overdue(self):
        task = self.new_task(self.manager, self.keeper, "Поздно", due_at=timezone.now() - timedelta(minutes=5),
                             requires_review=True)
        services.transition(self.keeper, task.pk, "start")
        services.transition(self.keeper, task.pk, "submit", "Сдал")
        task.refresh_from_db()
        self.assertTrue(task.is_overdue)
        services.transition(self.manager, task.pk, "accept")
        task.refresh_from_db()
        self.assertFalse(task.is_overdue)

    def test_cancel_and_reopen(self):
        with self.assertRaises(TaskForbidden):
            self.go(self.keeper, "cancel", "сам отменю")
        with self.assertRaises(TaskError):
            self.go(self.manager, "cancel", "")
        self.go(self.manager, "cancel", "Не нужно")
        task = self.reload()
        self.assertEqual((task.status, task.status_reason), (Task.CANCELLED, "Не нужно"))
        self.assertIsNotNone(task.cancelled_at)
        self.assertNotIn(task, Task.objects.filter(status__in=Task.ACTIVE))
        with self.assertRaises(TaskStateError):        # отменённую не меняем до возобновления
            services.update_task(self.manager, task.pk, task.version, changes_of(task, title="Новое"))
        with self.assertRaises(TaskStateError):
            services.set_item_done(self.keeper, task.pk, self.items[0].pk, True)
        services.add_comment(self.keeper, task.pk, "Понял")      # комментировать можно
        with self.assertRaises(TaskError):
            self.go(self.manager, "reopen", "")
        self.go(self.manager, "reopen", "Всё-таки нужно")
        task = self.reload()
        self.assertEqual((task.status, task.cancelled_at, task.status_reason), (Task.IN_PROGRESS, None, ""))

    def test_reopen_keeps_old_result_in_history(self):
        task = self.new_task(self.manager, self.keeper, "Отчёт")
        services.transition(self.keeper, task.pk, "start")
        services.transition(self.keeper, task.pk, "complete", "Первый вариант")
        services.transition(self.manager, task.pk, "reopen", "Переделать")
        task.refresh_from_db()
        self.assertEqual((task.status, task.result, task.completed_at), (Task.IN_PROGRESS, "", None))
        reopened = TaskEvent.objects.get(task=task, action=TaskEvent.REOPENED)
        self.assertEqual((reopened.old_value, reopened.old_status), ("Первый вариант", Task.DONE))
        self.assertTrue(TaskEvent.objects.filter(task=task, action=TaskEvent.COMPLETED,
                                                 text="Первый вариант").exists())

    def test_executor_actions_only_for_executor(self):
        for name in ("start", "wait", "submit", "complete"):
            with self.assertRaises(TaskForbidden):
                self.go(self.manager, name, "текст")
            with self.assertRaises(TaskForbidden):
                self.go(self.admin, name, "текст")
        with self.assertRaises(TaskForbidden):
            services.set_item_done(self.manager, self.task.pk, self.items[0].pk, True)
        self.assertEqual(self.reload().status, Task.NEW)

    def test_wrong_state(self):
        with self.assertRaises(TaskStateError):
            self.go(self.keeper, "submit", "сразу")    # из «Новой» сдать нельзя — сначала в работу
        with self.assertRaises(TaskStateError):
            self.go(self.manager, "accept")
        with self.assertRaises(TaskStateError):
            self.go(self.manager, "reopen", "зачем")
        self.assertEqual(actions(self.task), [TaskEvent.CREATED])

    def test_checklist_toggle_is_idempotent_and_logged(self):
        _, first = services.set_item_done(self.keeper, self.task.pk, self.items[0].pk, True)
        _, again = services.set_item_done(self.keeper, self.task.pk, self.items[0].pk, True)
        self.assertEqual((first, again), (True, False))
        services.set_item_done(self.keeper, self.task.pk, self.items[0].pk, False)
        item = ChecklistItem.objects.get(pk=self.items[0].pk)
        self.assertEqual((item.is_done, item.done_by, item.done_at), (False, None, None))
        self.assertEqual(actions(self.task)[1:], [TaskEvent.CHECK_DONE, TaskEvent.CHECK_UNDONE])
        with self.assertRaises(TaskStateError):
            services.set_item_done(self.keeper, self.task.pk, 999999, True)

    def test_submit_without_items_allowed(self):
        task = self.new_task(self.manager, self.keeper, "Без чек-листа", requires_review=True)
        services.transition(self.keeper, task.pk, "start")
        services.transition(self.keeper, task.pk, "submit", "Сделано")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.REVIEW)


class EditTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.keeper, "Заголовок", checklist=["Первый", "Второй"])

    def reload(self):
        self.task.refresh_from_db()
        return self.task

    def test_changes_are_logged_with_old_and_new(self):
        due = timezone.now().replace(second=0, microsecond=0) + timedelta(days=2)
        first, second = self.task.checklist.order_by("position")
        task, changed = services.update_task(self.manager, self.task.pk, 1, changes_of(
            self.task, title="Новый заголовок", due_at=due, priority=Task.PRIORITY_HIGH, requires_review=True,
            renamed={first.pk: "Первый пункт"}, removed={second.pk}, added=["Третий"]))
        self.assertTrue(changed)
        self.assertEqual(task.version, 2)
        events = {e.action: e for e in TaskEvent.objects.filter(task=task)}
        self.assertEqual((events[TaskEvent.TITLE].old_value, events[TaskEvent.TITLE].new_value),
                         ("Заголовок", "Новый заголовок"))
        self.assertEqual(events[TaskEvent.DUE].old_value, "без срока")
        self.assertEqual(events[TaskEvent.DUE].new_value, timezone.localtime(due).strftime("%d.%m.%Y %H:%M"))
        self.assertEqual(events[TaskEvent.REVIEW].new_value, "требуется")
        self.assertEqual(events[TaskEvent.CHECK_RENAMED].old_value, "Первый")
        self.assertEqual(events[TaskEvent.CHECK_REMOVED].text, "Второй")
        self.assertEqual(events[TaskEvent.CHECK_ADDED].text, "Третий")
        self.assertEqual(list(task.checklist.filter(is_removed=False).values_list("text", flat=True)),
                         ["Первый пункт", "Третий"])
        self.assertTrue(ChecklistItem.objects.filter(pk=second.pk, is_removed=True).exists())   # история цела
        # срок — единственное изменение, о котором уведомляют
        self.assertEqual(notices(task), [("keeper1", TaskEvent.CREATED), ("keeper1", TaskEvent.DUE)])

    def test_stale_version_conflicts(self):
        services.update_task(self.manager, self.task.pk, 1, changes_of(self.task, title="Версия 2"))
        with self.assertRaises(TaskConflict):
            services.update_task(self.admin, self.task.pk, 1, changes_of(self.task, title="Чужая правка",
                                                                         description="затёрла бы"))
        task = self.reload()
        self.assertEqual((task.title, task.description, task.version), ("Версия 2", "", 2))

    def test_repeated_same_save_is_not_a_conflict(self):
        changes = changes_of(self.task, title="Один раз")
        services.update_task(self.manager, self.task.pk, 1, changes)
        _, changed = services.update_task(self.manager, self.task.pk, 1, changes)
        self.assertFalse(changed)
        self.assertEqual(actions(self.task).count(TaskEvent.TITLE), 1)

    def test_executor_tick_makes_stale_form_conflict(self):
        item = self.task.checklist.first()
        services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        with self.assertRaises(TaskConflict):
            services.update_task(self.manager, self.task.pk, 1, changes_of(self.task, title="По старой форме"))

    def test_checklist_frozen_after_submit(self):
        self.task.requires_review = True
        self.task.save()
        services.transition(self.keeper, self.task.pk, "start")
        for item in self.task.checklist.all():
            services.set_item_done(self.keeper, self.task.pk, item.pk, True)
        services.transition(self.keeper, self.task.pk, "submit", "Готово")
        task = self.reload()
        with self.assertRaises(TaskStateError):
            services.update_task(self.manager, task.pk, task.version, changes_of(task, added=["Ещё"]))
        with self.assertRaises(TaskStateError):
            services.update_task(self.manager, task.pk, task.version, changes_of(task, requires_review=False))
        # срок на проверке поменять можно
        services.update_task(self.manager, task.pk, task.version,
                             changes_of(task, due_at=timezone.now() + timedelta(days=1)))

    def test_self_task_cannot_enable_review(self):
        own = self.new_task(self.keeper, self.keeper, "Своя")
        with self.assertRaises(TaskError):
            services.update_task(self.keeper, own.pk, 1, changes_of(own, requires_review=True))

    def test_other_manager_cannot_edit(self):
        with self.assertRaises(TaskNotFound):
            services.update_task(self.manager2, self.task.pk, 1, changes_of(self.task, title="Чужое"))
        self.assertEqual(self.reload().title, "Заголовок")


class ReassignTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.keeper, "Передать", requires_review=True, link="shipment")
        services.transition(self.keeper, self.task.pk, "start")

    def reload(self):
        self.task.refresh_from_db()
        return self.task

    def test_reassign_resets_to_new_and_moves_access(self):
        other_keeper = self.manager2
        with self.assertRaises(TaskError):              # причина обязательна
            services.reassign(self.manager, self.task.pk, self.reload().version, other_keeper, "")
        services.reassign(self.manager, self.task.pk, self.reload().version, other_keeper, "Отпуск")
        task = self.reload()
        self.assertEqual((task.assignee, task.status), (other_keeper, Task.NEW))
        event = TaskEvent.objects.get(task=task, action=TaskEvent.REASSIGNED)
        self.assertEqual((event.old_user, event.new_user, event.text, event.old_status),
                         (self.keeper, other_keeper, "Отпуск", Task.IN_PROGRESS))
        self.assertIn(("keeper1", TaskEvent.REASSIGNED), notices(task))
        self.assertIn(("manager2", TaskEvent.REASSIGNED), notices(task))
        # прежний исполнитель больше не видит задачу; новый видит
        self.assertFalse(Viewer(self.keeper).can_view(task))
        self.assertTrue(Viewer(other_keeper).can_view(task))
        with self.assertRaises(TaskNotFound):
            services.add_comment(self.keeper, task.pk, "А я?")
        # у прежнего остаётся уведомление «передана другому» — без заголовка задачи
        removed = services.visible_notifications(Viewer(self.keeper))
        self.assertEqual(list(removed.values_list("event__action", flat=True)), [TaskEvent.REASSIGNED])
        self.assertEqual(services.unread_count(Viewer(self.keeper)), 1)

    def test_reassign_blocked_on_review_and_when_final(self):
        services.transition(self.keeper, self.task.pk, "submit", "Сдал")
        with self.assertRaises(TaskStateError):
            services.reassign(self.manager, self.task.pk, self.reload().version, self.manager2, "Нужно")
        services.transition(self.manager, self.task.pk, "accept")
        with self.assertRaises(TaskStateError):
            services.reassign(self.manager, self.task.pk, self.reload().version, self.manager2, "Нужно")
        self.assertEqual(self.reload().assignee, self.keeper)

    def test_reassign_to_user_without_document_access(self):
        task = self.new_task(self.manager, self.manager2, "По счёту", link="invoice")
        with self.assertRaises(TaskError) as caught:
            services.reassign(self.manager, task.pk, task.version, self.keeper, "Пусть кладовщик")
        self.assertIn("не может открыть", caught.exception.message)
        task.refresh_from_db()
        self.assertEqual(task.assignee, self.manager2)
        self.assertFalse(TaskEvent.objects.filter(task=task, action=TaskEvent.REASSIGNED).exists())

    def test_reassign_to_creator_turns_review_off(self):
        services.reassign(self.manager, self.task.pk, self.reload().version, self.manager, "Сам сделаю")
        task = self.reload()
        self.assertFalse(task.requires_review)
        self.assertTrue(TaskEvent.objects.filter(task=task, action=TaskEvent.REVIEW).exists())

    def test_stale_version_and_same_assignee(self):
        version = self.reload().version
        services.update_task(self.manager, self.task.pk, version, changes_of(self.task, title="Поменяли"))
        with self.assertRaises(TaskConflict):
            services.reassign(self.manager, self.task.pk, version, self.manager2, "Отпуск")
        _, changed = services.reassign(self.manager, self.task.pk, version, self.keeper, "Тот же")
        self.assertFalse(changed)
        self.assertEqual(self.reload().assignee, self.keeper)

    def test_non_delegator_creator_cannot_reassign_to_other(self):
        own = self.new_task(self.keeper, self.keeper, "Своя")
        with self.assertRaises(TaskForbidden):
            services.reassign(self.keeper, own.pk, own.version, self.manager, "Передам")
        own.refresh_from_db()
        self.assertEqual(own.assignee, self.keeper)


class DeactivationTests(TaskWorld, TestCase):
    def test_inactive_assignee_keeps_history_and_admin_reassigns(self):
        self.make_world()
        task = self.new_task(self.manager, self.keeper, "Останется", link="shipment")
        services.add_comment(self.keeper, task.pk, "Начинаю")
        self.keeper.is_active = False
        self.keeper.save()
        task.refresh_from_db()
        self.assertEqual(task.assignee, self.keeper)        # без автоматического переназначения
        self.assertEqual(TaskComment.objects.filter(task=task).count(), 1)
        self.client.force_login(self.admin)
        page = self.client.get(reverse("task_all"), {"problem": "inactive"})
        self.assertContains(page, "Останется")
        self.assertContains(page, "исполнитель отключён")
        self.manager2.is_active = False
        self.manager2.save()
        with self.assertRaises(TaskError):                  # отключённому назначить нельзя
            services.reassign(self.admin, task.pk, task.version, self.manager2, "Ему")
        self.manager2.is_active = True
        self.manager2.save()
        services.reassign(self.admin, task.pk, task.version, self.manager2, "Сотрудник уволился")
        task.refresh_from_db()
        self.assertEqual(task.assignee, self.manager2)
        self.assertTrue(Viewer(self.manager2).can_view(task))
        # отключённому уведомлений не пишем
        self.assertFalse(Notification.objects.filter(recipient=self.keeper,
                                                     event__action=TaskEvent.REASSIGNED).exists())

    def test_users_with_tasks_cannot_be_deleted(self):
        self.make_people()
        task = self.new_task(self.manager, self.keeper, "Защита")
        for user in (self.manager, self.keeper):
            with self.assertRaises(ProtectedError):
                user.delete()
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())


class NotificationTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()

    def test_comment_notifies_other_participants_once(self):
        task = self.new_task(self.manager, self.keeper, "Обсудить")
        services.add_comment(self.keeper, task.pk, "Вопрос")
        services.add_comment(self.manager, task.pk, "Ответ")
        self.assertEqual(notices(task), sorted([
            ("keeper1", TaskEvent.CREATED), ("manager1", TaskEvent.COMMENTED), ("keeper1", TaskEvent.COMMENTED),
        ]))
        own = self.new_task(self.manager, self.manager, "Своя")
        services.add_comment(self.manager, own.pk, "Заметка")
        self.assertEqual(notices(own), [])             # себе не пишем

    def test_admin_actions_notify_creator(self):
        task = self.new_task(self.manager, self.keeper, "Отменит админ")
        services.transition(self.admin, task.pk, "cancel", "Неактуально")
        self.assertIn(("manager1", TaskEvent.CANCELLED), notices(task))
        self.assertIn(("keeper1", TaskEvent.CANCELLED), notices(task))

    def test_one_notification_per_event_and_recipient(self):
        task = self.new_task(self.manager, self.keeper, "Один раз")
        event = TaskEvent.objects.get(task=task)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(recipient=self.keeper, task=task, event=event)
        services._notify(event, [self.keeper, self.keeper])
        self.assertEqual(Notification.objects.filter(event=event).count(), 1)

    def test_unread_count_follows_current_access(self):
        task = self.new_task(self.manager, self.manager2, "По счёту", link="invoice")
        viewer = Viewer(self.manager2)
        self.assertEqual(services.unread_count(viewer), 1)
        self.manager2.groups.set([self.groups[roles.ROLE_STOREKEEPER]])
        self.assertEqual(services.unread_count(Viewer(self.manager2)), 0)
        self.assertEqual(list(services.visible_notifications(Viewer(self.manager2))), [])
        self.manager2.groups.set([self.groups[roles.ROLE_MANAGER]])
        self.assertEqual(services.mark_read(Viewer(self.manager2)), 1)
        self.assertEqual(services.unread_count(Viewer(self.manager2)), 0)
        self.assertTrue(Notification.objects.filter(task=task, read_at__isnull=False).exists())

    def test_mark_read_touches_only_own(self):
        self.new_task(self.manager, self.keeper, "Кладовщику")
        self.new_task(self.keeper, self.keeper, "Себе")
        services.add_comment(self.keeper, Task.objects.get(title="Кладовщику").pk, "Ок")
        self.assertEqual(services.mark_read(Viewer(self.manager)), 1)
        self.assertEqual(services.unread_count(Viewer(self.keeper)), 1)


class IntegrityTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.manager2, "Целостность", link="invoice")

    def test_task_is_not_deleted_and_creator_link_fixed(self):
        with self.assertRaises(TaskIntegrityError):
            self.task.delete()
        with self.assertRaises(TaskIntegrityError):
            Task.objects.filter(pk=self.task.pk).delete()
        with self.assertRaises(TaskIntegrityError):
            Task.objects.filter(pk=self.task.pk).update(creator=self.admin)
        with self.assertRaises(TaskIntegrityError):
            Task.objects.filter(pk=self.task.pk).update(invoice=None, link_type="")
        task = Task.objects.get(pk=self.task.pk)
        task.link_type, task.invoice, task.shipment = "shipment", None, self.shipment
        with self.assertRaises(TaskIntegrityError):
            task.save()
        task = Task.objects.get(pk=self.task.pk)
        task.creator = self.admin
        with self.assertRaises(TaskIntegrityError):
            task.save()
        self.task.refresh_from_db()
        self.assertEqual((self.task.creator, self.task.invoice), (self.manager, self.invoice))

    def test_history_is_append_only(self):
        event = TaskEvent.objects.get(task=self.task)
        event.text = "подделка"
        with self.assertRaises(TaskIntegrityError):
            event.save()
        with self.assertRaises(TaskIntegrityError):
            event.delete()
        with self.assertRaises(TaskIntegrityError):
            TaskEvent.objects.filter(task=self.task).update(text="x")
        with self.assertRaises(TaskIntegrityError):
            TaskEvent.objects.filter(task=self.task).delete()
        comment = services.add_comment(self.manager, self.task.pk, "Текст")
        with self.assertRaises(TaskIntegrityError):
            TaskComment.objects.filter(pk=comment.pk).update(text="правка")

    def test_database_allows_one_link_only(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.create(title="Две связи", creator=self.manager, assignee=self.manager,
                                link_type="invoice", invoice=self.invoice, shipment=self.shipment)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Task.objects.create(title="Тип без объекта", creator=self.manager, assignee=self.manager,
                                link_type="invoice")

    def test_task_models_not_in_admin(self):
        from django.contrib import admin

        for model in (Task, TaskEvent, TaskComment, Notification, ChecklistItem):
            self.assertFalse(admin.site.is_registered(model), model)


class AccountingUntouchedTests(TaskWorld, TestCase):
    """Задача и её завершение не меняют склад, деньги, долги и проведение."""

    def test_full_lifecycle_on_every_link_changes_no_accounting_row(self):
        self.make_world()
        before = accounting_state()
        for code in self.linked:
            assignee = self.admin
            task = self.new_task(self.manager, assignee, f"Задача: {code}", link=code, requires_review=True,
                                 checklist=["Пункт"])
            services.transition(assignee, task.pk, "start")
            services.set_item_done(assignee, task.pk, task.checklist.get().pk, True)
            services.add_comment(assignee, task.pk, "Комментарий")
            services.transition(assignee, task.pk, "submit", "Готово")
            services.transition(self.manager, task.pk, "return", "Ещё раз")
            services.transition(assignee, task.pk, "submit", "Готово 2")
            services.transition(self.manager, task.pk, "accept")
            services.transition(self.manager, task.pk, "reopen", "Повтор")
            services.transition(self.manager, task.pk, "cancel", "Хватит")
        self.assertEqual(accounting_state(), before)
