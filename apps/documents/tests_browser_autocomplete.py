"""Подсказки живого поиска в настоящем браузере: значения только как текст.

Названия, артикулы, единицы, телефоны и текст запроса приходят от пользователей.
В меню и окне серийных номеров они показываются буквально, разметка из них не
создаётся. Поведение подсказок при этом прежнее: выбор мышью и с клавиатуры,
остаток, «Создать», сообщения об ошибках.

Условия запуска те же, что в tests_browser_product_create: tag browser,
без Playwright, Chromium или CDN Bootstrap тесты пропускаются (skipped).
"""
import json
import re

from django.urls import reverse

from apps.catalog.models import Product, Unit
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from .tests_browser_product_create import BrowserWorld

MARKUP = 'Икс <img src=x onerror="window.__xss=1">'
SHORT_MARKUP = "<img src=x onerror=__xss=1>"    # для коротких полей: единица, телефон
ENTITY = 'Винт &amp; "гайка" <b>М6</b>'         # не раскрывается и не экранируется дважды

OPEN_MENU = ".ac-menu:not(.d-none)"


class AcWorld(BrowserWorld):
    def type_in_row(self, text, row=0):
        field = self.row(row).locator(".line-product-text")
        field.click()
        field.fill(text)
        return field

    def party_field(self, name="customer"):
        return self.page.locator(f'.ac-value[name="{name}"]').locator("xpath=..").locator(".ac-input")

    def type_party(self, text, name="customer"):
        field = self.party_field(name)
        field.click()
        field.fill(text)
        return field

    def menu(self):
        return self.page.locator(OPEN_MENU)

    def assertNothingInjected(self, *scopes):
        """Разметка не создана, обработчик не сработал (ошибка картинки приходит не сразу)."""
        for scope in (OPEN_MENU, *scopes):
            self.assertEqual(self.page.locator(f"{scope} img, {scope} b").count(), 0, scope)
        self.page.wait_for_timeout(300)
        self.assertIsNone(self.page.evaluate("window.__xss"))


