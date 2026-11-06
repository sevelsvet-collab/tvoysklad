"""Контрагент платежа в настоящем браузере: поиск и выбор по назначению.

Правило — как в tests_payment_counterparty: возвраты и оплаты по своей стороне
вместе с типом «оба», «не распределено» и «не указано» без счёта — любой. Здесь
то, что делает страница: параметры поиска после смены назначения и счёта,
снятие неподходящего выбора, опоздавшие ответы, быстрое создание, повторный
показ формы с ошибкой, сохранение и проведение.

Условия запуска те же, что в apps/documents/tests_browser_product_create: tag
browser, без Playwright, Chromium или CDN Bootstrap тесты пропускаются (skipped).
"""
import json
import re
from decimal import Decimal

from apps.catalog.models import Product
from apps.documents.tests_browser_product_create import BrowserWorld
from apps.partners.models import Counterparty
from apps.sales.models import Invoice

from .models import Account, Payment

D = Decimal
CUSTOMER, SUPPLIER, BOTH = "Тест покупатель", "Тест поставщик", "Тест оба"
OPEN_MENU = ".ac-menu:not(.d-none)"


class PaymentPageWorld(BrowserWorld):
    def setUp(self):
        super().setUp()
        self.account = Account.objects.create(organization=self.org, name="Р/с", is_default=True)
        self.customer = Counterparty.objects.create(name=CUSTOMER, partner_type=Counterparty.TYPE_CUSTOMER)
        self.supplier = Counterparty.objects.create(name=SUPPLIER, partner_type=Counterparty.TYPE_SUPPLIER)
        self.both = Counterparty.objects.create(name=BOTH, partner_type=Counterparty.TYPE_BOTH)

    def expect(self, locator):
        from playwright.sync_api import expect
        return expect(locator)

    def text_field(self):
        return self.page.locator(".ac:has(.ac-value[name=counterparty]) .ac-input")

    def value(self):
        return self.page.locator(".ac-value[name=counterparty]").input_value()

    def purpose(self, value):
        self.page.select_option("select[name=settlement_purpose]", value)

    def search(self, text="Тест"):
        field = self.text_field()
        field.click()
        field.fill(text)
        return field

    def assertMenu(self, *names):
        """Список подсказок (среди трёх тестовых) — ровно эти, по алфавиту."""
        self.expect(self.page.locator(f"{OPEN_MENU} .ac-label")).to_have_text(sorted(names))

    def choose(self, name):
        self.page.locator(f"{OPEN_MENU} .ac-item", has_text=name).click()
        self.expect(self.text_field()).to_have_value(name)

    def assertDropped(self, name, type_label):
        self.expect(self.text_field()).to_have_value("")
        self.assertEqual(self.value(), "")
        self.expect(self.page.locator(".ac-error")).to_contain_text(f"«{name}» — {type_label}")

    def settle(self):
        """Проверка выбранного после смены назначения — запрос к серверу; дождаться его."""
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(200)

    def save(self, action="save_post"):
        with self.page.expect_navigation():
            self.page.locator(f"button[name=action][value={action}]").click()

    def assertNoErrors(self):
        self.assertEqual(self.js_errors, [])


