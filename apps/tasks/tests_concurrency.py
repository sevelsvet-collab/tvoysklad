"""Одновременные операции с задачами — только PostgreSQL.

run_overlapping доказывает пересечение: первая операция останавливается,
уже держа блокировку строки задачи (или документа), вторая встаёт в ожидание
(видно по pg_stat_activity), затем первая продолжается. Проверяется итог:
одно событие, одно уведомление, не затёртые изменения, целые связи.
"""
from unittest import skipUnless

from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from apps.core.concurrency_testing import ConcurrencyMixin
from apps.sales.models import Invoice, Shipment

from . import services
from .models import Notification, Task, TaskComment, TaskEvent
from .services import TaskChanges, TaskConflict, TaskError, TaskNotFound, TaskStateError
from .testing import TaskWorld

POSTGRES = connection.vendor == "postgresql"


def changes(task, **values):
    data = dict(title=task.title, description=task.description, due_at=task.due_at, priority=task.priority,
                requires_review=task.requires_review)
    data.update(values)
    return TaskChanges(**data)


@skipUnless(POSTGRES, "блокировки строк проверяются только на PostgreSQL")
class ConcurrentTaskTests(TaskWorld, ConcurrencyMixin, TransactionTestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.keeper, "Общая", requires_review=True, link="shipment")
        services.transition(self.keeper, self.task.pk, "start")

    def submitted(self):
        services.transition(self.keeper, self.task.pk, "submit", "Готово")

    def test_double_accept_gives_one_event_and_notice(self):
        self.submitted()
        results = self.run_overlapping(
            lambda: services.transition(self.manager, self.task.pk, "accept"),
            lambda: services.transition(self.admin, self.task.pk, "accept"),
            pause_on=(services, "_touch"),
        )
        self.assertEqual(results, [None, None])            # второе — «уже выполнено», без ошибки
        self.assertEqual(TaskEvent.objects.filter(task=self.task, action=TaskEvent.ACCEPTED).count(), 1)
        self.assertEqual(Notification.objects.filter(event__action=TaskEvent.ACCEPTED).count(), 1)
        self.assertEqual(Task.objects.get(pk=self.task.pk).status, Task.DONE)

    def test_accept_and_return_at_once(self):
        self.submitted()
        results = self.run_overlapping(
            lambda: services.transition(self.manager, self.task.pk, "accept"),
            lambda: services.transition(self.admin, self.task.pk, "return", "Переделать"),
            pause_on=(services, "_touch"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskStateError)
        self.assertEqual(Task.objects.get(pk=self.task.pk).status, Task.DONE)
        self.assertFalse(TaskEvent.objects.filter(task=self.task, action=TaskEvent.RETURNED).exists())

    def test_two_stale_edits_second_conflicts(self):
        task = Task.objects.get(pk=self.task.pk)
        start = task.version
        results = self.run_overlapping(
            lambda: services.update_task(self.manager, task.pk, task.version, changes(task, title="Первая")),
            lambda: services.update_task(self.admin, task.pk, task.version, changes(task, title="Вторая")),
            pause_on=(services, "_touch"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskConflict)
        task.refresh_from_db()
        self.assertEqual((task.title, task.version), ("Первая", start + 1))
        self.assertEqual(TaskEvent.objects.filter(task=task, action=TaskEvent.TITLE).count(), 1)

    def test_reassign_first_then_old_assignee_submit(self):
        task = Task.objects.get(pk=self.task.pk)
        results = self.run_overlapping(
            lambda: services.reassign(self.manager, task.pk, task.version, self.manager2, "Отпуск"),
            lambda: services.transition(self.keeper, task.pk, "submit", "Успел?"),
            pause_on=(services, "_touch"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskNotFound)     # прежний исполнитель уже не видит задачу
        task.refresh_from_db()
        self.assertEqual((task.assignee, task.status, task.result), (self.manager2, Task.NEW, ""))

    def test_submit_first_then_reassign(self):
        task = Task.objects.get(pk=self.task.pk)
        results = self.run_overlapping(
            lambda: services.transition(self.keeper, task.pk, "submit", "Сдал"),
            lambda: services.reassign(self.manager, task.pk, task.version + 1, self.manager2, "Отпуск"),
            pause_on=(services, "_touch"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskStateError)   # на проверке исполнителя не меняют
        task.refresh_from_db()
        self.assertEqual((task.assignee, task.status), (self.keeper, Task.REVIEW))

    def test_comment_waits_for_reassignment(self):
        task = Task.objects.get(pk=self.task.pk)
        results = self.run_overlapping(
            lambda: services.reassign(self.manager, task.pk, task.version, self.manager2, "Отпуск"),
            lambda: services.add_comment(self.keeper, task.pk, "Последний комментарий"),
            pause_on=(services, "_touch"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskNotFound)
        self.assertFalse(TaskComment.objects.filter(text="Последний комментарий").exists())

    def test_same_submit_token_creates_one_task(self):
        clients = []
        for _ in range(2):
            client = Client()
            client.force_login(self.manager)
            clients.append(client)
        data = {"title": "Одна задача", "assignee": self.keeper.pk, "priority": "normal", "submit_token": "same"}
        responses = {}

        def post(index):
            responses[index] = clients[index].post(reverse("task_create"), data)

        results = self.run_overlapping(lambda: post(0), lambda: post(1), pause_on=(services, "_notify"))
        self.assertEqual(results, [None, None])
        task = Task.objects.get(title="Одна задача")
        self.assertEqual(responses[0]["Location"], reverse("task_detail", args=[task.pk]))
        self.assertEqual(responses[1]["Location"], responses[0]["Location"])

    def test_task_first_then_shipment_delete_refused(self):
        other = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        other.lines.create(product=self.product, quantity=1, price=50, vat_rate="none")
        other.post()

        def delete():
            Shipment.objects.get(pk=other.pk).delete()

        results = self.run_overlapping(
            lambda: self.new_task(self.manager, self.keeper, "К отгрузке", link=("shipment", other)),
            delete, pause_on=(services, "_check_assignee"),
        )
        self.assertIsNone(results[0])
        from django.db.models import ProtectedError

        self.assertIsInstance(results[1], ProtectedError)
        other.refresh_from_db()
        self.assertTrue(other.is_posted)
        self.assertEqual(Task.objects.get(title="К отгрузке").shipment_id, other.pk)

    def test_shipment_delete_first_then_task_refused(self):
        other = Shipment.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        other.lines.create(product=self.product, quantity=1, price=50, vat_rate="none")
        other.post()

        def delete():
            Shipment.objects.get(pk=other.pk).delete()

        results = self.run_overlapping(
            delete,
            lambda: services.create_task(self.manager, services.NewTask(
                title="Опоздала", assignee=self.keeper, link_code="shipment", link_id=other.pk)),
            pause_on=(Shipment, "unpost"),
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], TaskError)
        self.assertFalse(Shipment.objects.filter(pk=other.pk).exists())
        self.assertFalse(Task.objects.filter(title="Опоздала").exists())

    def test_task_first_then_invoice_delete_refused_by_database(self):
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        client = Client()
        client.force_login(self.admin)
        responses = {}

        def delete():
            responses["delete"] = client.post(reverse("invoice_delete", args=[invoice.pk]))

        results = self.run_overlapping(
            lambda: services.create_task(self.manager, services.NewTask(
                title="К счёту", assignee=self.manager2, link_code="invoice", link_id=invoice.pk)),
            delete, pause_on=(services, "_check_assignee"),
        )
        self.assertEqual(results, [None, None])
        self.assertEqual(responses["delete"].status_code, 302)
        self.assertTrue(Invoice.objects.filter(pk=invoice.pk).exists())
        self.assertEqual(Task.objects.get(title="К счёту").invoice_id, invoice.pk)