class LiteralProductTests(AcWorld):
    def test_name_is_literal_and_choice_works(self):
        product = Product.objects.create(name=MARKUP, unit=self.unit, sale_price="150.50")
        self.open("invoice_create")
        self.type_in_row("Икс")
        item = self.menu().locator(".ac-item")
        self.assertEqual(item.locator(".ac-label").inner_text(), MARKUP)
        self.assertNothingInjected()

        item.click()
        state = self.state(self.row())
        self.assertEqual((state["id"], state["label"], state["price"]), (str(product.pk), MARKUP, "150.50"))
        self.assertEqual(self.total(), "150,50")
        self.assertEqual(self.rows().count(), 2)
        self.assertNothingInjected(".lines-body")
        self.assertNoPageProblems()

    def test_keyboard_navigation_redraws_and_selects(self):
        Product.objects.create(name=f"А {MARKUP}", unit=self.unit)
        second = Product.objects.create(name=f"Б {MARKUP}", unit=self.unit)
        self.open("invoice_create")
        field = self.type_in_row("Икс")
        self.assertEqual(self.menu().locator(".ac-item.active .ac-label").inner_text(), f"А {MARKUP}")
        field.press("ArrowDown")
        self.assertEqual(self.menu().locator(".ac-item.active .ac-label").inner_text(), f"Б {MARKUP}")
        self.assertNothingInjected()
        field.press("Enter")
        self.assertEqual(self.state(self.row())["id"], str(second.pk))
        self.assertNoPageProblems()

    def test_click_selects_the_clicked_item(self):
        Product.objects.create(name="Первый товар", unit=self.unit)
        second = Product.objects.create(name="Второй товар", unit=self.unit)
        self.open("invoice_create")
        self.type_in_row("товар")
        self.menu().locator(".ac-item", has_text="Второй").click()
        self.assertEqual(self.state(self.row())["id"], str(second.pk))

    def test_article_and_unit_are_literal(self):
        unit = Unit.objects.create(name=SHORT_MARKUP, okei_code="999")
        Product.objects.create(name="Товар", article=SHORT_MARKUP, unit=unit)
        self.open("invoice_create")
        self.type_in_row("Товар")
        self.assertEqual(self.menu().locator(".ac-label").inner_text(), f"Товар ({SHORT_MARKUP})")
        stock = self.menu().locator(".ac-stock")
        self.assertEqual(stock.inner_text(), f"0 {SHORT_MARKUP}")
        self.assertIn("ac-stock-zero", stock.get_attribute("class"))
        self.assertNothingInjected()

    def test_entities_are_not_decoded_or_escaped_twice(self):
        Product.objects.create(name=ENTITY, unit=self.unit)
        self.open("invoice_create")
        self.type_in_row("Винт")
        self.assertEqual(self.menu().locator(".ac-label").inner_text(), ENTITY)
        self.assertNothingInjected()

    def test_query_is_literal_when_nothing_found(self):
        self.open("invoice_create")
        self.type_in_row(MARKUP)
        self.assertEqual(self.menu().locator(".ac-empty").inner_text(), "Ничего не найдено")
        self.assertEqual(self.menu().locator(".ac-create").inner_text().strip(),
                         f"Создать новый товар: «{MARKUP}»")
        self.assertNothingInjected()

    def test_saved_document_shows_name_literally(self):
        Counterparty.objects.create(name=MARKUP, partner_type=Counterparty.TYPE_BOTH)
        Product.objects.create(name=MARKUP, unit=self.unit, sale_price="10")
        self.open("invoice_create")
        self.type_in_row("Икс")
        self.menu().locator(".ac-item").click()
        self.type_party("Икс")
        self.menu().locator(".ac-item").click()
        self.page.locator("button[name=action][value=save]").click()
        self.page.locator(".alert-success").wait_for()
        self.page.reload()
        self.page.wait_for_load_state("load")

        self.assertEqual(self.state(self.row())["label"], MARKUP)
        self.assertEqual(self.party_field().input_value(), MARKUP)
        self.assertEqual(Invoice.objects.latest("pk").lines.get().product.name, MARKUP)
        self.assertNothingInjected(".lines-body")
        self.assertNoPageProblems()


class LiteralCounterpartyTests(AcWorld):
    def test_name_and_details_are_literal_and_choice_works(self):
        party = Counterparty.objects.create(
            name=MARKUP, inn="7700000000", phone=SHORT_MARKUP, partner_type=Counterparty.TYPE_CUSTOMER)
        self.open("invoice_create")
        field = self.type_party("Икс")
        item = self.menu().locator(".ac-item")
        self.assertEqual(item.locator(".ac-label").inner_text(), MARKUP)
        self.assertEqual(item.locator(".ac-sub").inner_text(), f"ИНН 7700000000 · {SHORT_MARKUP}")
        self.assertNothingInjected()

        item.click()
        self.assertEqual(self.page.locator('.ac-value[name="customer"]').input_value(), str(party.pk))
        self.assertEqual(field.input_value(), MARKUP)
        self.assertNothingInjected()
        self.assertNoPageProblems()

    def test_create_from_query_keeps_name_literal(self):
        self.open("invoice_create")
        field = self.type_party(MARKUP)
        self.assertEqual(self.menu().locator(".ac-empty").inner_text(), "Ничего не найдено")
        self.assertEqual(self.menu().locator(".ac-create").inner_text().strip(), f"Создать контрагента: «{MARKUP}»")
        self.assertNothingInjected()

        self.menu().locator(".ac-create").click()
        self.page.locator(OPEN_MENU).wait_for(state="detached")
        party = Counterparty.objects.get(name=MARKUP)
        self.assertEqual(self.page.locator('.ac-value[name="customer"]').input_value(), str(party.pk))
        self.assertEqual(field.input_value(), MARKUP)
        self.assertNothingInjected()

    def test_inn_query_offers_lookup(self):
        self.open("invoice_create")
        self.type_party("7700000000")
        create = self.menu().locator(".ac-create")
        self.assertIn("Создать по ИНН", create.inner_text())
        self.assertEqual(create.locator("i.bi-magic").count(), 1)

    def test_empty_field_prompts_for_input(self):
        Counterparty.objects.all().delete()
        self.open("invoice_create")
        self.party_field().click()
        self.assertEqual(self.menu().locator(".ac-empty").inner_text(), "Начните вводить название")