class SearchByPurposeTests(PaymentPageWorld):
    CASES = [
        ("payment_create_out", "customer_refund", (BOTH, CUSTOMER)),
        ("payment_create_in", "supplier_refund", (BOTH, SUPPLIER)),
        ("payment_create_in", "customer_payment", (BOTH, CUSTOMER)),
        ("payment_create_out", "supplier_payment", (BOTH, SUPPLIER)),
        ("payment_create_in", "unallocated", (BOTH, CUSTOMER, SUPPLIER)),
        ("payment_create_out", "unallocated", (BOTH, CUSTOMER, SUPPLIER)),
        ("payment_create_in", "", (BOTH, CUSTOMER, SUPPLIER)),
        ("payment_create_out", "", (BOTH, CUSTOMER, SUPPLIER)),
    ]

    def test_search_follows_purpose(self):
        for url_name, purpose, names in self.CASES:
            with self.subTest(url_name, purpose=purpose):
                self.open(url_name)
                self.purpose(purpose)
                self.search()
                self.assertMenu(*names)
        self.assertNoErrors()

    def test_customer_refund_is_saved_and_posted(self):
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.search()
        self.assertMenu(BOTH, CUSTOMER)
        self.choose(CUSTOMER)
        self.page.fill("[name=amount]", "400")
        self.save()
        payment = Payment.objects.get()
        self.assertEqual((payment.kind, payment.settlement_purpose, payment.counterparty_id, payment.status,
                          payment.amount), ("outgoing", "customer_refund", self.customer.pk, "posted", D("400")))
        self.assertEqual(Account.objects.get(pk=self.account.pk).balance, D("-400"))
        self.assertNoErrors()

    def test_supplier_refund_is_saved_and_posted(self):
        self.open("payment_create_in")
        self.purpose("supplier_refund")
        self.search()
        self.assertMenu(BOTH, SUPPLIER)
        self.choose(SUPPLIER)
        self.page.fill("[name=amount]", "250")
        self.save()
        payment = Payment.objects.get()
        self.assertEqual((payment.kind, payment.settlement_purpose, payment.counterparty_id, payment.status),
                         ("incoming", "supplier_refund", self.supplier.pk, "posted"))
        self.assertEqual(Account.objects.get(pk=self.account.pk).balance, D("250"))
        self.assertNoErrors()

    def test_both_type_is_saved_for_every_purpose(self):
        for url_name, purpose in (("payment_create_out", "customer_refund"), ("payment_create_in", "supplier_refund"),
                                  ("payment_create_in", "customer_payment"), ("payment_create_out", "supplier_payment")):
            with self.subTest(url_name, purpose=purpose):
                self.open(url_name)
                self.purpose(purpose)
                self.search()
                self.choose(BOTH)
                self.page.fill("[name=amount]", "10")
                self.save("save")
                payment = Payment.objects.order_by("-pk").first()
                self.assertEqual((payment.settlement_purpose, payment.counterparty_id), (purpose, self.both.pk))
        self.assertNoErrors()


