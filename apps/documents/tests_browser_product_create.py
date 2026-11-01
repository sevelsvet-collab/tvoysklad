"""Создание товара из строки документа — в настоящем браузере (Chromium, Playwright).

Цепочка: строка документа → окно карточки товара → сообщение окна документу →
выбор товара в строке → цена, НДС, сумма, итог → сохранение → повторное открытие.
Её основная часть работает в JavaScript, поэтому текста шаблона для проверки мало.

Нужны Playwright, Chromium (python -m playwright install chromium) и доступ
к CDN Bootstrap. Без них тесты пропускаются с причиной — в отчёте это skipped,
а не успех: tag browser. Пропуск только в этих трёх случаях: ответ страницы с
ошибкой, другой сбой запуска браузера или отсутствие Bootstrap при рабочем CDN —
это падение теста, иначе ошибка приложения выглядела бы как пропуск.
"""
import os
import re
import sys
import unittest
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.management.color import no_style
from django.db import connection
from django.test import SimpleTestCase, tag
from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.core import roles
from apps.core.models import Organization, Warehouse
from apps.partners.models import Counterparty
from apps.purchases.models import Receipt
from apps.sales.models import Invoice

User = get_user_model()

IDS = (999, 1000, 1540)
WAIT = 7000     # мс: ожидание шагов интерфейса
PAGE_LOAD = 30000   # мс: загрузка страницы, она ждёт внешний CDN (Bootstrap, htmx, Alpine)