class MessageTests(AcWorld):
    def _answer_create(self, body=None):
        def handler(route):
            if body is None:
                return route.abort()
            return route.fulfill(status=400, content_type="application/json", body=json.dumps(body))
        self.page.route(re.compile(r"/api/counterparties/quick-create/"), handler)

    def test_server_error_text_is_literal(self):
        self.open("invoice_create")
        self._answer_create({"ok": False, "error": MARKUP})
        self.type_party("Новый")
        self.menu().locator(".ac-create").click()
        message = self.menu().locator(".ac-empty.text-danger")
        message.wait_for()
        self.assertEqual(message.inner_text(), MARKUP)
        self.assertNothingInjected()

    def test_refusal_without_text_has_default_message(self):
        self.open("invoice_create")
        self._answer_create({"ok": False})
        self.type_party("Новый")
        self.menu().locator(".ac-create").click()
        message = self.menu().locator(".ac-empty.text-danger")
        message.wait_for()
        self.assertEqual(message.inner_text(), "Не удалось создать")

    def test_create_failure_message(self):
        self.open("invoice_create")
        self._answer_create()
        self.type_party("Новый")
        self.menu().locator(".ac-create").click()
        message = self.menu().locator(".ac-empty.text-danger")
        message.wait_for()
        self.assertEqual(message.inner_text(), "Ошибка создания")

    def test_search_failure_message(self):
        self.open("invoice_create")
        self.page.route(re.compile(r"/api/counterparties/search/"), lambda route: route.abort())
        self.type_party("Новый")
        message = self.menu().locator(".ac-empty.text-danger")
        message.wait_for()
        self.assertEqual(message.inner_text(), "Ошибка поиска")


class LiteralSerialsTests(AcWorld):
    """serials.js экранирует сам; проверка, что номера и подпись товара не становятся разметкой."""

    def _open_serials(self, url_name):
        Product.objects.create(name=MARKUP, unit=self.unit, track_serials=True)
        self.open(url_name)
        self.page.route(re.compile(r"/api/.*serials"), lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"serials": [MARKUP]})))
        self.type_in_row("Икс")
        self.menu().locator(".ac-item").click()
        self.row().locator(".line-serial-btn").click()
        self.page.locator("#serialModal .sn-input").wait_for()
        self.assertEqual(self.page.locator("#serialModal .modal-title").inner_text(),
                         f"Серийные номера — {MARKUP}")

    def test_entered_number_is_literal(self):
        self._open_serials("receipt_create")
        field = self.page.locator("#serialModal .sn-input")
        field.fill(MARKUP)
        field.press("Enter")
        self.assertEqual(self.page.locator("#serialModal .sn-list .font-monospace").inner_text(), MARKUP)
        self.assertNothingInjected("#serialModal")
        self.assertNoPageProblems()

    def test_available_number_and_missing_hint_are_literal(self):
        self._open_serials("shipment_create")
        check = self.page.locator("#serialModal .sn-check")
        check.wait_for()
        self.assertEqual(check.input_value(), MARKUP)
        self.assertEqual(self.page.locator("#serialModal .form-check-label").inner_text(), MARKUP)

        field = self.page.locator("#serialModal .sn-input")
        field.fill(f"Другой {SHORT_MARKUP}")
        field.press("Enter")
        self.assertEqual(self.page.locator("#serialModal .sn-hint").inner_text(),
                         f"Нет на складе: Другой {SHORT_MARKUP}")
        self.assertNothingInjected("#serialModal")
        self.assertNoPageProblems()


