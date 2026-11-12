"""Задачи в настоящем браузере (Chromium, Playwright) под разными пользователями.

От документа до принятия результата: менеджер ставит задачу из карточки
отгрузки, кладовщик с телефона (375 px) берёт её в работу, отмечает
чек-лист, пишет комментарий и сдаёт результат, менеджер возвращает и
принимает. Пропуск — только без Playwright, Chromium или CDN Bootstrap
(как у остальных браузерных тестов, tag browser).

TASKS_SCREENSHOTS=<папка> — сохранить снимки экранов шагов (для отчёта).
"""
import os
from datetime import timedelta

from django.test import Client, tag
from django.urls import reverse
from django.utils import timezone

from apps.documents.tests_browser_product_create import PAGE_LOAD, WAIT, BrowserWorld

from . import services
from .models import Task, TaskComment, TaskEvent
from .testing import TaskWorld, accounting_state

PHONE = {"width": 375, "height": 812}


@tag("browser")
class TaskBrowserTests(TaskWorld, BrowserWorld):
    def setUp(self):
        # свой набор данных вместо BrowserWorld.setUp: несколько сотрудников
        self.make_world()
        self.contexts = {}
        self.js_errors, self.dialogs = [], []
        self.page = None

    def tearDown(self):
        for context in self.contexts.values():
            context.close()

    def as_user(self, user, viewport=None):
        """Вкладка браузера под пользователем (свой контекст и сессия)."""
        key = (user.pk, bool(viewport))
        if key not in self.contexts:
            # у каждого пользователя свой клиент: вход другого под тем же клиентом удалил бы сессию
            client = Client()
            client.force_login(user)
            options = {"locale": "ru-RU"}
            if viewport:
                options.update(viewport=viewport, is_mobile=True, has_touch=True)
            context = self.browser.new_context(**options)
            context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value,
                                  "url": self.live_server_url}])
            context.set_default_timeout(WAIT)
            context.set_default_navigation_timeout(PAGE_LOAD)
            self.contexts[key] = context
        self.context = self.contexts[key]
        self.page = None
        return self

    def go(self, url_name, *args, query=""):
        self.open(url_name, *args)
        if query:
            self.page.goto(self.page.url + query)
        return self.page

    def shot(self, name):
        folder = os.environ.get("TASKS_SCREENSHOTS")
        if folder:
            self.page.screenshot(path=os.path.join(folder, f"{name}.png"), full_page=True)

    def assertNoHorizontalScroll(self):
        width = self.page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        self.assertLessEqual(width[0], width[1], f"горизонтальная прокрутка: {width}")

    def press(self, name):
        """Кнопка действия на карточке; для действий с текстом — раскрыть, ввести, отправить."""
        self.page.locator(f'form[data-action="{name}"] button').click()
        self.page.wait_for_load_state("load")

    def act_with_text(self, name, text):
        block = self.page.locator(f'details[data-action="{name}"]')
        block.locator("summary").click()
        block.locator("textarea").fill(text)
        block.locator("button[type=submit]").click()
        self.page.wait_for_load_state("load")

    def status_text(self):
        return self.page.locator(".task-card .badge").first.inner_text()

    # ----- сценарии

    def test_from_shipment_to_accepted_result_by_two_people_phone_for_executor(self):
        before = accounting_state()
        # 1. Менеджер: карточка отгрузки → вкладка «Задачи» → «Поставить задачу»
        self.as_user(self.manager)
        page = self.go("shipment_edit", self.shipment.pk)
        page.locator('[data-bs-target="#tab-tasks"]').click()
        page.locator("[data-task-create]").click()
        page.wait_for_load_state("load")
        self.assertIn(reverse("task_create"), page.url)
        page.locator("[name=title]").fill("Упаковать и отправить")
        page.locator("[name=assignee]").select_option(str(self.keeper.pk))
        due = timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        page.locator("[name=due_at]").fill(due)
        page.locator("[name=requires_review]").check()
        page.locator("[name=checklist]").fill("Упаковать\nНаклеить этикетку")
        self.shot("01-create-from-shipment")
        page.locator("button[type=submit].btn-primary").click()
        page.wait_for_load_state("load")
        task = Task.objects.get(title="Упаковать и отправить")
        self.assertEqual((task.shipment, task.assignee, task.requires_review), (self.shipment, self.keeper, True))
        self.assertEqual(accounting_state(), before)        # документ не сохранялся и не перепроводился

        # 2. Кладовщик с телефона: уведомление → карточка → в работу → чек-лист → комментарий → сдать
        self.as_user(self.keeper, viewport=PHONE)
        page = self.go("task_mine")
        self.assertNoHorizontalScroll()
        self.assertEqual(page.locator(".topbar-bell-count").inner_text(), "1")
        page.locator(".topbar-bell").click()
        page.wait_for_load_state("load")
        self.shot("02-phone-notifications")
        page.locator(".notice-list form button").first.click()
        page.wait_for_load_state("load")
        self.assertIn(reverse("task_detail", args=[task.pk]), page.url)
        self.assertNoHorizontalScroll()
        self.press("start")
        for index in range(2):
            page.locator(".checklist-toggle").nth(index).click()
            page.wait_for_load_state("load")
        self.assertEqual(page.locator('.checklist-toggle[aria-pressed="true"]').count(), 2)
        page.locator("#comment-text").fill("Этикетки заканчиваются")
        page.locator(".task-comment-form button[type=submit]").click()
        page.wait_for_load_state("load")
        self.assertTrue(TaskComment.objects.filter(task=task, text="Этикетки заканчиваются").exists())
        self.act_with_text("submit", "Упаковано, этикетка наклеена")
        self.assertNoHorizontalScroll()
        self.shot("03-phone-submitted")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.REVIEW)

        # 3. Менеджер возвращает с причиной
        self.as_user(self.manager)
        page = self.go("task_review")
        page.locator(f'a[href="{reverse("task_detail", args=[task.pk])}"]').click()
        page.wait_for_load_state("load")
        self.act_with_text("return", "Нужна фотография упаковки")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

        # 4. Кладовщик сдаёт ещё раз (результат подставлен прежний — дополняет)
        self.as_user(self.keeper, viewport=PHONE)
        page = self.go("task_detail", task.pk)
        self.assertIn("Нужна фотография упаковки", page.locator("#feed").inner_text())
        self.act_with_text("submit", "Фото в комментарии отправил")

        # 5. Менеджер принимает
        self.as_user(self.manager)
        page = self.go("task_detail", task.pk)
        self.press("accept")
        self.assertEqual(self.status_text(), "Завершена")
        self.shot("04-accepted-history")
        feed = page.locator("#feed").inner_text()
        for text in ("Задача поставлена", "Взята в работу", "Отмечен пункт чек-листа", "Этикетки заканчиваются",
                     "Результат сдан на проверку", "Возвращена на доработку", "Результат принят"):
            self.assertIn(text, feed)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(TaskEvent.objects.filter(task=task, action=TaskEvent.SUBMITTED).count(), 2)
        self.assertEqual(accounting_state(), before)
        self.assertNoPageProblems()

    def test_phone_list_is_cards_without_horizontal_scroll(self):
        for index in range(3):
            self.new_task(self.manager, self.keeper, f"Длинное название задачи номер {index} " * 3,
                          link="shipment", due_at=timezone.now() - timedelta(hours=index))
        self.as_user(self.keeper, viewport=PHONE)
        page = self.go("task_mine")
        self.assertNoHorizontalScroll()
        self.assertEqual(page.locator(".task-table thead").evaluate("e => getComputedStyle(e).display"), "none")
        self.assertEqual(page.locator(".task-table tbody tr").count(), 3)
        self.shot("05-phone-list")
        self.assertNoPageProblems()

    def test_double_click_create_makes_one_task(self):
        self.as_user(self.manager)
        page = self.go("task_create")
        page.locator("[name=title]").fill("Один раз")
        page.locator("[name=assignee]").select_option(str(self.keeper.pk))
        page.locator("button[type=submit].btn-primary").dblclick()
        page.wait_for_load_state("load")
        page.locator(".task-card").wait_for()
        self.assertEqual(Task.objects.filter(title="Один раз").count(), 1)

    def test_outsider_sees_not_found_and_no_counter(self):
        task = self.new_task(self.manager, self.keeper, "Чужая для второго менеджера")
        services.add_comment(self.keeper, task.pk, "Комментарий")
        self.as_user(self.manager2)
        page = self.context.new_page()
        self.page = page
        response = page.goto(self.live_server_url + reverse("task_detail", args=[task.pk]))
        self.assertEqual(response.status, 404)
        self.go("task_mine")
        self.assertEqual(self.page.locator(".topbar-bell-count").count(), 0)
        self.assertNotIn("Чужая для второго менеджера", self.page.content())
