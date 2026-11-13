"""Выпуск CRM-1 вместе с правилом «список и печать — как карточка».

Проверяется то, что получилось из объединения двух веток:
- меню: у каждой роли, у суперпользователя и у пользователя без роли — ровно
  те пункты, страницы которых открываются; «Все задачи» — только
  администратору; задачи и колокольчик — только сотрудникам с ролью;
- связанная задача, карточка, список и печать документа открываются одним
  и тем же ролям (задачи не расширяют прав на документы, и наоборот);
- потеря роли закрывает документ, его печать, задачу, уведомление и
  счётчик; возврат роли — открывает снова.
"""
from django.test import Client, TestCase
from django.urls import reverse

from apps.core import roles

from . import services
from .access import Viewer
from .links import LINK_TYPES
from .models import Task
from .testing import TaskWorld

LISTS = {
    "invoice": "invoice_list", "shipment": "shipment_list", "customer_return": "customer_return_list",
    "receipt": "receipt_list", "supplier_return": "supplier_return_list", "payment": "payment_list",
    "counterparty": "counterparty_list",
}
PRINTS = {
    "invoice": ["print_invoice_payment", "print_invoice_payment_qr"],
    "shipment": ["print_shipment_torg12", "print_shipment_act", "print_shipment_upd"],
    "payment": ["print_payment_cash_order"],
}


class ReleaseBase(TaskWorld, TestCase):
    def setUp(self):
        self.make_world()
        self.people = {
            "Администратор": self.admin, "Менеджер": self.manager, "Кладовщик": self.keeper,
            "Бухгалтер": self.accountant, "суперпользователь": self.boss, "без роли": self.nobody,
        }

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    @staticmethod
    def opened(response, what):
        if response.status_code not in (200, 403, 404):
            raise AssertionError(f"{what}: неожиданный ответ {response.status_code}")
        return response.status_code == 200


class MenuTests(ReleaseBase):
    def menu(self, client):
        page = client.get(reverse("dashboard"))
        self.assertEqual(page.status_code, 200)
        return page, page.context["nav_sections"]

    def test_menu_is_exactly_the_pages_that_open(self):
        _, full = self.menu(self.as_user(self.boss))
        candidates = {item["url"] for section in full for item in section["submenu"] if item["url"]}
        # раздел «Настройки» по прежнему решению показывается только администратору, хотя
        # отдельные его страницы открыты и другим (склады — кладовщику): это не ссылка на закрытую страницу
        admin_only = {item["url"] for section in full if section["admin_only"] for item in section["submenu"]}
        self.assertIn(reverse("task_all"), candidates)
        for name, user in self.people.items():
            client = self.as_user(user)
            _, nav = self.menu(client)
            shown = {item["url"] for section in nav for item in section["submenu"] if item["url"]}
            opens = {url for url in candidates if self.opened(client.get(url), f"{name}: {url}")}
            self.assertEqual(shown - opens, set(), f"{name}: пункты меню ведут на закрытые страницы")
            if name not in ("Администратор", "суперпользователь"):
                opens -= admin_only
            self.assertEqual(opens - shown, set(), f"{name}: доступные страницы не показаны в меню")
            for section in nav:                           # раздел ведёт на свой первый пункт
                self.assertEqual(section["url"], section["submenu"][0]["url"], f"{name}: {section['code']}")

    def test_tasks_section_and_bell_by_role(self):
        task_all = reverse("task_all")
        for name, user in self.people.items():
            page, nav = self.menu(self.as_user(user))
            tasks = next((s for s in nav if s["code"] == "tasks"), None)
            body = page.content.decode()
            if name == "без роли":
                self.assertIsNone(tasks, name)
                self.assertNotIn('class="topbar-bell', body, name)
                continue
            self.assertIsNotNone(tasks, name)
            self.assertIn('class="topbar-bell', body, name)
            urls = [item["url"] for item in tasks["submenu"]]
            self.assertEqual(urls[0], reverse("task_mine"), name)
            self.assertIn(reverse("task_notifications"), urls, name)
            self.assertEqual(task_all in urls, name in ("Администратор", "суперпользователь"), name)

    def test_sections_by_role(self):
        expected = {
            "Администратор": {"dashboard", "tasks", "purchases", "sales", "catalog", "stock", "partners", "money",
                              "reports", "settings"},
            "суперпользователь": {"dashboard", "tasks", "purchases", "sales", "catalog", "stock", "partners",
                                  "money", "reports", "settings"},
            "Менеджер": {"dashboard", "tasks", "purchases", "sales", "catalog", "stock", "partners", "money",
                         "reports"},
            "Кладовщик": {"dashboard", "tasks", "purchases", "sales", "catalog", "stock", "reports"},
            "Бухгалтер": {"dashboard", "tasks", "catalog", "stock", "partners", "money", "reports"},
            "без роли": {"dashboard", "catalog", "reports"},
        }
        for name, user in self.people.items():
            _, nav = self.menu(self.as_user(user))
            self.assertEqual({s["code"] for s in nav}, expected[name], name)
        _, nav = self.menu(self.as_user(self.keeper))
        sales = next(s for s in nav if s["code"] == "sales")
        self.assertEqual([i["url"] for i in sales["submenu"]],
                         [reverse("shipment_list"), reverse("customer_return_list")])