class PurposeChangeTests(PaymentPageWorld):
    def test_unfitting_choice_is_dropped_with_message(self):
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.search()
        self.choose(CUSTOMER)
        self.purpose("supplier_payment")
        self.assertDropped(CUSTOMER, "покупатель")
        self.search()
        self.assertMenu(BOTH, SUPPLIER)
        self.choose(SUPPLIER)
        self.expect(self.page.locator(".ac-error")).to_have_count(0)
        self.assertNoErrors()

    def test_fitting_choice_survives_purpose_change(self):
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.search()
        self.choose(BOTH)
        for purpose in ("supplier_payment", "unallocated", "", "customer_refund"):
            self.purpose(purpose)
            self.settle()
            self.assertEqual((self.value(), self.text_field().input_value()), (str(self.both.pk), BOTH), purpose)
        self.assertEqual(self.page.locator(".ac-error").count(), 0)
        self.assertNoErrors()

    def test_choice_made_under_unallocated_is_checked_on_purpose_change(self):
        self.open("payment_create_in")
        self.purpose("unallocated")
        self.search()
        self.choose(SUPPLIER)
        self.purpose("customer_payment")
        self.assertDropped(SUPPLIER, "поставщик")
        self.assertNoErrors()

    def test_invoice_turns_not_specified_into_customer_payment(self):
        product = Product.objects.create(name="Товар", unit=self.unit, sale_price=100)
        invoice = Invoice.objects.create(organization=self.org, warehouse=self.wh, customer=self.customer)
        invoice.lines.create(product=product, quantity=1, price=500)
        invoice.post()
        self.open("payment_create_in")
        self.search()
        self.assertMenu(BOTH, CUSTOMER, SUPPLIER)
        self.choose(SUPPLIER)
        self.page.select_option("select[name=invoice]", str(invoice.pk))
        self.assertDropped(SUPPLIER, "поставщик")
        self.search()
        self.assertMenu(BOTH, CUSTOMER)
        self.choose(CUSTOMER)
        self.page.fill("[name=amount]", "500")
        self.save()
        payment = Payment.objects.get()
        self.assertEqual((payment.invoice_id, payment.settlement_purpose, payment.counterparty_id),
                         (invoice.pk, "customer_payment", self.customer.pk))
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).paid_amount, D("500"))
        self.assertNoErrors()

    def change_purpose_keeping_focus(self, value):
        """Смена назначения, пока курсор в поле контрагента и список открыт."""
        self.page.evaluate("""value => {
          const select = document.querySelector('select[name=settlement_purpose]');
          select.value = value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
        }""", value)

    def test_open_list_is_searched_again_on_purpose_change(self):
        self.open("payment_create_out")
        self.purpose("supplier_payment")
        self.search()
        self.assertMenu(BOTH, SUPPLIER)
        self.change_purpose_keeping_focus("customer_refund")
        self.assertMenu(BOTH, CUSTOMER)
        self.assertTrue(self.page.evaluate("document.activeElement.classList.contains('ac-input')"))
        self.assertNoErrors()

    def test_late_answer_for_old_purpose_is_ignored(self):
        """Ответ на поиск прежнего назначения пришёл после смены — список не меняет.
        Задержка включается после первого списка и только для поиска поставщиков."""
        self.context.add_init_script("""(() => {
          const original = window.fetch;
          window.__delayType = null;
          window.__sent = 0;
          window.__late = 0;
          window.fetch = async (url, options) => {
            const late = window.__delayType && /api\\/counterparties\\/search/.test(url)
              && new RegExp('[?&]type=' + window.__delayType + '(&|$)').test(url);
            if (late) window.__sent += 1;
            const response = await original(url, options);
            if (late) {
              await new Promise((done) => setTimeout(done, 1500));
              window.__late += 1;
            }
            return response;
          };
        })();""")
        self.open("payment_create_out")
        self.purpose("supplier_payment")
        self.search()
        self.assertMenu(BOTH, SUPPLIER)
        self.page.evaluate("window.__delayType = 'supplier'")
        self.text_field().fill("Тест п")                # поставщику — «Тест поставщик», ответ задержан
        self.page.wait_for_function("window.__sent >= 1")
        self.change_purpose_keeping_focus("customer_refund")
        self.assertMenu(CUSTOMER)                       # «Тест п» среди покупателей
        self.page.wait_for_function("window.__late >= window.__sent")
        self.page.wait_for_timeout(200)                 # опоздавший ответ обработан
        self.assertMenu(CUSTOMER)
        self.choose(CUSTOMER)
        self.assertEqual(self.value(), str(self.customer.pk))
        self.assertNoErrors()