@tag("browser")
class BrowserWorld(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright не установлен: {exc}")
        # Playwright держит цикл событий в этом потоке; Django иначе запрещает ORM
        cls._async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        cls.playwright = None
        try:
            # Браузер — раньше живого сервера: без браузера сервер не нужен
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:
            if cls.playwright is not None:
                cls.playwright.stop()
            cls._restore_async_flag()
            # Только отсутствие браузера — другие сбои запуска видны как ошибка
            if isinstance(exc, PlaywrightError) and "Executable doesn't exist" in str(exc):
                raise unittest.SkipTest("Chromium не установлен: " + str(exc).splitlines()[0])
            raise
        try:
            super().setUpClass()
        except Exception:
            cls.browser.close()
            cls.playwright.stop()
            cls._restore_async_flag()
            raise

    @classmethod
    def _restore_async_flag(cls):
        if cls._async_unsafe is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = cls._async_unsafe

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()
        cls._restore_async_flag()

    def setUp(self):
        # Вид цены и НДС документа зависят от организации
        self.org = Organization.objects.create(name="Плательщик НДС", is_default=True)
        self.free_org = Organization.objects.create(name="Без НДС", vat_payer=False)
        self.wh = Warehouse.objects.create(name="Основной", is_default=True)
        self.unit = Unit.objects.create(name="шт", okei_code="796")
        self.party = Counterparty.objects.create(name="Контрагент", partner_type=Counterparty.TYPE_BOTH)
        # Живой сервер очищает базу между тестами вместе с данными миграций
        group, _ = Group.objects.get_or_create(name=roles.ROLE_MANAGER)
        self.user = User.objects.create_user("m", password="pass12345")
        self.user.groups.add(group)

        self.client.force_login(self.user)
        self.context = self.browser.new_context(locale="ru-RU")
        self.context.add_cookies([{
            "name": "sessionid", "value": self.client.cookies["sessionid"].value, "url": self.live_server_url,
        }])
        self.context.set_default_timeout(WAIT)
        self.context.set_default_navigation_timeout(PAGE_LOAD)
        self.page = None
        self.js_errors, self.dialogs = [], []
        self.addCleanup(self.context.close)

    def _on_dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.dismiss()

    # ----- подготовка

    def next_product_pk(self, pk):
        """Следующий созданный товар получит этот id (скрытый товар-затравка)."""
        Product.objects.get_or_create(pk=pk - 1, defaults={
            "name": f"Затравка {pk}", "unit": self.unit, "is_active": False,
        })
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                # счётчик SQLite не сбрасывается при очистке базы между тестами
                cursor.execute("UPDATE sqlite_sequence SET seq = %s WHERE name = %s",
                               [pk - 1, Product._meta.db_table])
            for sql in connection.ops.sequence_reset_sql(no_style(), [Product]):
                cursor.execute(sql)

    def open(self, url_name, *args):
        # Каждый документ — в новой вкладке: уход с заполненной формы спрашивает подтверждение
        if self.page is not None:
            self.page.close()
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda exc: self.js_errors.append(f"{exc.name}: {exc.message} {exc.stack} @ {self.page.url}"))
        self.page.on("dialog", self._on_dialog)
        bootstrap_failed = []
        self.page.on("requestfailed",
                     lambda r: bootstrap_failed.append(r.url) if "bootstrap.bundle" in r.url else None)
        self.page.on("response",
                     lambda r: bootstrap_failed.append(r.url) if "bootstrap.bundle" in r.url and r.status >= 400 else None)
        url = self.live_server_url + reverse(url_name, args=args)
        response = self.page.goto(url)
        self.assertTrue(response.ok, f"{url}: HTTP {response.status}")     # ошибка страницы — падение, не пропуск
        self.page.wait_for_load_state("load")
        if not self.page.evaluate("Boolean(window.bootstrap)"):
            if bootstrap_failed:
                raise unittest.SkipTest(f"Bootstrap с CDN не загрузился: {bootstrap_failed[0]}")
            self.fail(f"{url}: на странице нет Bootstrap, хотя CDN отвечает")

    # ----- строка документа

    def rows(self):
        return self.page.locator(".lines-body tr.line-row:not(.d-none)")

    def row(self, index=0):
        return self.rows().nth(index)

    def state(self, row):
        """То, что на экране и что уйдёт на сервер."""
        return {
            "id": row.locator(".line-product").input_value(),
            "label": row.locator(".line-product-link").inner_text(),
            "qty": row.locator(".line-qty").input_value(),
            "price": row.locator(".line-price").input_value(),
            "vat": row.locator('select[name$="-vat_rate"]').input_value(),
            "discount": row.locator(".line-discount-value").input_value(),
            "sum": row.locator(".line-sum").inner_text(),
        }

    def total(self):
        return self.page.locator(".total-sum").inner_text()

    def ask_create(self, row, name):
        """Ввести название в строке и нажать «Создать новый товар»."""
        field = row.locator(".line-product-text")
        field.click()
        field.fill(name)
        self.page.locator(".ac-menu:not(.d-none) .ac-create").click()
        return self.page.frame_locator(".entity-frame")

    def fill_and_save(self, frame, **values):
        frame.locator("[name=name]").wait_for()
        for field, value in values.items():
            frame.locator(f"[name={field}]").fill(value)
        frame.locator("button.btn-success[type=submit]").click()

    def wait_fully_open(self):
        """Анимация открытия закончилась: только теперь окно реагирует на закрытие."""
        self.page.wait_for_function("""() => {
          const modal = bootstrap.Modal.getInstance(document.getElementById('entityModal'));
          return modal && modal._isShown && !modal._isTransitioning;
        }""")

    def wait_closed(self):
        self.page.locator("#entityModal").wait_for(state="hidden")

    def create_from(self, row, name, **values):
        frame = self.ask_create(row, name)
        self.fill_and_save(frame, **values)
        self.wait_closed()
        row.locator(".line-product-link").wait_for()

    def assertNoPageProblems(self):
        self.assertEqual(self.js_errors, [])
        self.assertEqual(self.dialogs, [])


