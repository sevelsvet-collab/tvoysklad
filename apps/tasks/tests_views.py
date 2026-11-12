"""Экраны задач и прямые адреса: права на сервере, подмена полей, повторы,
счётчики, вкладка «Задачи» документа, экранирование текста."""
from datetime import timedelta

from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.core import roles

from . import services
from .models import Notification, Task, TaskComment, TaskEvent
from .testing import TaskWorld, accounting_state


def state():
    return {
        "tasks": list(Task.objects.order_by("pk").values()),
        "events": TaskEvent.objects.count(),
        "comments": TaskComment.objects.count(),
        "notices": list(Notification.objects.order_by("pk").values("pk", "read_at")),
    }


class ViewsBase(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.task = self.new_task(self.manager, self.keeper, "Отгрузить заказ", link="shipment",
                                  requires_review=True, checklist=["Собрать"])

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    def task_urls(self, task):
        item = task.checklist.first()
        return {
            "detail": ("get", reverse("task_detail", args=[task.pk]), {}),
            "edit_get": ("get", reverse("task_edit", args=[task.pk]), {}),
            "edit": ("post", reverse("task_edit", args=[task.pk]), {"title": "Взлом", "priority": "normal",
                                                                    "version": task.version}),
            "start": ("post", reverse("task_action", args=[task.pk, "start"]), {}),
            "cancel": ("post", reverse("task_action", args=[task.pk, "cancel"]), {"text": "x"}),
            "accept": ("post", reverse("task_action", args=[task.pk, "accept"]), {}),
            "reassign": ("post", reverse("task_reassign", args=[task.pk]),
                         {"assignee": self.manager2.pk, "reason": "x", "version": task.version}),
            "check": ("post", reverse("task_check", args=[task.pk, item.pk]), {"done": "1"}),
            "comment": ("post", reverse("task_comment", args=[task.pk]), {"text": "Подсмотрел", "submit_token": "t1"}),
            "read": ("post", reverse("task_read", args=[task.pk]), {}),
        }


class AccessViewsTests(ViewsBase):
    def test_outsider_gets_404_everywhere_and_nothing_changes(self):
        before = state()
        for user in (self.manager2, self.accountant):
            client = self.as_user(user)
            for name, (method, url, data) in self.task_urls(self.task).items():
                response = getattr(client, method)(url, data)
                self.assertEqual(response.status_code, 404, (user.username, name))
        self.assertEqual(state(), before)

    def test_user_without_role_gets_403(self):
        client = self.as_user(self.nobody)
        for name, (method, url, data) in self.task_urls(self.task).items():
            self.assertEqual(getattr(client, method)(url, data).status_code, 403, name)
        for url in (reverse("task_mine"), reverse("task_notifications"), reverse("task_create")):
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_guest_is_sent_to_login_even_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        token = client.cookies["csrftoken"].value
        before = state()
        for name, (method, url, data) in self.task_urls(self.task).items():
            response = getattr(client, method)(url, {**data, "csrfmiddlewaretoken": token})
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"], name)
        self.assertEqual(state(), before)

    def test_post_without_csrf_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.keeper)
        response = client.post(reverse("task_action", args=[self.task.pk, "start"]))
        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.NEW)

    def test_get_never_changes_anything(self):
        before = state()
        for user in (self.keeper, self.manager, self.admin):
            client = self.as_user(user)
            for name, (method, url, _data) in self.task_urls(self.task).items():
                client.get(url)
            for url in (reverse("task_mine"), reverse("task_delegated"), reverse("task_review"),
                        reverse("task_notifications"), reverse("shipment_edit", args=[self.shipment.pk])):
                client.get(url)
        self.assertEqual(state(), before)

    def test_executor_cannot_use_manager_actions(self):
        client = self.as_user(self.keeper)
        before = state()
        for name in ("edit", "cancel", "reassign"):
            method, url, data = self.task_urls(self.task)[name]
            self.assertEqual(getattr(client, method)(url, data).status_code, 403, name)
        self.assertEqual(state(), before)

    def test_former_assignee_loses_access_by_url(self):
        services.reassign(self.manager, self.task.pk, self.task.version, self.admin, "Передаю")
        client = self.as_user(self.keeper)
        self.assertEqual(client.get(reverse("task_detail", args=[self.task.pk])).status_code, 404)
        page = client.get(reverse("task_notifications"))
        self.assertContains(page, "передана другому исполнителю")
        self.assertNotContains(page, "Отгрузить заказ")          # заголовок закрыт
        self.assertNotContains(client.get(reverse("task_mine")), "Отгрузить заказ")

    def test_lost_document_access_hides_task_in_every_view(self):
        task = self.new_task(self.manager, self.manager2, "Секрет счёта", link="invoice")
        services.add_comment(self.manager, task.pk, "Сумма в счёте")
        self.manager2.groups.set([self.groups[roles.ROLE_STOREKEEPER]])
        client = self.as_user(self.manager2)
        self.assertEqual(client.get(reverse("task_detail", args=[task.pk])).status_code, 404)
        self.assertEqual(client.post(reverse("task_comment", args=[task.pk]), {"text": "x"}).status_code, 404)
        for url in (reverse("task_mine"), reverse("task_notifications"), reverse("task_mine") + "?q=Секрет",
                    reverse("task_mine") + f"?q={task.pk}&show=all"):
            self.assertNotContains(client.get(url), "Секрет счёта", msg_prefix=url)
        self.assertNotContains(client.get(reverse("task_mine")), 'topbar-bell-count')
        # администратор видит проблему в общем списке
        page = self.as_user(self.admin).get(reverse("task_all"), {"problem": "no_access"})
        self.assertContains(page, "Секрет счёта")
        self.assertContains(page, "исполнитель не видит документ")

    def test_all_tasks_only_for_admin(self):
        self.assertEqual(self.as_user(self.manager).get(reverse("task_all")).status_code, 403)
        self.assertEqual(self.as_user(self.admin).get(reverse("task_all")).status_code, 200)
        self.assertEqual(self.as_user(self.boss).get(reverse("task_all")).status_code, 200)