class QuickCreateTests(PaymentPageWorld):
    def create_from_field(self, name):
        self.search(name)
        self.page.locator(f"{OPEN_MENU} .ac-create").click()

    def test_existing_of_other_type_is_not_substituted_or_changed(self):
        count = Counterparty.objects.count()
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.search(SUPPLIER)
        self.expect(self.page.locator(f"{OPEN_MENU} .ac-empty")).to_have_text("Ничего не найдено")
        self.page.locator(f"{OPEN_MENU} .ac-create").click()
        self.assertDropped(SUPPLIER, "поставщик")
        self.assertEqual(Counterparty.objects.count(), count)
        self.assertEqual(Counterparty.objects.get(pk=self.supplier.pk).partner_type, Counterparty.TYPE_SUPPLIER)
        self.assertNoErrors()

    def test_new_counterparty_gets_type_of_purpose(self):
        cases = [("payment_create_in", "supplier_refund", "Новый поставщик", Counterparty.TYPE_SUPPLIER),
                 ("payment_create_out", "customer_refund", "Новый покупатель", Counterparty.TYPE_CUSTOMER),
                 ("payment_create_out", "unallocated", "Новый без стороны", Counterparty.TYPE_SUPPLIER)]
        for url_name, purpose, name, partner_type in cases:
            with self.subTest(url_name, purpose=purpose):
                self.open(url_name)
                self.purpose(purpose)
                self.create_from_field(name)
                # набранный текст совпадает с названием: ждать подстановки id
                self.expect(self.page.locator(".ac-value[name=counterparty]")).not_to_have_value("")
                self.expect(self.text_field()).to_have_value(name)
                created = Counterparty.objects.get(name=name)
                self.assertEqual((created.partner_type, self.value()), (partner_type, str(created.pk)))
        self.assertNoErrors()


class RedisplayAndEditTests(PaymentPageWorld):
    def test_form_with_error_keeps_choice_and_rule(self):
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.search()
        self.choose(CUSTOMER)
        self.page.fill("[name=amount]", "0")
        self.save()                                     # проведение с нулём — ошибка суммы
        self.expect(self.page.locator("text=Нельзя провести платёж с нулевой суммой")).to_be_visible()
        self.assertFalse(Payment.objects.exists())
        self.assertEqual((self.value(), self.text_field().input_value()), (str(self.customer.pk), CUSTOMER))
        self.assertEqual(self.page.locator("select[name=settlement_purpose]").input_value(), "customer_refund")
        self.search()
        self.assertMenu(BOTH, CUSTOMER)
        self.choose(CUSTOMER)
        self.purpose("supplier_payment")
        self.assertDropped(CUSTOMER, "покупатель")
        self.assertNoErrors()

    def test_server_refusal_shows_the_refused_counterparty(self):
        """Страница без своего скрипта (например, старая копия в кэше) прислала
        неподходящего: сервер отказывает, рядом с полем — он же, а не скрытый id."""
        self.open("payment_create_out")
        self.purpose("customer_refund")
        self.page.evaluate("""id => {
          document.querySelector('.ac-value[name=counterparty]').value = id;
          document.querySelector('.ac-input').value = 'подпись не того';
        }""", str(self.supplier.pk))
        self.page.fill("[name=amount]", "100")
        self.save("save")
        self.expect(self.page.locator("text=«Возврат покупателю» оформляется на покупателя")).to_be_visible()
        self.assertEqual((self.value(), self.text_field().input_value()), (str(self.supplier.pk), SUPPLIER))
        self.assertFalse(Payment.objects.exists())
        self.assertNoErrors()

    def test_edit_saved_refund(self):
        payment = Payment.objects.create(kind="outgoing", organization=self.org, account=self.account,
                                         counterparty=self.customer, amount=100, settlement_purpose="customer_refund")
        self.open("payment_edit", payment.pk)
        self.assertEqual((self.value(), self.text_field().input_value()), (str(self.customer.pk), CUSTOMER))
        self.search()
        self.assertMenu(BOTH, CUSTOMER)
        self.page.keyboard.press("Escape")
        self.page.fill("[name=amount]", "150")
        self.save("save")
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.amount, stored.counterparty_id, stored.settlement_purpose),
                         (D("150"), self.customer.pk, "customer_refund"))
        self.assertNoErrors()

    def test_rules_on_page_are_valid_json(self):
        self.open("payment_create_in")
        rules = json.loads(self.page.locator("#payment-counterparty-rules").text_content())
        self.assertEqual(rules["purposes"]["supplier_refund"]["none"]["type"], "supplier")
        self.assertTrue(re.search(r"\{name\}.*\{type\}", rules["purposes"]["customer_payment"]["none"]["message"]))