class BrowserAvailabilityTests(SimpleTestCase):
    """Когда браузерные тесты пропускаются, а когда падают. Сам браузер здесь не нужен."""

    def setUp(self):
        try:
            from playwright.sync_api import Error
        except ImportError as exc:
            self.skipTest(f"Playwright не установлен: {exc}")
        self.playwright_error = Error
        flag = os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        if flag is not None:
            self.addCleanup(os.environ.__setitem__, "DJANGO_ALLOW_ASYNC_UNSAFE", flag)

    def start_with_launch_error(self, error):
        driver = mock.Mock()
        driver.chromium.launch.side_effect = error
        starter = mock.Mock()
        starter.start.return_value = driver
        with mock.patch("playwright.sync_api.sync_playwright", return_value=starter):
            try:
                BrowserWorld.setUpClass()
            finally:
                driver.stop.assert_called_once()
                self.assertNotIn("DJANGO_ALLOW_ASYNC_UNSAFE", os.environ)

    def test_missing_package_is_skipped_with_reason(self):
        with mock.patch.dict(sys.modules, {"playwright.sync_api": None}):
            with self.assertRaises(unittest.SkipTest) as caught:
                BrowserWorld.setUpClass()
        self.assertIn("Playwright не установлен", str(caught.exception))
        self.assertNotIn("DJANGO_ALLOW_ASYNC_UNSAFE", os.environ)

    def test_missing_chromium_is_skipped_with_reason(self):
        error = self.playwright_error("BrowserType.launch: Executable doesn't exist at /x/chrome\nподробности")
        with self.assertRaises(unittest.SkipTest) as caught:
            self.start_with_launch_error(error)
        self.assertIn("Chromium не установлен", str(caught.exception))
        self.assertNotIn("подробности", str(caught.exception))

    def assertLaunchErrorIsNotSkip(self, error):
        # SkipTest внутри assertRaises сделал бы пропущенным сам этот тест
        try:
            with self.assertRaises(type(error)):
                self.start_with_launch_error(error)
        except unittest.SkipTest as skip:
            self.fail(f"пропуск вместо ошибки: {skip}")

    def test_other_browser_failure_is_an_error(self):
        self.assertLaunchErrorIsNotSkip(
            self.playwright_error("BrowserType.launch: Target page, context or browser has been closed"))

    def test_unexpected_exception_is_an_error(self):
        self.assertLaunchErrorIsNotSkip(ZeroDivisionError("сбой"))


class OpenPageOutcomeTests(BrowserWorld):
    """Пропуск — только когда не загрузился Bootstrap с CDN; ошибка страницы — падение."""

    def test_page_error_fails_instead_of_skipping(self):
        self.context.route(re.compile(r"/sales/invoices/new/"),
                           lambda route: route.fulfill(status=500, body="<h1>Server Error</h1>"))
        with self.assertRaisesMessage(AssertionError, "HTTP 500"):
            self.open("invoice_create")

    def test_real_403_fails_instead_of_skipping(self):
        self.user.groups.clear()
        with self.assertRaisesMessage(AssertionError, "HTTP 403"):
            self.open("invoice_create")

    def test_unreachable_cdn_is_skipped(self):
        self.context.route(re.compile(r"bootstrap\.bundle"), lambda route: route.abort())
        with self.assertRaises(unittest.SkipTest) as caught:
            self.open("invoice_create")
        self.assertIn("Bootstrap с CDN не загрузился", str(caught.exception))

    def test_cdn_error_status_is_skipped(self):
        self.context.route(re.compile(r"bootstrap\.bundle"), lambda route: route.fulfill(status=503, body=""))
        with self.assertRaises(unittest.SkipTest):
            self.open("invoice_create")

    def test_missing_bootstrap_with_working_cdn_fails(self):
        self.context.route(re.compile(r"bootstrap\.bundle"),
                           lambda route: route.fulfill(status=200, content_type="text/javascript", body=""))
        with self.assertRaisesMessage(AssertionError, "на странице нет Bootstrap"):
            self.open("invoice_create")