class SearchResponseOrderTests(BrowserWorld):
    """Отложенный поиск и опоздавшие ответы не должны менять закрытое меню.

    Раньше choose() и close() не отменяли поиск: после быстрого выбора меню строки
    открывалось снова, с кнопкой «Создать», и следующий клик создавал товар не от
    той строки. Опоздавший ответ на прежний запрос заменял свежий список.
    Задержка задана в самой странице: результат не зависит от нагрузки на машину,
    а обработчик page.route останавливал бы очередь запросов."""

    EMPTY_QUERY = r"[?&]q=(&|$)"
    TEXT_QUERY = r"[?&]q=[^&]"

    def setUp(self):
        super().setUp()
        Product.objects.create(name="Аааа первый", unit=self.unit, sale_price=1)
        self.wanted = Product.objects.create(name="Яяяя нужный", unit=self.unit, sale_price=2)

    def delay_answers(self, query_pattern, ms=1200):
        self.context.add_init_script("""(() => {
          const original = window.fetch;
          window.__sent = 0;
          window.__answered = 0;
          window.fetch = async (url, options) => {
            const late = /api\\/products\\/search/.test(url) && new RegExp(%s).test(url);
            if (late) window.__sent += 1;
            const response = await original(url, options);
            if (late) {
              await new Promise((done) => setTimeout(done, %d));
              window.__answered += 1;
            }
            return response;
          };
        })();""" % (json.dumps(query_pattern), ms))

    def wait_sent(self):
        self.page.wait_for_function("window.__sent >= 1")

    def wait_answered(self):
        self.page.wait_for_function("window.__answered >= 1")
        self.page.wait_for_timeout(100)     # ответ обработан

    def menu_of_row_closed(self, index=0):
        return self.page.evaluate(
            "i => document.querySelectorAll('.lines-body tr.line-row .ac')[i]._ac.menu.classList.contains('d-none')",
            index)

    def field(self, index=0):
        return self.row(index).locator(".line-product-text")

    def first_item(self):
        return self.page.locator(".ac-menu:not(.d-none) .ac-item").first

    def test_late_answer_to_old_query_does_not_replace_list(self):
        self.delay_answers(self.EMPTY_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.wait_sent()
        self.field().fill("Яяяя")
        self.page.locator(".ac-menu:not(.d-none) .ac-label", has_text="Яяяя нужный").wait_for()
        self.wait_answered()
        self.assertEqual(self.page.locator(".ac-menu:not(.d-none) .ac-label").all_inner_texts(), ["Яяяя нужный"])

    def test_late_answer_does_not_reopen_menu_after_choice(self):
        self.delay_answers(self.TEXT_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Яяяя")
        self.wait_sent()
        self.first_item().click()       # в списке ещё ответ на пустой запрос
        self.wait_answered()
        self.assertTrue(self.menu_of_row_closed())

    def test_choice_before_search_starts_cancels_it(self):
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Яяяя")
        self.first_item().click()       # раньше, чем сработает задержка ввода
        self.page.wait_for_timeout(700)
        self.assertTrue(self.menu_of_row_closed())

    def test_late_answer_does_not_open_menu_after_leaving_field(self):
        self.delay_answers(self.EMPTY_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.wait_sent()
        self.page.locator(".doc-heading").click()
        self.wait_answered()
        self.assertEqual(self.page.locator(".ac-menu:not(.d-none)").count(), 0)

    def test_create_goes_to_row_where_name_was_typed(self):
        """Именно так выглядел сбой: ответ первой строки приходит, когда пользователь
        уже во второй, и «Создать» срабатывает от первой."""
        self.delay_answers(self.TEXT_QUERY)
        self.open("invoice_create")
        self.field(0).click()
        self.first_item().wait_for()
        self.field(0).fill("Яяяя")
        self.wait_sent()
        self.first_item().click()
        chosen = self.state(self.row(0))
        self.field(1).click()
        self.field(1).fill("Новый из второй")
        frame_opener = self.page.locator(".ac-menu:not(.d-none) .ac-create")
        frame_opener.wait_for()
        self.page.wait_for_timeout(1300)        # опоздавший ответ первой строки уже пришёл
        self.assertEqual(self.menu_of_row_closed(0), True)
        frame_opener.first.click()
        frame = self.page.frame_locator(".entity-frame")
        frame.locator('[name=name][value="Новый из второй"]').wait_for()
        self.fill_and_save(frame, sale_price="5")
        self.wait_closed()
        self.row(1).locator(".line-product-link").wait_for()
        self.assertEqual(self.state(self.row(0))["id"], chosen["id"])
        self.assertEqual(self.state(self.row(1))["label"], "Новый из второй")

    def test_search_still_works_after_closing_menu(self):
        """Отмена прежних запросов не мешает новому поиску."""
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.page.locator(".doc-heading").click()
        self.field().click()
        self.field().fill("Яяяя")
        self.page.locator(".ac-menu:not(.d-none) .ac-label", has_text="Яяяя нужный").click()
        self.assertEqual(self.row().locator(".line-product").input_value(), str(self.wanted.pk))

    # ----- Enter при незавершённом поиске: так работает сканер штрихкодов

    def scanned_product(self):
        return Product.objects.create(name="Штрих", unit=self.unit, sale_price=3, barcode="4600000000017")

    def chosen_after_answers(self):
        self.page.wait_for_timeout(1800)
        return self.row().locator(".line-product").input_value()

    def test_scanner_enter_with_old_list_open(self):
        """Код введён, Enter сразу, в меню ещё список пустого запроса."""
        product = self.scanned_product()
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("4600000000017")
        self.field().press("Enter")
        self.assertEqual(self.chosen_after_answers(), str(product.pk))

    def test_scanner_enter_while_answer_is_on_its_way(self):
        product = self.scanned_product()
        self.delay_answers(self.TEXT_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("4600000000017")
        self.wait_sent()
        self.field().press("Enter")
        self.assertEqual(self.chosen_after_answers(), str(product.pk))

    def test_enter_picks_match_for_typed_text(self):
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Яяяя")
        self.field().press("Enter")
        self.assertEqual(self.chosen_after_answers(), str(self.wanted.pk))
        self.assertEqual(self.page.url, self.live_server_url + reverse("invoice_create"))     # форма не ушла

    def test_enter_on_unknown_name_offers_creation(self):
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Такого товара нет")
        self.field().press("Enter")
        frame = self.page.frame_locator(".entity-frame")
        frame.locator('[name=name][value="Такого товара нет"]').wait_for()
        self.assertEqual(self.row().locator(".line-product").input_value(), "")

    def test_enter_then_leaving_field_chooses_nothing(self):
        """Ушёл из поля до ответа — опоздавший выбор не срабатывает."""
        self.delay_answers(self.TEXT_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Яяяя")
        self.wait_sent()
        self.field().press("Enter")
        self.page.locator(".doc-heading").click()
        self.wait_answered()
        self.page.wait_for_timeout(300)
        self.assertEqual(self.row().locator(".line-product").input_value(), "")

    def test_cancelled_enter_does_not_choose_on_next_search(self):
        """Enter, уход из поля, возврат и новый ввод: меню показано, но сам товар не выбран."""
        self.delay_answers(self.TEXT_QUERY)
        self.open("invoice_create")
        self.field().click()
        self.first_item().wait_for()
        self.field().fill("Яяяя")
        self.wait_sent()
        self.field().press("Enter")
        self.page.locator(".doc-heading").click()
        self.page.wait_for_timeout(300)
        self.field().click()
        self.field().fill("Аааа")
        self.page.locator(".ac-menu:not(.d-none) .ac-label", has_text="Аааа первый").wait_for()
        self.assertEqual(self.row().locator(".line-product").input_value(), "")