class ListViewsTests(ViewsBase):
    def test_mine_delegated_review(self):
        keeper, manager = self.as_user(self.keeper), self.as_user(self.manager)
        self.assertContains(keeper.get(reverse("task_mine")), "Отгрузить заказ")
        self.assertContains(manager.get(reverse("task_delegated")), "Отгрузить заказ")
        self.assertNotContains(manager.get(reverse("task_mine")), "Отгрузить заказ")
        self.assertNotContains(manager.get(reverse("task_review")), "Отгрузить заказ")
        services.transition(self.keeper, self.task.pk, "start")
        services.set_item_done(self.keeper, self.task.pk, self.task.checklist.get().pk, True)
        services.transition(self.keeper, self.task.pk, "submit", "Готово")
        self.assertContains(manager.get(reverse("task_review")), "Отгрузить заказ")
        self.assertContains(self.as_user(self.admin).get(reverse("task_review")), "Отгрузить заказ")
        self.assertNotContains(self.as_user(self.manager2).get(reverse("task_review")), "Отгрузить заказ")

    def test_closed_hidden_by_default_and_shown_by_filter(self):
        services.transition(self.manager, self.task.pk, "cancel", "Не нужна")
        keeper = self.as_user(self.keeper)
        self.assertNotContains(keeper.get(reverse("task_mine")), "Отгрузить заказ")
        self.assertContains(keeper.get(reverse("task_mine"), {"show": "closed"}), "Отгрузить заказ")

    def test_filters_and_search_stay_inside_visible(self):
        other = self.new_task(self.manager2, self.manager2, "Чужая задача")
        manager = self.as_user(self.manager)
        for params in ({"q": "Чужая"}, {"q": str(other.pk)}, {"assignee": self.manager2.pk},
                       {"show": "all", "status": "new"}):
            self.assertNotContains(manager.get(reverse("task_delegated"), params), "Чужая задача",
                                   msg_prefix=str(params))

    def test_overdue_filter_and_mark(self):
        late = self.new_task(self.manager, self.keeper, "Просроченная", due_at=timezone.now() - timedelta(hours=2))
        services.transition(self.keeper, late.pk, "wait", "Жду")
        page = self.as_user(self.keeper).get(reverse("task_mine"), {"overdue": "yes"})
        self.assertContains(page, "Просроченная")
        self.assertContains(page, "Просрочена")
        self.assertNotContains(page, "Отгрузить заказ")

    def test_list_queries_do_not_grow_with_rows(self):
        client = self.as_user(self.admin)
        client.get(reverse("task_all"))
        with CaptureQueriesContext(connection) as few:
            client.get(reverse("task_all"))
        for index in range(15):
            self.new_task(self.manager, self.keeper, f"Ещё {index}", link="shipment", checklist=["a", "b"])
        with CaptureQueriesContext(connection) as many:
            client.get(reverse("task_all"))
        self.assertLessEqual(len(many), len(few) + 2)