class CreateAndSelectTests(BrowserWorld):
    def test_created_product_is_selected_for_each_id(self):
        """999 работало и до исправления, 1000 и 1540 — нет: «id: 1 540» в скрипте окна."""
        for pk in IDS:
            with self.subTest(id=pk):
                self.next_product_pk(pk)
                self.open("invoice_create")
                self.create_from(self.row(), f"Новый {pk}", sale_price="490.50")
                state = self.state(self.row())
                self.assertEqual(state["id"], str(pk))
                self.assertEqual(state["label"], f"Новый {pk}")
                self.assertEqual(state["price"], "490.50")
                self.assertEqual(state["sum"], "490,50")
                self.assertEqual(self.total(), "490,50")
                # выбранная строка и одна пустая для ввода — без дублей
                self.assertEqual(self.rows().count(), 2)
                self.assertEqual(self.row(1).locator(".line-product").input_value(), "")
                self.assertEqual(Product.objects.filter(name=f"Новый {pk}").count(), 1)
                self.assertNoPageProblems()

    def test_price_kind_follows_document(self):
        """Продажа — цена продажи, закупка — закупочная; другая цена не подмешивается."""
        documents = {
            "invoice_create": "200.75", "shipment_create": "200.75", "customer_return_create": "200.75",
            "receipt_create": "100.25", "supplier_return_create": "100.25",
        }
        for url_name, expected in documents.items():
            with self.subTest(url_name):
                self.open(url_name)
                self.create_from(self.row(), f"Цены {url_name}", purchase_price="100.25", sale_price="200.75")
                self.assertEqual(self.state(self.row())["price"], expected)
                self.assertNoPageProblems()

    def test_only_other_kind_of_price_gives_zero(self):
        self.open("invoice_create")
        self.create_from(self.row(), "Только закупочная", purchase_price="100.25", sale_price="0")
        state = self.state(self.row())
        self.assertEqual(Decimal(state["price"]), Decimal("0"))
        self.assertEqual(self.total(), "0,00")

    def test_card_prefills_price_of_document_kind(self):
        """Цена, уже введённая в строке, предлагается в окне как цена этого вида."""
        self.open("receipt_create")
        self.row().locator(".line-price").fill("77.50")
        frame = self.ask_create(self.row(), "С ценой из строки")
        frame.locator("[name=name]").wait_for()
        self.assertEqual(frame.locator("[name=purchase_price]").input_value(), "77.50")
        self.assertEqual(Decimal(frame.locator("[name=sale_price]").input_value()), Decimal("0"))

    def test_name_with_special_characters(self):
        self.open("invoice_create")
        name = 'Винт "М6" & гайка'
        self.create_from(self.row(), name, sale_price="1")
        self.assertEqual(self.state(self.row())["label"], name)
        self.assertNoPageProblems()


class ManualPriceTests(BrowserWorld):
    def _create_after_price(self, typed, product_price="500.00"):
        self.open("invoice_create")
        if typed is not None:
            self.row().locator(".line-price").fill(typed)
        self.create_from(self.row(), f"Цена {typed}", sale_price=product_price)
        return self.state(self.row())["price"]

    def test_manual_price_is_kept(self):
        self.assertEqual(self._create_after_price("300.75"), "300.75")

    def test_manual_zero_is_kept(self):
        self.assertEqual(Decimal(self._create_after_price("0")), Decimal("0"))

    def test_untouched_row_gets_product_price(self):
        self.assertEqual(self._create_after_price(None), "500.00")

    def test_cleared_price_gets_product_price(self):
        self.assertEqual(self._create_after_price(""), "500.00")

    def test_fractional_product_price(self):
        self.assertEqual(self._create_after_price(None, "12.34"), "12.34")
        self.assertEqual(self.state(self.row())["sum"], "12,34")

    def test_zero_product_price(self):
        self.assertEqual(Decimal(self._create_after_price(None, "0")), Decimal("0"))
        self.assertEqual(self.total(), "0,00")

    def test_auto_price_is_replaced_by_next_product(self):
        """Подставленная цена — не ручная: при замене товара она меняется."""
        self.open("invoice_create")
        self.create_from(self.row(), "Первый", sale_price="100")
        row = self.row()
        row.locator(".line-product-change").click()
        self.create_from(row, "Второй", sale_price="250")
        state = self.state(row)
        self.assertEqual((state["label"], state["price"]), ("Второй", "250.00"))

    def test_manual_price_survives_product_replacement(self):
        self.open("invoice_create")
        self.create_from(self.row(), "Первый", sale_price="100")
        row = self.row()
        row.locator(".line-price").fill("123.45")
        row.locator(".line-product-change").click()
        self.create_from(row, "Второй", sale_price="250")
        self.assertEqual(self.state(row)["price"], "123.45")