class LinkedAccessTests(ReleaseBase):
    def test_task_link_card_list_and_print_open_to_same_roles(self):
        for link in LINK_TYPES:
            obj = self.linked[link.code]
            for name, user in self.people.items():
                client = self.as_user(user)
                card = self.opened(client.get(link.url(obj.pk)), f"{name}: карточка {link.code}")
                listed = self.opened(client.get(reverse(LISTS[link.code])), f"{name}: список {link.code}")
                self.assertEqual(listed, card, f"{name}: список {link.code}")
                for print_name in PRINTS.get(link.code, []):
                    printed = self.opened(client.get(reverse(print_name, args=[obj.pk]), {"fmt": "html"}),
                                          f"{name}: {print_name}")
                    self.assertEqual(printed, card, f"{name}: {print_name}")
                self.assertEqual(Viewer(user).link_allowed(link.code), card, f"{name}: задача по {link.code}")

    def check_access(self, user, task, expected):
        client = self.as_user(user)
        card = client.get(reverse("invoice_edit", args=[self.invoice.pk])).status_code
        listing = client.get(reverse("invoice_list")).status_code
        printed = client.get(reverse("print_invoice_payment", args=[self.invoice.pk]), {"fmt": "html"}).status_code
        detail = client.get(reverse("task_detail", args=[task.pk])).status_code
        notices = client.get(reverse("task_notifications"))
        page = client.get(reverse("task_mine"))
        nav_urls = {item["url"] for s in page.context["nav_sections"] for item in s["submenu"]}
        state = {
            "карточка": card, "список": listing, "печать": printed, "задача": detail,
            "уведомление": "Секретный счёт" in notices.content.decode(),
            "счётчик": "topbar-bell-count" in page.content.decode(),
            "в меню": reverse("invoice_list") in nav_urls,
            "в списке задач": "Секретный счёт" in page.content.decode(),
        }
        self.assertEqual(state, expected)

    def test_role_loss_closes_everything_and_return_reopens(self):
        task = self.new_task(self.manager, self.manager2, "Секретный счёт", link="invoice")
        services.add_comment(self.manager, task.pk, "Сумма по счёту")
        open_state = {"карточка": 200, "список": 200, "печать": 200, "задача": 200, "уведомление": True,
                      "счётчик": True, "в меню": True, "в списке задач": True}
        self.check_access(self.manager2, task, open_state)
        self.manager2.groups.set([self.groups[roles.ROLE_STOREKEEPER]])
        self.check_access(self.manager2, task, {
            "карточка": 403, "список": 403, "печать": 403, "задача": 404, "уведомление": False,
            "счётчик": False, "в меню": False, "в списке задач": False})
        # администратор видит проблему и может передать задачу
        page = self.as_user(self.admin).get(reverse("task_all"), {"problem": "no_access"})
        self.assertContains(page, "исполнитель не видит документ")
        self.manager2.groups.set([self.groups[roles.ROLE_MANAGER]])
        self.check_access(self.manager2, task, open_state)

    def test_storekeeper_cannot_get_invoice_task_nor_invoice_print(self):
        with self.assertRaises(services.TaskError):
            self.new_task(self.manager, self.keeper, "Счёт кладовщику", link="invoice")
        client = self.as_user(self.keeper)
        self.assertEqual(client.get(reverse("print_invoice_payment", args=[self.invoice.pk])).status_code, 403)
        task = self.new_task(self.manager, self.keeper, "Отгрузку кладовщику", link="shipment")
        self.assertEqual(client.get(reverse("task_detail", args=[task.pk])).status_code, 200)
        self.assertEqual(client.get(reverse("print_shipment_torg12", args=[self.shipment.pk]),
                                    {"fmt": "html"}).status_code, 200)
        self.assertFalse(Task.objects.filter(title="Счёт кладовщику").exists())

    def test_accountant_payment_task_follows_payment_card(self):
        task = self.new_task(self.manager, self.accountant, "Проверить платёж", link="payment")
        client = self.as_user(self.accountant)
        self.assertEqual(client.get(reverse("task_detail", args=[task.pk])).status_code, 200)
        self.assertEqual(client.get(reverse("print_payment_cash_order", args=[self.payment.pk]),
                                    {"fmt": "html"}).status_code, 200)
        self.accountant.groups.clear()
        self.assertEqual(client.get(reverse("task_detail", args=[task.pk])).status_code, 403)   # без роли — вовсе
        self.assertEqual(client.get(reverse("print_payment_cash_order", args=[self.payment.pk])).status_code, 403)
        self.assertEqual(client.get(reverse("payment_list")).status_code, 403)