class CreateViewTests(ViewsBase):
    def post_create(self, client, **data):
        payload = {"title": "Новая", "assignee": self.keeper.pk, "priority": "normal", "submit_token": "tok-1"}
        payload.update(data)
        return client.post(reverse("task_create"), payload)

    def test_create_from_document_and_repeat_does_not_duplicate(self):
        client = self.as_user(self.manager)
        page = client.get(reverse("task_create"), {"link": f"shipment:{self.shipment.pk}"})
        self.assertContains(page, str(self.shipment))
        before_doc = accounting_state()
        first = self.post_create(client, link_type="shipment", link_pk=self.shipment.pk, checklist="Раз\nДва")
        second = self.post_create(client, link_type="shipment", link_pk=self.shipment.pk, checklist="Раз\nДва")
        task = Task.objects.get(title="Новая")
        self.assertEqual((task.shipment, task.creator), (self.shipment, self.manager))
        self.assertEqual(first["Location"], reverse("task_detail", args=[task.pk]))
        self.assertEqual(second["Location"], first["Location"])
        self.assertEqual(Task.objects.filter(title="Новая").count(), 1)
        self.assertEqual(accounting_state(), before_doc)      # документ не отправлен и не перепроведён

    def test_forged_creator_status_rejected_whole(self):
        client = self.as_user(self.manager)
        for forged in ({"creator": self.admin.pk}, {"status": "done"}, {"version": 9}, {"result": "готово"}):
            response = self.post_create(client, submit_token=f"tok-{len(forged)}-{list(forged)[0]}", **forged)
            self.assertEqual(response.status_code, 400, forged)
        self.assertFalse(Task.objects.filter(title="Новая").exists())

    def test_storekeeper_cannot_assign_other(self):
        client = self.as_user(self.keeper)
        response = self.post_create(client, assignee=self.manager.pk)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(title="Новая").exists())
        self.post_create(client, assignee=self.keeper.pk, submit_token="tok-own")
        self.assertTrue(Task.objects.filter(title="Новая", creator=self.keeper, assignee=self.keeper).exists())

    def test_forged_link_rejected(self):
        client = self.as_user(self.keeper)
        cases = (("invoice", self.invoice.pk, 403), ("shipment", 999999, 404), ("payment", self.payment.pk, 403))
        for code, pk, status in cases:
            self.assertEqual(client.get(reverse("task_create"), {"link": f"{code}:{pk}"}).status_code, status)
            response = self.post_create(client, assignee=self.keeper.pk, link_type=code, link_pk=pk,
                                        submit_token=f"tok-{code}-{pk}")
            self.assertEqual(response.status_code, status, (code, pk))
        self.assertEqual(client.get(reverse("task_create"), {"link": "core.user:1"}).status_code, 404)
        self.assertFalse(Task.objects.filter(title="Новая").exists())

    def test_assignee_without_document_access_explained(self):
        client = self.as_user(self.manager)
        response = self.post_create(client, link_type="invoice", link_pk=self.invoice.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "не может открыть счёт покупателю")
        self.assertContains(response, "без связи")
        self.assertFalse(Task.objects.filter(title="Новая").exists())

    def test_html_in_title_and_comment_is_text(self):
        client = self.as_user(self.manager)
        self.post_create(client, title='<img src=x onerror="alert(1)">', description="<b>жирно</b>")
        task = Task.objects.get(creator=self.manager, assignee=self.keeper, link_type="")
        services.add_comment(self.manager, task.pk, "<script>alert('c')</script>")
        for user in (self.manager, self.keeper):
            for url in (reverse("task_detail", args=[task.pk]), reverse("task_mine"), reverse("task_delegated"),
                        reverse("task_notifications")):
                body = self.as_user(user).get(url).content.decode()
                self.assertNotIn('<img src=x onerror="alert(1)">', body, url)
                self.assertNotIn("<script>alert('c')</script>", body, url)
                self.assertNotIn("<b>жирно</b>", body, url)
        body = self.as_user(self.keeper).get(reverse("task_detail", args=[task.pk])).content.decode()
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", body)
        self.assertIn("&lt;script&gt;alert(&#x27;c&#x27;)&lt;/script&gt;", body)