class RowDataTests(BrowserWorld):
    def test_quantity_discount_and_totals(self):
        self.open("invoice_create")
        row = self.row()
        row.locator(".line-qty").fill("3")
        row.locator(".line-discount-input").fill("10")
        self.create_from(row, "Со скидкой", sale_price="100")
        state = self.state(row)
        self.assertEqual((state["qty"], state["discount"]), ("3", "10"))
        self.assertEqual(state["sum"], "270,00")
        self.assertEqual(self.total(), "270,00")

    def test_discount_in_money_is_recalculated_from_new_price(self):
        """Скидка в рублях, введённая до выбора товара, не теряется."""
        self.open("invoice_create")
        row = self.row()
        row.locator(".line-qty").fill("2")
        row.locator(".line-discount-mode").click()
        row.locator(".line-discount-input").fill("50")
        self.create_from(row, "Скидка в рублях", sale_price="100")
        self.assertEqual(self.state(row)["sum"], "150,00")
        self.assertEqual(Decimal(self.state(row)["discount"]), Decimal("25"))

    def test_vat_follows_product_for_payer(self):
        self.open("invoice_create")
        frame = self.ask_create(self.row(), "НДС 10")
        frame.locator("[name=vat_rate]").select_option("10")
        self.fill_and_save(frame, sale_price="100")
        self.wait_closed()
        self.row().locator(".line-product-link").wait_for()
        self.assertEqual(self.state(self.row())["vat"], "10")

    def test_vat_is_none_for_non_payer(self):
        self.open("invoice_create")
        self.page.locator("[name=organization]").select_option(str(self.free_org.pk))
        frame = self.ask_create(self.row(), "НДС у неплательщика")
        frame.locator("[name=vat_rate]").select_option("10")
        self.fill_and_save(frame, sale_price="100")
        self.wait_closed()
        self.row().locator(".line-product-link").wait_for()
        self.assertEqual(self.state(self.row())["vat"], "none")

    def test_other_rows_are_not_touched(self):
        self.open("invoice_create")
        self.create_from(self.row(0), "Первая строка", sale_price="100")
        self.row(0).locator(".line-qty").fill("4")
        before = self.state(self.row(0))
        self.create_from(self.row(1), "Вторая строка", sale_price="30")
        self.assertEqual(self.state(self.row(0)), {**before, "sum": "400,00"})
        self.assertEqual(self.state(self.row(1))["label"], "Вторая строка")
        self.assertEqual(self.total(), "430,00")
        self.assertEqual(self.rows().count(), 3)


