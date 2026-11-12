"""Удаление объекта, с которым связана задача.

Связь задачи — внешний ключ с запретом удаления (on_delete=PROTECT). Документ
или контрагент с задачей удалить нельзя никаким путём: из карточки, из списка
отмеченных, из административной панели и вызовом delete(). Отказ понятный,
без частичных изменений: проведённый документ остаётся проведённым, остатки,
деньги и задачи — прежними.
"""
from django.contrib.messages import get_messages
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .models import Task, TaskEvent
from .testing import TaskWorld, accounting_state

SINGLE = {
    "invoice": "invoice_delete",
    "shipment": "shipment_delete",
    "customer_return": "customer_return_delete",
    "receipt": "receipt_delete",
    "supplier_return": "supplier_return_delete",
    "payment": "payment_delete",
}
BULK = {
    "invoice": "invoice_bulk_delete",
    "shipment": "shipment_bulk_delete",
    "customer_return": "customer_return_bulk_delete",
    "receipt": "receipt_bulk_delete",
    "supplier_return": "supplier_return_bulk_delete",
    "payment": "payment_bulk_delete",
}
ADMIN_DELETE = {
    "invoice": "admin:sales_invoice_delete",
    "payment": "admin:finance_payment_delete",
    "counterparty": "admin:partners_counterparty_delete",
    "customer_return": "admin:sales_customerreturn_delete",     # черновик: админка удаляет только черновики
    "supplier_return": "admin:purchases_supplierreturn_delete",
}
ADMIN_LIST = {
    "invoice": "admin:sales_invoice_changelist",
    "payment": "admin:finance_payment_changelist",
    "counterparty": "admin:partners_counterparty_changelist",
}


class DeleteLinkedObjectTests(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.tasks = {code: self.new_task(self.manager, self.admin, f"Задача: {code}", link=code)
                      for code in self.linked}
        self.state = accounting_state()
        self.events = TaskEvent.objects.count()

    def assertNothingChanged(self):
        self.assertEqual(accounting_state(), self.state)
        self.assertEqual(Task.objects.count(), len(self.tasks))
        self.assertEqual(TaskEvent.objects.count(), self.events)
        for code, task in self.tasks.items():
            task.refresh_from_db()
            self.assertEqual(getattr(task, f"{code}_id"), self.linked[code].pk)

    def message_texts(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_single_delete_refused_with_message(self):
        self.client.force_login(self.admin)
        for code, url_name in SINGLE.items():
            obj = self.linked[code]
            response = self.client.post(reverse(url_name, args=[obj.pk]))
            self.assertEqual(response.status_code, 302, code)
            texts = " ".join(self.message_texts(response))
            self.assertIn("связанные задачи", texts, code)
            self.assertNotIn("Задача: ", texts, code)        # заголовки задач в отказе не раскрываются
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), code)
        self.assertNothingChanged()
        self.shipment.refresh_from_db()
        self.receipt.refresh_from_db()
        self.assertTrue(self.shipment.is_posted and self.receipt.is_posted)

    def test_bulk_delete_refused_with_message(self):
        self.client.force_login(self.admin)
        for code, url_name in BULK.items():
            obj = self.linked[code]
            response = self.client.post(reverse(url_name), {"ids": [obj.pk]})
            self.assertEqual(response.status_code, 302, code)
            self.assertIn("связанные задачи", " ".join(self.message_texts(response)), code)
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), code)
        self.assertNothingChanged()

    def test_bulk_invoice_delete_is_all_or_nothing(self):
        from apps.sales.models import Invoice

        free = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("invoice_bulk_delete"), {"ids": [free.pk, self.invoice.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertIn("связанные задачи", " ".join(self.message_texts(response)))
        self.assertTrue(Invoice.objects.filter(pk=free.pk).exists())
        self.assertTrue(Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_admin_delete_page_refuses(self):
        self.client.force_login(self.boss)
        for code, url_name in ADMIN_DELETE.items():
            obj = self.linked[code]
            url = reverse(url_name, args=[obj.pk])
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, code)
            # в переводе Django встречаются оба написания: «удается» и «удаётся»
            self.assertRegex(page.content.decode(), "Не уда[её]тся удалить", code)
            response = self.client.post(url, {"post": "yes"})
            self.assertIn(response.status_code, (200, 403), code)
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), code)
        self.assertNothingChanged()

    def test_admin_names_task_as_protection(self):
        from apps.partners.models import Counterparty

        lonely = Counterparty.objects.create(name="Только с задачей")
        self.new_task(self.manager, self.admin, "Позвонить", link=("counterparty", lonely))
        self.client.force_login(self.boss)
        url = reverse("admin:partners_counterparty_delete", args=[lonely.pk])
        page = self.client.get(url)
        self.assertContains(page, "защищённых")
        self.assertContains(page, "Задача №")
        self.client.post(url, {"post": "yes"})
        self.assertTrue(Counterparty.objects.filter(pk=lonely.pk).exists())

    def test_admin_bulk_action_refuses(self):
        self.client.force_login(self.boss)
        for code, url_name in ADMIN_LIST.items():
            obj = self.linked[code]
            response = self.client.post(reverse(url_name), {
                "action": "delete_selected", "_selected_action": [obj.pk], "post": "yes"})
            self.assertIn(response.status_code, (200, 302), code)
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists(), code)
        self.assertNothingChanged()

    def test_model_delete_raises_and_rolls_back(self):
        for code, obj in self.linked.items():
            with self.assertRaises(ProtectedError, msg=code):
                type(obj).objects.get(pk=obj.pk).delete()
        self.assertNothingChanged()

    def test_user_with_tasks_protected_in_admin(self):
        self.client.force_login(self.boss)
        url = reverse("admin:core_user_delete", args=[self.manager.pk])
        self.assertContains(self.client.get(url), "защищённых")
        self.client.post(url, {"post": "yes"})
        self.assertTrue(type(self.manager).objects.filter(pk=self.manager.pk).exists())

    def test_cancelled_task_still_protects(self):
        from . import services

        task = self.tasks["invoice"]
        services.transition(self.manager, task.pk, "cancel", "Не нужна")
        self.client.force_login(self.admin)
        self.client.post(reverse("invoice_delete", args=[self.invoice.pk]))
        self.assertTrue(type(self.invoice).objects.filter(pk=self.invoice.pk).exists())

    def test_object_without_task_still_deletes(self):
        from apps.sales.models import Invoice

        free = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        self.client.force_login(self.admin)
        self.client.post(reverse("invoice_delete", args=[free.pk]))
        self.assertFalse(Invoice.objects.filter(pk=free.pk).exists())