class EditViewTests(ViewsBase):
    def edit_data(self, **overrides):
        data = {"title": self.task.title, "description": "", "priority": "normal", "version": self.task.version,
                "requires_review": "on", f"item_{self.task.checklist.get().pk}": "Собрать", "new_items": ""}
        data.update(overrides)
        return data

    def test_conflict_keeps_newer_version(self):
        manager, admin = self.as_user(self.manager), self.as_user(self.admin)
        stale = self.edit_data()
        manager.post(reverse("task_edit", args=[self.task.pk]), self.edit_data(title="От менеджера"))
        response = admin.post(reverse("task_edit", args=[self.task.pk]), {**stale, "title": "От администратора"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Задачу изменили", status_code=409)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "От менеджера")

    def test_forged_assignee_status_link_rejected_whole(self):
        client = self.as_user(self.manager)
        before = state()
        for forged in ({"assignee": self.manager2.pk}, {"status": "done"}, {"link_type": "invoice"},
                       {"invoice": self.invoice.pk}, {"creator": self.admin.pk}):
            response = client.post(reverse("task_edit", args=[self.task.pk]), self.edit_data(title="Изм", **forged))
            self.assertEqual(response.status_code, 400, forged)
        self.assertEqual(state(), before)

    def test_disabled_review_is_not_cleared_by_form(self):
        own = self.new_task(self.manager, self.manager, "Своя")
        client = self.as_user(self.manager)
        client.post(reverse("task_edit", args=[own.pk]), {"title": "Своя 2", "priority": "normal",
                                                          "version": own.version})
        own.refresh_from_db()
        self.assertEqual((own.title, own.requires_review), ("Своя 2", False))
        # задача на проверке: поле проверки недоступно и не сбрасывается
        services.transition(self.keeper, self.task.pk, "start")
        services.set_item_done(self.keeper, self.task.pk, self.task.checklist.get().pk, True)
        services.transition(self.keeper, self.task.pk, "submit", "Готово")
        self.task.refresh_from_db()
        client.post(reverse("task_edit", args=[self.task.pk]), {"title": "На проверке", "priority": "normal",
                                                                "version": self.task.version})
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.requires_review), ("На проверке", True))