class WindowOutcomeTests(BrowserWorld):
    def test_form_error_keeps_window_and_row(self):
        self.open("invoice_create")
        self.row().locator(".line-qty").fill("6")
        frame = self.ask_create(self.row(), "С ошибкой")
        # Такую сумму принимает поле браузера, но не сервер: слишком много цифр
        self.fill_and_save(frame, sale_price="99999999999999999", article="АРТ-1")
        frame.locator(".text-danger").first.wait_for()
        self.assertTrue(self.page.locator("#entityModal").is_visible())
        self.assertEqual(frame.locator("[name=name]").input_value(), "С ошибкой")
        self.assertEqual(frame.locator("[name=article]").input_value(), "АРТ-1")
        self.assertEqual(frame.locator("[name=sale_price]").input_value(), "99999999999999999")
        self.assertEqual(self.row().locator(".line-product").input_value(), "")
        self.assertEqual(self.row().locator(".line-qty").input_value(), "6")
        self.assertFalse(Product.objects.filter(name="С ошибкой").exists())

        # исправил в том же окне — товар попадает в ту же строку
        self.fill_and_save(frame, sale_price="10.50")
        self.wait_closed()
        self.row().locator(".line-product-link").wait_for()
        state = self.state(self.row())
        self.assertEqual((state["label"], state["qty"], state["price"]), ("С ошибкой (АРТ-1)", "6", "10.50"))
        self.assertNoPageProblems()

    def test_cancel_selects_nothing(self):
        self.open("invoice_create")
        frame = self.ask_create(self.row(), "Отменённый")
        frame.locator("[name=sale_price]").fill("100")
        self.page.locator("#entityModal .btn-close").click()
        self.wait_closed()
        self.page.wait_for_timeout(400)
        self.assertEqual(self.row().locator(".line-product").input_value(), "")
        self.assertEqual(self.total(), "0,00")
        self.assertFalse(Product.objects.filter(name="Отменённый").exists())
        self.assertNoPageProblems()

    def test_cancel_leaves_no_old_page_for_next_window(self):
        """Введённое в отменённом окне не появляется в следующем."""
        self.open("invoice_create")
        frame = self.ask_create(self.row(), "Отменённый")
        frame.locator("[name=sale_price]").fill("100")
        self.page.locator("#entityModal .btn-close").click()
        self.wait_closed()
        self.page.wait_for_function(
            "document.querySelector('.entity-frame').getAttribute('src') === 'about:blank'")
        frame = self.ask_create(self.row(), "Следующий")
        frame.locator('[name=name][value="Следующий"]').wait_for()
        self.assertEqual(Decimal(frame.locator("[name=sale_price]").input_value()), Decimal("0"))
        self.assertNoPageProblems()

    def test_click_outside_does_not_close_window(self):
        self.open("invoice_create")
        frame = self.ask_create(self.row(), "Не теряется")
        frame.locator("[name=sale_price]").fill("100")
        self.wait_fully_open()
        self.page.mouse.click(5, 5)
        self.page.wait_for_timeout(500)
        self.assertTrue(self.page.locator("#entityModal").is_visible())
        self.assertEqual(frame.locator("[name=sale_price]").input_value(), "100")

    def test_lookup_failure_shows_message_and_does_not_create_again(self):
        self.open("invoice_create")
        for failure in ("abort", "500", "empty"):
            with self.subTest(failure):
                name = f"Сбой {failure}"

                def handler(route):
                    if "id=" not in route.request.url:
                        return route.continue_()
                    if failure == "abort":
                        return route.abort()
                    if failure == "500":
                        return route.fulfill(status=500, body="<h1>Server Error</h1>")
                    return route.fulfill(status=200, content_type="application/json", body='{"results": []}')

                pattern = re.compile(r"/api/products/search/")
                self.page.route(pattern, handler)
                row = self.row(self.rows().count() - 1)     # пустая строка внизу
                frame = self.ask_create(row, name)
                self.fill_and_save(frame, sale_price="100")
                self.wait_closed()
                message = row.locator(".ac-error")
                message.wait_for()
                self.assertIn(name, message.inner_text())
                self.assertIn("создавать заново не нужно", message.inner_text())
                self.assertEqual(row.locator(".line-product").input_value(), "")
                self.assertEqual(Product.objects.filter(name=name).count(), 1)
                self.page.unroute(pattern)

                # нашёл товар поиском — сообщение исчезло, товар в строке
                field = row.locator(".line-product-text")
                field.click()
                field.fill(name)
                # С текстом: до ответа на ввод в меню ещё список пустого запроса,
                # и «первая подсказка» была бы другим товаром
                self.page.locator(".ac-menu:not(.d-none) .ac-item", has_text=name).click()
                self.assertEqual(row.locator(".ac-error").count(), 0)
                self.assertEqual(Product.objects.filter(name=name).count(), 1)
                self.assertEqual(row.locator(".line-product").input_value(),
                                 str(Product.objects.get(name=name).pk))
        self.assertEqual(self.dialogs, [])

    def test_product_card_opened_from_row_does_not_change_price(self):
        """Правка уже выбранного товара: новое имя видно, цена строки прежняя."""
        self.next_product_pk(1540)
        self.open("invoice_create")
        self.create_from(self.row(), "Старое имя", sale_price="100")
        row = self.row()
        row.locator(".line-qty").fill("2")
        row.locator(".line-product-link").click()
        frame = self.page.frame_locator(".entity-frame")
        frame.locator("[name=name]").fill("Новое имя")
        frame.locator("[name=sale_price]").fill("999")
        frame.locator("button.btn-success[type=submit]").first.click()
        self.wait_closed()
        self.page.wait_for_function(
            "document.querySelector('.line-product-link').textContent.trim() === 'Новое имя'")
        state = self.state(row)
        self.assertEqual((state["id"], state["qty"]), ("1540", "2"))
        self.assertEqual(Decimal(state["price"]), Decimal("100"))
        self.assertNoPageProblems()


class WindowRoutingTests(BrowserWorld):
    def test_products_from_different_rows_land_in_their_rows(self):
        self.next_product_pk(1000)
        self.open("invoice_create")
        self.create_from(self.row(0), "Строка 1", sale_price="1")
        self.create_from(self.row(1), "Строка 2", sale_price="2")
        self.create_from(self.row(2), "Строка 3", sale_price="3")
        self.assertEqual([self.state(self.row(i))["label"] for i in range(3)],
                         ["Строка 1", "Строка 2", "Строка 3"])
        self.assertEqual([self.state(self.row(i))["id"] for i in range(3)], ["1000", "1001", "1002"])
        self.assertEqual(self.total(), "6,00")
        self.assertEqual(self.rows().count(), 4)

    def test_replaced_product_price_is_not_offered_in_window(self):
        """В окно не уходит цена, подставленная от прежнего товара."""
        self.open("invoice_create")
        self.create_from(self.row(), "Прежний", sale_price="100")
        row = self.row()
        row.locator(".line-product-change").click()
        frame = self.ask_create(row, "Следующий")
        frame.locator("[name=name]").wait_for()
        self.assertEqual(Decimal(frame.locator("[name=sale_price]").input_value()), Decimal("0"))

    def test_earlier_row_after_later_rows(self):
        """Окно из верхней пустой строки при уже заполненной нижней."""
        self.open("invoice_create")
        self.page.locator(".add-line").click()
        self.create_from(self.row(1), "Нижняя", sale_price="5")
        self.create_from(self.row(0), "Верхняя", sale_price="7")
        self.assertEqual(self.state(self.row(0))["label"], "Верхняя")
        self.assertEqual(self.state(self.row(1))["label"], "Нижняя")
        self.assertEqual(self.state(self.row(0))["price"], "7.00")
        self.assertEqual(self.state(self.row(1))["price"], "5.00")

    def test_cancel_then_create_from_other_row(self):
        self.open("invoice_create")
        self.page.locator(".add-line").click()
        self.ask_create(self.row(0), "Отменён").locator("[name=name]").wait_for()
        self.page.locator("#entityModal .btn-close").click()
        self.wait_closed()
        self.create_from(self.row(1), "Из второй", sale_price="9")
        self.assertEqual(self.row(0).locator(".line-product").input_value(), "")
        self.assertEqual(self.state(self.row(1))["label"], "Из второй")
        self.assertEqual(Product.objects.filter(name="Отменён").count(), 0)

    def test_reopen_during_close_animation(self):
        """Повторное открытие, пока прежнее окно ещё закрывается."""
        self.open("invoice_create")
        self.page.locator(".add-line").click()
        self.ask_create(self.row(0), "Первое окно").locator("[name=name]").wait_for()
        self.wait_fully_open()
        self.page.evaluate("""() => {
          bootstrap.Modal.getInstance(document.getElementById('entityModal')).hide();
          const ac = document.querySelectorAll('.lines-body tr.line-row .ac')[1];
          ac.dispatchEvent(new CustomEvent('ac:create', { detail: { name: 'Второе окно' }, bubbles: true }));
        }""")
        frame = self.page.frame_locator(".entity-frame")
        frame.locator('[name=name][value="Второе окно"]').wait_for()
        self.wait_fully_open()
        self.fill_and_save(frame, sale_price="4")
        self.wait_closed()
        self.row(1).locator(".line-product-link").wait_for()
        self.assertEqual(self.row(0).locator(".line-product").input_value(), "")
        self.assertEqual(self.state(self.row(1))["label"], "Второе окно")

    def test_stale_and_foreign_messages_are_ignored(self):
        """Ответ чужого или прошлого окна не меняет строку и не закрывает окно."""
        decoy = Product.objects.create(name="Посторонний", unit=self.unit, sale_price=1)
        self.open("invoice_create")
        self.page.locator(".add-line").click()
        # ответ прошлого окна: первое открыто и закрыто, его метка известна
        self.ask_create(self.row(0), "Первое").locator("[name=name]").wait_for()
        old_token = self.page.evaluate(
            "new URL(document.querySelector('.entity-frame').src).searchParams.get('modal')")
        self.assertTrue(old_token)
        self.page.locator("#entityModal .btn-close").click()
        self.wait_closed()

        frame = self.ask_create(self.row(1), "Второе")
        frame.locator("[name=name]").wait_for()
        new_token = self.page.evaluate(
            "new URL(document.querySelector('.entity-frame').src).searchParams.get('modal')")
        self.assertNotEqual(old_token, new_token)
        # из самого окна, но с прошлой меткой или без метки
        frame_handle = self.page.frames[-1]
        for payload in (
            {"type": "entity-saved", "model": "product", "id": decoy.pk, "name": "x", "token": old_token},
            {"type": "entity-saved", "model": "product", "id": decoy.pk, "name": "x"},
        ):
            frame_handle.evaluate("p => window.parent.postMessage(p, window.location.origin)", payload)
        # от самой страницы документа, с верной меткой, — источник не окно
        self.page.evaluate(
            "p => window.postMessage(p, window.location.origin)",
            {"type": "entity-saved", "model": "product", "id": decoy.pk, "name": "x", "token": new_token})
        self.page.wait_for_timeout(500)
        self.assertTrue(self.page.locator("#entityModal").is_visible())
        self.assertEqual(self.row(0).locator(".line-product").input_value(), "")
        self.assertEqual(self.row(1).locator(".line-product").input_value(), "")

        self.fill_and_save(frame, sale_price="3")
        self.wait_closed()
        self.row(1).locator(".line-product-link").wait_for()
        self.assertEqual(self.state(self.row(1))["label"], "Второе")
        self.assertEqual(self.row(0).locator(".line-product").input_value(), "")