class ActionViewsTests(ViewsBase):
    def test_repeated_accept_by_double_click(self):
        services.transition(self.keeper, self.task.pk, "start")
        services.set_item_done(self.keeper, self.task.pk, self.task.checklist.get().pk, True)
        services.transition(self.keeper, self.task.pk, "submit", "Готово")
        client = self.as_user(self.manager)
        url = reverse("task_action", args=[self.task.pk, "accept"])
        first, second = client.post(url), client.post(url)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertIn("Уже выполнено", " ".join(str(m) for m in get_messages(second.wsgi_request)))
        self.assertEqual(TaskEvent.objects.filter(task=self.task, action=TaskEvent.ACCEPTED).count(), 1)
        self.assertEqual(Notification.objects.filter(event__action=TaskEvent.ACCEPTED).count(), 1)

    def test_comment_double_submit_creates_one(self):
        client = self.as_user(self.keeper)
        url = reverse("task_comment", args=[self.task.pk])
        client.post(url, {"text": "Один раз", "submit_token": "same"})
        client.post(url, {"text": "Один раз", "submit_token": "same"})
        self.assertEqual(TaskComment.objects.filter(text="Один раз").count(), 1)

    def test_unknown_action_404(self):
        client = self.as_user(self.manager)
        self.assertEqual(client.post(reverse("task_action", args=[self.task.pk, "delete"])).status_code, 404)

    def test_notification_open_marks_read_and_redirects(self):
        client = self.as_user(self.keeper)
        notice = Notification.objects.get(recipient=self.keeper)
        page = client.get(reverse("task_mine"))
        self.assertContains(page, "topbar-bell-count")
        response = client.post(reverse("notification_open", args=[notice.pk]))
        self.assertEqual(response["Location"], reverse("task_detail", args=[self.task.pk]))
        notice.refresh_from_db()
        self.assertIsNotNone(notice.read_at)
        self.assertNotContains(client.get(reverse("task_mine")), "topbar-bell-count")
        # чужое уведомление открыть нельзя
        other = self.as_user(self.manager2)
        self.assertEqual(other.post(reverse("notification_open", args=[notice.pk])).status_code, 404)


class DocumentTabTests(ViewsBase):
    def test_tab_lists_only_visible_tasks(self):
        self.new_task(self.admin, self.admin, "Админская по отгрузке", link="shipment")
        keeper = self.as_user(self.keeper).get(reverse("shipment_edit", args=[self.shipment.pk]))
        self.assertContains(keeper, "Отгрузить заказ")
        self.assertNotContains(keeper, "Админская по отгрузке")
        self.assertContains(keeper, f'?link=shipment:{self.shipment.pk}')
        admin = self.as_user(self.admin).get(reverse("shipment_edit", args=[self.shipment.pk]))
        self.assertContains(admin, "Админская по отгрузке")

    def test_every_document_card_has_tasks_tab_and_events_stays_off(self):
        client = self.as_user(self.admin)
        cards = {"invoice": "invoice_edit", "shipment": "shipment_edit", "customer_return": "customer_return_edit",
                 "receipt": "receipt_edit", "supplier_return": "supplier_return_edit", "payment": "payment_edit",
                 "counterparty": "counterparty_edit"}
        for code, url_name in cards.items():
            page = client.get(reverse(url_name, args=[self.linked[code].pk]))
            self.assertContains(page, 'data-bs-target="#tab-tasks"', msg_prefix=code)
            self.assertContains(page, f"?link={code}:{self.linked[code].pk}", msg_prefix=code)
            if code != "counterparty":
                self.assertContains(page, 'type="button" title="Журнал изменений документа', msg_prefix=code)

    def test_new_document_suggests_saving_first(self):
        page = self.as_user(self.admin).get(reverse("invoice_create"))
        self.assertContains(page, "после его сохранения")
        self.assertNotContains(page, 'data-bs-target="#tab-tasks"')

    def test_tasks_tab_counts_ignore_hidden(self):
        self.new_task(self.admin, self.admin, "Скрытая", link="shipment")
        page = self.as_user(self.keeper).get(reverse("shipment_edit", args=[self.shipment.pk])).content.decode()
        tab = page[page.index('data-bs-target="#tab-tasks"'):]
        self.assertIn('rounded-pill">1</span>', tab[:200])


class NavigationTests(ViewsBase):
    def test_menu(self):
        manager = self.as_user(self.manager).get(reverse("task_mine")).content.decode()
        self.assertIn(reverse("task_delegated"), manager)
        self.assertNotIn(reverse("task_all"), manager)
        admin = self.as_user(self.admin).get(reverse("task_mine")).content.decode()
        self.assertIn(reverse("task_all"), admin)