class EndToEndTests(BrowserWorld):
    def _save_and_reopen(self, party_field):
        field = self.page.locator(f'.ac-value[name="{party_field}"]').locator("xpath=..").locator(".ac-input")
        field.click()
        field.fill("Контрагент")
        self.page.locator(".ac-menu:not(.d-none) .ac-item").first.click()
        self.page.locator('button[name=action][value=save]').click()
        self.page.locator(".alert-success").wait_for()
        self.page.reload()
        self.page.wait_for_load_state("load")

    def test_invoice_keeps_product_and_price_after_reopen(self):
        for pk in IDS:
            with self.subTest(id=pk):
                self.next_product_pk(pk)
                self.open("invoice_create")
                self.create_from(self.row(), f"Сквозной {pk}", sale_price="1234.56")
                self.row().locator(".line-qty").fill("2")
                self.assertEqual(self.total(), "2\xa0469,12")
                self._save_and_reopen("customer")

                state = self.state(self.row())
                self.assertEqual((state["id"], state["label"]), (str(pk), f"Сквозной {pk}"))
                self.assertEqual(Decimal(state["price"]), Decimal("1234.56"))
                self.assertEqual(self.total(), "2\xa0469,12")
                invoice = Invoice.objects.latest("pk")
                line = invoice.lines.get()
                self.assertEqual((line.product_id, line.price, line.quantity),
                                 (pk, Decimal("1234.56"), Decimal("2")))
                self.assertNoPageProblems()

    def test_receipt_keeps_purchase_price_after_reopen(self):
        self.next_product_pk(1540)
        self.open("receipt_create")
        self.create_from(self.row(), "Закупка", purchase_price="55.40", sale_price="99")
        self._save_and_reopen("supplier")
        self.assertEqual(Decimal(self.state(self.row())["price"]), Decimal("55.40"))
        line = Receipt.objects.latest("pk").lines.get()
        self.assertEqual((line.product_id, line.price), (1540, Decimal("55.40")))
        self.assertNoPageProblems()
